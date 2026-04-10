"""Classification services: single classify + batch processing."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterator

import httpx
from fastapi import HTTPException

from jobinbox.web.models import (
    BatchClassifyRequest,
    BatchClassifyResponse,
    ClassificationRequest,
    ClassificationResult,
)
from jobinbox.web.runtime import WebRuntime


def classify_message(runtime: WebRuntime, payload: ClassificationRequest) -> ClassificationResult:
    gmail_id = payload.gmail_id.strip()
    if not gmail_id:
        raise HTTPException(status_code=400, detail="gmail_id is required.")

    try:
        if not runtime.store.email_exists(gmail_id=gmail_id):
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

        result = runtime.classifier().classify_email(
            subject=payload.subject,
            sender=payload.sender,
            snippet=payload.snippet,
            body=payload.body,
        )
        runtime.store.upsert_result(
            gmail_id=gmail_id,
            result=result,
            llm_provider=runtime.settings.llm_provider,
            llm_model=runtime.active_model_name(),
        )
        return ClassificationResult(**result)
    except httpx.HTTPStatusError as exc:
        name = "OpenAI" if runtime.settings.llm_provider == "openai" else "Ollama"
        detail = f"{name} returned {exc.response.status_code}: {exc.response.text}"
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.TimeoutException as exc:
        name = "OpenAI" if runtime.settings.llm_provider == "openai" else "Ollama"
        detail = (
            f"{name} timed out (read limit {runtime.settings.ollama_timeout_s}s). "
            "Increase JOBINBOX_OLLAMA_TIMEOUT_S or reduce email body size."
        )
        raise HTTPException(status_code=504, detail=detail) from exc
    except httpx.RequestError as exc:
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
    except ValueError as exc:
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
    concurrency_used = min(runtime.resolve_batch_concurrency(payload.concurrency), len(candidates))
    processed = 0
    failed_ids: list[str] = []

    yield {
        "type": "start",
        "requested": payload.count,
        "attempted": len(candidates),
        "concurrency_used": concurrency_used,
    }

    def classify_one(row: dict[str, object]) -> tuple[str, dict[str, object]]:
        gmail_id = str(row.get("id") or "")
        result = classifier.classify_email(
            subject=str(row.get("subject") or ""),
            sender=str(row.get("sender") or ""),
            snippet=str(row.get("snippet") or ""),
            body=str(row.get("body") or ""),
        )
        return gmail_id, result

    total = len(candidates)

    if concurrency_used <= 1:
        for row in candidates:
            gmail_id = str(row.get("id") or "")
            ok = False
            result_payload: dict[str, object] | None = None
            try:
                _, result = classify_one(row)
                runtime.store.upsert_result(
                    gmail_id=gmail_id,
                    result=result,
                    llm_provider=runtime.settings.llm_provider,
                    llm_model=runtime.active_model_name(),
                )
                processed += 1
                ok = True
                result_payload = result
            except Exception:  # noqa: BLE001 - keep batch running across individual failures.
                failed_ids.append(gmail_id)
            yield {
                "type": "progress",
                "finished": processed + len(failed_ids),
                "total": total,
                "gmail_id": gmail_id,
                "ok": ok,
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
                    runtime.store.upsert_result(
                        gmail_id=gmail_id,
                        result=result,
                        llm_provider=runtime.settings.llm_provider,
                        llm_model=runtime.active_model_name(),
                    )
                    processed += 1
                    ok = True
                    result_payload = result
                except Exception:  # noqa: BLE001 - keep batch running across individual failures.
                    failed_ids.append(gmail_id)
                yield {
                    "type": "progress",
                    "finished": processed + len(failed_ids),
                    "total": total,
                    "gmail_id": gmail_id,
                    "ok": ok,
                    "result": result_payload,
                }

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
