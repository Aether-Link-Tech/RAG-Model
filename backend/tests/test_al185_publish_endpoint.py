"""AL-185: POST /api/v1/documents/{id}/publish (owner only).

Needs PostgreSQL (DATABASE_URL, migrated to head, including the AL-184
`publish_document` function). Chroma and Ollama are replaced by an in-memory
client and deterministic fake embeddings.
"""
import io
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1 import documents as documents_api
from backend.app.api.v1 import users as users_api
from backend.app.core.config import settings
from backend.app.db.chroma import get_private_collection, get_public_collection
from backend.app.db.rls import configure_rls_context
from backend.app.models.document import Document
from backend.app.models.user import User
from backend.app.services import document_publish, vector_store
from backend.app.services.document_ingest import index_document
from backend.tests.conftest import TestingSessionLocal, bearer, test_engine


@pytest_asyncio.fixture(autouse=True)
async def require_postgres():
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        if os.getenv("CI"):
            pytest.fail(f"PostgreSQL required in CI but unreachable: {exc}")
        pytest.skip(f"PostgreSQL unreachable, skipping publish endpoint tests: {exc}")


@pytest.fixture(autouse=True)
def real_vector_ops(monkeypatch, chroma_client, fake_embeddings):
    """Undo conftest's no-op vector stubs so uploads/deletes really hit (in-memory) Chroma."""
    monkeypatch.setattr(documents_api, "index_document", index_document)
    monkeypatch.setattr(documents_api, "delete_document_chunks", vector_store.delete_document_chunks)
    monkeypatch.setattr(
        users_api, "delete_user_private_knowledge", vector_store.delete_user_private_knowledge
    )


def _content(label: str = "report") -> bytes:
    return f"alpha {label} confidential figures {uuid.uuid4()}".encode()


async def _upload(client, user: User, content: bytes, *, visibility="private", name="doc.txt"):
    response = await client.post(
        "/api/v1/documents",
        headers=bearer(user),
        data={"visibility": visibility},
        files={"file": (name, io.BytesIO(content), "text/plain")},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _publish(client, user: User | None, document_id, **kwargs):
    headers = bearer(user) if user is not None else {}
    return await client.post(
        f"/api/v1/documents/{document_id}/publish", headers=headers, **kwargs
    )


async def _row(db: AsyncSession, viewer: User, document_id):
    """Fetch (id, user_id, visibility, status, file_path) as `viewer`, or None.

    Uses a fresh session so the fixtures' ORM objects are never expired.
    """
    async with TestingSessionLocal() as session:
        await configure_rls_context(session, viewer.id, viewer.role)
        result = await session.execute(
            select(
                Document.id,
                Document.user_id,
                Document.visibility,
                Document.status,
                Document.file_path,
            ).where(Document.id == uuid.UUID(str(document_id)))
        )
        return result.one_or_none()


def _owner_files(owner: User) -> set[str]:
    directory = Path(settings.UPLOAD_DIR) / "users" / str(owner.id) / "documents"
    return {p.name for p in directory.glob("*")} if directory.exists() else set()


def _chunks(collection, document_id) -> dict:
    if collection is None:
        return {"ids": [], "metadatas": []}
    return collection.get(
        where={"document_id": {"$eq": str(document_id)}}, include=["metadatas"]
    )


@pytest.mark.asyncio
async def test_owner_publishes_own_indexed_private_document(
    async_client, test_user: User, db_session: AsyncSession
):
    """1. 201, public, new id, same user_id, file at the new path, old private row gone."""
    doc = await _upload(async_client, test_user, _content())
    old_id, old_path = doc["id"], (await _row(db_session, test_user, doc["id"])).file_path
    assert doc["visibility"] == "private" and doc["status"] == "indexed"

    response = await _publish(async_client, test_user, old_id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["visibility"] == "public"
    assert body["id"] != old_id
    assert body["user_id"] == str(test_user.id)
    assert body["is_owner"] is True
    assert body["file_hash"] == doc["file_hash"]
    assert body["warning"] is None

    new_row = await _row(db_session, test_user, body["id"])
    assert new_row is not None and new_row.visibility == "public"
    assert new_row.user_id == test_user.id
    assert os.path.exists(new_row.file_path)
    assert new_row.file_path != old_path
    assert Path(new_row.file_path).name.startswith(body["id"])
    assert await _row(db_session, test_user, old_id) is None
    # The old private file is cleaned up once the publish succeeded.
    assert not os.path.exists(old_path)


@pytest.mark.asyncio
async def test_owner_publishes_with_empty_body(
    async_client, test_user: User, db_session: AsyncSession
):
    """2. An empty JSON body and no body at all both work."""
    doc_a = await _upload(async_client, test_user, _content("a"))
    doc_b = await _upload(async_client, test_user, _content("b"))

    with_json = await _publish(async_client, test_user, doc_a["id"], json={})
    without_body = await _publish(async_client, test_user, doc_b["id"])

    assert with_json.status_code == 201, with_json.text
    assert without_body.status_code == 201, without_body.text


@pytest.mark.asyncio
async def test_admin_cannot_publish_another_users_private_document(
    async_client, test_user: User, admin_user: User, db_session: AsyncSession
):
    """3. Admin -> 404; private row unchanged; no file copied."""
    doc = await _upload(async_client, test_user, _content())
    before_files = _owner_files(test_user)
    old_row = await _row(db_session, test_user, doc["id"])

    response = await _publish(async_client, admin_user, doc["id"])

    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found"
    assert str(test_user.id) not in response.text
    after = await _row(db_session, test_user, doc["id"])
    assert after is not None and after.visibility == "private"
    assert after.file_path == old_row.file_path and os.path.exists(after.file_path)
    assert _owner_files(test_user) == before_files
    assert _chunks(get_private_collection(test_user.id), doc["id"])["ids"]


@pytest.mark.asyncio
async def test_non_owner_cannot_publish_another_users_private_document(
    async_client, test_user: User, test_user_2: User, db_session: AsyncSession
):
    """4. Another normal user -> 404; private row unchanged."""
    doc = await _upload(async_client, test_user, _content())
    before_files = _owner_files(test_user)

    response = await _publish(async_client, test_user_2, doc["id"])

    assert response.status_code == 404
    assert str(test_user.id) not in response.text
    after = await _row(db_session, test_user, doc["id"])
    assert after is not None and after.visibility == "private"
    assert _owner_files(test_user) == before_files
    assert _owner_files(test_user_2) == set()


@pytest.mark.asyncio
async def test_unauthenticated_publish_is_401(async_client):
    """5. No token -> 401."""
    response = await _publish(async_client, None, uuid.uuid4())
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_unknown_document_id_is_404(async_client, test_user: User):
    """6. Unknown id -> 404."""
    response = await _publish(async_client, test_user, uuid.uuid4())
    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found"


@pytest.mark.asyncio
async def test_non_indexed_document_is_409_and_no_file_copied(
    async_client, test_user: User, db_session: AsyncSession
):
    """7. Not indexed -> 409; no file copied; row unchanged."""
    doc = await _upload(async_client, test_user, _content())
    async with TestingSessionLocal() as session:
        await configure_rls_context(session, test_user.id, "user")
        await session.execute(
            text("UPDATE documents SET status = 'pending' WHERE id = :id"),
            {"id": uuid.UUID(doc["id"])},
        )
        await session.commit()
    before_files = _owner_files(test_user)

    response = await _publish(async_client, test_user, doc["id"])

    assert response.status_code == 409
    assert response.json()["detail"] == "Document must be indexed before it can be published"
    assert _owner_files(test_user) == before_files
    after = await _row(db_session, test_user, doc["id"])
    assert after is not None and after.visibility == "private" and after.status == "pending"


@pytest.mark.asyncio
async def test_already_public_document_is_409_for_owner_404_for_others(
    async_client, test_user: User, test_user_2: User, db_session: AsyncSession
):
    """8. Already public: the owner gets 409 (Jira AL-114/AL-185); anyone else gets 404.

    The function alone would raise P0002 here (it only sees private rows); the
    owner-scoped preflight turns that into the 409 the ticket asks for.
    """
    doc = await _upload(async_client, test_user, _content(), visibility="public")
    before_files = _owner_files(test_user)

    owner_response = await _publish(async_client, test_user, doc["id"])
    other_response = await _publish(async_client, test_user_2, doc["id"])

    assert owner_response.status_code == 409
    assert owner_response.json()["detail"] == "This document is already published."
    assert other_response.status_code == 404
    assert _owner_files(test_user) == before_files
    row = await _row(db_session, test_user, doc["id"])
    assert row is not None and row.visibility == "public"


@pytest.mark.asyncio
async def test_duplicate_public_hash_is_409_and_nothing_changes(
    async_client, test_user: User, test_user_2: User, db_session: AsyncSession
):
    """9. A public copy with the same hash exists -> 409; private row, file, chunks unchanged."""
    shared = _content("shared")
    await _upload(async_client, test_user_2, shared, visibility="public")
    doc = await _upload(async_client, test_user, shared)
    old_row = await _row(db_session, test_user, doc["id"])
    before_files = _owner_files(test_user)
    chunks_before = _chunks(get_private_collection(test_user.id), doc["id"])["ids"]
    assert chunks_before

    response = await _publish(async_client, test_user, doc["id"])

    assert response.status_code == 409
    assert response.json()["detail"] == "This document is already published."
    after = await _row(db_session, test_user, doc["id"])
    assert after is not None and after.visibility == "private"
    assert after.file_path == old_row.file_path and os.path.exists(after.file_path)
    assert _owner_files(test_user) == before_files  # the copied file was removed again
    assert _chunks(get_private_collection(test_user.id), doc["id"])["ids"] == chunks_before


@pytest.mark.asyncio
async def test_indexing_failure_after_publish_keeps_failed_public_document(
    async_client, test_user: User, db_session: AsyncSession, monkeypatch
):
    """10. Public indexing fails after the function committed -> 201, status 'failed'."""

    def _boom(**kwargs):
        raise RuntimeError("chroma is down")

    monkeypatch.setattr(document_publish, "copy_private_chunks_to_public", _boom)
    doc = await _upload(async_client, test_user, _content())

    response = await _publish(async_client, test_user, doc["id"])

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] != doc["id"]
    assert body["visibility"] == "public"
    assert body["status"] == "failed"
    assert body["error_message"]
    new_row = await _row(db_session, test_user, body["id"])
    assert new_row is not None and new_row.status == "failed"
    assert os.path.exists(new_row.file_path)
    assert await _row(db_session, test_user, doc["id"]) is None  # the function committed


@pytest.mark.asyncio
async def test_chunks_move_from_private_to_public_collection(
    async_client, test_user: User
):
    """11. Public chunks under the new id (public, owner's user_id); none left under the old id."""
    doc = await _upload(async_client, test_user, _content())
    old_chunks = _chunks(get_private_collection(test_user.id), doc["id"])
    assert old_chunks["ids"]

    response = await _publish(async_client, test_user, doc["id"])
    assert response.status_code == 201, response.text
    new_id = response.json()["id"]

    public = _chunks(get_public_collection(), new_id)
    assert len(public["ids"]) == len(old_chunks["ids"])
    assert all(chunk_id.startswith(f"{new_id}:") for chunk_id in public["ids"])
    for metadata in public["metadatas"]:
        assert metadata["visibility"] == "public"
        assert metadata["user_id"] == test_user.id.hex
        assert metadata["document_id"] == new_id
    assert _chunks(get_private_collection(test_user.id), doc["id"])["ids"] == []
    assert _chunks(get_public_collection(), doc["id"])["ids"] == []


@pytest.mark.asyncio
async def test_success_response_exposes_user_id_only_to_the_owner(
    async_client, test_user: User, test_user_2: User
):
    """12. Success carries user_id == owner; every non-owner attempt reveals no user_id."""
    doc = await _upload(async_client, test_user, _content())

    denied = await _publish(async_client, test_user_2, doc["id"])
    assert denied.status_code == 404
    assert str(test_user.id) not in denied.text
    assert "user_id" not in denied.text

    ok = await _publish(async_client, test_user, doc["id"])
    assert ok.status_code == 201, ok.text
    assert ok.json()["user_id"] == str(test_user.id)
