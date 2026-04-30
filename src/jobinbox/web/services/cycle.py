"""Application-cycle bulk run services."""

from __future__ import annotations

from datetime import date, datetime
import time
from typing import Any, Iterator

from fastapi import HTTPException

from jobinbox.web.models import (
    ApplicationCycleRunRequest,
    ApplicationCycleRunResponse,
    BatchClassifyRequest,
)
from jobinbox.web.runtime import WebRuntime
from jobinbox.web.services.classification import iter_batch_events


def _most_recent_june_first(today: date | None = None) -> date:
    ref = today or date.today()
    year = ref.year if (ref.month, ref.day) >= (6, 1) else (ref.year - 1)
    return date(year, 6, 1)


def _local_epoch_seconds(d: date) -> int:
    local_tz = datetime.now().astimezone().tzinfo
    dt = datetime(d.year, d.month, d.day, tzinfo=local_tz)
    return int(dt.timestamp())


def _build_cycle_query(base_query: str, *, after_epoch_s: int) -> str:
    base = (base_query or "").strip()
    clause = f"after:{after_epoch_s}"
    return f"{base} {clause}".strip() if base else clause


def _retry_sleep_s(attempt: int) -> float:
    # Small capped exponential backoff for Gmail message fetch retries.
    return min(8.0, 0.45 * (2**max(0, attempt - 1)))


def _fetch_message_with_retry(client: Any, gmail_id: str) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, 5):
        try:
            row = client.get_message_with_body(gmail_id)
            if not isinstance(row, dict):
                raise ValueError("Gmail message payload is not a dict.")
            return row
        except Exception as exc:  # noqa: BLE001 - retries need broad coverage
            last_exc = exc if isinstance(exc, Exception) else Exception(str(exc))
            if attempt >= 4:
                break
            time.sleep(_retry_sleep_s(attempt))
    raise last_exc or RuntimeError(f"Fetch failed for gmail_id={gmail_id!r}")


def iter_application_cycle_events(
    runtime: WebRuntime,
    payload: ApplicationCycleRunRequest,
) -> Iterator[dict[str, Any]]:
    cycle_start = _most_recent_june_first()
    cycle_start_iso = cycle_start.isoformat()
    after_epoch_s = _local_epoch_seconds(cycle_start)
    # Keep a 1-day safety pad so timezone/date-header quirks do not create false negatives.
    min_internal_ts_ms = max(0, (after_epoch_s - 86400) * 1000)
    cycle_query = _build_cycle_query(
        payload.query or runtime.settings.gmail_query,
        after_epoch_s=after_epoch_s,
    )

    run_id = runtime.store.create_processing_run(
        user_id=runtime.settings.user_id,
        run_kind="application_cycle",
        cycle_start_date=cycle_start_iso,
        cycle_query=cycle_query,
        requested_fetch_limit=payload.fetch_limit,
        classify_batch_size=payload.classify_batch_size,
        classify_concurrency=payload.concurrency,
    )

    def _record_event(event_type: str, payload_obj: dict[str, Any]) -> None:
        try:
            runtime.store.record_processing_run_event(
                user_id=runtime.settings.user_id,
                run_id=run_id,
                event_type=event_type,
                payload=payload_obj,
            )
        except Exception:
            # Event logging should never fail the bulk run.
            return

    listed = 0
    fetched_new = 0
    duplicates = 0
    fetch_failed = 0
    total_cached = 0
    classify_attempted = 0
    classify_processed = 0
    classify_failed = 0
    classify_yes = 0
    classify_no = 0
    classify_yes_missing_company = 0
    failed_ids: list[str] = []
    failed_seen: set[str] = set()
    stalled_failed_signature: tuple[str, ...] | None = None
    status = "running"

    yield {
        "type": "run_start",
        "run_id": run_id,
        "cycle_start_date": cycle_start_iso,
        "cycle_query": cycle_query,
        "fetch_limit": payload.fetch_limit,
        "classify_batch_size": payload.classify_batch_size,
        "classify_concurrency": payload.concurrency,
    }
    _record_event(
        "run_start",
        {
            "cycle_start_date": cycle_start_iso,
            "cycle_query": cycle_query,
            "fetch_limit": payload.fetch_limit,
            "classify_batch_size": payload.classify_batch_size,
            "classify_concurrency": payload.concurrency,
        },
    )

    try:
        client = runtime.gmail_client()
        ids = client.list_message_ids(max_results=payload.fetch_limit, query=cycle_query)
        listed = len(ids)
        existing = runtime.store.existing_user_email_ids(
            user_id=runtime.settings.user_id,
            gmail_ids=ids,
        )
        duplicates = len(existing)
        new_ids = [mid for mid in ids if mid not in existing]

        yield {
            "type": "fetch_start",
            "run_id": run_id,
            "listed": listed,
            "new_candidates": len(new_ids),
            "duplicates": duplicates,
        }

        for idx, gmail_id in enumerate(new_ids, start=1):
            try:
                row = _fetch_message_with_retry(client, gmail_id)
                if runtime.store.upsert_email(user_id=runtime.settings.user_id, row=row):
                    fetched_new += 1
            except Exception as exc:  # noqa: BLE001 - keep long run progressing
                fetch_failed += 1
                runtime.store.record_classification_failure(
                    user_id=runtime.settings.user_id,
                    gmail_id=gmail_id,
                    error_type=type(exc).__name__,
                    error_message=f"cycle_fetch: {exc}",
                )
                _record_event(
                    "fetch_error",
                    {
                        "gmail_id": gmail_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            if idx % 25 == 0 or idx == len(new_ids):
                yield {
                    "type": "fetch_progress",
                    "run_id": run_id,
                    "finished": idx,
                    "total": len(new_ids),
                    "fetched_new": fetched_new,
                    "failed": fetch_failed,
                }

        total_cached = runtime.store.count_user_emails(user_id=runtime.settings.user_id)
        runtime.store.update_processing_run_fetch(
            user_id=runtime.settings.user_id,
            run_id=run_id,
            listed_total=listed,
            fetched_new_total=fetched_new,
            duplicate_total=duplicates,
            fetch_failed_total=fetch_failed,
        )

        yield {
            "type": "fetch_done",
            "run_id": run_id,
            "listed": listed,
            "fetched_new": fetched_new,
            "duplicates": duplicates,
            "fetch_failed": fetch_failed,
            "total_cached": total_cached,
        }
        _record_event(
            "fetch_done",
            {
                "listed": listed,
                "fetched_new": fetched_new,
                "duplicates": duplicates,
                "fetch_failed": fetch_failed,
                "total_cached": total_cached,
            },
        )

        batch_index = 0
        while True:
            batch_index += 1
            batch_done: dict[str, Any] | None = None
            batch_payload = BatchClassifyRequest(
                count=payload.classify_batch_size,
                concurrency=payload.concurrency,
                min_internal_ts_ms=min_internal_ts_ms,
            )
            for event in iter_batch_events(runtime, batch_payload):
                event_type = str(event.get("type") or "")
                if event_type == "start":
                    yield {
                        "type": "classify_batch_start",
                        "run_id": run_id,
                        "batch_index": batch_index,
                        "attempted": int(event.get("attempted") or 0),
                        "concurrency_used": int(event.get("concurrency_used") or 0),
                    }
                    continue

                if event_type == "progress":
                    ok = event.get("ok") is not False
                    gmail_id = str(event.get("gmail_id") or "").strip()
                    result_payload = event.get("result")
                    if ok and isinstance(result_payload, dict):
                        app = str(result_payload.get("application") or "no").strip().lower()
                        if app == "yes":
                            classify_yes += 1
                            if not str(result_payload.get("company") or "").strip():
                                classify_yes_missing_company += 1
                        else:
                            classify_no += 1
                    elif not ok and gmail_id and gmail_id not in failed_seen:
                        failed_seen.add(gmail_id)
                        failed_ids.append(gmail_id)

                    yield {
                        "type": "classify_progress",
                        "run_id": run_id,
                        "batch_index": batch_index,
                        "finished": int(event.get("finished") or 0),
                        "total": int(event.get("total") or 0),
                        "ok": ok,
                        "gmail_id": gmail_id,
                        "error": event.get("error"),
                        "classify_yes_total": classify_yes,
                        "classify_no_total": classify_no,
                        "classify_yes_missing_company_total": classify_yes_missing_company,
                    }
                    continue

                if event_type == "done":
                    batch_done = event

            if not batch_done:
                raise RuntimeError("Batch classification returned no completion event.")

            attempted = int(batch_done.get("attempted") or 0)
            processed = int(batch_done.get("processed") or 0)
            failed = int(batch_done.get("failed") or 0)
            classify_attempted += attempted
            classify_processed += processed
            classify_failed += failed

            runtime.store.update_processing_run_classification(
                user_id=runtime.settings.user_id,
                run_id=run_id,
                classify_attempted_total=classify_attempted,
                classify_processed_total=classify_processed,
                classify_failed_total=classify_failed,
                classify_yes_total=classify_yes,
                classify_no_total=classify_no,
                classify_yes_missing_company_total=classify_yes_missing_company,
            )

            yield {
                "type": "classify_batch_done",
                "run_id": run_id,
                "batch_index": batch_index,
                "attempted": attempted,
                "processed": processed,
                "failed": failed,
                "classify_attempted_total": classify_attempted,
                "classify_processed_total": classify_processed,
                "classify_failed_total": classify_failed,
            }
            _record_event(
                "classify_batch_done",
                {
                    "batch_index": batch_index,
                    "attempted": attempted,
                    "processed": processed,
                    "failed": failed,
                    "classify_attempted_total": classify_attempted,
                    "classify_processed_total": classify_processed,
                    "classify_failed_total": classify_failed,
                    "classify_yes_total": classify_yes,
                    "classify_no_total": classify_no,
                    "classify_yes_missing_company_total": classify_yes_missing_company,
                },
            )

            if attempted == 0:
                break

            # Protect long runs from infinite retries on the same permanently-failing rows.
            if attempted > 0 and processed == 0 and failed > 0:
                failed_sig = tuple(
                    sorted(str(gid) for gid in (batch_done.get("failed_ids") or []) if str(gid))
                )
                if failed_sig and failed_sig == stalled_failed_signature:
                    _record_event(
                        "classify_stalled",
                        {
                            "batch_index": batch_index,
                            "failed_ids": list(failed_sig),
                            "reason": "repeated_failed_only_batch",
                        },
                    )
                    break
                stalled_failed_signature = failed_sig
            else:
                stalled_failed_signature = None

        status = "completed_with_errors" if (fetch_failed > 0 or classify_failed > 0) else "completed"
        runtime.store.complete_processing_run(
            user_id=runtime.settings.user_id,
            run_id=run_id,
            status=status,
            last_error=None,
        )

        yield {
            "type": "done",
            "run_id": run_id,
            "status": status,
            "cycle_start_date": cycle_start_iso,
            "cycle_query": cycle_query,
            "listed": listed,
            "fetched_new": fetched_new,
            "duplicates": duplicates,
            "fetch_failed": fetch_failed,
            "total_cached": total_cached,
            "classify_attempted": classify_attempted,
            "classify_processed": classify_processed,
            "classify_failed": classify_failed,
            "classify_yes": classify_yes,
            "classify_no": classify_no,
            "classify_yes_missing_company": classify_yes_missing_company,
            "failed_ids": failed_ids,
        }
        _record_event(
            "done",
            {
                "status": status,
                "listed": listed,
                "fetched_new": fetched_new,
                "duplicates": duplicates,
                "fetch_failed": fetch_failed,
                "total_cached": total_cached,
                "classify_attempted": classify_attempted,
                "classify_processed": classify_processed,
                "classify_failed": classify_failed,
                "classify_yes": classify_yes,
                "classify_no": classify_no,
                "classify_yes_missing_company": classify_yes_missing_company,
                "failed_ids": failed_ids,
            },
        )
    except FileNotFoundError as exc:
        runtime.store.complete_processing_run(
            user_id=runtime.settings.user_id,
            run_id=run_id,
            status="failed",
            last_error=str(exc),
        )
        _record_event("run_failed", {"error_type": "FileNotFoundError", "error": str(exc)})
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException as exc:
        runtime.store.complete_processing_run(
            user_id=runtime.settings.user_id,
            run_id=run_id,
            status="failed",
            last_error=str(exc.detail),
        )
        _record_event("run_failed", {"error_type": "HTTPException", "error": str(exc.detail)})
        raise
    except Exception as exc:  # pragma: no cover - defensive fallback for API surface
        runtime.store.complete_processing_run(
            user_id=runtime.settings.user_id,
            run_id=run_id,
            status="failed",
            last_error=str(exc),
        )
        _record_event("run_failed", {"error_type": type(exc).__name__, "error": str(exc)})
        raise HTTPException(status_code=500, detail=f"Application cycle run failed: {exc}") from exc


def run_application_cycle(
    runtime: WebRuntime,
    payload: ApplicationCycleRunRequest,
) -> ApplicationCycleRunResponse:
    done: dict[str, Any] | None = None
    for event in iter_application_cycle_events(runtime, payload):
        if event.get("type") == "done":
            done = event

    if not done:
        raise HTTPException(status_code=500, detail="Application cycle run produced no completion event.")

    return ApplicationCycleRunResponse(
        run_id=int(done.get("run_id") or 0),
        status=str(done.get("status") or "completed"),
        cycle_start_date=str(done.get("cycle_start_date") or ""),
        cycle_query=str(done.get("cycle_query") or ""),
        listed=int(done.get("listed") or 0),
        fetched_new=int(done.get("fetched_new") or 0),
        duplicates=int(done.get("duplicates") or 0),
        fetch_failed=int(done.get("fetch_failed") or 0),
        total_cached=int(done.get("total_cached") or 0),
        classify_attempted=int(done.get("classify_attempted") or 0),
        classify_processed=int(done.get("classify_processed") or 0),
        classify_failed=int(done.get("classify_failed") or 0),
        classify_yes=int(done.get("classify_yes") or 0),
        classify_no=int(done.get("classify_no") or 0),
        classify_yes_missing_company=int(done.get("classify_yes_missing_company") or 0),
        failed_ids=[str(gid) for gid in (done.get("failed_ids") or []) if str(gid)],
    )
