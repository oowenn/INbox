"""Classification services: single classify + batch processing."""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterator

import httpx
from fastapi import HTTPException

from jobinbox.web.models import (
    BatchClassifyRequest,
    BatchClassifyResponse,
    ClassificationFailureRow,
    ClassificationFailuresResponse,
    ClassificationRequest,
    ClassificationResult,
)
from jobinbox.web.runtime import WebRuntime

_COMPANY_HISTORY_LIMIT = 200

_log = logging.getLogger("jobinbox.classification")


def _canonical_company_key_from_result(result: dict[str, object]) -> str:
    # Prefer canonical_company stored by DB layer, but fall back to normalized raw company.
    key = str(result.get("canonical_company") or "").strip().lower()
    if key:
        return key
    raw = str(result.get("company") or "").strip().lower()
    return raw


def _adaptive_batch_concurrency(runtime: WebRuntime, *, requested: int | None, batch_size: int) -> int:
    """
    Choose a safe concurrency for this batch.

    Users can still override via the input. When unset, we automatically reduce
    concurrency for large OpenAI batches to avoid rate-limit storms.
    """
    base = runtime.resolve_batch_concurrency(requested)
    if requested is not None:
        return base
    if runtime.settings.llm_provider != "openai":
        return base
    # Heuristic: large batches tend to trip rate limits at high parallelism.
    if batch_size >= 350:
        return min(base, 2)
    if batch_size >= 200:
        return min(base, 3)
    return base


def _is_retryable_batch_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.RequestError):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        return "database is locked" in msg or "locked" in msg
    if isinstance(exc, httpx.HTTPStatusError):
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code in {408, 409, 425, 429}:
            return True
        if isinstance(code, int) and 500 <= code <= 599:
            return True
    return False


def _retry_sleep_s(attempt: int) -> float:
    # Exponential backoff with jitter, capped.
    base = min(12.0, 0.7 * (2**max(0, attempt - 1)))
    return base * (0.65 + random.random() * 0.7)


def _format_classification_exception(exc: BaseException) -> tuple[str, str]:
    """Return (error_type_name, message) for storage and APIs."""
    error_type = type(exc).__name__
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            code = exc.response.status_code
            body = (exc.response.text or "")[:2500]
        except Exception:  # noqa: BLE001 - best-effort serialization
            code = "?"
            body = ""
        message = f"HTTP {code}: {body}"
    elif isinstance(exc, httpx.TimeoutException):
        message = str(exc) or "Request timed out."
    elif isinstance(exc, httpx.RequestError):
        message = str(exc) or repr(exc)
    elif isinstance(exc, sqlite3.OperationalError):
        message = str(exc)
    elif isinstance(exc, TimeoutError):
        message = str(exc) or "TimeoutError"
    elif isinstance(exc, ValueError):
        message = str(exc) or "ValueError"
    else:
        message = str(exc) or repr(exc)
    if len(message) > 8000:
        message = message[:7997] + "..."
    return error_type, message


def _record_classification_failure(
    runtime: WebRuntime,
    gmail_id: str,
    exc: BaseException,
    *,
    context: str,
) -> tuple[str, str]:
    """
    Log and persist one classification failure. Returns (error_type, message) for SSE payloads.
    Never raises: failures to persist are logged only.
    """
    error_type, message = _format_classification_exception(exc)
    _log.warning(
        "classification_failure context=%s gmail_id=%s type=%s message=%.800s",
        context,
        gmail_id,
        error_type,
        message,
    )
    try:
        runtime.store.record_classification_failure(
            user_id=runtime.settings.user_id,
            gmail_id=gmail_id,
            error_type=error_type,
            error_message=message,
        )
    except Exception as persist_exc:  # noqa: BLE001
        _log.exception(
            "Could not persist classification failure (context=%s gmail_id=%s): %s",
            context,
            gmail_id,
            persist_exc,
        )
    return error_type, message


def _is_application_yes(result: dict[str, Any]) -> bool:
    return str(result.get("application") or "no").strip().lower() == "yes"


def _build_company_history(
    runtime: WebRuntime,
    *,
    company: str,
) -> list[dict[str, Any]]:
    rows = runtime.store.list_company_history_events(
        user_id=runtime.settings.user_id,
        company=company,
        limit=_COMPANY_HISTORY_LIMIT,
    )
    history: list[dict[str, Any]] = []
    for row in rows:
        internal_ts = int(row.get("internal_ts") or 0)
        history.append(
            {
                "gmail_id": str(row.get("gmail_id") or ""),
                "internal_ts": internal_ts if internal_ts > 0 else None,
                "role": row.get("role"),
                "canonical_role": row.get("canonical_role"),
                "stage": str(row.get("stage") or "Unknown"),
                "interview_date": row.get("interview_date"),
                "application": str(row.get("application") or "no"),
            }
        )
    return history


def _apply_company_curation(
    runtime: WebRuntime,
    *,
    classifier: Any,
    company: str,
    trigger_gmail_id: str,
    ignore_timeout: bool,
) -> None:
    curate_history = getattr(classifier, "curate_company_history", None)
    if not callable(curate_history):
        return

    company_key = runtime.store.company_key_for_lock(company)
    lock_ctx = runtime.store.company_curation_lock(
        user_id=runtime.settings.user_id,
        company_key=company_key,
    )
    try:
        with lock_ctx:
            history = _build_company_history(runtime, company=company)
            if not history:
                return

            payload = curate_history(
                company=company,
                trigger_gmail_id=trigger_gmail_id,
                history=history,
            )
            if not isinstance(payload, dict):
                return

            role_updates = payload.get("role_updates")
            remove_ids = payload.get("remove_ids")
            role_updates_list = role_updates if isinstance(role_updates, list) else []
            remove_ids_list = remove_ids if isinstance(remove_ids, list) else []

            allowed_ids = {
                str(item.get("gmail_id") or "").strip()
                for item in history
                if str(item.get("gmail_id") or "").strip()
            }
            if not allowed_ids:
                return

            runtime.store.apply_role_backfills(
                user_id=runtime.settings.user_id,
                updates=[item for item in role_updates_list if isinstance(item, dict)],
                allowed_gmail_ids=allowed_ids,
            )
            runtime.store.remove_events_from_timeline(
                user_id=runtime.settings.user_id,
                gmail_ids=[str(gid).strip() for gid in remove_ids_list if str(gid).strip()],
                allowed_gmail_ids=allowed_ids,
            )
    except TimeoutError:
        if not ignore_timeout:
            raise


def _classify_store_and_curate(
    runtime: WebRuntime,
    *,
    classifier: Any,
    gmail_id: str,
    subject: str,
    sender: str,
    snippet: str,
    body: str,
    ignore_curation_timeout: bool,
) -> dict[str, Any]:
    extracted = classifier.classify_email(
        subject=subject,
        sender=sender,
        snippet=snippet,
        body=body,
    )

    # Persist extractor output immediately so processed rows always exist,
    # including application=no rows and rows later "removed" by curation.
    runtime.store.upsert_result(
        gmail_id=gmail_id,
        result=extracted,
        extraction=extracted,
        llm_provider=runtime.settings.llm_provider,
        llm_model=runtime.active_model_name(),
    )

    if _is_application_yes(extracted):
        company = str(extracted.get("company") or "").strip()
        if company:
            _apply_company_curation(
                runtime,
                classifier=classifier,
                company=company,
                trigger_gmail_id=gmail_id,
                ignore_timeout=ignore_curation_timeout,
            )

    stored = runtime.store.get_result_for_user_message(
        user_id=runtime.settings.user_id,
        gmail_id=gmail_id,
    )
    return stored if isinstance(stored, dict) else extracted


def _classify_and_store_only(
    runtime: WebRuntime,
    *,
    classifier: Any,
    gmail_id: str,
    subject: str,
    sender: str,
    snippet: str,
    body: str,
) -> dict[str, Any]:
    """
    Extract + persist immediately, but do NOT run company history curation.
    Used by batch mode to defer curation to one call per company at end of batch.
    """
    extracted = classifier.classify_email(
        subject=subject,
        sender=sender,
        snippet=snippet,
        body=body,
    )
    runtime.store.upsert_result(
        gmail_id=gmail_id,
        result=extracted,
        extraction=extracted,
        llm_provider=runtime.settings.llm_provider,
        llm_model=runtime.active_model_name(),
    )
    stored = runtime.store.get_result_for_user_message(
        user_id=runtime.settings.user_id,
        gmail_id=gmail_id,
    )
    return stored if isinstance(stored, dict) else extracted


def _ensure_email_exists(
    runtime: WebRuntime,
    *,
    gmail_id: str,
    payload: ClassificationRequest,
) -> None:
    if runtime.store.email_exists(gmail_id=gmail_id):
        return
    runtime.store.upsert_email(
        user_id=runtime.settings.user_id,
        row={
            "id": gmail_id,
            "threadId": None,
            "internalDate": None,
            "subject": payload.subject,
            "from": payload.sender,
            "to": "",
            "date": "",
            "snippet": payload.snippet,
            "body": payload.body,
        },
    )


def classify_message(runtime: WebRuntime, payload: ClassificationRequest) -> ClassificationResult:
    gmail_id = payload.gmail_id.strip()
    if not gmail_id:
        raise HTTPException(status_code=400, detail="gmail_id is required.")

    try:
        _ensure_email_exists(runtime, gmail_id=gmail_id, payload=payload)
        classifier = runtime.classifier()
        result = _classify_store_and_curate(
            runtime,
            classifier=classifier,
            gmail_id=gmail_id,
            subject=payload.subject,
            sender=payload.sender,
            snippet=payload.snippet,
            body=payload.body,
            ignore_curation_timeout=False,
        )
        return ClassificationResult(**result)
    except httpx.HTTPStatusError as exc:
        _record_classification_failure(runtime, gmail_id, exc, context="classify_single")
        name = "OpenAI" if runtime.settings.llm_provider == "openai" else "Ollama"
        detail = f"{name} returned {exc.response.status_code}: {exc.response.text}"
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.TimeoutException as exc:
        _record_classification_failure(runtime, gmail_id, exc, context="classify_single")
        name = "OpenAI" if runtime.settings.llm_provider == "openai" else "Ollama"
        detail = (
            f"{name} timed out (read limit {runtime.settings.ollama_timeout_s}s). "
            "Increase JOBINBOX_OLLAMA_TIMEOUT_S or reduce email body size."
        )
        raise HTTPException(status_code=504, detail=detail) from exc
    except httpx.RequestError as exc:
        _record_classification_failure(runtime, gmail_id, exc, context="classify_single")
        if runtime.settings.llm_provider == "openai":
            detail = (
                f"Cannot reach OpenAI at {runtime.settings.openai_base_url}. "
                f"({type(exc).__name__}: {exc})"
            )
        else:
            detail = (
                f"Cannot reach Ollama at {runtime.settings.ollama_base_url}. "
                f"Start Ollama and pull model '{runtime.settings.ollama_model}'. ({type(exc).__name__}: {exc})"
            )
        raise HTTPException(status_code=503, detail=detail) from exc
    except TimeoutError as exc:
        _record_classification_failure(runtime, gmail_id, exc, context="classify_single")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        _record_classification_failure(runtime, gmail_id, exc, context="classify_single")
        raise HTTPException(status_code=502, detail=f"Invalid LLM response: {exc}") from exc


def iter_batch_events(runtime: WebRuntime, payload: BatchClassifyRequest) -> Iterator[dict[str, Any]]:
    """
    Run batch classification and yield progress events.

    Events:
    - start: attempted, concurrency_used
    - progress: finished, total, gmail_id, ok, result
    - done: processed, failed, failed_ids
    """
    candidates = runtime.store.list_latest_unprocessed_messages(
        user_id=runtime.settings.user_id,
        limit=payload.count,
        min_internal_ts=payload.min_internal_ts_ms,
        max_internal_ts=payload.max_internal_ts_ms,
    )
    if not candidates:
        yield {
            "type": "start",
            "requested": payload.count,
            "attempted": 0,
            "concurrency_used": 0,
        }
        yield {
            "type": "done",
            "requested": payload.count,
            "attempted": 0,
            "processed": 0,
            "failed": 0,
            "concurrency_used": 0,
            "failed_ids": [],
        }
        return

    classifier = runtime.classifier()
    concurrency_used = min(
        _adaptive_batch_concurrency(runtime, requested=payload.concurrency, batch_size=len(candidates)),
        len(candidates),
    )
    processed = 0
    failed_ids: list[str] = []
    touched_companies: dict[str, str] = {}  # canonical_company_key -> trigger_gmail_id

    yield {
        "type": "start",
        "requested": payload.count,
        "attempted": len(candidates),
        "concurrency_used": concurrency_used,
    }

    def classify_one(row: dict[str, object]) -> tuple[str, dict[str, object]]:
        gmail_id = str(row.get("id") or "")
        last_exc: Exception | None = None
        for attempt in range(1, 5):
            try:
                result = _classify_and_store_only(
                    runtime,
                    classifier=classifier,
                    gmail_id=gmail_id,
                    subject=str(row.get("subject") or ""),
                    sender=str(row.get("sender") or ""),
                    snippet=str(row.get("snippet") or ""),
                    body=str(row.get("body") or ""),
                )
                return gmail_id, result
            except Exception as exc:  # noqa: BLE001 - retry loop
                last_exc = exc if isinstance(exc, Exception) else Exception(str(exc))
                if attempt >= 4 or not _is_retryable_batch_error(exc):
                    raise
                time.sleep(_retry_sleep_s(attempt))
        # Defensive; should be unreachable.
        raise last_exc or RuntimeError("Batch classify failed.")
        return gmail_id, result

    total = len(candidates)

    if concurrency_used <= 1:
        for row in candidates:
            gmail_id = str(row.get("id") or "")
            ok = False
            result_payload: dict[str, object] | None = None
            try:
                _, result = classify_one(row)
                processed += 1
                ok = True
                result_payload = result
            except Exception as exc:  # noqa: BLE001 - keep batch running across individual failures.
                failed_ids.append(gmail_id)
                err_type, err_msg = _record_classification_failure(
                    runtime, gmail_id, exc, context="classify_batch"
                )
                yield {
                    "type": "progress",
                    "finished": processed + len(failed_ids),
                    "total": total,
                    "gmail_id": gmail_id,
                    "ok": False,
                    "result": None,
                    "error": {"type": err_type, "message": err_msg[:2000]},
                }
                continue
            if ok and isinstance(result_payload, dict) and _is_application_yes(result_payload):
                ck = _canonical_company_key_from_result(result_payload)
                if ck:
                    touched_companies[ck] = gmail_id

            yield {
                "type": "progress",
                "finished": processed + len(failed_ids),
                "total": total,
                "gmail_id": gmail_id,
                "ok": True,
                "result": result_payload,
            }
    else:
        with ThreadPoolExecutor(max_workers=concurrency_used) as pool:
            future_by_id = {
                pool.submit(classify_one, row): str(row.get("id") or "")
                for row in candidates
            }
            for future in as_completed(future_by_id):
                gmail_id = future_by_id[future]
                ok = False
                result_payload: dict[str, object] | None = None
                try:
                    _, result = future.result()
                    processed += 1
                    ok = True
                    result_payload = result
                except Exception as exc:  # noqa: BLE001 - keep batch running across individual failures.
                    failed_ids.append(gmail_id)
                    err_type, err_msg = _record_classification_failure(
                        runtime, gmail_id, exc, context="classify_batch"
                    )
                    yield {
                        "type": "progress",
                        "finished": processed + len(failed_ids),
                        "total": total,
                        "gmail_id": gmail_id,
                        "ok": False,
                        "result": None,
                        "error": {"type": err_type, "message": err_msg[:2000]},
                    }
                    continue
                yield {
                    "type": "progress",
                    "finished": processed + len(failed_ids),
                    "total": total,
                    "gmail_id": gmail_id,
                    "ok": True,
                    "result": result_payload,
                }
                if isinstance(result_payload, dict) and _is_application_yes(result_payload):
                    ck = _canonical_company_key_from_result(result_payload)
                    if ck:
                        touched_companies[ck] = gmail_id

    # Deferred curation: run one pass per company touched in this batch.
    for company_key, trigger_gmail_id in sorted(touched_companies.items()):
        try:
            _apply_company_curation(
                runtime,
                classifier=classifier,
                company=company_key,
                trigger_gmail_id=trigger_gmail_id,
                ignore_timeout=True,
            )
        except Exception as exc:  # noqa: BLE001 - do not fail batch completion
            _record_classification_failure(
                runtime,
                trigger_gmail_id,
                exc,
                context=f"curate_deferred:{company_key}",
            )

    yield {
        "type": "done",
        "requested": payload.count,
        "attempted": total,
        "processed": processed,
        "failed": len(failed_ids),
        "concurrency_used": concurrency_used,
        "failed_ids": failed_ids,
    }


def classify_batch(runtime: WebRuntime, payload: BatchClassifyRequest) -> BatchClassifyResponse:
    done: dict[str, Any] | None = None
    for event in iter_batch_events(runtime, payload):
        if event.get("type") == "done":
            done = event

    if not done:
        raise HTTPException(status_code=500, detail="Batch classification produced no result.")

    return BatchClassifyResponse(
        requested=int(done["requested"]),
        attempted=int(done["attempted"]),
        processed=int(done["processed"]),
        failed=int(done["failed"]),
        concurrency_used=int(done["concurrency_used"]),
        failed_ids=list(done["failed_ids"]),
    )


def list_classification_failures(
    runtime: WebRuntime,
    *,
    limit: int = 200,
    gmail_id: str | None = None,
) -> ClassificationFailuresResponse:
    rows = runtime.store.list_classification_failures(
        user_id=runtime.settings.user_id,
        limit=limit,
        gmail_id=gmail_id,
    )
    out: list[ClassificationFailureRow] = []
    for r in rows:
        sub_raw = r.get("subject")
        subject = (str(sub_raw).strip() or None) if sub_raw is not None else None
        out.append(
            ClassificationFailureRow(
                id=int(r.get("id") or 0),
                gmail_id=str(r.get("gmail_id") or ""),
                error_type=str(r.get("error_type") or "Exception"),
                error_message=str(r.get("error_message") or ""),
                created_at=str(r.get("created_at") or ""),
                subject=subject,
            )
        )
    return ClassificationFailuresResponse(failures=out)
