"""Gmail API ingestion: OAuth, list messages, normalize to plain dicts for downstream layers."""

from __future__ import annotations

import base64
import html
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_METADATA_HEADERS = ("Subject", "From", "Date")


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _oauth_port() -> int:
    raw = (os.environ.get("JOBINBOX_OAUTH_PORT") or "8090").strip()
    try:
        p = int(raw)
    except ValueError:
        return 8090
    return p if 1 <= p <= 65535 else 8090


def _run_installed_app_flow(flow: InstalledAppFlow) -> Credentials:
    """
    Loopback OAuth using a *fixed* redirect port.

    ``port=0`` (random) breaks Google Cloud when the OAuth client is type
    **Web application**, because each run uses a new redirect_uri. Use a stable
    port (default 8090) and register ``http://localhost:<port>/`` if required.

    In Docker, set JOBINBOX_OAUTH_BIND_ADDR=0.0.0.0 and publish the same port
    on the host so the browser can reach the callback.
    """
    mode = (os.environ.get("JOBINBOX_OAUTH_MODE") or "local_server").strip().lower()
    # "console" kept for older docs; library removed run_console(), so this is headless loopback.
    if mode in {"console", "manual", "headless"}:
        open_browser = _env_flag("JOBINBOX_OAUTH_OPEN_BROWSER", False)
    else:
        open_browser = _env_flag("JOBINBOX_OAUTH_OPEN_BROWSER", True)

    host = (os.environ.get("JOBINBOX_OAUTH_HOST") or "localhost").strip() or "localhost"
    bind_raw = (os.environ.get("JOBINBOX_OAUTH_BIND_ADDR") or "").strip()
    bind_addr = bind_raw or None
    port = _oauth_port()

    return flow.run_local_server(
        host=host,
        bind_addr=bind_addr,
        port=port,
        open_browser=open_browser,
    )


def _load_credentials(desktop_credentials_path: Path, token_path: Path) -> Credentials:
    creds: Credentials | None = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds
    if not desktop_credentials_path.is_file():
        raise FileNotFoundError(
            f"Missing Desktop OAuth client file: {desktop_credentials_path}. "
            "Use the Google Cloud **Desktop app** client JSON (e.g. desktop_credentials.json) "
            "or set JOBINBOX_DESKTOP_CREDENTIALS."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(desktop_credentials_path), SCOPES)
    creds = _run_installed_app_flow(flow)
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


def _decode_base64url(data: str) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("utf-8"))
    return raw.decode("utf-8", errors="replace")


def _is_skipped_attachment_part(part: dict[str, Any]) -> bool:
    mime = (part.get("mimeType") or "").lower()
    filename = part.get("filename") or ""
    body = part.get("body") or {}
    has_attachment = bool(body.get("attachmentId"))
    if mime.startswith("multipart/"):
        return False
    if mime in {"text/plain", "text/html"}:
        return bool(filename and has_attachment)
    if has_attachment or filename:
        return True
    if mime.startswith(("image/", "audio/", "video/", "application/")):
        return True
    return False


def _collect_text_parts(part: dict[str, Any], plain_parts: list[str], html_parts: list[str]) -> None:
    mime = (part.get("mimeType") or "").lower()
    if mime.startswith("multipart/"):
        for child in part.get("parts") or []:
            _collect_text_parts(child, plain_parts, html_parts)
        return

    if _is_skipped_attachment_part(part):
        return

    data = (part.get("body") or {}).get("data")
    if not data:
        return
    decoded = _decode_base64url(data).strip()
    if not decoded:
        return
    if mime == "text/plain":
        plain_parts.append(decoded)
    elif mime == "text/html":
        html_parts.append(decoded)


def _html_to_text(html_text: str) -> str:
    no_script = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_text)
    no_tags = re.sub(r"(?s)<[^>]+>", " ", no_script)
    unescaped = html.unescape(no_tags)
    collapsed = re.sub(r"[ \t]+", " ", unescaped)
    collapsed = re.sub(r"\n{3,}", "\n\n", collapsed)
    return collapsed.strip()


def _extract_body_text(payload: dict[str, Any]) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    _collect_text_parts(payload, plain_parts, html_parts)
    if plain_parts:
        return "\n\n".join(plain_parts)
    if html_parts:
        return _html_to_text("\n".join(html_parts))
    return ""


def _normalize_message(raw: dict[str, Any], *, include_body: bool) -> dict[str, Any]:
    payload = raw.get("payload") or {}
    headers = _header_map(payload)
    row = {
        "id": raw.get("id"),
        "threadId": raw.get("threadId"),
        "internalDate": raw.get("internalDate"),
        "snippet": raw.get("snippet") or "",
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "date": headers.get("date", ""),
        "labelIds": raw.get("labelIds") or [],
    }
    if include_body:
        row["body"] = _extract_body_text(payload)
    return row


@dataclass
class GmailIngestion:
    """Thin wrapper around Gmail API v1. Keeps HTTP details out of CLI and future workers."""

    desktop_credentials_path: Path
    token_path: Path
    _svc: Any = field(default=None, init=False, repr=False)

    def _service(self):
        if self._svc is None:
            creds = _load_credentials(self.desktop_credentials_path, self.token_path)
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
        return _normalize_message(raw, include_body=False)

    def get_message_with_body(self, message_id: str) -> dict[str, Any]:
        svc = self._service()
        raw = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
        return _normalize_message(raw, include_body=True)


def fetch_inbox_preview(
    *,
    desktop_credentials_path: Path,
    token_path: Path,
    max_results: int,
    query: str,
) -> list[dict[str, Any]]:
    """High-level PoC helper: return normalized inbox rows (metadata + snippet)."""
    g = GmailIngestion(desktop_credentials_path=desktop_credentials_path, token_path=token_path)
    ids = g.list_message_ids(max_results=max_results, query=query)
    return [g.get_message_metadata(mid) for mid in ids]


def fetch_inbox_with_bodies(
    *,
    desktop_credentials_path: Path,
    token_path: Path,
    max_results: int,
    query: str,
) -> list[dict[str, Any]]:
    """Return message metadata plus plain text body with attachments excluded."""
    g = GmailIngestion(desktop_credentials_path=desktop_credentials_path, token_path=token_path)
    ids = g.list_message_ids(max_results=max_results, query=query)
    return [g.get_message_with_body(mid) for mid in ids]
