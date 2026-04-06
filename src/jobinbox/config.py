"""Environment-backed settings. Expand per layer (ingestion, storage) as the app grows."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _project_root() -> Path:
    """
    Directory for ``token.json``, optional relative credential paths, and ``index.html`` in Docker.

    When the package is installed into ``site-packages``, ``Path(__file__).parents[2]`` points
    into the Python lib tree (wrong). Prefer ``JOBINBOX_PROJECT_ROOT`` or ``cwd`` in that case.
    """
    env = os.environ.get("JOBINBOX_PROJECT_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve()
    if "site-packages" in here.parts:
        return Path.cwd()
    # Source layout: <repo>/src/jobinbox/config.py -> repo root is parents[2]
    if here.parent.name == "jobinbox" and here.parent.parent.name == "src":
        return here.parents[2]
    return here.parents[2]


def _resolve_desktop_credentials_path(root: Path) -> Path:
    """Desktop OAuth JSON for Python ``InstalledAppFlow`` (Gmail API)."""
    explicit = os.environ.get("JOBINBOX_DESKTOP_CREDENTIALS")
    if explicit:
        return Path(explicit)
    return root / "desktop_credentials.json"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _resolve_webapp_credentials_path(root: Path) -> Path:
    """Web OAuth client JSON (browser-only flows). Not used by the Python Gmail client."""
    explicit = os.environ.get("JOBINBOX_WEBAPP_CREDENTIALS")
    if explicit:
        return Path(explicit)
    return root / "webapp_credentials.json"


@dataclass(frozen=True)
class Settings:
    """Paths and limits for local PoC. Override via env for CI or different machines."""

    project_root: Path
    desktop_credentials_path: Path
    webapp_credentials_path: Path
    token_path: Path
    max_messages: int
    gmail_query: str
    ollama_base_url: str
    ollama_model: str
    ollama_timeout_s: float
    llm_provider: str
    openai_api_key: str | None
    openai_model: str
    openai_base_url: str
    openai_json_mode: bool
    web_host: str
    web_port: int

    @classmethod
    def load(cls) -> "Settings":
        root = _project_root()
        token = os.environ.get("JOBINBOX_TOKEN", str(root / "token.json"))
        max_msg = int(os.environ.get("JOBINBOX_MAX_MESSAGES", "200"))
        query = os.environ.get("JOBINBOX_GMAIL_QUERY", "in:inbox")
        ollama_base_url = os.environ.get("JOBINBOX_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        ollama_model = os.environ.get("JOBINBOX_OLLAMA_MODEL", "llama3.1:8b")
        ollama_timeout_s = float(os.environ.get("JOBINBOX_OLLAMA_TIMEOUT_S", "180"))
        llm_provider = os.environ.get("JOBINBOX_LLM_PROVIDER", "ollama").strip().lower()
        if llm_provider not in ("ollama", "openai"):
            llm_provider = "ollama"
        key_jobinbox = os.environ.get("JOBINBOX_OPENAI_API_KEY", "").strip()
        key_std = os.environ.get("OPENAI_API_KEY", "").strip()
        openai_api_key = key_jobinbox or key_std or None
        openai_model = os.environ.get("JOBINBOX_OPENAI_MODEL", "gpt-4o-mini").strip()
        openai_base_url = os.environ.get(
            "JOBINBOX_OPENAI_BASE_URL", "https://api.openai.com/v1"
        ).strip().rstrip("/")
        openai_json_mode = _env_bool("JOBINBOX_OPENAI_JSON_MODE", True)
        web_host = os.environ.get("JOBINBOX_WEB_HOST", "127.0.0.1")
        web_port = int(os.environ.get("JOBINBOX_WEB_PORT", "8000"))
        return cls(
            project_root=root,
            desktop_credentials_path=_resolve_desktop_credentials_path(root),
            webapp_credentials_path=_resolve_webapp_credentials_path(root),
            token_path=Path(token),
            max_messages=max_msg,
            gmail_query=query,
            ollama_base_url=ollama_base_url,
            ollama_model=ollama_model,
            ollama_timeout_s=ollama_timeout_s,
            llm_provider=llm_provider,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            openai_base_url=openai_base_url,
            openai_json_mode=openai_json_mode,
            web_host=web_host,
            web_port=web_port,
        )
