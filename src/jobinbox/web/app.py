"""FastAPI application for the Job Inbox product dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from jobinbox.web.models import (
    ApplicationCycleRunRequest,
    ApplicationCycleRunResponse,
    BatchClassifyRequest,
    BatchClassifyResponse,
    ClassificationFailuresResponse,
    ClassificationRequest,
    ClassificationResult,
    ClearResultsResponse,
    DashboardSummaryResponse,
    FetchMessagesRequest,
    FetchMessagesResponse,
    InboxResponse,
    MonthlyCountsResponse,
    CycleEstimateResponse,
    SankeyResponse,
    WipeCacheResponse,
)
from jobinbox.web.runtime import build_runtime
from jobinbox.web.services import (
    build_dashboard_summary,
    build_monthly_counts,
    build_sankey,
    classify_batch,
    classify_message,
    clear_results,
    fetch_messages,
    get_cycle_estimate,
    iter_cycle_count_list_events,
    iter_application_cycle_analyze_events,
    iter_application_cycle_events,
    iter_application_cycle_load_events,
    iter_batch_events,
    list_cached_messages,
    list_classification_failures,
    run_application_cycle,
    wipe_cached_messages,
)

runtime = build_runtime()
app = FastAPI(title="jobinbox", version="1.0.0")

assets_dir = Path(runtime.settings.project_root) / "assets"
app.mount(
    "/assets",
    StaticFiles(directory=str(assets_dir), check_dir=False),
    name="assets",
)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(Path(runtime.settings.project_root) / "index.html")


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "llm_provider": runtime.settings.llm_provider,
        "llm_model": runtime.active_model_name(),
        "openai_configured": bool(runtime.settings.openai_api_key),
        "user_id": runtime.settings.user_id,
        "db_path": str(runtime.settings.db_path),
        "desktop_credentials_path": str(runtime.settings.desktop_credentials_path),
        "desktop_credentials_found": runtime.settings.desktop_credentials_path.is_file(),
        "webapp_credentials_path": str(runtime.settings.webapp_credentials_path),
        "webapp_credentials_found": runtime.settings.webapp_credentials_path.is_file(),
    }


@app.get("/api/dashboard/summary", response_model=DashboardSummaryResponse)
def dashboard_summary(cycle_start_year: int | None = Query(default=None, ge=2000, le=2100)) -> DashboardSummaryResponse:
    try:
        return build_dashboard_summary(runtime, cycle_start_year=cycle_start_year)
    except Exception as exc:  # pragma: no cover - defensive fallback for API surface
        raise HTTPException(status_code=500, detail=f"Dashboard summary error: {exc}") from exc


@app.get("/api/analytics/sankey", response_model=SankeyResponse)
def sankey_analytics(cycle_start_year: int | None = Query(default=None, ge=2000, le=2100)) -> SankeyResponse:
    try:
        return build_sankey(runtime, cycle_start_year=cycle_start_year)
    except Exception as exc:  # pragma: no cover - defensive fallback for API surface
        raise HTTPException(status_code=500, detail=f"Sankey analytics error: {exc}") from exc


@app.delete("/api/cycle/gmail-count")
def delete_cycle_gmail_count_route(
    cycle_start_year: int = Query(..., ge=2000, le=2100),
) -> dict[str, bool]:
    runtime.store.delete_cycle_gmail_list_count(
        user_id=runtime.settings.user_id,
        cycle_start_year=cycle_start_year,
    )
    return {"ok": True}


@app.post("/api/cycle/count/stream")
def cycle_count_stream_route(payload: ApplicationCycleRunRequest) -> StreamingResponse:
    def event_bytes() -> Iterator[bytes]:
        for event in iter_cycle_count_list_events(runtime, payload):
            yield f"data: {json.dumps(event)}\n\n".encode("utf-8")

    return StreamingResponse(
        event_bytes(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/cycle/estimate", response_model=CycleEstimateResponse)
def cycle_estimate(
    cycle_start_year: int = Query(..., ge=2000, le=2100),
    query: str = Query(
        default="in:inbox",
        description="Base Gmail query; cycle date bounds are appended server-side.",
    ),
) -> CycleEstimateResponse:
    try:
        return get_cycle_estimate(runtime, cycle_start_year=cycle_start_year, query=query)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"Cycle estimate error: {exc}") from exc


@app.get("/api/analytics/monthly", response_model=MonthlyCountsResponse)
def monthly_analytics(
    limit_months: int = Query(default=48, ge=1, le=240),
    cycle_start_year: int | None = Query(default=None, ge=2000, le=2100),
) -> MonthlyCountsResponse:
    try:
        return build_monthly_counts(
            runtime,
            limit_months=limit_months,
            cycle_start_year=cycle_start_year,
        )
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"Monthly analytics error: {exc}") from exc


@app.get("/api/messages", response_model=InboxResponse)
def list_messages(limit: int = Query(default=runtime.settings.max_messages, ge=1, le=500)) -> InboxResponse:
    return list_cached_messages(runtime, limit=limit)


@app.post("/api/messages/fetch", response_model=FetchMessagesResponse)
def fetch_messages_route(payload: FetchMessagesRequest) -> FetchMessagesResponse:
    return fetch_messages(runtime, payload)


@app.post("/api/messages/wipe", response_model=WipeCacheResponse)
def wipe_messages_route() -> WipeCacheResponse:
    return wipe_cached_messages(runtime)


@app.post("/api/results/clear", response_model=ClearResultsResponse)
def clear_results_route() -> ClearResultsResponse:
    return clear_results(runtime)


@app.post("/api/classify", response_model=ClassificationResult)
def classify_message_route(payload: ClassificationRequest) -> ClassificationResult:
    return classify_message(runtime, payload)


@app.get("/api/classification/failures", response_model=ClassificationFailuresResponse)
def classification_failures(
    limit: int = Query(default=200, ge=1, le=500),
    gmail_id: str | None = Query(default=None, description="Optional filter to one message id."),
) -> ClassificationFailuresResponse:
    return list_classification_failures(runtime, limit=limit, gmail_id=gmail_id)


@app.post("/api/classify/batch", response_model=BatchClassifyResponse)
def classify_messages_batch(payload: BatchClassifyRequest) -> BatchClassifyResponse:
    return classify_batch(runtime, payload)


@app.post("/api/classify/batch/stream")
def classify_messages_batch_stream(payload: BatchClassifyRequest) -> StreamingResponse:
    def event_bytes() -> Iterator[bytes]:
        for event in iter_batch_events(runtime, payload):
            yield f"data: {json.dumps(event)}\n\n".encode("utf-8")

    return StreamingResponse(
        event_bytes(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/cycle/run", response_model=ApplicationCycleRunResponse)
def run_application_cycle_route(payload: ApplicationCycleRunRequest) -> ApplicationCycleRunResponse:
    return run_application_cycle(runtime, payload)


@app.post("/api/cycle/run/stream")
def run_application_cycle_stream_route(payload: ApplicationCycleRunRequest) -> StreamingResponse:
    def event_bytes() -> Iterator[bytes]:
        for event in iter_application_cycle_events(runtime, payload):
            yield f"data: {json.dumps(event)}\n\n".encode("utf-8")

    return StreamingResponse(
        event_bytes(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/cycle/load/stream")
def load_application_cycle_stream_route(payload: ApplicationCycleRunRequest) -> StreamingResponse:
    def event_bytes() -> Iterator[bytes]:
        for event in iter_application_cycle_load_events(runtime, payload):
            yield f"data: {json.dumps(event)}\n\n".encode("utf-8")

    return StreamingResponse(
        event_bytes(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/cycle/analyze/stream")
def analyze_application_cycle_stream_route(payload: ApplicationCycleRunRequest) -> StreamingResponse:
    def event_bytes() -> Iterator[bytes]:
        for event in iter_application_cycle_analyze_events(runtime, payload):
            yield f"data: {json.dumps(event)}\n\n".encode("utf-8")

    return StreamingResponse(
        event_bytes(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
