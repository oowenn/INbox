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
        desktop_credentials_path=settings.desktop_credentials_path,
        token_path=settings.token_path,
        max_results=args.limit or settings.max_messages,
        query=args.query or settings.gmail_query,
    )
    json.dump(rows, sys.stdout, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
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
        help="Max messages (default: JOBINBOX_MAX_MESSAGES or 200)",
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

    ns = parser.parse_args(argv)
    return int(ns.func(ns))
