#!/usr/bin/env python3
"""CLI entrypoint for AL-253 legacy per-user storage layout migration.

Dry-run is the default. Pass ``--apply`` to move files and update PostgreSQL.

Examples:
    python -m backend.scripts.migrate_legacy_storage
    python -m backend.scripts.migrate_legacy_storage --json
    python -m backend.scripts.migrate_legacy_storage --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Allow direct invocation via `python backend/scripts/migrate_legacy_storage.py`.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.app.services.legacy_storage_migration import (  # noqa: E402
    MigrationReport,
    run_database_migration,
)


def _format_text_report(report: MigrationReport) -> str:
    lines = [
        f"Legacy Storage Migration Report (mode={report.mode})",
        f"Upload dir        : {report.upload_dir}",
        f"Total documents   : {report.total_documents}",
        f"Already canonical : {report.already_canonical}",
        f"Orphaned legacy   : {report.orphaned_legacy_source}",
        f"Planned           : {report.planned}",
        f"Migrated          : {report.migrated}",
        f"Recovered cleanup : {report.recovered}",
        f"Skipped / Failed  : {report.skipped_or_failed}",
        "",
        "Per-document results:",
    ]
    if not report.items:
        lines.append("  (no documents found)")
    for item in report.items:
        line = (
            f"  [{item.status}] doc={item.document_id} user={item.user_id}"
            f" src={item.source_path or '-'} -> dst={item.destination_path or '-'}"
        )
        if item.detail:
            line += f" | {item.detail}"
        if item.warning:
            line += f" | WARNING: {item.warning}"
        lines.append(line)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate legacy <UPLOAD_DIR>/<user_id>/... files to <UPLOAD_DIR>/users/<user_id>/documents/<doc_id><ext>."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute file moves and transactional database updates. Without --apply, runs in dry-run mode.",
    )
    parser.add_argument(
        "--upload-dir",
        default=None,
        help="Override UPLOAD_DIR (defaults to settings.UPLOAD_DIR).",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override DATABASE_URL (defaults to settings.DATABASE_URL).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the full migration report as JSON.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = asyncio.run(
        run_database_migration(
            database_url=args.database_url,
            upload_dir=args.upload_dir,
            apply=args.apply,
        )
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(_format_text_report(report))
    return 1 if report.skipped_or_failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
