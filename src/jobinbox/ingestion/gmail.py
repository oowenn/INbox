"""Gmail API ingestion: OAuth, list messages, normalize to plain dicts for downstream layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_METADATA_HEADERS = ("Subject", "From", "Date")


def _load_credentials(credentials_path: Path, token_path: Path) -> Credentials:
    creds: Credentials | None = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds
    if not credentials_path.is_file():
        raise FileNotFoundError(
            f"Missing OAuth client file: {credentials_path}. "
            "Download JSON from Google Cloud Console (Desktop app) and set JOBINBOX_CREDENTIALS."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in payload.get("headers", []) or []:
        name = (h.get("name") or "").lower()
        val = h.get("value") or ""
        if name:
            out[name] = val
    return out


def _normalize_message(raw: dict[str, Any]) -> dict[str, Any]:
    payload = raw.get("payload") or {}
    headers = _header_map(payload)
    return {
        "id": raw.get("id"),
        "threadId": raw.get("threadId"),
        "internalDate": raw.get("internalDate"),
        "snippet": raw.get("snippet") or "",
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "date": headers.get("date", ""),
        "labelIds": raw.get("labelIds") or [],
    }


@dataclass
class GmailIngestion:
    """Thin wrapper around Gmail API v1. Keeps HTTP details out of CLI and future workers."""

    credentials_path: Path
    token_path: Path
    _svc: Any = field(default=None, init=False, repr=False)

    def _service(self):
        if self._svc is None:
            creds = _load_credentials(self.credentials_path, self.token_path)
            self._svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._svc

    def list_message_ids(self, *, max_results: int, query: str) -> list[str]:
        svc = self._service()
        ids: list[str] = []
        page_token: str | None = None
        while len(ids) < max_results:
            remaining = max_results - len(ids)
            req = (
                svc.users()
                .messages()
                .list(
                    userId="me",
                    maxResults=min(remaining, 100),
                    q=query or None,
                    pageToken=page_token,
                )
            )
            res = req.execute()
            for m in res.get("messages") or []:
                if m.get("id"):
                    ids.append(m["id"])
                if len(ids) >= max_results:
                    break
            page_token = res.get("nextPageToken")
            if not page_token:
                break
        return ids[:max_results]

    def get_message_metadata(self, message_id: str) -> dict[str, Any]:
        svc = self._service()
        raw = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=list(_METADATA_HEADERS),
            )
            .execute()
        )
        return _normalize_message(raw)


def fetch_inbox_preview(
    *,
    credentials_path: Path,
    token_path: Path,
    max_results: int,
    query: str,
) -> list[dict[str, Any]]:
    """High-level PoC helper: return normalized inbox rows (metadata + snippet)."""
    g = GmailIngestion(credentials_path=credentials_path, token_path=token_path)
    ids = g.list_message_ids(max_results=max_results, query=query)
    return [g.get_message_metadata(mid) for mid in ids]
