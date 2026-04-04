"""CLI entry points. Keep thin: parse args, call ingestion, serialize output."""

from __future__ import annotations

import argparse
import json
import sys

from jobinbox.config import Settings
from jobinbox.ingestion import fetch_inbox_preview


def _cmd_fetch(args: argparse.Namespace) -> int:
    settings = Settings.load()
    rows = fetch_inbox_preview(
        credentials_path=settings.credentials_path,
        token_path=settings.token_path,
        max_results=args.limit or settings.max_messages,
        query=args.query or settings.gmail_query,
    )
    json.dump(rows, sys.stdout, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobinbox", description="Job inbox PoC — Gmail ingestion")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="List recent inbox messages (metadata + snippet) as JSON")
    p_fetch.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max messages (default: JOBINBOX_MAX_MESSAGES or 25)",
    )
    p_fetch.add_argument(
        "--query",
        type=str,
        default=None,
        help="Gmail search query (default: JOBINBOX_GMAIL_QUERY or in:inbox)",
    )
    p_fetch.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    p_fetch.set_defaults(func=_cmd_fetch)

    ns = parser.parse_args(argv)
    return int(ns.func(ns))
