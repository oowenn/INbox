"""CLI entry points. Keep thin: parse args, call ingestion, serialize output."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jobinbox.config import Settings
from jobinbox.truth_commands import (
    get_truth_entry,
    import_from_main_db,
    parse_json_file,
    truth_count,
    upsert_correction,
)


def _cmd_fetch(args: argparse.Namespace) -> int:
    # Lazy import so truth commands work without Gmail deps installed locally.
    from jobinbox.ingestion import fetch_inbox_preview

    settings = Settings.load()
    rows = fetch_inbox_preview(
        desktop_credentials_path=settings.desktop_credentials_path,
        token_path=settings.token_path,
        max_results=args.limit or settings.max_messages,
        query=args.query or settings.gmail_query,
    )
    json.dump(rows, sys.stdout, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


def _cmd_truth_import(args: argparse.Namespace) -> int:
    settings = Settings.load()
    inserted, skipped = import_from_main_db(settings=settings, limit=args.limit)
    print(
        f"Truth import: {inserted} new row(s) inserted, {skipped} skipped (already in truth dataset) "
        f"→ {settings.truth_db_path}"
    )
    return 0


def _cmd_truth_set(args: argparse.Namespace) -> int:
    settings = Settings.load()
    result: dict
    if args.result_file:
        result = parse_json_file(Path(args.result_file))
    elif args.result_json is not None:
        result = json.loads(args.result_json)
        if not isinstance(result, dict):
            print("error: --result-json must be a JSON object", file=sys.stderr)
            return 2
    else:
        print("error: provide --result-file or --result-json", file=sys.stderr)
        return 2

    email = None
    if args.email_file:
        email = parse_json_file(Path(args.email_file))

    upsert_correction(
        settings=settings,
        gmail_id=args.gmail_id,
        result=result,
        email=email,
    )
    print(f"Upserted truth entry for {args.gmail_id!r} in {settings.truth_db_path}")
    return 0


def _cmd_truth_show(args: argparse.Namespace) -> int:
    settings = Settings.load()
    row = get_truth_entry(settings, args.gmail_id)
    if not row:
        print(f"No truth entry for gmail_id={args.gmail_id!r}", file=sys.stderr)
        return 1
    json.dump(row, sys.stdout, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


def _cmd_truth_count(args: argparse.Namespace) -> int:
    settings = Settings.load()
    n = truth_count(settings)
    print(n)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "jobinbox.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(prog="jobinbox", description="Job inbox PoC — Gmail ingestion")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="List recent inbox messages (metadata + snippet) as JSON")
    p_fetch.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max messages (default: JOBINBOX_MAX_MESSAGES or 500)",
    )
    p_fetch.add_argument(
        "--query",
        type=str,
        default=None,
        help="Gmail search query (default: JOBINBOX_GMAIL_QUERY or in:inbox)",
    )
    p_fetch.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    p_fetch.set_defaults(func=_cmd_fetch)

    p_serve = sub.add_parser("serve", help="Run local full-stack API + web UI")
    p_serve.add_argument("--host", type=str, default=settings.web_host, help="Host to bind web server")
    p_serve.add_argument("--port", type=int, default=settings.web_port, help="Port to bind web server")
    p_serve.add_argument("--reload", action="store_true", help="Enable auto-reload for local development")
    p_serve.set_defaults(func=_cmd_serve)

    p_truth = sub.add_parser("truth", help="Truth dataset (ground labels) — import from main DB or correct rows")
    truth_sub = p_truth.add_subparsers(dest="truth_command", required=True)

    p_t_import = truth_sub.add_parser(
        "import",
        help="Insert classified rows from JOBINBOX_DB_PATH into truth DB (skip gmail_ids already present)",
    )
    p_t_import.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max rows to import (default: all, newest internal_date first)",
    )
    p_t_import.set_defaults(func=_cmd_truth_import)

    p_t_set = truth_sub.add_parser(
        "set",
        help="Upsert one row (overwrite corrections). Email comes from main DB or prior truth row unless --email-file",
    )
    p_t_set.add_argument("--gmail-id", required=True, dest="gmail_id", help="Gmail message id")
    g = p_t_set.add_mutually_exclusive_group(required=True)
    g.add_argument("--result-file", type=str, help="JSON file with result object (e.g. final + extraction)")
    g.add_argument("--result-json", type=str, help="Inline JSON object string")
    p_t_set.add_argument(
        "--email-file",
        type=str,
        help="Optional JSON file with email fields (id, subject, body, ...). Required if id missing everywhere else.",
    )
    p_t_set.set_defaults(func=_cmd_truth_set)

    p_t_show = truth_sub.add_parser("show", help="Print one truth entry as JSON")
    p_t_show.add_argument("--gmail-id", required=True, dest="gmail_id")
    p_t_show.add_argument("--pretty", action="store_true")
    p_t_show.set_defaults(func=_cmd_truth_show)

    p_t_count = truth_sub.add_parser("count", help="Print number of truth rows for JOBINBOX_USER_ID")
    p_t_count.set_defaults(func=_cmd_truth_count)

    ns = parser.parse_args(argv)
    return int(ns.func(ns))


if __name__ == "__main__":
    raise SystemExit(main())
