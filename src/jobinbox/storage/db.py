"""SQLite storage for cached Gmail content and extracted classification results."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class JobInboxStore:
    """Small SQLite wrapper for PoC persistence."""

    def __init__(self, *, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_emails (
                    user_id TEXT NOT NULL,
                    gmail_id TEXT NOT NULL,
                    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, gmail_id)
                );

                CREATE TABLE IF NOT EXISTS email_content (
                    gmail_id TEXT PRIMARY KEY,
                    thread_id TEXT,
                    internal_date TEXT,
                    sender TEXT,
                    recipient TEXT,
                    subject TEXT,
                    date_header TEXT,
                    snippet TEXT,
                    body TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS email_results (
                    gmail_id TEXT PRIMARY KEY,
                    application TEXT NOT NULL,
                    company TEXT,
                    role TEXT,
                    stage TEXT NOT NULL,
                    interview_date TEXT,
                    llm_provider TEXT,
                    llm_model TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (gmail_id) REFERENCES email_content(gmail_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_user_emails_user_fetch
                    ON user_emails(user_id, fetched_at DESC);

                CREATE INDEX IF NOT EXISTS idx_email_content_internal_date
                    ON email_content(internal_date DESC);
                """
            )

    def upsert_email(self, *, user_id: str, row: dict[str, Any]) -> bool:
        """Insert or update content, and track that this user has fetched this gmail_id."""
        gmail_id = str(row.get("id") or "").strip()
        if not gmail_id:
            return False

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO email_content (
                    gmail_id, thread_id, internal_date, sender, recipient, subject, date_header,
                    snippet, body, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(gmail_id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    internal_date = excluded.internal_date,
                    sender = excluded.sender,
                    recipient = excluded.recipient,
                    subject = excluded.subject,
                    date_header = excluded.date_header,
                    snippet = excluded.snippet,
                    body = excluded.body,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    gmail_id,
                    str(row.get("threadId") or "") or None,
                    str(row.get("internalDate") or "") or None,
                    str(row.get("from") or "") or None,
                    str(row.get("to") or "") or None,
                    str(row.get("subject") or "") or None,
                    str(row.get("date") or "") or None,
                    str(row.get("snippet") or "") or None,
                    str(row.get("body") or "") or None,
                ),
            )

            cur = conn.execute(
                """
                INSERT INTO user_emails (user_id, gmail_id, fetched_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, gmail_id) DO NOTHING
                """,
                (user_id, gmail_id),
            )
            inserted_for_user = cur.rowcount > 0
            if not inserted_for_user:
                conn.execute(
                    """
                    UPDATE user_emails
                    SET fetched_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND gmail_id = ?
                    """,
                    (user_id, gmail_id),
                )
            return inserted_for_user

    def upsert_result(
        self,
        *,
        gmail_id: str,
        result: dict[str, Any],
        llm_provider: str,
        llm_model: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO email_results (
                    gmail_id, application, company, role, stage, interview_date,
                    llm_provider, llm_model, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(gmail_id) DO UPDATE SET
                    application = excluded.application,
                    company = excluded.company,
                    role = excluded.role,
                    stage = excluded.stage,
                    interview_date = excluded.interview_date,
                    llm_provider = excluded.llm_provider,
                    llm_model = excluded.llm_model,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    gmail_id,
                    str(result.get("application") or "no"),
                    result.get("company"),
                    result.get("role"),
                    str(result.get("stage") or "Unknown"),
                    result.get("interview_date"),
                    llm_provider,
                    llm_model,
                ),
            )

    def email_exists(self, *, gmail_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM email_content WHERE gmail_id = ?",
                (gmail_id,),
            ).fetchone()
        return row is not None

    def list_cached_messages(self, *, user_id: str, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    ec.gmail_id AS id,
                    ec.thread_id AS threadId,
                    ec.internal_date AS internalDate,
                    ec.subject AS subject,
                    ec.sender AS sender,
                    ec.recipient AS recipient,
                    ec.date_header AS date,
                    ec.snippet AS snippet,
                    ec.body AS body,
                    ue.fetched_at AS fetchedAt,
                    er.application AS result_application,
                    er.company AS result_company,
                    er.role AS result_role,
                    er.stage AS result_stage,
                    er.interview_date AS result_interview_date,
                    er.updated_at AS resultUpdatedAt
                FROM user_emails AS ue
                JOIN email_content AS ec ON ec.gmail_id = ue.gmail_id
                LEFT JOIN email_results AS er ON er.gmail_id = ue.gmail_id
                WHERE ue.user_id = ?
                ORDER BY CAST(COALESCE(ec.internal_date, '0') AS INTEGER) DESC, ue.fetched_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_latest_unprocessed_messages(self, *, user_id: str, limit: int) -> list[dict[str, Any]]:
        """Most recent messages for this user that do not have a stored classification yet."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    ec.gmail_id AS id,
                    ec.subject AS subject,
                    ec.sender AS sender,
                    ec.snippet AS snippet,
                    ec.body AS body
                FROM user_emails AS ue
                JOIN email_content AS ec ON ec.gmail_id = ue.gmail_id
                LEFT JOIN email_results AS er ON er.gmail_id = ue.gmail_id
                WHERE ue.user_id = ? AND er.gmail_id IS NULL
                ORDER BY CAST(COALESCE(ec.internal_date, '0') AS INTEGER) DESC, ue.fetched_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def existing_user_email_ids(self, *, user_id: str, gmail_ids: list[str]) -> set[str]:
        if not gmail_ids:
            return set()
        placeholders = ",".join("?" for _ in gmail_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT gmail_id
                FROM user_emails
                WHERE user_id = ? AND gmail_id IN ({placeholders})
                """,
                [user_id, *gmail_ids],
            ).fetchall()
        return {str(r["gmail_id"]) for r in rows}

    def count_user_emails(self, *, user_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM user_emails WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def clear_results_for_user(self, *, user_id: str) -> int:
        """Delete stored classification rows for the provided user."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM email_results
                WHERE gmail_id IN (
                    SELECT gmail_id
                    FROM user_emails
                    WHERE user_id = ?
                )
                """,
                (user_id,),
            )
        return int(cur.rowcount or 0)

