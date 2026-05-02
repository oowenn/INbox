"""SQLite storage for cached Gmail content and extracted classification results."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from jobinbox.extraction.company_normalization import canonical_company_for_storage
from jobinbox.extraction.role_normalization import canonical_role_for_storage

_UNKNOWN_ROLE_VALUES = {
    "unknown",
    "unknown role",
    "(unknown role)",
    "n/a",
    "na",
    "none",
    "null",
    "tbd",
}


def _cycle_bounds_ms(cycle_start_year: int) -> tuple[int, int]:
    """Return inclusive start / exclusive end epoch-ms bounds for one June-to-June cycle."""
    local_tz = datetime.now().astimezone().tzinfo
    start_dt = datetime(cycle_start_year, 6, 1, tzinfo=local_tz)
    end_dt = datetime(cycle_start_year + 1, 6, 1, tzinfo=local_tz)
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


def _cycle_filter_sql(*, cycle_start_year: int | None, internal_ms_expr: str) -> tuple[str, list[int]]:
    if not isinstance(cycle_start_year, int) or cycle_start_year < 2000 or cycle_start_year > 2100:
        return "", []
    start_ms, end_ms = _cycle_bounds_ms(cycle_start_year)
    return f" AND {internal_ms_expr} >= ? AND {internal_ms_expr} < ?", [start_ms, end_ms]


class JobInboxStore:
    """Small SQLite wrapper for PoC persistence."""

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
                    canonical_company TEXT,
                    role TEXT,
                    canonical_role TEXT,
                    stage TEXT NOT NULL,
                    interview_date TEXT,
                    extraction_json TEXT,
                    llm_provider TEXT,
                    llm_model TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (gmail_id) REFERENCES email_content(gmail_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_user_emails_user_fetch
                    ON user_emails(user_id, fetched_at DESC);

                CREATE INDEX IF NOT EXISTS idx_email_content_internal_date
                    ON email_content(internal_date DESC);

                CREATE TABLE IF NOT EXISTS company_curation_locks (
                    user_id TEXT NOT NULL,
                    company_key TEXT NOT NULL,
                    lock_token TEXT NOT NULL,
                    locked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, company_key)
                );

                CREATE TABLE IF NOT EXISTS classification_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    gmail_id TEXT NOT NULL,
                    error_type TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_classification_failures_user_time
                    ON classification_failures(user_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_classification_failures_user_gmail
                    ON classification_failures(user_id, gmail_id);

                CREATE TABLE IF NOT EXISTS processing_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    run_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cycle_start_date TEXT,
                    cycle_query TEXT,
                    requested_fetch_limit INTEGER NOT NULL DEFAULT 0,
                    classify_batch_size INTEGER NOT NULL DEFAULT 0,
                    classify_concurrency INTEGER,
                    listed_total INTEGER NOT NULL DEFAULT 0,
                    fetched_new_total INTEGER NOT NULL DEFAULT 0,
                    duplicate_total INTEGER NOT NULL DEFAULT 0,
                    fetch_failed_total INTEGER NOT NULL DEFAULT 0,
                    classify_attempted_total INTEGER NOT NULL DEFAULT 0,
                    classify_processed_total INTEGER NOT NULL DEFAULT 0,
                    classify_failed_total INTEGER NOT NULL DEFAULT 0,
                    classify_yes_total INTEGER NOT NULL DEFAULT 0,
                    classify_no_total INTEGER NOT NULL DEFAULT 0,
                    classify_yes_missing_company_total INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_processing_runs_user_started
                    ON processing_runs(user_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS processing_run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (run_id) REFERENCES processing_runs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_processing_run_events_run_time
                    ON processing_run_events(run_id, created_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS user_cycle_gmail_list_counts (
                    user_id TEXT NOT NULL,
                    cycle_start_year INTEGER NOT NULL,
                    cycle_query TEXT NOT NULL,
                    exact_listed_count INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (user_id, cycle_start_year)
                );

                CREATE INDEX IF NOT EXISTS idx_cycle_gmail_counts_user
                    ON user_cycle_gmail_list_counts(user_id);
                """
            )
            self._migrate_schema(conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(email_results)").fetchall()
        column_names = {str(r[1]) for r in rows}
        if "extraction_json" not in column_names:
            conn.execute("ALTER TABLE email_results ADD COLUMN extraction_json TEXT")
        if "canonical_company" not in column_names:
            conn.execute("ALTER TABLE email_results ADD COLUMN canonical_company TEXT")
            conn.execute(
                """
                UPDATE email_results
                SET canonical_company = LOWER(TRIM(COALESCE(company, '')))
                WHERE canonical_company IS NULL
                """
            )
        if "canonical_role" not in column_names:
            conn.execute("ALTER TABLE email_results ADD COLUMN canonical_role TEXT")
            conn.execute(
                """
                UPDATE email_results
                SET canonical_role = role
                WHERE canonical_role IS NULL
                """
            )

        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='classification_failures'"
        ).fetchone()
        if existing is None:
            conn.executescript(
                """
                CREATE TABLE classification_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    gmail_id TEXT NOT NULL,
                    error_type TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX idx_classification_failures_user_time
                    ON classification_failures(user_id, created_at DESC);
                CREATE INDEX idx_classification_failures_user_gmail
                    ON classification_failures(user_id, gmail_id);
                """
            )

    @staticmethod
    def company_key_for_lock(company: str) -> str:
        key = canonical_company_for_storage(company)
        return key or company.strip().lower()

    @contextmanager
    def company_curation_lock(
        self,
        *,
        user_id: str,
        company_key: str,
        timeout_s: float = 120.0,
    ) -> Iterator[None]:
        """
        Serialize timeline curation for one (user, company_key).

        Lock rows older than 5 minutes are treated as stale and removed.
        """
        token = uuid.uuid4().hex
        deadline = time.monotonic() + timeout_s
        acquired = False
        while time.monotonic() < deadline:
            with self._connect() as conn:
                conn.execute(
                    """
                    DELETE FROM company_curation_locks
                    WHERE locked_at < datetime('now', '-300 seconds')
                    """
                )
                try:
                    conn.execute(
                        """
                        INSERT INTO company_curation_locks (user_id, company_key, lock_token, locked_at)
                        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (user_id, company_key, token),
                    )
                    acquired = True
                    break
                except sqlite3.IntegrityError:
                    pass
            time.sleep(0.05)

        if not acquired:
            raise TimeoutError(
                f"Company curation lock not acquired for {company_key!r} within {timeout_s:.0f}s"
            )

        try:
            yield
        finally:
            with self._connect() as conn:
                conn.execute(
                    """
                    DELETE FROM company_curation_locks
                    WHERE user_id = ? AND company_key = ? AND lock_token = ?
                    """,
                    (user_id, company_key, token),
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
        extraction: dict[str, Any] | None = None,
    ) -> None:
        extraction_blob = (
            json.dumps(extraction, ensure_ascii=True, separators=(",", ":"))
            if extraction is not None
            else None
        )
        company_value = result.get("company")
        canonical_company = canonical_company_for_storage(
            company_value if isinstance(company_value, str) else None
        )

        role_value = result.get("role")
        canonical_value = canonical_role_for_storage(
            role_value if isinstance(role_value, str) else None
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO email_results (
                    gmail_id, application, company, canonical_company,
                    role, canonical_role,
                    stage, interview_date,
                    extraction_json,
                    llm_provider, llm_model, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(gmail_id) DO UPDATE SET
                    application = excluded.application,
                    company = excluded.company,
                    canonical_company = excluded.canonical_company,
                    role = excluded.role,
                    canonical_role = excluded.canonical_role,
                    stage = excluded.stage,
                    interview_date = excluded.interview_date,
                    extraction_json = excluded.extraction_json,
                    llm_provider = excluded.llm_provider,
                    llm_model = excluded.llm_model,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    gmail_id,
                    str(result.get("application") or "no"),
                    company_value,
                    canonical_company,
                    role_value,
                    canonical_value,
                    str(result.get("stage") or "Unknown"),
                    result.get("interview_date"),
                    extraction_blob,
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
                    er.extraction_json AS result_extraction_json,
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

    def get_result_for_user_message(self, *, user_id: str, gmail_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    er.application AS application,
                    er.company AS company,
                    er.canonical_company AS canonical_company,
                    er.role AS role,
                    er.stage AS stage,
                    er.interview_date AS interview_date
                FROM user_emails AS ue
                JOIN email_results AS er ON er.gmail_id = ue.gmail_id
                WHERE ue.user_id = ? AND ue.gmail_id = ?
                """,
                (user_id, gmail_id),
            ).fetchone()
        if not row:
            return None
        return {
            "application": str(row["application"] or "no"),
            "company": (str(row["company"] or "").strip() or None),
            "canonical_company": (str(row["canonical_company"] or "").strip() or None),
            "role": (str(row["role"] or "").strip() or None),
            "stage": str(row["stage"] or "Unknown"),
            "interview_date": (str(row["interview_date"] or "").strip() or None),
        }

    def list_latest_unprocessed_messages(
        self,
        *,
        user_id: str,
        limit: int,
        min_internal_ts: int | None = None,
        max_internal_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        """Most recent messages for this user that do not have a stored classification yet."""
        params: list[Any] = [user_id]
        internal_clause = ""
        if isinstance(min_internal_ts, int) and min_internal_ts > 0:
            internal_clause += " AND CAST(COALESCE(ec.internal_date, '0') AS INTEGER) >= ?"
            params.append(int(min_internal_ts))
        if isinstance(max_internal_ts, int) and max_internal_ts > 0:
            internal_clause += " AND CAST(COALESCE(ec.internal_date, '0') AS INTEGER) < ?"
            params.append(int(max_internal_ts))
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
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
                  {internal_clause}
                ORDER BY CAST(COALESCE(ec.internal_date, '0') AS INTEGER) DESC, ue.fetched_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def count_unprocessed_messages(
        self,
        *,
        user_id: str,
        min_internal_ts: int | None = None,
        max_internal_ts: int | None = None,
    ) -> int:
        params: list[Any] = [user_id]
        internal_clause = ""
        if isinstance(min_internal_ts, int) and min_internal_ts > 0:
            internal_clause += " AND CAST(COALESCE(ec.internal_date, '0') AS INTEGER) >= ?"
            params.append(int(min_internal_ts))
        if isinstance(max_internal_ts, int) and max_internal_ts > 0:
            internal_clause += " AND CAST(COALESCE(ec.internal_date, '0') AS INTEGER) < ?"
            params.append(int(max_internal_ts))
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM user_emails AS ue
                JOIN email_content AS ec ON ec.gmail_id = ue.gmail_id
                LEFT JOIN email_results AS er ON er.gmail_id = ue.gmail_id
                WHERE ue.user_id = ?
                  AND er.gmail_id IS NULL
                  {internal_clause}
                """,
                params,
            ).fetchone()
        return int(row["c"] or 0) if row else 0

    def existing_user_email_ids(self, *, user_id: str, gmail_ids: list[str]) -> set[str]:
        ids = [str(gid).strip() for gid in gmail_ids if str(gid).strip()]
        if not ids:
            return set()
        found: set[str] = set()
        chunk_size = 800  # Keep comfortably under SQLite's variable limit.
        with self._connect() as conn:
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start : start + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT gmail_id
                    FROM user_emails
                    WHERE user_id = ? AND gmail_id IN ({placeholders})
                    """,
                    [user_id, *chunk],
                ).fetchall()
                found.update(str(r["gmail_id"]) for r in rows)
        return found

    def get_cycle_gmail_list_count(
        self, *, user_id: str, cycle_start_year: int, cycle_query: str
    ) -> int | None:
        """Return stored Gmail list count when the cycle query still matches."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT cycle_query, exact_listed_count
                FROM user_cycle_gmail_list_counts
                WHERE user_id = ? AND cycle_start_year = ?
                """,
                (user_id, cycle_start_year),
            ).fetchone()
        if row is None:
            return None
        if str(row["cycle_query"] or "") != str(cycle_query or ""):
            return None
        return int(row["exact_listed_count"] or 0)

    def upsert_cycle_gmail_list_count(
        self,
        *,
        user_id: str,
        cycle_start_year: int,
        cycle_query: str,
        exact_listed_count: int,
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_cycle_gmail_list_counts (
                    user_id, cycle_start_year, cycle_query, exact_listed_count, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, cycle_start_year) DO UPDATE SET
                    cycle_query = excluded.cycle_query,
                    exact_listed_count = excluded.exact_listed_count,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    cycle_start_year,
                    str(cycle_query or ""),
                    max(0, int(exact_listed_count)),
                    now,
                ),
            )

    def delete_cycle_gmail_list_count(self, *, user_id: str, cycle_start_year: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM user_cycle_gmail_list_counts
                WHERE user_id = ? AND cycle_start_year = ?
                """,
                (user_id, cycle_start_year),
            )

    def count_user_emails(self, *, user_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM user_emails WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def get_user_dashboard_summary(self, *, user_id: str, cycle_start_year: int | None = None) -> dict[str, Any]:
        """Aggregate dashboard totals and stage counts for a single user."""
        cycle_clause, cycle_params = _cycle_filter_sql(
            cycle_start_year=cycle_start_year,
            internal_ms_expr="CAST(COALESCE(ec.internal_date, '0') AS INTEGER)",
        )
        with self._connect() as conn:
            totals = conn.execute(
                f"""
                SELECT
                    COUNT(ue.gmail_id) AS cached_total,
                    COALESCE(SUM(CASE WHEN er.gmail_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS classified_total,
                    COALESCE(SUM(CASE WHEN er.gmail_id IS NULL THEN 1 ELSE 0 END), 0) AS unprocessed_total,
                    COALESCE(SUM(CASE
                        WHEN LOWER(COALESCE(er.application, '')) = 'yes'
                         AND TRIM(COALESCE(er.company, '')) <> ''
                        THEN 1 ELSE 0 END), 0) AS application_yes_total,
                    COALESCE(SUM(CASE WHEN LOWER(COALESCE(er.application, '')) = 'no' THEN 1 ELSE 0 END), 0) AS application_no_total
                FROM user_emails AS ue
                JOIN email_content AS ec ON ec.gmail_id = ue.gmail_id
                LEFT JOIN email_results AS er ON er.gmail_id = ue.gmail_id
                WHERE ue.user_id = ?
                {cycle_clause}
                """,
                (user_id, *cycle_params),
            ).fetchone()

            stage_rows = conn.execute(
                f"""
                SELECT
                    er.stage AS stage,
                    COUNT(*) AS count
                FROM user_emails AS ue
                JOIN email_results AS er ON er.gmail_id = ue.gmail_id
                JOIN email_content AS ec ON ec.gmail_id = ue.gmail_id
                WHERE ue.user_id = ?
                  AND LOWER(er.application) = 'yes'
                  AND TRIM(COALESCE(er.company, '')) <> ''
                  {cycle_clause}
                GROUP BY er.stage
                ORDER BY count DESC, er.stage ASC
                """,
                (user_id, *cycle_params),
            ).fetchall()

        return {
            "cached_total": int(totals["cached_total"] or 0) if totals else 0,
            "classified_total": int(totals["classified_total"] or 0) if totals else 0,
            "unprocessed_total": int(totals["unprocessed_total"] or 0) if totals else 0,
            "application_yes_total": int(totals["application_yes_total"] or 0) if totals else 0,
            "application_no_total": int(totals["application_no_total"] or 0) if totals else 0,
            "stage_counts": [
                {
                    "stage": str(r["stage"] or "Unknown"),
                    "count": int(r["count"] or 0),
                }
                for r in stage_rows
            ],
        }

    def list_cached_email_month_counts(
        self,
        *,
        user_id: str,
        limit_months: int = 48,
        cycle_start_year: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Count cached emails per month for this user.

        Returns rows like:
          { "month": "YYYY-MM", "count": int, "received_count": int }
        sorted ascending by month.
        Months are derived from Gmail internalDate (epoch ms) when present, else the row is ignored.
        """
        cap = max(1, min(240, int(limit_months or 48)))
        cycle_clause, cycle_params = _cycle_filter_sql(
            cycle_start_year=cycle_start_year,
            internal_ms_expr="CAST(COALESCE(ec.internal_date, '0') AS INTEGER)",
        )
        params: list[Any] = [user_id, *cycle_params, cap]
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                WITH per_message AS (
                  SELECT
                    CAST(COALESCE(ec.internal_date, '0') AS INTEGER) AS internal_ms,
                    LOWER(COALESCE(er.application, '')) AS application,
                    COALESCE(er.stage, '') AS stage
                  FROM user_emails AS ue
                  JOIN email_content AS ec ON ec.gmail_id = ue.gmail_id
                  LEFT JOIN email_results AS er ON er.gmail_id = ue.gmail_id
                  WHERE ue.user_id = ?
                    {cycle_clause}
                )
                SELECT
                  strftime('%Y-%m', datetime(internal_ms / 1000, 'unixepoch')) AS month,
                  COUNT(*) AS count,
                  COALESCE(SUM(CASE WHEN application = 'yes' AND stage = 'Received' THEN 1 ELSE 0 END), 0) AS received_count
                FROM per_message
                WHERE internal_ms > 0
                GROUP BY month
                ORDER BY month DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        out = [dict(r) for r in rows if r["month"]]
        out.reverse()
        return out

    def list_user_classified_stage_events(
        self,
        *,
        user_id: str,
        cycle_start_year: int | None = None,
    ) -> list[dict[str, Any]]:
        """Classified stage events for this user, ordered by application pair and time."""
        cycle_clause, cycle_params = _cycle_filter_sql(
            cycle_start_year=cycle_start_year,
            internal_ms_expr="CAST(COALESCE(ec.internal_date, '0') AS INTEGER)",
        )
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    TRIM(er.company) AS company,
                    COALESCE(NULLIF(TRIM(er.canonical_company), ''), '') AS canonical_company,
                    COALESCE(NULLIF(TRIM(er.role), ''), '(Unknown Role)') AS role,
                    COALESCE(NULLIF(TRIM(er.canonical_role), ''), '') AS canonical_role,
                    er.stage AS stage,
                    er.interview_date AS interview_date,
                    CAST(COALESCE(ec.internal_date, '0') AS INTEGER) AS internal_ts,
                    ue.fetched_at AS fetched_at,
                    er.updated_at AS result_updated_at
                FROM user_emails AS ue
                JOIN email_content AS ec ON ec.gmail_id = ue.gmail_id
                JOIN email_results AS er ON er.gmail_id = ue.gmail_id
                WHERE ue.user_id = ?
                  AND LOWER(er.application) = 'yes'
                  AND TRIM(COALESCE(er.company, '')) <> ''
                  {cycle_clause}
                ORDER BY
                    LOWER(TRIM(er.company)),
                    LOWER(COALESCE(NULLIF(TRIM(er.role), ''), '(Unknown Role)')),
                    CAST(COALESCE(ec.internal_date, '0') AS INTEGER),
                    ue.fetched_at,
                    er.updated_at
                """,
                (user_id, *cycle_params),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_company_history_events(
        self,
        *,
        user_id: str,
        company: str,
        limit: int = 20,
        exclude_gmail_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recent yes-classified events for one company, oldest->newest."""
        company_norm = company.strip()
        company_key = canonical_company_for_storage(company_norm) or company_norm.strip().lower()
        if not company_key:
            return []

        exclude_clause = ""
        params: list[Any] = [user_id, company_key]
        if exclude_gmail_id:
            exclude_clause = "AND er.gmail_id <> ?"
            params.append(exclude_gmail_id)
        params.append(max(1, limit))

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    er.gmail_id AS gmail_id,
                    CAST(COALESCE(ec.internal_date, '0') AS INTEGER) AS internal_ts,
                    er.role AS role,
                    er.canonical_role AS canonical_role,
                    er.stage AS stage,
                    er.interview_date AS interview_date,
                    er.application AS application
                FROM user_emails AS ue
                JOIN email_results AS er ON er.gmail_id = ue.gmail_id
                JOIN email_content AS ec ON ec.gmail_id = ue.gmail_id
                WHERE ue.user_id = ?
                  AND LOWER(
                        COALESCE(
                            NULLIF(TRIM(er.canonical_company), ''),
                            TRIM(COALESCE(er.company, ''))
                        )
                      ) = LOWER(TRIM(?))
                  AND LOWER(COALESCE(er.application, '')) = 'yes'
                  {exclude_clause}
                ORDER BY
                    CAST(COALESCE(ec.internal_date, '0') AS INTEGER) DESC,
                    er.updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

        # Query gets most-recent rows first; validator context is easier oldest->newest.
        out = [dict(r) for r in rows]
        out.reverse()
        return out

    def remove_events_from_timeline(
        self,
        *,
        user_id: str,
        gmail_ids: list[str],
        allowed_gmail_ids: set[str],
    ) -> int:
        """
        Apply remove_event operation by converting rows to application=no.

        We intentionally keep rows so the system can distinguish "processed and removed"
        from "never processed".
        """
        if not gmail_ids or not allowed_gmail_ids:
            return 0

        unique_ids = {
            str(gid).strip()
            for gid in gmail_ids
            if str(gid).strip() and str(gid).strip() in allowed_gmail_ids
        }
        if not unique_ids:
            return 0

        updated = 0
        with self._connect() as conn:
            for gmail_id in unique_ids:
                cur = conn.execute(
                    """
                    UPDATE email_results
                    SET
                        application = 'no',
                        company = NULL,
                        canonical_company = NULL,
                        role = NULL,
                        canonical_role = NULL,
                        stage = 'Unknown',
                        interview_date = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE gmail_id = ?
                      AND EXISTS (
                        SELECT 1 FROM user_emails AS ue
                        WHERE ue.user_id = ? AND ue.gmail_id = email_results.gmail_id
                      )
                    """,
                    (gmail_id, user_id),
                )
                updated += int(cur.rowcount or 0)

        return updated

    def apply_role_backfills(
        self,
        *,
        user_id: str,
        updates: list[dict[str, str]],
        allowed_gmail_ids: set[str],
    ) -> int:
        """
        Backfill role values for existing results with strict safety constraints.

        - only gmail_ids owned by user_id
        - only gmail_ids present in allowed_gmail_ids (typically validator history ids)
        - only rows where application = yes
        - only rows where current role is empty/unknown placeholder
        """
        if not updates or not allowed_gmail_ids:
            return 0

        normalized_updates: dict[str, str] = {}
        for item in updates:
            if not isinstance(item, dict):
                continue
            gmail_id = str(item.get("gmail_id") or "").strip()
            role = str(item.get("role") or "").strip()
            if not gmail_id or not role:
                continue
            if gmail_id not in allowed_gmail_ids:
                continue
            if role.lower() in _UNKNOWN_ROLE_VALUES:
                continue
            normalized_updates[gmail_id] = role

        if not normalized_updates:
            return 0

        updated_count = 0
        with self._connect() as conn:
            for gmail_id, role in normalized_updates.items():
                current = conn.execute(
                    """
                    SELECT er.role AS role, er.application AS application
                    FROM user_emails AS ue
                    JOIN email_results AS er ON er.gmail_id = ue.gmail_id
                    WHERE ue.user_id = ? AND ue.gmail_id = ?
                    """,
                    (user_id, gmail_id),
                ).fetchone()
                if not current:
                    continue
                if str(current["application"] or "").strip().lower() != "yes":
                    continue

                current_role_raw = str(current["role"] or "").strip()
                if current_role_raw and current_role_raw.lower() not in _UNKNOWN_ROLE_VALUES:
                    # Do not overwrite existing non-placeholder roles.
                    continue

                canonical = canonical_role_for_storage(role)
                cur = conn.execute(
                    """
                    UPDATE email_results
                    SET role = ?,
                        canonical_role = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE gmail_id = ?
                    """,
                    (role, canonical, gmail_id),
                )
                if (cur.rowcount or 0) > 0:
                    updated_count += 1

        return updated_count

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

    def clear_cached_messages_for_user(self, *, user_id: str) -> dict[str, int]:
        """
        Remove all cached emails for one user and prune orphan rows.

        `email_results` tied to orphaned `email_content` rows are removed by FK cascade.
        """
        with self._connect() as conn:
            before_results = conn.execute(
                "SELECT COUNT(*) AS c FROM email_results"
            ).fetchone()

            user_links_cur = conn.execute(
                "DELETE FROM user_emails WHERE user_id = ?",
                (user_id,),
            )

            orphan_content_cur = conn.execute(
                """
                DELETE FROM email_content
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM user_emails AS ue
                    WHERE ue.gmail_id = email_content.gmail_id
                )
                """
            )

            after_results = conn.execute(
                "SELECT COUNT(*) AS c FROM email_results"
            ).fetchone()

        before_results_n = int(before_results["c"] or 0) if before_results else 0
        after_results_n = int(after_results["c"] or 0) if after_results else 0
        return {
            "deleted_user_links": int(user_links_cur.rowcount or 0),
            "deleted_orphan_messages": int(orphan_content_cur.rowcount or 0),
            "deleted_orphan_results": max(0, before_results_n - after_results_n),
        }

    def record_classification_failure(
        self,
        *,
        user_id: str,
        gmail_id: str,
        error_type: str,
        error_message: str,
    ) -> None:
        """Append one row for debugging batch/single classification failures."""
        gid = str(gmail_id or "").strip()
        if not gid:
            return
        et = (error_type or "Exception")[:200]
        msg = error_message or ""
        if len(msg) > 8000:
            msg = msg[:7997] + "..."
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO classification_failures (user_id, gmail_id, error_type, error_message)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, gid, et, msg),
            )

    def list_classification_failures(
        self,
        *,
        user_id: str,
        limit: int = 200,
        gmail_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recent failures for this user, newest first, optional gmail_id filter."""
        cap = max(1, min(500, limit))
        params: list[Any] = [user_id]
        filter_sql = ""
        if gmail_id and str(gmail_id).strip():
            filter_sql = "AND cf.gmail_id = ?"
            params.append(str(gmail_id).strip())
        params.append(cap)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    cf.id AS id,
                    cf.gmail_id AS gmail_id,
                    cf.error_type AS error_type,
                    cf.error_message AS error_message,
                    cf.created_at AS created_at,
                    ec.subject AS subject
                FROM classification_failures AS cf
                LEFT JOIN email_content AS ec ON ec.gmail_id = cf.gmail_id
                WHERE cf.user_id = ?
                {filter_sql}
                ORDER BY cf.created_at DESC, cf.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def create_processing_run(
        self,
        *,
        user_id: str,
        run_kind: str,
        cycle_start_date: str,
        cycle_query: str,
        requested_fetch_limit: int,
        classify_batch_size: int,
        classify_concurrency: int | None,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO processing_runs (
                    user_id, run_kind, status,
                    cycle_start_date, cycle_query,
                    requested_fetch_limit, classify_batch_size, classify_concurrency,
                    started_at, updated_at
                ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    user_id,
                    str(run_kind or "").strip() or "unknown",
                    str(cycle_start_date or "").strip() or None,
                    str(cycle_query or "").strip() or None,
                    int(max(0, requested_fetch_limit)),
                    int(max(0, classify_batch_size)),
                    int(classify_concurrency) if isinstance(classify_concurrency, int) else None,
                ),
            )
            return int(cur.lastrowid or 0)

    def update_processing_run_fetch(
        self,
        *,
        user_id: str,
        run_id: int,
        listed_total: int,
        fetched_new_total: int,
        duplicate_total: int,
        fetch_failed_total: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE processing_runs
                SET listed_total = ?,
                    fetched_new_total = ?,
                    duplicate_total = ?,
                    fetch_failed_total = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                (
                    int(max(0, listed_total)),
                    int(max(0, fetched_new_total)),
                    int(max(0, duplicate_total)),
                    int(max(0, fetch_failed_total)),
                    int(run_id),
                    user_id,
                ),
            )

    def update_processing_run_classification(
        self,
        *,
        user_id: str,
        run_id: int,
        classify_attempted_total: int,
        classify_processed_total: int,
        classify_failed_total: int,
        classify_yes_total: int,
        classify_no_total: int,
        classify_yes_missing_company_total: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE processing_runs
                SET classify_attempted_total = ?,
                    classify_processed_total = ?,
                    classify_failed_total = ?,
                    classify_yes_total = ?,
                    classify_no_total = ?,
                    classify_yes_missing_company_total = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                (
                    int(max(0, classify_attempted_total)),
                    int(max(0, classify_processed_total)),
                    int(max(0, classify_failed_total)),
                    int(max(0, classify_yes_total)),
                    int(max(0, classify_no_total)),
                    int(max(0, classify_yes_missing_company_total)),
                    int(run_id),
                    user_id,
                ),
            )

    def complete_processing_run(
        self,
        *,
        user_id: str,
        run_id: int,
        status: str,
        last_error: str | None = None,
    ) -> None:
        err = str(last_error or "").strip() or None
        if err and len(err) > 8000:
            err = err[:7997] + "..."
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE processing_runs
                SET status = ?,
                    last_error = ?,
                    completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                (str(status or "completed"), err, int(run_id), user_id),
            )

    def get_processing_run(
        self,
        *,
        user_id: str,
        run_id: int,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    user_id,
                    run_kind,
                    status,
                    cycle_start_date,
                    cycle_query,
                    requested_fetch_limit,
                    classify_batch_size,
                    classify_concurrency,
                    listed_total,
                    fetched_new_total,
                    duplicate_total,
                    fetch_failed_total,
                    classify_attempted_total,
                    classify_processed_total,
                    classify_failed_total,
                    classify_yes_total,
                    classify_no_total,
                    classify_yes_missing_company_total,
                    last_error,
                    started_at,
                    updated_at,
                    completed_at
                FROM processing_runs
                WHERE id = ? AND user_id = ?
                """,
                (int(run_id), user_id),
            ).fetchone()
        return dict(row) if row else None

    def record_processing_run_event(
        self,
        *,
        user_id: str,
        run_id: int,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        payload_json = None
        if payload is not None:
            payload_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
            if len(payload_json) > 16000:
                payload_json = payload_json[:15997] + "..."
        with self._connect() as conn:
            owned = conn.execute(
                "SELECT 1 FROM processing_runs WHERE id = ? AND user_id = ?",
                (int(run_id), user_id),
            ).fetchone()
            if not owned:
                return
            conn.execute(
                """
                INSERT INTO processing_run_events (run_id, event_type, payload_json)
                VALUES (?, ?, ?)
                """,
                (int(run_id), str(event_type or "event"), payload_json),
            )

