"""FR-010 — PUT /api/v1/documents/{id}: rename / replace + re-index."""
import hashlib
import io
import uuid
from pathlib import Path

import pytest

from backend.app.api.v1 import documents as documents_api
from backend.app.core.security import create_access_token
from backend.app.db.rls import configure_rls_context
from backend.app.models.document import Document
from backend.app.models.user import User
from backend.app.services import clamav, document_ingest
from backend.tests.conftest import TestingSessionLocal


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, role=user.role)}"}


async def _upload(client, user: User, body: bytes, *, visibility="private", name="notes.txt") -> dict:
    response = await client.post(
        "/api/v1/documents",
        headers=_headers(user),
        files={"file": (name, io.BytesIO(body), "text/plain")},
        data={"visibility": visibility},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _load(user: User, doc_id: str) -> Document | None:
    async with TestingSessionLocal() as session:
        await configure_rls_context(session, user.id, user.role)
        return await session.get(Document, uuid.UUID(doc_id))


@pytest.fixture
def reindex_calls(monkeypatch):
    calls = []

    async def record(document_id, user_id, user_role="user"):
        calls.append((document_id, user_id, user_role))

    monkeypatch.setattr(documents_api, "reindex_document_in_background", record)
    monkeypatch.setattr(documents_api, "rename_document_chunks", lambda **kw: None)
    return calls


async def test_rename_keeps_type_and_returns_owner_view(async_client, test_user: User, reindex_calls):
    doc = await _upload(async_client, test_user, b"rename me", name="old.txt")

    r = await async_client.put(f"/api/v1/documents/{doc['id']}", headers=_headers(test_user), data={"filename": "new.txt"})

    assert r.status_code == 202, r.text
    assert r.json()["filename"] == "new.txt"
    assert r.json()["is_owner"] is True
    assert (await _load(test_user, doc["id"])).filename == "new.txt"


async def test_rename_cannot_change_file_type(async_client, test_user: User, reindex_calls):
    doc = await _upload(async_client, test_user, b"typed", name="keep.txt")
    r = await async_client.put(f"/api/v1/documents/{doc['id']}", headers=_headers(test_user), data={"filename": "keep.pdf"})
    assert r.status_code == 400


async def test_replace_swaps_file_and_queues_reindex_as_owner(async_client, test_user: User, reindex_calls):
    doc = await _upload(async_client, test_user, b"version one", name="v.txt")
    old_path = Path((await _load(test_user, doc["id"])).file_path)

    r = await async_client.put(
        f"/api/v1/documents/{doc['id']}",
        headers=_headers(test_user),
        files={"file": ("v.txt", io.BytesIO(b"version two, longer"), "text/plain")},
    )

    assert r.status_code == 202, r.text
    stored = await _load(test_user, doc["id"])
    assert stored.status == "processing"
    assert stored.file_hash == hashlib.sha256(b"version two, longer").hexdigest()
    assert Path(stored.file_path).read_bytes() == b"version two, longer"
    assert not old_path.exists(), "old file must be removed after a successful replace"
    assert reindex_calls == [(uuid.UUID(doc["id"]), test_user.id, test_user.role)]


async def test_other_users_private_doc_is_404(async_client, test_user: User, test_user_2: User, reindex_calls):
    doc = await _upload(async_client, test_user, b"private")
    r = await async_client.put(f"/api/v1/documents/{doc['id']}", headers=_headers(test_user_2), data={"filename": "x.txt"})
    assert r.status_code == 404
    assert (await _load(test_user, doc["id"])).filename == "notes.txt"


async def test_other_users_public_doc_is_403(async_client, test_user: User, test_user_2: User, reindex_calls):
    doc = await _upload(async_client, test_user, b"public body", visibility="public", name="pub.txt")
    r = await async_client.put(f"/api/v1/documents/{doc['id']}", headers=_headers(test_user_2), data={"filename": "x.txt"})
    assert r.status_code == 403


@pytest.mark.parametrize(
    "name,body",
    [
        ("evil.exe.txt", b"looks like text"),  # FR-064 double extension
        ("fake.txt", b"MZ\x90\x00\x03\x00\x00\x00binary\x00\x00"),  # FR-065 content mismatch
    ],
)
async def test_replacement_uses_upload_validation(async_client, test_user: User, reindex_calls, name, body):
    doc = await _upload(async_client, test_user, b"original body")
    r = await async_client.put(
        f"/api/v1/documents/{doc['id']}",
        headers=_headers(test_user),
        files={"file": (name, io.BytesIO(body), "text/plain")},
    )
    assert r.status_code == 400, r.text
    assert Path((await _load(test_user, doc["id"])).file_path).read_bytes() == b"original body"
    assert reindex_calls == []


async def test_infected_replacement_is_blocked_and_original_kept(async_client, test_user: User, reindex_calls, monkeypatch):
    doc = await _upload(async_client, test_user, b"clean original")

    def infected(path, user_id, document_id):
        raise clamav.InfectedFileError("Eicar-Test-Signature")

    monkeypatch.setattr(documents_api, "scan_and_quarantine", infected)
    r = await async_client.put(
        f"/api/v1/documents/{doc['id']}",
        headers=_headers(test_user),
        files={"file": ("notes.txt", io.BytesIO(b"pretend malware"), "text/plain")},
    )

    assert r.status_code == 422
    stored = await _load(test_user, doc["id"])
    assert Path(stored.file_path).read_bytes() == b"clean original"
    assert stored.status != "processing"
    assert reindex_calls == []


async def test_background_reindex_runs_under_the_owners_rls_context(async_client, test_user: User, monkeypatch):
    """Regression: the background session used to see 0 rows under RLS."""
    doc = await _upload(async_client, test_user, b"reindex me")
    seen = []

    async def fake_index(db, document):
        seen.append(document.id)
        document.status = "indexed"
        await db.commit()
        return document

    monkeypatch.setattr(document_ingest, "index_document", fake_index)
    monkeypatch.setattr(document_ingest, "AsyncSessionLocal", TestingSessionLocal)

    await document_ingest.reindex_document_in_background(uuid.UUID(doc["id"]), test_user.id, test_user.role)

    assert seen == [uuid.UUID(doc["id"])], "document must be visible to the background session"
    assert (await _load(test_user, doc["id"])).status == "indexed"
