"""Message ingestion + cache mutation services."""

from __future__ import annotations

from fastapi import HTTPException

from jobinbox.web.models import (
    ClearResultsResponse,
    FetchMessagesRequest,
    FetchMessagesResponse,
    WipeCacheResponse,
)
from jobinbox.web.runtime import WebRuntime


def fetch_messages(runtime: WebRuntime, payload: FetchMessagesRequest) -> FetchMessagesResponse:
    try:
        client = runtime.gmail_client()
        ids = client.list_message_ids(
            max_results=payload.count,
            query=payload.query or runtime.settings.gmail_query,
        )
        existing = runtime.store.existing_user_email_ids(user_id=runtime.settings.user_id, gmail_ids=ids)
        new_ids = [mid for mid in ids if mid not in existing]

        fetched_new = 0
        for mid in new_ids:
            row = client.get_message_with_body(mid)
            if runtime.store.upsert_email(user_id=runtime.settings.user_id, row=row):
                fetched_new += 1

        total_cached = runtime.store.count_user_emails(user_id=runtime.settings.user_id)
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


def clear_results(runtime: WebRuntime) -> ClearResultsResponse:
    try:
        deleted = runtime.store.clear_results_for_user(user_id=runtime.settings.user_id)
        return ClearResultsResponse(deleted=deleted)
    except Exception as exc:  # pragma: no cover - defensive fallback for API surface
        raise HTTPException(status_code=500, detail=f"Clear results error: {exc}") from exc


def wipe_cached_messages(runtime: WebRuntime) -> WipeCacheResponse:
    try:
        out = runtime.store.clear_cached_messages_for_user(user_id=runtime.settings.user_id)
        return WipeCacheResponse(
            deleted_user_links=int(out.get("deleted_user_links") or 0),
            deleted_orphan_messages=int(out.get("deleted_orphan_messages") or 0),
            deleted_orphan_results=int(out.get("deleted_orphan_results") or 0),
        )
    except Exception as exc:  # pragma: no cover - defensive fallback for API surface
        raise HTTPException(status_code=500, detail=f"Wipe cache error: {exc}") from exc
