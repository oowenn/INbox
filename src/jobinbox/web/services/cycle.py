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
    CycleEstimateResponse,
)
from jobinbox.web.runtime import WebRuntime
from jobinbox.web.services.classification import iter_batch_events

_ESTIMATED_COST_PER_EMAIL_USD = 1.35 / 5107.0
_DEFAULT_CLASSIFY_BATCH_SIZE = 250
# Upper bound for messages.list pagination (avoids runaway memory); practically unlimited for inbox use.
_GMAIL_LIST_MAX_RESULTS = 10_000_000


def _resolve_list_cap(payload: ApplicationCycleRunRequest) -> int:
    """Resolve Gmail list/count cap: None means walk up to _GMAIL_LIST_MAX_RESULTS."""
    if payload.fetch_limit is None:
        return _GMAIL_LIST_MAX_RESULTS
    return max(1, min(int(payload.fetch_limit), _GMAIL_LIST_MAX_RESULTS))


def _most_recent_june_first(today: date | None = None, *, override_year: int | None = None) -> date:
    ref = today or date.today()
    if isinstance(override_year, int) and 2000 <= override_year <= ref.year:
        year = override_year
    else:
        year = ref.year if (ref.month, ref.day) >= (6, 1) else (ref.year - 1)
    return date(year, 6, 1)


def _local_epoch_seconds(d: date) -> int:
    local_tz = datetime.now().astimezone().tzinfo
    dt = datetime(d.year, d.month, d.day, tzinfo=local_tz)
    return int(dt.timestamp())


def _build_cycle_query(base_query: str, *, after_epoch_s: int, before_epoch_s: int) -> str:
    base = (base_query or "").strip()
    clause = f"after:{after_epoch_s} before:{before_epoch_s}"
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


def _estimate_cost_usd(email_count: int) -> float:
    n = max(0, int(email_count or 0))
    return round(n * _ESTIMATED_COST_PER_EMAIL_USD, 4)


def get_cycle_estimate(
    runtime: WebRuntime,
    *,
    cycle_start_year: int,
    query: str | None = None,
) -> CycleEstimateResponse:
    """
    Return local counts plus either a cached Gmail id-list total or a one-call resultSizeEstimate.
    """
    if not isinstance(cycle_start_year, int) or cycle_start_year < 2000 or cycle_start_year > 2100:
        raise HTTPException(status_code=400, detail="Invalid cycle_start_year.")
    payload = ApplicationCycleRunRequest(
        cycle_start_year=cycle_start_year,
        query=query or "in:inbox",
    )
    (
        cycle_start,
        cycle_start_iso,
        _after_epoch_s,
        _before_epoch_s,
        min_internal_ts_ms,
        max_internal_ts_ms,
        cycle_query,
    ) = _resolve_cycle_window(runtime, payload)

    stored_exact = runtime.store.get_cycle_gmail_list_count(
        user_id=runtime.settings.user_id,
        cycle_start_year=cycle_start.year,
        cycle_query=cycle_query,
    )

    ready = runtime.store.count_unprocessed_messages(
        user_id=runtime.settings.user_id,
        min_internal_ts=min_internal_ts_ms,
        max_internal_ts=max_internal_ts_ms,
    )
    dash = runtime.store.get_user_dashboard_summary(
        user_id=runtime.settings.user_id,
        cycle_start_year=cycle_start.year,
    )
    cached_total = int(dash.get("cached_total") or 0)
    classified_total = int(dash.get("classified_total") or 0)

    gmail_est: int | None = None
    if stored_exact is None:
        try:
            client = runtime.gmail_client()
            gmail_est = client.estimate_query_result_size(query=cycle_query)
        except Exception:
            gmail_est = None

    ready_int = int(ready)
    if isinstance(stored_exact, int):
        not_analyzed_total = max(0, int(stored_exact) - classified_total)
    else:
        # Without a stored exact Gmail count, the only exact actionable count is cached-unclassified.
        not_analyzed_total = max(0, ready_int)
    estimated_cost = _estimate_cost_usd(not_analyzed_total)
    potential_cost: float | None = estimated_cost if isinstance(stored_exact, int) else None

    needs_scan = stored_exact is None

    return CycleEstimateResponse(
        cycle_start_year=cycle_start.year,
        cycle_start_date=cycle_start_iso,
        cycle_query=cycle_query,
        cached_total=cached_total,
        ready_to_analyze=ready_int,
        not_analyzed_total=not_analyzed_total,
        estimated_cost_usd=estimated_cost,
        potential_estimated_cost_usd=potential_cost,
        gmail_exact_list_count=stored_exact,
        needs_exact_list_scan=needs_scan,
        gmail_result_size_estimate=(None if stored_exact is not None else gmail_est),
    )


def iter_cycle_count_list_events(
    runtime: WebRuntime,
    payload: ApplicationCycleRunRequest,
) -> Iterator[dict[str, Any]]:
    """
    Paginate Gmail messages.list counting ids only; persist total per (user, cycle year, query).
    Skips network when the same total is already cached.
    """
    (
        cycle_start,
        cycle_start_iso,
        _after_epoch_s,
        _before_epoch_s,
        _min_internal_ts_ms,
        _max_internal_ts_ms,
        cycle_query,
    ) = _resolve_cycle_window(runtime, payload)
    list_cap = _resolve_list_cap(payload)
    uid = runtime.settings.user_id

    cached = runtime.store.get_cycle_gmail_list_count(
        user_id=uid,
        cycle_start_year=cycle_start.year,
        cycle_query=cycle_query,
    )
    if cached is not None:
        yield {
            "type": "count_done",
            "cycle_start_year": cycle_start.year,
            "cycle_start_date": cycle_start_iso,
            "cycle_query": cycle_query,
            "gmail_exact_list_count": int(cached),
            "from_cache": True,
            "list_cap": list_cap,
        }
        return

    yield {
        "type": "count_start",
        "cycle_start_year": cycle_start.year,
        "cycle_start_date": cycle_start_iso,
        "cycle_query": cycle_query,
        "list_cap": list_cap,
    }

    final = 0
    try:
        client = runtime.gmail_client()
        for total in client.iter_count_query_matches(query=cycle_query, max_results=list_cap):
            final = int(total)
            yield {
                "type": "count_progress",
                "counted": final,
                "list_cap": list_cap,
            }
        runtime.store.upsert_cycle_gmail_list_count(
            user_id=uid,
            cycle_start_year=cycle_start.year,
            cycle_query=cycle_query,
            exact_listed_count=final,
        )
        yield {
            "type": "count_done",
            "cycle_start_year": cycle_start.year,
            "cycle_start_date": cycle_start_iso,
            "cycle_query": cycle_query,
            "gmail_exact_list_count": final,
            "from_cache": False,
            "list_cap": list_cap,
        }
    except Exception as exc:  # noqa: BLE001 - surface to client stream
        yield {"type": "count_failed", "error": str(exc)}


def _resolve_cycle_window(
    runtime: WebRuntime,
    payload: ApplicationCycleRunRequest,
) -> tuple[date, str, int, int, int, int, str]:
    cycle_start = _most_recent_june_first(override_year=payload.cycle_start_year)
    cycle_start_iso = cycle_start.isoformat()
    cycle_end = date(cycle_start.year + 1, 6, 1)
    after_epoch_s = _local_epoch_seconds(cycle_start)
    before_epoch_s = _local_epoch_seconds(cycle_end)
    min_internal_ts_ms = max(0, after_epoch_s * 1000)
    max_internal_ts_ms = max(0, before_epoch_s * 1000)
    cycle_query = _build_cycle_query(
        payload.query or runtime.settings.gmail_query,
        after_epoch_s=after_epoch_s,
        before_epoch_s=before_epoch_s,
    )
    return (
        cycle_start,
        cycle_start_iso,
        after_epoch_s,
        before_epoch_s,
        min_internal_ts_ms,
        max_internal_ts_ms,
        cycle_query,
    )


def iter_application_cycle_load_events(
    runtime: WebRuntime,
    payload: ApplicationCycleRunRequest,
) -> Iterator[dict[str, Any]]:
    (
        cycle_start,
        cycle_start_iso,
        _after_epoch_s,
        _before_epoch_s,
        min_internal_ts_ms,
        max_internal_ts_ms,
        cycle_query,
    ) = _resolve_cycle_window(runtime, payload)
    fetch_limit = _resolve_list_cap(payload)

    run_id = runtime.store.create_processing_run(
        user_id=runtime.settings.user_id,
        run_kind="application_cycle_load",
        cycle_start_date=cycle_start_iso,
        cycle_query=cycle_query,
        requested_fetch_limit=fetch_limit,
        classify_batch_size=0,
        classify_concurrency=None,
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
            return

    listed = 0
    fetched_new = 0
    duplicates = 0
    fetch_failed = 0
    total_cached = 0
    ready_to_analyze = 0

    yield {
        "type": "load_start",
        "run_id": run_id,
        "cycle_start_year": cycle_start.year,
        "cycle_start_date": cycle_start_iso,
        "cycle_query": cycle_query,
        "fetch_limit": fetch_limit,
    }
    _record_event(
        "load_start",
        {
            "cycle_start_year": cycle_start.year,
            "cycle_start_date": cycle_start_iso,
            "cycle_query": cycle_query,
            "fetch_limit": fetch_limit,
        },
    )

    try:
        client = runtime.gmail_client()
        ids = client.list_message_ids(max_results=fetch_limit, query=cycle_query)
        listed = len(ids)
        runtime.store.upsert_cycle_gmail_list_count(
            user_id=runtime.settings.user_id,
            cycle_start_year=cycle_start.year,
            cycle_query=cycle_query,
            exact_listed_count=listed,
        )
        existing = runtime.store.existing_user_email_ids(
            user_id=runtime.settings.user_id,
            gmail_ids=ids,
        )
        duplicates = len(existing)
        new_ids = [mid for mid in ids if mid not in existing]

        yield {
            "type": "load_fetch_start",
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
            except Exception as exc:  # noqa: BLE001 - continue loading remaining emails
                fetch_failed += 1
                runtime.store.record_classification_failure(
                    user_id=runtime.settings.user_id,
                    gmail_id=gmail_id,
                    error_type=type(exc).__name__,
                    error_message=f"cycle_load_fetch: {exc}",
                )
                _record_event(
                    "load_fetch_error",
                    {
                        "gmail_id": gmail_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            if idx % 25 == 0 or idx == len(new_ids):
                yield {
                    "type": "load_fetch_progress",
                    "run_id": run_id,
                    "finished": idx,
                    "total": len(new_ids),
                    "fetched_new": fetched_new,
                    "failed": fetch_failed,
                }

        total_cached = runtime.store.count_user_emails(user_id=runtime.settings.user_id)
        ready_to_analyze = runtime.store.count_unprocessed_messages(
            user_id=runtime.settings.user_id,
            min_internal_ts=min_internal_ts_ms,
            max_internal_ts=max_internal_ts_ms,
        )
        estimated_cost_usd = _estimate_cost_usd(ready_to_analyze)

        runtime.store.update_processing_run_fetch(
            user_id=runtime.settings.user_id,
            run_id=run_id,
            listed_total=listed,
            fetched_new_total=fetched_new,
            duplicate_total=duplicates,
            fetch_failed_total=fetch_failed,
        )

        status = "completed_with_errors" if fetch_failed > 0 else "completed"
        runtime.store.complete_processing_run(
            user_id=runtime.settings.user_id,
            run_id=run_id,
            status=status,
            last_error=None,
        )

        yield {
            "type": "load_done",
            "run_id": run_id,
            "status": status,
            "cycle_start_year": cycle_start.year,
            "cycle_start_date": cycle_start_iso,
            "cycle_query": cycle_query,
            "listed": listed,
            "fetched_new": fetched_new,
            "duplicates": duplicates,
            "fetch_failed": fetch_failed,
            "total_cached": total_cached,
            "ready_to_analyze": ready_to_analyze,
            "estimated_cost_usd": estimated_cost_usd,
        }
        _record_event(
            "load_done",
            {
                "status": status,
                "listed": listed,
                "fetched_new": fetched_new,
                "duplicates": duplicates,
                "fetch_failed": fetch_failed,
                "total_cached": total_cached,
                "ready_to_analyze": ready_to_analyze,
                "estimated_cost_usd": estimated_cost_usd,
            },
        )
    except FileNotFoundError as exc:
        runtime.store.complete_processing_run(
            user_id=runtime.settings.user_id,
            run_id=run_id,
            status="failed",
            last_error=str(exc),
        )
        _record_event("load_failed", {"error_type": "FileNotFoundError", "error": str(exc)})
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException as exc:
        runtime.store.complete_processing_run(
            user_id=runtime.settings.user_id,
            run_id=run_id,
            status="failed",
            last_error=str(exc.detail),
        )
        _record_event("load_failed", {"error_type": "HTTPException", "error": str(exc.detail)})
        raise
    except Exception as exc:  # pragma: no cover - defensive fallback for API surface
        runtime.store.complete_processing_run(
            user_id=runtime.settings.user_id,
            run_id=run_id,
            status="failed",
            last_error=str(exc),
        )
        _record_event("load_failed", {"error_type": type(exc).__name__, "error": str(exc)})
        raise HTTPException(status_code=500, detail=f"Cycle email loading failed: {exc}") from exc


def iter_application_cycle_analyze_events(
    runtime: WebRuntime,
    payload: ApplicationCycleRunRequest,
) -> Iterator[dict[str, Any]]:
    (
        cycle_start,
        cycle_start_iso,
        _after_epoch_s,
        _before_epoch_s,
        min_internal_ts_ms,
        max_internal_ts_ms,
        cycle_query,
    ) = _resolve_cycle_window(runtime, payload)
    classify_batch_size = max(10, min(500, int(payload.classify_batch_size or _DEFAULT_CLASSIFY_BATCH_SIZE)))
    classify_concurrency = payload.concurrency

    run_id = runtime.store.create_processing_run(
        user_id=runtime.settings.user_id,
        run_kind="application_cycle_analyze",
        cycle_start_date=cycle_start_iso,
        cycle_query=cycle_query,
        requested_fetch_limit=0,
        classify_batch_size=classify_batch_size,
        classify_concurrency=classify_concurrency,
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
            return

    ready_to_analyze = runtime.store.count_unprocessed_messages(
        user_id=runtime.settings.user_id,
        min_internal_ts=min_internal_ts_ms,
        max_internal_ts=max_internal_ts_ms,
    )
    estimated_cost_usd = _estimate_cost_usd(ready_to_analyze)

    classify_attempted = 0
    classify_processed = 0
    classify_failed = 0
    classify_yes = 0
    classify_no = 0
    classify_yes_missing_company = 0
    failed_ids: list[str] = []
    failed_seen: set[str] = set()
    stalled_failed_signature: tuple[str, ...] | None = None

    yield {
        "type": "analyze_start",
        "run_id": run_id,
        "cycle_start_year": cycle_start.year,
        "cycle_start_date": cycle_start_iso,
        "ready_to_analyze": ready_to_analyze,
        "estimated_cost_usd": estimated_cost_usd,
        "classify_batch_size": classify_batch_size,
    }
    _record_event(
        "analyze_start",
        {
            "cycle_start_year": cycle_start.year,
            "cycle_start_date": cycle_start_iso,
            "ready_to_analyze": ready_to_analyze,
            "estimated_cost_usd": estimated_cost_usd,
            "classify_batch_size": classify_batch_size,
            "classify_concurrency": classify_concurrency,
        },
    )

    try:
        if ready_to_analyze <= 0:
            runtime.store.complete_processing_run(
                user_id=runtime.settings.user_id,
                run_id=run_id,
                status="completed",
                last_error=None,
            )
            yield {
                "type": "analyze_done",
                "run_id": run_id,
                "status": "completed",
                "cycle_start_year": cycle_start.year,
                "cycle_start_date": cycle_start_iso,
                "analyzed": 0,
                "application_related_found": 0,
                "not_application_related": 0,
                "missing_company": 0,
                "failed": 0,
                "failed_ids": [],
                "remaining_unprocessed": 0,
                "estimated_cost_usd": 0.0,
            }
            return

        batch_index = 0
        while True:
            batch_index += 1
            batch_done: dict[str, Any] | None = None
            batch_payload = BatchClassifyRequest(
                count=classify_batch_size,
                concurrency=classify_concurrency,
                min_internal_ts_ms=min_internal_ts_ms,
                max_internal_ts_ms=max_internal_ts_ms,
            )
            for event in iter_batch_events(runtime, batch_payload):
                event_type = str(event.get("type") or "")
                if event_type == "start":
                    yield {
                        "type": "analyze_batch_start",
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
                        "type": "analyze_progress",
                        "run_id": run_id,
                        "batch_index": batch_index,
                        "finished": int(event.get("finished") or 0),
                        "total": int(event.get("total") or 0),
                        "ok": ok,
                        "gmail_id": gmail_id,
                        "error": event.get("error"),
                        "found_total": classify_yes,
                        "analyzed_total": classify_attempted + int(event.get("finished") or 0),
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
                "type": "analyze_batch_done",
                "run_id": run_id,
                "batch_index": batch_index,
                "attempted": attempted,
                "processed": processed,
                "failed": failed,
                "analyzed_total": classify_attempted,
                "found_total": classify_yes,
            }
            _record_event(
                "analyze_batch_done",
                {
                    "batch_index": batch_index,
                    "attempted": attempted,
                    "processed": processed,
                    "failed": failed,
                    "analyzed_total": classify_attempted,
                    "found_total": classify_yes,
                    "not_application_related": classify_no,
                    "missing_company": classify_yes_missing_company,
                },
            )

            if attempted == 0:
                break

            if attempted > 0 and processed == 0 and failed > 0:
                failed_sig = tuple(
                    sorted(str(gid) for gid in (batch_done.get("failed_ids") or []) if str(gid))
                )
                if failed_sig and failed_sig == stalled_failed_signature:
                    _record_event(
                        "analyze_stalled",
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

        remaining_unprocessed = runtime.store.count_unprocessed_messages(
            user_id=runtime.settings.user_id,
            min_internal_ts=min_internal_ts_ms,
            max_internal_ts=max_internal_ts_ms,
        )
        final_estimated_cost_usd = _estimate_cost_usd(classify_attempted)

        status = "completed_with_errors" if classify_failed > 0 else "completed"
        runtime.store.complete_processing_run(
            user_id=runtime.settings.user_id,
            run_id=run_id,
            status=status,
            last_error=None,
        )

        yield {
            "type": "analyze_done",
            "run_id": run_id,
            "status": status,
            "cycle_start_year": cycle_start.year,
            "cycle_start_date": cycle_start_iso,
            "analyzed": classify_attempted,
            "application_related_found": classify_yes,
            "not_application_related": classify_no,
            "missing_company": classify_yes_missing_company,
            "failed": classify_failed,
            "failed_ids": failed_ids,
            "remaining_unprocessed": remaining_unprocessed,
            "estimated_cost_usd": final_estimated_cost_usd,
        }
        _record_event(
            "analyze_done",
            {
                "status": status,
                "analyzed": classify_attempted,
                "application_related_found": classify_yes,
                "not_application_related": classify_no,
                "missing_company": classify_yes_missing_company,
                "failed": classify_failed,
                "remaining_unprocessed": remaining_unprocessed,
                "estimated_cost_usd": final_estimated_cost_usd,
            },
        )
    except HTTPException as exc:
        runtime.store.complete_processing_run(
            user_id=runtime.settings.user_id,
            run_id=run_id,
            status="failed",
            last_error=str(exc.detail),
        )
        _record_event("analyze_failed", {"error_type": "HTTPException", "error": str(exc.detail)})
        raise
    except Exception as exc:  # pragma: no cover - defensive fallback for API surface
        runtime.store.complete_processing_run(
            user_id=runtime.settings.user_id,
            run_id=run_id,
            status="failed",
            last_error=str(exc),
        )
        _record_event("analyze_failed", {"error_type": type(exc).__name__, "error": str(exc)})
        raise HTTPException(status_code=500, detail=f"Cycle analysis failed: {exc}") from exc


def iter_application_cycle_events(
    runtime: WebRuntime,
    payload: ApplicationCycleRunRequest,
) -> Iterator[dict[str, Any]]:
    (
        cycle_start,
        cycle_start_iso,
        _after_epoch_s,
        _before_epoch_s,
        min_internal_ts_ms,
        max_internal_ts_ms,
        cycle_query,
    ) = _resolve_cycle_window(runtime, payload)

    list_cap = _resolve_list_cap(payload)

    run_id = runtime.store.create_processing_run(
        user_id=runtime.settings.user_id,
        run_kind="application_cycle",
        cycle_start_date=cycle_start_iso,
        cycle_query=cycle_query,
        requested_fetch_limit=list_cap,
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
        "cycle_start_year": cycle_start.year,
        "cycle_start_date": cycle_start_iso,
        "cycle_query": cycle_query,
        "fetch_limit": list_cap,
        "classify_batch_size": payload.classify_batch_size,
        "classify_concurrency": payload.concurrency,
    }
    _record_event(
        "run_start",
        {
            "cycle_start_year": cycle_start.year,
            "cycle_start_date": cycle_start_iso,
            "cycle_query": cycle_query,
            "fetch_limit": list_cap,
            "classify_batch_size": payload.classify_batch_size,
            "classify_concurrency": payload.concurrency,
        },
    )

    try:
        client = runtime.gmail_client()
        ids = client.list_message_ids(max_results=list_cap, query=cycle_query)
        listed = len(ids)
        runtime.store.upsert_cycle_gmail_list_count(
            user_id=runtime.settings.user_id,
            cycle_start_year=cycle_start.year,
            cycle_query=cycle_query,
            exact_listed_count=listed,
        )
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
                max_internal_ts_ms=max_internal_ts_ms,
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
            "cycle_start_year": cycle_start.year,
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
                "cycle_start_year": cycle_start.year,
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
