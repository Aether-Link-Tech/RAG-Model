"""Run the cross-layer integrity check (FR-075 / AL-270).

    python -m backend.app.scripts.integrity_check                 # report only
    python -m backend.app.scripts.integrity_check --json out.json # also write a JSON report
    python -m backend.app.scripts.integrity_check --repair        # remove bad vectors, mark missing files failed

Reads every user's documents, so it connects with the migration (owner) database URL; it
never runs inside the API process. Exit code: 0 = clean, 1 = issues found, 2 = critical
(possible cross-user exposure) found.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.core.config import settings
from backend.app.services.integrity_check import DocumentRecord, Report, check_integrity

logger = logging.getLogger("integrity_check")


async def load_documents(database_url: str | None = None) -> list[DocumentRecord]:
    engine = create_async_engine(database_url or settings.migration_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT id, user_id, visibility, status, file_path FROM documents"))
            return [DocumentRecord(r.id, r.user_id, r.visibility, r.status, r.file_path) for r in rows]
    finally:
        await engine.dispose()


async def mark_documents_failed(items: list[tuple[uuid.UUID, str]], database_url: str | None = None) -> None:
    if not items:
        return
    engine = create_async_engine(database_url or settings.migration_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            for doc_id, message in items:
                await conn.execute(
                    text("UPDATE documents SET status = 'failed', error_message = :m WHERE id = :id"),
                    {"m": message, "id": doc_id},
                )
    finally:
        await engine.dispose()


def run(argv: list[str] | None = None) -> tuple[Report, int]:
    parser = argparse.ArgumentParser(description="PostgreSQL / ChromaDB / storage integrity check")
    parser.add_argument("--repair", action="store_true", help="remove bad vectors and mark missing files failed")
    parser.add_argument("--json", type=Path, help="write the full report to this file")
    args = parser.parse_args(argv)

    documents = asyncio.run(load_documents())
    to_fail: list[tuple[uuid.UUID, str]] = []
    report = check_integrity(documents, repair=args.repair, mark_failed=lambda d, m: to_fail.append((d, m)))
    asyncio.run(mark_documents_failed(to_fail))

    summary = report.summary()
    for issue in report.issues:
        logger.warning("[%s] %s: %s (document=%s collection=%s path=%s)", issue.severity, issue.kind,
                       issue.detail, issue.document_id, issue.collection, issue.path)
    logger.info("Integrity summary: %s", json.dumps(summary))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    code = 2 if report.has_critical else (1 if report.issues else 0)
    return report, code


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _, code = run()
    sys.exit(code)


if __name__ == "__main__":
    main()
