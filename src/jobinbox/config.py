"""Environment-backed settings. Expand per layer (ingestion, storage) as the app grows."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Paths and limits for local PoC. Override via env for CI or different machines."""

    project_root: Path
    credentials_path: Path
    token_path: Path
    max_messages: int
    gmail_query: str

    @classmethod
    def load(cls) -> "Settings":
        root = Path(__file__).resolve().parents[2]
        creds = os.environ.get("JOBINBOX_CREDENTIALS", str(root / "credentials.json"))
        token = os.environ.get("JOBINBOX_TOKEN", str(root / "token.json"))
        max_msg = int(os.environ.get("JOBINBOX_MAX_MESSAGES", "25"))
        query = os.environ.get("JOBINBOX_GMAIL_QUERY", "in:inbox")
        return cls(
            project_root=root,
            credentials_path=Path(creds),
            token_path=Path(token),
            max_messages=max_msg,
            gmail_query=query,
        )
