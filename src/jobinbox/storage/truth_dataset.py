"""Separate SQLite DB for curated ground-truth labels (independent of job cache)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class TruthDatasetStore:
    """
    Stores (gmail_id, email snapshot, result JSON) for evaluation / labeling.

    Kept separate from ``JobInboxStore`` so you can reset the main cache without
    losing a labeled truth set.
    """

    def __init__(self, *, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=60.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 60000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS truth_entries (
                    user_id TEXT NOT NULL,
                    gmail_id TEXT NOT NULL,
                    email_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, gmail_id)
                );

                CREATE INDEX IF NOT EXISTS idx_truth_entries_user_updated
                    ON truth_entries(user_id, updated_at DESC);
                """
            )

    def upsert_entry(
        self,
        *,
        user_id: str,
        gmail_id: str,
        email: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Replace or insert one labeled row."""
        gid = str(gmail_id or "").strip()
        if not gid:
            raise ValueError("gmail_id is required.")
        email_blob = json.dumps(email, ensure_ascii=True, separators=(",", ":"))
        result_blob = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO truth_entries (user_id, gmail_id, email_json, result_json, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, gmail_id) DO UPDATE SET
                    email_json = excluded.email_json,
                    result_json = excluded.result_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, gid, email_blob, result_blob),
            )

    def insert_entry_if_absent(
        self,
        *,
        user_id: str,
        gmail_id: str,
        email: dict[str, Any],
        result: dict[str, Any],
    ) -> bool:
        """
        Insert only when (user_id, gmail_id) is not already present.

        Returns True if a new row was inserted, False if skipped (already exists).
        """
        gid = str(gmail_id or "").strip()
        if not gid:
            raise ValueError("gmail_id is required.")
        email_blob = json.dumps(email, ensure_ascii=True, separators=(",", ":"))
        result_blob = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO truth_entries (user_id, gmail_id, email_json, result_json, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (user_id, gid, email_blob, result_blob),
            )
        return int(cur.rowcount or 0) > 0

    def get_entry(self, *, user_id: str, gmail_id: str) -> dict[str, Any] | None:
        gid = str(gmail_id or "").strip()
        if not gid:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT gmail_id, email_json, result_json, updated_at
                FROM truth_entries
                WHERE user_id = ? AND gmail_id = ?
                """,
                (user_id, gid),
            ).fetchone()
        if not row:
            return None
        return _row_to_payload(row)

    def list_entries(self, *, user_id: str, limit: int = 10_000) -> list[dict[str, Any]]:
        cap = max(1, min(limit, 50_000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT gmail_id, email_json, result_json, updated_at
                FROM truth_entries
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (user_id, cap),
            ).fetchall()
        return [_row_to_payload(r) for r in rows]

    def delete_entry(self, *, user_id: str, gmail_id: str) -> bool:
        gid = str(gmail_id or "").strip()
        if not gid:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM truth_entries WHERE user_id = ? AND gmail_id = ?",
                (user_id, gid),
            )
        return int(cur.rowcount or 0) > 0

    def count_entries(self, *, user_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM truth_entries WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["c"]) if row else 0


def _row_to_payload(row: sqlite3.Row) -> dict[str, Any]:
    email = _safe_json(row["email_json"])
    result = _safe_json(row["result_json"])
    return {
        "gmail_id": str(row["gmail_id"]),
        "email": email if isinstance(email, dict) else {},
        "result": result if isinstance(result, dict) else {},
        "updated_at": str(row["updated_at"] or ""),
    }


def _safe_json(blob: str) -> Any:
    if not blob or not str(blob).strip():
        return {}
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return {}
