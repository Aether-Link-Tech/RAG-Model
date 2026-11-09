"""FR-075 / AL-271: the integrity check detects deliberately broken consistency.

Each test starts from a consistent system (documents, per-user files, vectors in the right
collections) and breaks exactly one thing, then asserts the check reports it.
"""
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from backend.app.db import chroma
from backend.app.scripts import integrity_check as cli
from backend.app.services.integrity_check import CRITICAL, DocumentRecord, check_integrity
from backend.app.services.vector_store import upsert_chunks

pytestmark = pytest.mark.isolation

EMB = [[0.1, 0.2, 0.3]]


def _doc(upload_dir, user_id, visibility="private", status="indexed", *, write_file=True, name=None):
    doc_id = uuid.uuid4()
    folder = Path(upload_dir) / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name or f"{doc_id}.txt")
    if write_file:
        path.write_text("content")
    return DocumentRecord(doc_id, user_id, visibility, status, str(path))


def _index(doc):
    upsert_chunks(user_id=doc.user_id, ids=[f"{doc.id}:0"], embeddings=EMB, documents=["chunk"],
                  metadatas=[{"document_id": str(doc.id), "filename": "f.txt", "chunk_index": 0}],
                  visibility=doc.visibility)


@pytest.fixture
def world(chroma_client, isolate_storage):
    """User A: one private + one public document; user B: one private document. All consistent."""
    a, b = uuid.uuid4(), uuid.uuid4()
    docs = [_doc(isolate_storage, a), _doc(isolate_storage, a, "public"), _doc(isolate_storage, b)]
    for d in docs:
        _index(d)
    return {"a": a, "b": b, "docs": docs, "dir": isolate_storage, "client": chroma_client}


def _kinds(report):
    return sorted(i.kind for i in report.issues)


def test_clean_system_reports_no_issues(world):
    report = check_integrity(world["docs"], chroma_client=world["client"], upload_dir=world["dir"])
    assert report.issues == []
    assert report.documents_checked == 3 and report.chunks_checked == 3
    assert report.collections_checked == 3  # public + A private + B private


def test_detects_orphan_vector(world):
    gone = world["docs"][0]
    report = check_integrity(world["docs"][1:], chroma_client=world["client"], upload_dir=world["dir"])
    assert "orphan_vector" in _kinds(report)
    assert report.by_kind("orphan_vector")[0].document_id == str(gone.id)


def test_detects_chunk_in_another_users_private_collection(world):
    a_doc = world["docs"][0]
    b_collection = chroma.get_or_create_private_collection(world["b"])
    b_collection.add(ids=["planted"], embeddings=EMB, documents=["leak"],
                     metadatas=[{"document_id": str(a_doc.id), "user_id": world["b"].hex, "visibility": "private"}])
    report = check_integrity(world["docs"], chroma_client=world["client"], upload_dir=world["dir"])
    issue = report.by_kind("owner_mismatch")[0]
    assert issue.chroma_ids == ["planted"] and issue.severity == CRITICAL
    assert report.has_critical


def test_detects_private_chunk_in_public_collection(world):
    a_private = world["docs"][0]
    chroma.get_public_collection().add(
        ids=["leaked"], embeddings=EMB, documents=["secret"],
        metadatas=[{"document_id": str(a_private.id), "user_id": world["a"].hex, "visibility": "public"}])
    report = check_integrity(world["docs"], chroma_client=world["client"], upload_dir=world["dir"])
    assert [i.chroma_ids for i in report.by_kind("private_chunk_in_public")] == [["leaked"]]
    assert report.has_critical


def test_detects_wrong_owner_metadata_and_invalid_chunks(world):
    a_collection = chroma.get_or_create_private_collection(world["a"])
    a_doc = world["docs"][0]
    a_collection.add(ids=["no-doc-id", "bad-owner"], embeddings=EMB * 2, documents=["x", "y"],
                     metadatas=[{"user_id": world["a"].hex, "visibility": "private"},
                                {"document_id": str(a_doc.id), "user_id": world["b"].hex, "visibility": "private"}])
    report = check_integrity(world["docs"], chroma_client=world["client"], upload_dir=world["dir"])
    flagged = {cid for i in report.issues for cid in i.chroma_ids}
    assert {"no-doc-id", "bad-owner"} <= flagged


def test_detects_mis_stamped_and_unknown_collections(world):
    client = world["client"]
    client.create_collection(chroma.private_collection_name(uuid.uuid4()),
                             metadata={"visibility": "private", "user_id": uuid.uuid4().hex})
    client.create_collection("scratch_collection")
    kinds = _kinds(check_integrity(world["docs"], chroma_client=client, upload_dir=world["dir"]))
    assert "collection_owner_mismatch" in kinds and "unknown_collection" in kinds


def test_detects_missing_file_and_file_outside_owner_folder(world):
    a_doc = world["docs"][0]
    Path(a_doc.file_path).unlink()
    b_folder = Path(world["dir"]) / str(world["b"])
    misplaced = DocumentRecord(uuid.uuid4(), world["a"], "private", "pending", str(b_folder / "a-in-b.txt"))
    (b_folder / "a-in-b.txt").write_text("x")
    report = check_integrity(world["docs"] + [misplaced], chroma_client=world["client"], upload_dir=world["dir"])
    assert [i.document_id for i in report.by_kind("missing_file")] == [str(a_doc.id)]
    outside = report.by_kind("file_outside_owner_folder")
    assert [i.document_id for i in outside] == [str(misplaced.id)] and outside[0].severity == CRITICAL


def test_detects_orphan_files_but_ignores_transient_ones(world):
    folder = Path(world["dir"]) / str(world["a"])
    (folder / "forgotten.pdf").write_text("x")
    (folder / "upload.txt.123.tmp").write_text("x")
    (folder / ".doc.txt.abc.deleting").write_text("x")
    report = check_integrity(world["docs"], chroma_client=world["client"], upload_dir=world["dir"])
    assert [Path(i.path).name for i in report.by_kind("orphan_file")] == ["forgotten.pdf"]


def test_detects_indexed_document_without_vectors(world):
    unindexed = _doc(world["dir"], world["a"], status="indexed")
    report = check_integrity(world["docs"] + [unindexed], chroma_client=world["client"], upload_dir=world["dir"])
    assert [i.document_id for i in report.by_kind("missing_vectors")] == [str(unindexed.id)]


def test_read_only_by_default_and_repair_fixes_vectors(world):
    a_doc = world["docs"][0]
    chroma.get_public_collection().add(
        ids=["leaked"], embeddings=EMB, documents=["secret"],
        metadatas=[{"document_id": str(a_doc.id), "user_id": world["a"].hex, "visibility": "public"}])
    Path(world["docs"][2].file_path).unlink()

    check_integrity(world["docs"], chroma_client=world["client"], upload_dir=world["dir"])
    assert chroma.get_public_collection().get(ids=["leaked"])["ids"] == ["leaked"]  # untouched

    failed = []
    report = check_integrity(world["docs"], chroma_client=world["client"], upload_dir=world["dir"],
                             repair=True, mark_failed=lambda d, m: failed.append(d))
    assert report.repaired_vectors == 1
    assert chroma.get_public_collection().get(ids=["leaked"])["ids"] == []
    assert failed == [world["docs"][2].id] and report.documents_marked_failed == 1
    assert Path(world["docs"][0].file_path).exists()  # files are never deleted

    again = check_integrity(world["docs"], chroma_client=world["client"], upload_dir=world["dir"])
    assert _kinds(again) == ["missing_file"]


async def test_loader_reads_every_users_documents(test_user, test_user_2, db_session):
    """The CLI loads documents of all users (privileged connection), unlike the RLS-bound API."""
    from backend.tests.conftest import _CleanupSessionLocal

    ids = []
    async with _CleanupSessionLocal() as session:
        for owner in (test_user.id, test_user_2.id):
            doc_id = uuid.uuid4()
            ids.append(doc_id)
            await session.execute(text(
                "INSERT INTO documents (id, user_id, filename, file_path, mime_type, file_size, file_hash, status, visibility) "
                "VALUES (:id, :u, 'x.txt', '/nowhere/x.txt', 'text/plain', 1, :h, 'pending', 'private')"),
                {"id": doc_id, "u": owner, "h": uuid.uuid4().hex * 2})
        await session.commit()
    loaded = {d.id for d in await cli.load_documents()}
    assert set(ids) <= loaded


def test_cli_exit_codes_and_json_report(world, monkeypatch, tmp_path):
    async def fake_load(database_url=None):
        return world["docs"]

    monkeypatch.setattr(cli, "load_documents", fake_load)
    _, code = cli.run([])
    assert code == 0

    Path(world["docs"][0].file_path).unlink()
    out = tmp_path / "report.json"
    report, code = cli.run(["--json", str(out)])
    assert code == 1 and out.exists() and '"missing_file"' in out.read_text()

    chroma.get_public_collection().add(
        ids=["leaked"], embeddings=EMB, documents=["s"],
        metadatas=[{"document_id": str(world["docs"][2].id), "user_id": world["b"].hex, "visibility": "public"}])
    _, code = cli.run([])
    assert code == 2
