"""FastAPI app for inbox browsing + LLM classification PoC."""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from jobinbox.config import Settings
from jobinbox.extraction import OllamaEmailClassifier, OpenAIEmailClassifier
from jobinbox.ingestion import GmailIngestion

settings = Settings.load()
app = FastAPI(title="jobinbox", version="0.1.0")


class InboxMessage(BaseModel):
    id: str
    threadId: str | None = None
    internalDate: str | None = None
    subject: str = ""
    sender: str = ""
    date: str = ""
    snippet: str = ""
    body: str = ""


class InboxResponse(BaseModel):
    messages: list[InboxMessage]


class ClassificationRequest(BaseModel):
    subject: str = ""
    sender: str = ""
    snippet: str = ""
    body: str = ""


class ClassificationResponse(BaseModel):
    application: str = Field(pattern="^(yes|no)$")
    company: str | None = None
    role: str | None = None
    stage: str


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


def _to_inbox_message(row: dict[str, object]) -> InboxMessage:
    return InboxMessage(
        id=str(row.get("id") or ""),
        threadId=str(row.get("threadId") or "") or None,
        internalDate=str(row.get("internalDate") or "") or None,
        subject=str(row.get("subject") or ""),
        sender=str(row.get("from") or ""),
        date=str(row.get("date") or ""),
        snippet=str(row.get("snippet") or ""),
        body=str(row.get("body") or ""),
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(Path(settings.project_root) / "index.html")


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "llm_provider": settings.llm_provider,
        "openai_configured": bool(settings.openai_api_key),
        "desktop_credentials_path": str(settings.desktop_credentials_path),
        "desktop_credentials_found": settings.desktop_credentials_path.is_file(),
        "webapp_credentials_path": str(settings.webapp_credentials_path),
        "webapp_credentials_found": settings.webapp_credentials_path.is_file(),
    }


@app.get("/api/messages", response_model=InboxResponse)
def list_messages(
    limit: int = Query(default=200, ge=1, le=200),
    query: str = Query(default=""),
) -> InboxResponse:
    try:
        client = _gmail_client()
        ids = client.list_message_ids(max_results=limit, query=query or settings.gmail_query)
        rows = [client.get_message_with_body(mid) for mid in ids]
        messages = [_to_inbox_message(row) for row in rows if row.get("id")]
        return InboxResponse(messages=messages)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive fallback for API surface
        raise HTTPException(status_code=500, detail=f"Gmail API error: {exc}") from exc


@app.post("/api/classify", response_model=ClassificationResponse)
def classify_message(payload: ClassificationRequest) -> ClassificationResponse:
    try:
        result = _classifier().classify_email(
            subject=payload.subject,
            sender=payload.sender,
            snippet=payload.snippet,
            body=payload.body,
        )
        return ClassificationResponse(**result)
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
