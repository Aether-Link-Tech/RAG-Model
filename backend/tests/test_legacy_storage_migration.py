from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from backend.app.core.config import settings
from backend.app.core.crypto import email_index
from backend.app.models.document import Document
from backend.app.models.user import User
from backend.app.services.legacy_storage_migration import (
    DocumentMigrationRecord,
    migrate_legacy_storage,
    run_database_migration,
)
from backend.app.services.storage_paths import resolve_user_file_path
from backend.scripts.migrate_legacy_storage import main as cli_main
from backend.tests.conftest import _CleanupSessionLocal


def _write_file(path: Path, content: bytes = b"sample-pdf-bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.asyncio
async def test_dry_run_makes_no_filesystem_or_db_changes(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    legacy_file = _write_file(upload_dir / str(user_id) / "report.pdf", b"legacy-data")
    unrelated = _write_file(upload_dir / str(user_id) / "notes.txt", b"keep-me")

    db_updates: list[tuple[uuid.UUID, str]] = []

    async def fake_update(did: uuid.UUID, new_path: str) -> None:
        db_updates.append((did, new_path))

    record = DocumentMigrationRecord(
        id=doc_id,
        user_id=user_id,
        filename="report.pdf",
        file_path=str(legacy_file),
    )

    report = await migrate_legacy_storage(
        [record],
        update_document_path=fake_update,
        upload_dir=upload_dir,
        apply=False,
    )

    assert report.mode == "dry-run"
    assert report.planned == 1
    assert report.migrated == 0
    assert report.items[0].status == "planned"
    assert db_updates == []
    assert legacy_file.exists()
    assert unrelated.exists()
    expected_dest = upload_dir / "users" / str(user_id) / "documents" / f"{doc_id}.pdf"
    assert not expected_dest.exists()
    assert not (upload_dir / "users").exists()


@pytest.mark.asyncio
async def test_successful_migration_moves_file_and_preserves_unrelated_files(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    legacy_file = _write_file(upload_dir / str(user_id) / f"{doc_id}.pdf", b"important-bytes")
    unrelated = _write_file(upload_dir / str(user_id) / "unrelated.txt", b"do-not-delete")

    db_state: dict[uuid.UUID, str] = {doc_id: str(legacy_file)}

    async def update_db(did: uuid.UUID, new_path: str) -> None:
        db_state[did] = new_path

    record = DocumentMigrationRecord(
        id=doc_id,
        user_id=user_id,
        filename="quarterly_report.pdf",
        file_path=str(legacy_file),
    )

    report = await migrate_legacy_storage(
        [record],
        update_document_path=update_db,
        upload_dir=upload_dir,
        apply=True,
    )

    assert report.mode == "apply"
    assert report.migrated == 1
    assert report.skipped_or_failed == 0
    assert report.items[0].status == "migrated"

    dest = Path(db_state[doc_id])
    assert dest == upload_dir.resolve() / "users" / str(user_id) / "documents" / f"{doc_id}.pdf"
    assert dest.is_file()
    assert dest.read_bytes() == b"important-bytes"
    assert not legacy_file.exists()
    assert unrelated.is_file()
    assert unrelated.read_bytes() == b"do-not-delete"

    # Destination must now pass strict runtime resolution.
    resolved = resolve_user_file_path(user_id, str(dest), base_dir=upload_dir, must_exist=True)
    assert resolved == dest


@pytest.mark.asyncio
async def test_existing_destination_conflict_never_overwrites(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    legacy_file = _write_file(upload_dir / str(user_id) / "doc.pdf", b"legacy-content")
    existing_dest = _write_file(
        upload_dir / "users" / str(user_id) / "documents" / f"{doc_id}.pdf",
        b"existing-canonical-content",
    )

    db_updates: list[tuple[uuid.UUID, str]] = []

    async def update_db(did: uuid.UUID, new_path: str) -> None:
        db_updates.append((did, new_path))

    record = DocumentMigrationRecord(
        id=doc_id,
        user_id=user_id,
        filename="doc.pdf",
        file_path=str(legacy_file),
    )

    report = await migrate_legacy_storage(
        [record],
        update_document_path=update_db,
        upload_dir=upload_dir,
        apply=True,
    )

    assert report.migrated == 0
    assert report.skipped_or_failed == 1
    assert report.items[0].status == "destination_exists"
    assert existing_dest.read_bytes() == b"existing-canonical-content"
    assert legacy_file.read_bytes() == b"legacy-content"
    assert db_updates == []


@pytest.mark.asyncio
async def test_missing_source_file_is_reported_without_changes(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    missing_path = upload_dir / str(user_id) / "missing.pdf"

    db_updates: list[tuple[uuid.UUID, str]] = []

    async def update_db(did: uuid.UUID, new_path: str) -> None:
        db_updates.append((did, new_path))

    record = DocumentMigrationRecord(
        id=doc_id,
        user_id=user_id,
        filename="missing.pdf",
        file_path=str(missing_path),
    )

    report = await migrate_legacy_storage(
        [record],
        update_document_path=update_db,
        upload_dir=upload_dir,
        apply=True,
    )

    assert report.migrated == 0
    assert report.skipped_or_failed == 1
    assert report.items[0].status == "missing_source"
    assert db_updates == []


@pytest.mark.asyncio
async def test_cross_user_path_reference_is_rejected(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    doc_id = uuid.uuid4()
    user_b_file = _write_file(upload_dir / str(user_b) / "secret.pdf", b"user-b-secret")

    db_updates: list[tuple[uuid.UUID, str]] = []

    async def update_db(did: uuid.UUID, new_path: str) -> None:
        db_updates.append((did, new_path))

    record = DocumentMigrationRecord(
        id=doc_id,
        user_id=user_a,
        filename="secret.pdf",
        file_path=str(user_b_file),
    )

    report = await migrate_legacy_storage(
        [record],
        update_document_path=update_db,
        upload_dir=upload_dir,
        apply=True,
    )

    assert report.migrated == 0
    assert report.skipped_or_failed == 1
    assert report.items[0].status == "cross_user_path"
    assert user_b_file.read_bytes() == b"user-b-secret"
    assert db_updates == []


@pytest.mark.asyncio
async def test_symlink_source_is_rejected(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    external_target = _write_file(tmp_path / "etc_passwd_copy", b"sensitive-outside-file")
    legacy_dir = upload_dir / str(user_id)
    legacy_dir.mkdir(parents=True, exist_ok=True)
    symlink_path = legacy_dir / "link.pdf"
    os.symlink(external_target, symlink_path)

    db_updates: list[tuple[uuid.UUID, str]] = []

    async def update_db(did: uuid.UUID, new_path: str) -> None:
        db_updates.append((did, new_path))

    record = DocumentMigrationRecord(
        id=doc_id,
        user_id=user_id,
        filename="link.pdf",
        file_path=str(symlink_path),
    )

    report = await migrate_legacy_storage(
        [record],
        update_document_path=update_db,
        upload_dir=upload_dir,
        apply=True,
    )

    assert report.migrated == 0
    assert report.skipped_or_failed == 1
    assert report.items[0].status == "symlink_source"
    assert external_target.read_bytes() == b"sensitive-outside-file"
    assert db_updates == []


@pytest.mark.asyncio
async def test_db_update_failure_preserves_source_and_rolls_back_destination(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    legacy_file = _write_file(upload_dir / str(user_id) / "doc.pdf", b"preserve-on-db-fail")
    expected_dest = upload_dir.resolve() / "users" / str(user_id) / "documents" / f"{doc_id}.pdf"

    async def failing_db_update(did: uuid.UUID, new_path: str) -> None:
        raise RuntimeError("simulated database commit failure")

    record = DocumentMigrationRecord(
        id=doc_id,
        user_id=user_id,
        filename="doc.pdf",
        file_path=str(legacy_file),
    )

    report = await migrate_legacy_storage(
        [record],
        update_document_path=failing_db_update,
        upload_dir=upload_dir,
        apply=True,
    )

    assert report.migrated == 0
    assert report.skipped_or_failed == 1
    assert report.items[0].status == "db_update_failed"
    assert legacy_file.is_file()
    assert legacy_file.read_bytes() == b"preserve-on-db-fail"
    assert not expected_dest.exists()


@pytest.mark.asyncio
async def test_retry_after_partial_failure_succeeds_cleanly(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    user_id = uuid.uuid4()
    doc_1 = uuid.uuid4()
    doc_2 = uuid.uuid4()
    file_1 = _write_file(upload_dir / str(user_id) / "one.pdf", b"file-one")
    file_2 = _write_file(upload_dir / str(user_id) / "two.pdf", b"file-two")

    db_state: dict[uuid.UUID, str] = {doc_1: str(file_1), doc_2: str(file_2)}
    attempts = {"doc_2": 0}

    async def flaky_db_update(did: uuid.UUID, new_path: str) -> None:
        if did == doc_2 and attempts["doc_2"] == 0:
            attempts["doc_2"] += 1
            raise RuntimeError("transient DB error on doc_2")
        db_state[did] = new_path

    records = [
        DocumentMigrationRecord(id=doc_1, user_id=user_id, filename="one.pdf", file_path=db_state[doc_1]),
        DocumentMigrationRecord(id=doc_2, user_id=user_id, filename="two.pdf", file_path=db_state[doc_2]),
    ]

    first_report = await migrate_legacy_storage(
        records,
        update_document_path=flaky_db_update,
        upload_dir=upload_dir,
        apply=True,
    )
    assert first_report.migrated == 1
    assert first_report.skipped_or_failed == 1
    assert not file_1.exists()
    assert file_2.is_file()

    # Retry using updated DB state: doc_1 is already canonical, doc_2 migrates cleanly.
    retry_records = [
        DocumentMigrationRecord(id=doc_1, user_id=user_id, filename="one.pdf", file_path=db_state[doc_1]),
        DocumentMigrationRecord(id=doc_2, user_id=user_id, filename="two.pdf", file_path=db_state[doc_2]),
    ]
    retry_report = await migrate_legacy_storage(
        retry_records,
        update_document_path=flaky_db_update,
        upload_dir=upload_dir,
        apply=True,
    )
    assert retry_report.already_canonical == 1
    assert retry_report.migrated == 1
    assert retry_report.skipped_or_failed == 0
    assert not file_2.exists()
    assert Path(db_state[doc_2]).read_bytes() == b"file-two"


@pytest.mark.asyncio
async def test_duplicate_destination_detection_blocks_both_records(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    user_id = uuid.uuid4()
    shared_doc_id = uuid.uuid4()
    file_a = _write_file(upload_dir / str(user_id) / "a.pdf", b"version-a")
    file_b = _write_file(upload_dir / str(user_id) / "b.pdf", b"version-b")

    db_updates: list[tuple[uuid.UUID, str]] = []

    async def update_db(did: uuid.UUID, new_path: str) -> None:
        db_updates.append((did, new_path))

    records = [
        DocumentMigrationRecord(id=shared_doc_id, user_id=user_id, filename="a.pdf", file_path=str(file_a)),
        DocumentMigrationRecord(id=shared_doc_id, user_id=user_id, filename="b.pdf", file_path=str(file_b)),
    ]

    report = await migrate_legacy_storage(
        records,
        update_document_path=update_db,
        upload_dir=upload_dir,
        apply=True,
    )

    assert report.migrated == 0
    assert report.skipped_or_failed == 2
    assert all(item.status == "duplicate_destination" for item in report.items)
    assert file_a.read_bytes() == b"version-a"
    assert file_b.read_bytes() == b"version-b"
    assert db_updates == []


@pytest.mark.asyncio
async def test_invalid_uuid_and_outside_owner_dir_rejected(tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    user_id = uuid.uuid4()
    outside_file = _write_file(tmp_path / "outside.pdf", b"outside")

    records = [
        DocumentMigrationRecord(
            id="not-a-uuid",
            user_id=user_id,
            filename="bad.pdf",
            file_path=str(upload_dir / str(user_id) / "bad.pdf"),
        ),
        DocumentMigrationRecord(
            id=uuid.uuid4(),
            user_id=user_id,
            filename="outside.pdf",
            file_path=str(outside_file),
        ),
    ]

    report = await migrate_legacy_storage(records, upload_dir=upload_dir, apply=False)
    assert report.skipped_or_failed == 2
    assert report.items[0].status == "invalid_uuid"
    assert report.items[1].status == "outside_owner_dir"


@pytest.mark.asyncio
async def test_database_migration_and_cli_dry_run_then_apply(tmp_path: Path, capsys):
    """End-to-end PostgreSQL test for dry-run, --apply, and idempotent re-run."""
    upload_dir = tmp_path / "uploads"
    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    legacy_file = _write_file(upload_dir / str(user_id) / "legacy.pdf", b"db-e2e-bytes")

    async with _CleanupSessionLocal() as session:
        user_email = f"mig-{user_id}@example.com"
        session.add(
            User(
                id=user_id,
                email=user_email,
                email_hash=email_index(user_email),
                hashed_password="not-a-real-hash",
                role="user",
            )
        )
        session.add(
            Document(
                id=doc_id,
                user_id=user_id,
                filename="legacy.pdf",
                file_path=str(legacy_file),
                mime_type="application/pdf",
                file_size=12,
                file_hash="a" * 64,
                status="indexed",
                visibility="private",
            )
        )
        await session.commit()

    try:
        # 1. CLI dry-run (run in worker thread because cli_main uses asyncio.run)
        exit_code = await asyncio.to_thread(
            cli_main, ["--upload-dir", str(upload_dir), "--json"]
        )
        captured = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert captured["mode"] == "dry-run"
        assert captured["planned"] >= 1
        assert legacy_file.is_file()

        # 2. Apply via run_database_migration
        apply_report = await run_database_migration(
            database_url=settings.DATABASE_URL,
            upload_dir=upload_dir,
            apply=True,
        )
        target_items = [item for item in apply_report.items if item.document_id == str(doc_id)]
        assert len(target_items) == 1
        assert target_items[0].status == "migrated"
        expected_dest = upload_dir.resolve() / "users" / str(user_id) / "documents" / f"{doc_id}.pdf"
        assert expected_dest.read_bytes() == b"db-e2e-bytes"
        assert not legacy_file.exists()

        async with _CleanupSessionLocal() as session:
            row = (await session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
            assert row.file_path == str(expected_dest)

        # 3. Re-running dry-run reports already_canonical
        rerun = await run_database_migration(
            database_url=settings.DATABASE_URL,
            upload_dir=upload_dir,
            apply=False,
        )
        rerun_item = [item for item in rerun.items if item.document_id == str(doc_id)][0]
        assert rerun_item.status == "already_canonical"
    finally:
        async with _CleanupSessionLocal() as session:
            await session.execute(delete(Document).where(Document.id == doc_id))
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()


@pytest.mark.asyncio
async def test_crash_recovery_after_db_commit_before_legacy_unlink(tmp_path: Path, monkeypatch):
    """Simulate a crash after DB commit and before legacy unlink, then verify safe recovery."""
    import backend.app.services.legacy_storage_migration as migration_mod

    upload_dir = tmp_path / "uploads"
    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    legacy_file = _write_file(upload_dir / str(user_id) / f"{doc_id}.pdf", b"exact-payload-bytes")
    unrelated = _write_file(upload_dir / str(user_id) / "unrelated.pdf", b"exact-payload-bytes")

    db_state: dict[uuid.UUID, str] = {doc_id: str(legacy_file)}

    async def update_db(did: uuid.UUID, new_path: str) -> None:
        db_state[did] = new_path

    real_unlink = migration_mod.os.unlink

    def crash_on_legacy_unlink(path):
        if Path(path).resolve() == legacy_file.resolve():
            raise OSError("simulated crash before legacy source unlink")
        return real_unlink(path)

    monkeypatch.setattr(migration_mod.os, "unlink", crash_on_legacy_unlink)

    record = DocumentMigrationRecord(
        id=doc_id,
        user_id=user_id,
        filename="report.pdf",
        file_path=db_state[doc_id],
    )
    crashed_report = await migrate_legacy_storage(
        [record],
        update_document_path=update_db,
        upload_dir=upload_dir,
        apply=True,
    )
    assert crashed_report.migrated == 1
    assert crashed_report.items[0].warning is not None
    canonical_dest = Path(db_state[doc_id])
    assert canonical_dest.read_bytes() == b"exact-payload-bytes"
    assert legacy_file.exists()

    # Restore real unlink and re-run dry-run, then --apply.
    monkeypatch.setattr(migration_mod.os, "unlink", real_unlink)
    post_crash_record = DocumentMigrationRecord(
        id=doc_id,
        user_id=user_id,
        filename="report.pdf",
        file_path=db_state[doc_id],
    )

    dry_report = await migrate_legacy_storage(
        [post_crash_record],
        upload_dir=upload_dir,
        apply=False,
    )
    assert dry_report.orphaned_legacy_source == 1
    assert dry_report.skipped_or_failed == 0
    assert dry_report.items[0].status == "orphaned_legacy_source"
    assert legacy_file.exists()

    recovery_report = await migrate_legacy_storage(
        [post_crash_record],
        update_document_path=update_db,
        upload_dir=upload_dir,
        apply=True,
    )
    assert recovery_report.recovered == 1
    assert recovery_report.skipped_or_failed == 0
    assert recovery_report.items[0].status == "recovered_legacy_cleanup"
    assert not legacy_file.exists()
    assert canonical_dest.read_bytes() == b"exact-payload-bytes"
    assert unrelated.read_bytes() == b"exact-payload-bytes"


@pytest.mark.asyncio
async def test_integrity_mismatch_aborts_before_db_update_and_preserves_source(tmp_path: Path, monkeypatch):
    """If copied destination bytes do not match source, abort before DB update and remove bad dest."""
    import backend.app.services.legacy_storage_migration as migration_mod

    upload_dir = tmp_path / "uploads"
    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    legacy_file = _write_file(upload_dir / str(user_id) / "doc.pdf", b"original-bytes")
    expected_dest = upload_dir.resolve() / "users" / str(user_id) / "documents" / f"{doc_id}.pdf"

    db_updates: list[tuple[uuid.UUID, str]] = []

    async def update_db(did: uuid.UUID, new_path: str) -> None:
        db_updates.append((did, new_path))

    def corrupted_sha256(_path: Path) -> tuple[str, int]:
        return ("0" * 64, 999)

    monkeypatch.setattr(migration_mod, "_sha256_file", corrupted_sha256)

    record = DocumentMigrationRecord(
        id=doc_id,
        user_id=user_id,
        filename="doc.pdf",
        file_path=str(legacy_file),
    )

    report = await migrate_legacy_storage(
        [record],
        update_document_path=update_db,
        upload_dir=upload_dir,
        apply=True,
    )

    assert report.migrated == 0
    assert report.skipped_or_failed == 1
    assert report.items[0].status == "io_error"
    assert db_updates == []
    assert legacy_file.read_bytes() == b"original-bytes"
    assert not expected_dest.exists()


@pytest.mark.asyncio
async def test_dry_run_performs_zero_filesystem_mutations_snapshot(tmp_path: Path):
    """Dry-run must not create directories, temp files, or modify any existing entries."""
    upload_dir = tmp_path / "uploads"
    user_id = uuid.uuid4()
    doc_planned = uuid.uuid4()
    doc_orphaned = uuid.uuid4()

    planned_src = _write_file(upload_dir / str(user_id) / f"{doc_planned}.pdf", b"planned")
    orphaned_src = _write_file(upload_dir / str(user_id) / f"{doc_orphaned}.pdf", b"orphaned")
    orphaned_dst = _write_file(
        upload_dir / "users" / str(user_id) / "documents" / f"{doc_orphaned}.pdf",
        b"orphaned",
    )

    def _snapshot(root: Path) -> set[tuple[str, str, bytes | None]]:
        entries: set[tuple[str, str, bytes | None]] = set()
        for p in sorted(root.rglob("*")):
            rel = str(p.relative_to(root))
            if p.is_dir():
                entries.add(("dir", rel, None))
            else:
                entries.add(("file", rel, p.read_bytes()))
        return entries

    before = _snapshot(upload_dir)
    records = [
        DocumentMigrationRecord(id=doc_planned, user_id=user_id, filename="a.pdf", file_path=str(planned_src)),
        DocumentMigrationRecord(id=doc_orphaned, user_id=user_id, filename="b.pdf", file_path=str(orphaned_dst)),
    ]

    report = await migrate_legacy_storage(records, upload_dir=upload_dir, apply=False)
    after = _snapshot(upload_dir)

    assert report.planned == 1
    assert report.orphaned_legacy_source == 1
    assert before == after
    assert orphaned_src.exists()


@pytest.mark.asyncio
async def test_shared_storage_documents_layout_is_rejected_as_outside_owner_dir(tmp_path: Path):
    """Paths under <UPLOAD_DIR>/storage/documents/ lack owner scoping and must be rejected."""
    upload_dir = tmp_path / "uploads"
    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    shared_path = _write_file(upload_dir / "storage" / "documents" / f"{doc_id}.pdf", b"shared")

    record = DocumentMigrationRecord(
        id=doc_id,
        user_id=user_id,
        filename="shared.pdf",
        file_path=str(shared_path),
    )

    report = await migrate_legacy_storage([record], upload_dir=upload_dir, apply=False)
    assert report.planned == 0
    assert report.skipped_or_failed == 1
    assert report.items[0].status == "outside_owner_dir"
