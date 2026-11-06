"""FR-011 — DELETE /api/v1/documents/{id}: cascade cleanup with rollback."""
import io
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1 import documents as documents_api
from backend.app.core.security import create_access_token
from backend.app.models.document import Document
from backend.app.models.user import User
from backend.app.services import vector_store
from backend.tests.conftest import TestingSessionLocal


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, role=user.role)}"}


async def _upload(client, user: User, body: bytes, *, visibility="private", name="doc.txt") -> dict:
    response = await client.post(
        "/api/v1/documents",
        headers=_headers(user),
        files={"file": (name, io.BytesIO(body), "text/plain")},
        data={"visibility": visibility},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _row_exists(user: User, doc_id: str) -> bool:
    from backend.app.db.rls import configure_rls_context

    async with TestingSessionLocal() as session:
        await configure_rls_context(session, user.id, user.role)
        return await session.get(Document, uuid.UUID(doc_id)) is not None


async def _file_path(user: User, doc_id: str) -> Path:
    from backend.app.db.rls import configure_rls_context

    async with TestingSessionLocal() as session:
        await configure_rls_context(session, user.id, user.role)
        return Path((await session.get(Document, uuid.UUID(doc_id))).file_path)


async def test_delete_removes_row_file_and_chunks(async_client, test_user: User, monkeypatch):
    doc = await _upload(async_client, test_user, b"to be deleted")
    path = await _file_path(test_user, doc["id"])
    assert path.exists()

    removed_chunks = []
    monkeypatch.setattr(documents_api, "delete_document_chunks", lambda **kw: removed_chunks.append(kw))

    response = await async_client.delete(f"/api/v1/documents/{doc['id']}", headers=_headers(test_user))

    assert response.status_code == 204
    assert not await _row_exists(test_user, doc["id"])
    assert not path.exists()
    assert not list(path.parent.glob(f".{path.name}.*.deleting")), "staged tombstone left behind"
    assert removed_chunks and removed_chunks[0]["document_id"] == uuid.UUID(doc["id"])
    assert removed_chunks[0]["user_id"] == test_user.id


async def test_failed_delete_rolls_back_file_and_row(async_client, test_user: User, monkeypatch):
    """If chunk cleanup fails, nothing is lost: 503, row kept, file restored."""
    doc = await _upload(async_client, test_user, b"must survive a failed delete")
    path = await _file_path(test_user, doc["id"])

    def broken_vector_delete(**kwargs):
        raise RuntimeError("chroma down")

    reindexed = []

    async def fake_index(db, document):
        reindexed.append(document.id)
        return document

    monkeypatch.setattr(documents_api, "delete_document_chunks", broken_vector_delete)
    monkeypatch.setattr(documents_api, "index_document", fake_index)

    response = await async_client.delete(f"/api/v1/documents/{doc['id']}", headers=_headers(test_user))

    assert response.status_code == 503
    assert await _row_exists(test_user, doc["id"])
    assert path.exists() and path.read_bytes() == b"must survive a failed delete"
    assert not list(path.parent.glob(f".{path.name}.*.deleting"))
    assert reindexed == [uuid.UUID(doc["id"])], "index should be rebuilt after a failed delete"


async def test_other_users_private_document_is_404_and_untouched(async_client, test_user: User, test_user_2: User):
    doc = await _upload(async_client, test_user, b"not yours")
    response = await async_client.delete(f"/api/v1/documents/{doc['id']}", headers=_headers(test_user_2))
    assert response.status_code == 404
    assert await _row_exists(test_user, doc["id"])


async def test_other_users_public_document_is_403_for_non_admin(async_client, test_user: User, test_user_2: User):
    doc = await _upload(async_client, test_user, b"shared doc", visibility="public", name="shared.txt")
    response = await async_client.delete(f"/api/v1/documents/{doc['id']}", headers=_headers(test_user_2))
    assert response.status_code == 403
    assert await _row_exists(test_user, doc["id"])
