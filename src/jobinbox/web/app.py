"""FastAPI app for inbox browsing + LLM classification PoC."""

from __future__ import annotations

import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterator

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from jobinbox.config import Settings
from jobinbox.extraction import OllamaEmailClassifier, OpenAIEmailClassifier
from jobinbox.ingestion import GmailIngestion
from jobinbox.storage import JobInboxStore

settings = Settings.load()
store = JobInboxStore(db_path=settings.db_path)
app = FastAPI(title="jobinbox", version="0.1.0")


class ClassificationResult(BaseModel):
    application: str = Field(pattern="^(yes|no)$")
    company: str | None = None
    role: str | None = None
    stage: str
    interview_date: str | None = None


class InboxMessage(BaseModel):
    id: str
    threadId: str | None = None
    internalDate: str | None = None
    subject: str = ""
    sender: str = ""
    recipient: str = ""
    date: str = ""
    snippet: str = ""
    body: str = ""
    fetchedAt: str | None = None
    result: ClassificationResult | None = None
    resultUpdatedAt: str | None = None


class InboxResponse(BaseModel):
    messages: list[InboxMessage]


class FetchMessagesRequest(BaseModel):
    count: int = Field(default=100, ge=1, le=500)
    query: str = ""


class FetchMessagesResponse(BaseModel):
    requested: int
    fetched_new: int
    duplicates: int
    total_cached: int


class ClearResultsResponse(BaseModel):
    deleted: int


class ClassificationRequest(BaseModel):
    gmail_id: str
    subject: str = ""
    sender: str = ""
    snippet: str = ""
    body: str = ""


class BatchClassifyRequest(BaseModel):
    count: int = Field(default=100, ge=1, le=500)
    concurrency: int | None = Field(default=None, ge=1, le=16)


class BatchClassifyResponse(BaseModel):
    requested: int
    attempted: int
    processed: int
    failed: int
    concurrency_used: int
    failed_ids: list[str]


class SankeyNode(BaseModel):
    label: str


class SankeyLink(BaseModel):
    id: str
    source: int
    target: int
    value: int


class SankeyCompanyRolePair(BaseModel):
    company: str
    role: str


class SankeyResponse(BaseModel):
    nodes: list[SankeyNode]
    links: list[SankeyLink]
    branch_pairs: dict[str, list[SankeyCompanyRolePair]]
    total_pairs: int


def _gmail_client() -> GmailIngestion:
    return GmailIngestion(
        desktop_credentials_path=settings.desktop_credentials_path,
        token_path=settings.token_path,
    )


def _classifier() -> OllamaEmailClassifier | OpenAIEmailClassifier:
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise HTTPException(
                status_code=503,
                detail=(
                    "LLM provider is OpenAI but no API key is set. "
                    "Set JOBINBOX_OPENAI_API_KEY or OPENAI_API_KEY (e.g. in .env)."
                ),
            )
        return OpenAIEmailClassifier(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            timeout_s=settings.ollama_timeout_s,
            use_json_response_format=settings.openai_json_mode,
        )
    return OllamaEmailClassifier(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout_s=settings.ollama_timeout_s,
    )


def _active_llm_model_name() -> str:
    if settings.llm_provider == "openai":
        return settings.openai_model
    return settings.ollama_model


def _default_batch_concurrency() -> int:
    # OpenAI endpoints usually tolerate higher parallelism than local Ollama.
    return 6 if settings.llm_provider == "openai" else 2


def _resolve_batch_concurrency(requested: int | None) -> int:
    if requested is None:
        return _default_batch_concurrency()
    return max(1, min(16, requested))


def _iter_classify_batch_events(payload: BatchClassifyRequest) -> Iterator[dict[str, Any]]:
    """Run batch classification and yield progress events (shared by JSON and SSE endpoints)."""
    candidates = store.list_latest_unprocessed_messages(
        user_id=settings.user_id,
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

    classifier = _classifier()
    concurrency_used = min(_resolve_batch_concurrency(payload.concurrency), len(candidates))
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
                store.upsert_result(
                    gmail_id=gmail_id,
                    result=result,
                    llm_provider=settings.llm_provider,
                    llm_model=_active_llm_model_name(),
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
                    store.upsert_result(
                        gmail_id=gmail_id,
                        result=result,
                        llm_provider=settings.llm_provider,
                        llm_model=_active_llm_model_name(),
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


def _to_inbox_message(row: dict[str, object]) -> InboxMessage:
    result: ClassificationResult | None = None
    if row.get("result_application") is not None:
        result = ClassificationResult(
            application=str(row.get("result_application") or "no"),
            company=(str(row.get("result_company") or "").strip() or None),
            role=(str(row.get("result_role") or "").strip() or None),
            stage=str(row.get("result_stage") or "Unknown"),
            interview_date=(str(row.get("result_interview_date") or "").strip() or None),
        )

    return InboxMessage(
        id=str(row.get("id") or ""),
        threadId=str(row.get("threadId") or "") or None,
        internalDate=str(row.get("internalDate") or "") or None,
        subject=str(row.get("subject") or ""),
        sender=str(row.get("sender") or row.get("from") or ""),
        recipient=str(row.get("recipient") or row.get("to") or ""),
        date=str(row.get("date") or ""),
        snippet=str(row.get("snippet") or ""),
        body=str(row.get("body") or ""),
        fetchedAt=str(row.get("fetchedAt") or "") or None,
        result=result,
        resultUpdatedAt=str(row.get("resultUpdatedAt") or "") or None,
    )


def _build_sankey_response() -> SankeyResponse:
    rows = store.list_user_classified_stage_events(user_id=settings.user_id)
    if not rows:
        return SankeyResponse(nodes=[SankeyNode(label="Applications")], links=[], branch_pairs={}, total_pairs=0)

    events_by_pair: dict[tuple[str, str], list[tuple[tuple[int, str, str], str]]] = defaultdict(list)
    for row in rows:
        company = str(row.get("company") or "(Unknown Company)")
        role = str(row.get("role") or "(Unknown Role)")
        stage = str(row.get("stage") or "Unknown").strip() or "Unknown"
        sort_key = (
            int(row.get("internal_ts") or 0),
            str(row.get("fetched_at") or ""),
            str(row.get("result_updated_at") or ""),
        )
        events_by_pair[(company, role)].append((sort_key, stage))

    transition_counts: dict[tuple[str, str], int] = defaultdict(int)
    transition_pairs: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)

    for pair, events in events_by_pair.items():
        ordered = sorted(events, key=lambda item: item[0])
        stage_path: list[str] = []
        for _, stage in ordered:
            if not stage_path or stage_path[-1] != stage:
                stage_path.append(stage)
        if not stage_path:
            continue

        edges: list[tuple[str, str]] = [("Applications", stage_path[0])]
        edges.extend((a, b) for a, b in zip(stage_path, stage_path[1:]))

        seen_for_pair: set[tuple[str, str]] = set()
        for edge in edges:
            if edge in seen_for_pair:
                continue
            seen_for_pair.add(edge)
            transition_counts[edge] += 1
            transition_pairs[edge].add(pair)

    labels = {"Applications"}
    for source, target in transition_counts:
        labels.add(source)
        labels.add(target)

    preferred = [
        "Applications",
        "Received",
        "Online Assessment",
        "Interview",
        "Offer",
        "Rejection",
        "Unknown",
    ]
    ordered_labels = [label for label in preferred if label in labels]
    ordered_labels.extend(sorted(label for label in labels if label not in ordered_labels))
    index_by_label = {label: idx for idx, label in enumerate(ordered_labels)}

    links: list[SankeyLink] = []
    branch_pairs: dict[str, list[SankeyCompanyRolePair]] = {}
    for (source, target), value in sorted(
        transition_counts.items(),
        key=lambda item: (index_by_label.get(item[0][0], 999), index_by_label.get(item[0][1], 999)),
    ):
        edge_id = f"{source} -> {target}"
        links.append(
            SankeyLink(
                id=edge_id,
                source=index_by_label[source],
                target=index_by_label[target],
                value=value,
            )
        )
        pairs = sorted(
            transition_pairs[(source, target)],
            key=lambda pair: (pair[0].lower(), pair[1].lower()),
        )
        branch_pairs[edge_id] = [
            SankeyCompanyRolePair(company=company, role=role)
            for company, role in pairs
        ]

    return SankeyResponse(
        nodes=[SankeyNode(label=label) for label in ordered_labels],
        links=links,
        branch_pairs=branch_pairs,
        total_pairs=len(events_by_pair),
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(Path(settings.project_root) / "index.html")


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "llm_provider": settings.llm_provider,
        "llm_model": _active_llm_model_name(),
        "openai_configured": bool(settings.openai_api_key),
        "user_id": settings.user_id,
        "db_path": str(settings.db_path),
        "desktop_credentials_path": str(settings.desktop_credentials_path),
        "desktop_credentials_found": settings.desktop_credentials_path.is_file(),
        "webapp_credentials_path": str(settings.webapp_credentials_path),
        "webapp_credentials_found": settings.webapp_credentials_path.is_file(),
    }


@app.get("/api/analytics/sankey", response_model=SankeyResponse)
def sankey_analytics() -> SankeyResponse:
    try:
        return _build_sankey_response()
    except Exception as exc:  # pragma: no cover - defensive fallback for API surface
        raise HTTPException(status_code=500, detail=f"Sankey analytics error: {exc}") from exc


@app.get("/api/messages", response_model=InboxResponse)
def list_messages(limit: int = Query(default=settings.max_messages, ge=1, le=500)) -> InboxResponse:
    rows = store.list_cached_messages(user_id=settings.user_id, limit=limit)
    messages = [_to_inbox_message(row) for row in rows if row.get("id")]
    return InboxResponse(messages=messages)


@app.post("/api/messages/fetch", response_model=FetchMessagesResponse)
def fetch_messages(payload: FetchMessagesRequest) -> FetchMessagesResponse:
    try:
        client = _gmail_client()
        ids = client.list_message_ids(
            max_results=payload.count,
            query=payload.query or settings.gmail_query,
        )
        existing = store.existing_user_email_ids(user_id=settings.user_id, gmail_ids=ids)
        new_ids = [mid for mid in ids if mid not in existing]

        fetched_new = 0
        for mid in new_ids:
            row = client.get_message_with_body(mid)
            if store.upsert_email(user_id=settings.user_id, row=row):
                fetched_new += 1

        total_cached = store.count_user_emails(user_id=settings.user_id)
        return FetchMessagesResponse(
            requested=len(ids),
            fetched_new=fetched_new,
            duplicates=len(existing),
            total_cached=total_cached,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive fallback for API surface
        raise HTTPException(status_code=500, detail=f"Gmail API error: {exc}") from exc


@app.post("/api/results/clear", response_model=ClearResultsResponse)
def clear_results() -> ClearResultsResponse:
    try:
        deleted = store.clear_results_for_user(user_id=settings.user_id)
        return ClearResultsResponse(deleted=deleted)
    except Exception as exc:  # pragma: no cover - defensive fallback for API surface
        raise HTTPException(status_code=500, detail=f"Clear results error: {exc}") from exc


@app.post("/api/classify", response_model=ClassificationResult)
def classify_message(payload: ClassificationRequest) -> ClassificationResult:
    gmail_id = payload.gmail_id.strip()
    if not gmail_id:
        raise HTTPException(status_code=400, detail="gmail_id is required.")

    try:
        # In normal flow the row already exists (loaded from DB cache). Create a minimal row only
        # for defensive API usage where classify is called directly on an unseen gmail_id.
        if not store.email_exists(gmail_id=gmail_id):
            store.upsert_email(
                user_id=settings.user_id,
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

        result = _classifier().classify_email(
            subject=payload.subject,
            sender=payload.sender,
            snippet=payload.snippet,
            body=payload.body,
        )
        store.upsert_result(
            gmail_id=gmail_id,
            result=result,
            llm_provider=settings.llm_provider,
            llm_model=_active_llm_model_name(),
        )
        return ClassificationResult(**result)
    except httpx.HTTPStatusError as exc:
        name = "OpenAI" if settings.llm_provider == "openai" else "Ollama"
        detail = f"{name} returned {exc.response.status_code}: {exc.response.text}"
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.TimeoutException as exc:
        name = "OpenAI" if settings.llm_provider == "openai" else "Ollama"
        detail = (
            f"{name} timed out (read limit {settings.ollama_timeout_s}s). "
            "Increase JOBINBOX_OLLAMA_TIMEOUT_S or reduce email body size."
        )
        raise HTTPException(status_code=504, detail=detail) from exc
    except httpx.RequestError as exc:
        if settings.llm_provider == "openai":
            detail = (
                f"Cannot reach OpenAI at {settings.openai_base_url}. "
                f"({type(exc).__name__}: {exc})"
            )
        else:
            detail = (
                f"Cannot reach Ollama at {settings.ollama_base_url}. "
                f"Start Ollama and pull model '{settings.ollama_model}'. ({type(exc).__name__}: {exc})"
            )
        raise HTTPException(status_code=503, detail=detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"Invalid LLM response: {exc}") from exc


@app.post("/api/classify/batch", response_model=BatchClassifyResponse)
def classify_messages_batch(payload: BatchClassifyRequest) -> BatchClassifyResponse:
    done: dict[str, Any] | None = None
    for event in _iter_classify_batch_events(payload):
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


@app.post("/api/classify/batch/stream")
def classify_messages_batch_stream(payload: BatchClassifyRequest) -> StreamingResponse:
    def event_bytes() -> Iterator[bytes]:
        for event in _iter_classify_batch_events(payload):
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
