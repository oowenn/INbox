#!/usr/bin/env python3
"""
Validate the rule-based pre-filter (extraction/prefilter.py) against real classification
history in jobinbox.db, read-only. No LLM calls — safe to run regardless of API budget.

Usage: .venv/bin/python3 scripts/evaluate_prefilter.py [path/to/jobinbox.db]

Reports:
  1. Recall check: any application=yes row the filter would skip (must be zero before
     a rule is safe to ship).
  2. Coverage on already-classified application=no rows (retroactive savings, for
     visibility only — those calls are already paid for).
  3. Projected savings on cached-but-not-yet-classified rows (the real forward-looking
     number).

Re-run this whenever extraction/prefilter.py's rule lists change.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobinbox.extraction.prefilter import rule_skip_reason  # noqa: E402


def _default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "jobinbox.db"


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else _default_db_path()
    if not db_path.exists():
        print(f"No database found at {db_path}")
        return 1

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    classified = conn.execute(
        """
        SELECT ec.sender AS sender, ec.subject AS subject, er.application AS application
        FROM email_content ec
        JOIN email_results er ON er.gmail_id = ec.gmail_id
        """
    ).fetchall()

    false_negatives: list[tuple[str, str, str]] = []
    no_total = 0
    no_skipped = 0
    for row in classified:
        reason = rule_skip_reason(sender=row["sender"] or "", subject=row["subject"] or "")
        application = str(row["application"] or "").strip().lower()
        if application == "yes":
            if reason:
                false_negatives.append((row["sender"], row["subject"], reason))
        elif application == "no":
            no_total += 1
            if reason:
                no_skipped += 1

    unclassified = conn.execute(
        """
        SELECT ec.sender AS sender, ec.subject AS subject
        FROM email_content ec
        LEFT JOIN email_results er ON er.gmail_id = ec.gmail_id
        WHERE er.gmail_id IS NULL
        """
    ).fetchall()
    backlog_total = len(unclassified)
    backlog_skippable = sum(
        1
        for row in unclassified
        if rule_skip_reason(sender=row["sender"] or "", subject=row["subject"] or "")
    )

    conn.close()

    print(f"Database: {db_path}")
    print()
    print("1. Recall check (must be zero false negatives)")
    if false_negatives:
        print(f"   FAIL: {len(false_negatives)} application=yes rows would be skipped:")
        for sender, subject, reason in false_negatives[:20]:
            print(f"     - reason={reason!r} sender={sender!r} subject={subject!r}")
        if len(false_negatives) > 20:
            print(f"     ... and {len(false_negatives) - 20} more")
    else:
        print("   PASS: zero application=yes rows would be skipped.")
    print()

    no_pct = (no_skipped / no_total * 100.0) if no_total else 0.0
    print("2. Coverage on already-classified application=no rows")
    print(f"   {no_skipped} / {no_total} ({no_pct:.1f}%) would have been skippable.")
    print()

    backlog_pct = (backlog_skippable / backlog_total * 100.0) if backlog_total else 0.0
    print("3. Projected savings on cached-but-unclassified backlog")
    print(f"   {backlog_skippable} / {backlog_total} ({backlog_pct:.1f}%) are skippable now.")

    return 1 if false_negatives else 0


if __name__ == "__main__":
    raise SystemExit(main())
