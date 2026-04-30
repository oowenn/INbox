"""CLI helpers: sync main SQLite cache into truth dataset and manual corrections."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from jobinbox.config import Settings
from jobinbox.storage import TruthDatasetStore


def parse_json_file(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def _result_payload_from_main_row(row: sqlite3.Row) -> dict[str, Any]:
    extraction_raw = row["extraction_json"]
    extraction: dict[str, Any] | None = None
    if isinstance(extraction_raw, str) and extraction_raw.strip():
        try:
            parsed = json.loads(extraction_raw)
            extraction = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            extraction = None

    final = {
        "application": str(row["result_application"] or "no"),
        "company": (str(row["result_company"] or "").strip() or None),
        "role": (str(row["result_role"] or "").strip() or None),
        "stage": str(row["result_stage"] or "Unknown"),
        "interview_date": (str(row["result_interview_date"] or "").strip() or None),
    }
    return {"final": final, "extraction": extraction}


def _email_payload_from_main_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"] or ""),
        "threadId": str(row["threadId"] or "") or None,
        "internalDate": str(row["internalDate"] or "") or None,
        "subject": str(row["subject"] or ""),
        "sender": str(row["sender"] or ""),
        "recipient": str(row["recipient"] or ""),
        "date": str(row["date"] or ""),
        "snippet": str(row["snippet"] or ""),
        "body": str(row["body"] or ""),
    }


def _open_main(settings: Settings) -> sqlite3.Connection:
    conn = sqlite3.connect(str(settings.db_path), timeout=60.0)
    conn.row_factory = sqlite3.Row
    return conn


def import_from_main_db(
    *,
    settings: Settings,
    limit: int | None = None,
) -> tuple[int, int]:
    """
    Copy classified rows from the main DB into the truth dataset.

    Does **not** overwrite rows that already exist in the truth dataset (same `user_id` + `gmail_id`).

    Returns (inserted_count, skipped_existing_count).
    """
    truth = TruthDatasetStore(db_path=settings.truth_db_path)
    user_id = settings.user_id

    with _open_main(settings) as conn:
        q = """
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
                er.application AS result_application,
                er.company AS result_company,
                er.role AS result_role,
                er.stage AS result_stage,
                er.interview_date AS result_interview_date,
                er.extraction_json AS extraction_json
            FROM user_emails AS ue
            JOIN email_content AS ec ON ec.gmail_id = ue.gmail_id
            JOIN email_results AS er ON er.gmail_id = ue.gmail_id
            WHERE ue.user_id = ?
            ORDER BY CAST(COALESCE(ec.internal_date, '0') AS INTEGER) DESC
        """
        params: list[Any] = [user_id]
        if limit is not None and limit > 0:
            q += " LIMIT ?"
            params.append(limit)

        rows = conn.execute(q, params).fetchall()

    inserted = 0
    skipped_existing = 0
    for row in rows:
        gid = str(row["id"] or "").strip()
        if not gid:
            continue
        email = _email_payload_from_main_row(row)
        result = _result_payload_from_main_row(row)
        if truth.insert_entry_if_absent(
            user_id=user_id, gmail_id=gid, email=email, result=result
        ):
            inserted += 1
        else:
            skipped_existing += 1

    return inserted, skipped_existing


def load_email_for_gmail_id(settings: Settings, gmail_id: str) -> dict[str, Any] | None:
    gid = str(gmail_id or "").strip()
    if not gid:
        return None
    with _open_main(settings) as conn:
        row = conn.execute(
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
                ec.body AS body
            FROM user_emails AS ue
            JOIN email_content AS ec ON ec.gmail_id = ue.gmail_id
            WHERE ue.user_id = ? AND ue.gmail_id = ?
            """,
            (settings.user_id, gid),
        ).fetchone()
    if not row:
        return None
    return _email_payload_from_main_row(row)


def upsert_correction(
    *,
    settings: Settings,
    gmail_id: str,
    result: dict[str, Any],
    email: dict[str, Any] | None,
) -> None:
    """Overwrite (or insert) one truth row; email required if not already in truth and not in main cache."""
    truth = TruthDatasetStore(db_path=settings.truth_db_path)
    gid = str(gmail_id or "").strip()
    if not gid:
        raise ValueError("gmail_id is required.")

    resolved_email = email
    if resolved_email is None:
        existing = truth.get_entry(user_id=settings.user_id, gmail_id=gid)
        if existing and isinstance(existing.get("email"), dict):
            resolved_email = existing["email"]
        else:
            resolved_email = load_email_for_gmail_id(settings, gid)

    if not resolved_email:
        raise ValueError(
            "No email content found: pass --email-file, or ensure this gmail_id exists in "
            "the main cache or truth dataset."
        )

    truth.upsert_entry(
        user_id=settings.user_id,
        gmail_id=gid,
        email=resolved_email,
        result=result,
    )


def get_truth_entry(settings: Settings, gmail_id: str) -> dict[str, Any] | None:
    truth = TruthDatasetStore(db_path=settings.truth_db_path)
    return truth.get_entry(user_id=settings.user_id, gmail_id=str(gmail_id).strip())


def truth_count(settings: Settings) -> int:
    truth = TruthDatasetStore(db_path=settings.truth_db_path)
    return truth.count_entries(user_id=settings.user_id)
