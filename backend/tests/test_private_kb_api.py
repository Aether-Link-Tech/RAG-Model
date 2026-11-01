"""TC-ISO: end-to-end private knowledge base isolation through the HTTP API (FR-079).

Needs PostgreSQL (DATABASE_URL, migrated to head). Chroma and Ollama are replaced
by an in-memory client and deterministic fake embeddings.

Locally the module is skipped when PostgreSQL is unreachable; in CI (CI=true)
an unreachable database fails the run instead of silently skipping.
"""
import io
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1 import documents as documents_api
from backend.app.api.v1 import users as users_api
from backend.app.core.security import create_access_token
from backend.app.db.chroma import get_private_collection, get_public_collection
from backend.app.models.document import Document
from backend.app.models.user import User
from backend.app.services import vector_store
from backend.app.services.document_ingest import index_document
from backend.tests.conftest import test_engine

SECRET_TEXT = b"alpha quarterly salary report confidential figures"


@pytest_asyncio.fixture(autouse=True)
async def require_postgres():
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        if os.getenv("CI"):
            pytest.fail(f"PostgreSQL required in CI but unreachable: {exc}")
        pytest.skip(f"PostgreSQL unreachable, skipping API isolation tests: {exc}")


@pytest.fixture(autouse=True)
def real_vector_ops(monkeypatch, chroma_client, fake_embeddings):
    """Undo conftest's no-op vector stubs so uploads/deletes really hit (in-memory) Chroma."""
    monkeypatch.setattr(documents_api, "index_document", index_document)
    monkeypatch.setattr(documents_api, "delete_document_chunks", vector_store.delete_document_chunks)
    monkeypatch.setattr(
        users_api, "delete_user_private_knowledge", vector_store.delete_user_private_knowledge
    )


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, role=user.role)}"}


async def _upload(client, user: User, content: bytes, *, visibility="private", name="secret.txt"):
    response = await client.post(
        "/api/v1/documents",
        headers=_headers(user),
        files={"file": (name, io.BytesIO(content), "text/plain")},
        data={"visibility": visibility},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "indexed", body
    return body


async def _search(client, user: User, query: str, **flags):
    response = await client.post(
        "/api/v1/retrieval/query",
        headers=_headers(user),
        json={"query": query, "n_results": 10, **flags},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_tc_iso_api_01_user_b_search_returns_zero_private_results(
    async_client, test_user: User, test_user_2: User
):
    """User A uploads private doc -> User B searches -> 0 results."""
    await _upload(async_client, test_user, SECRET_TEXT)

    b_result = await _search(async_client, test_user_2, "alpha quarterly salary report")
    assert b_result["results"] == []
    assert b_result["private_collection_hit"] is False

    a_result = await _search(async_client, test_user, "alpha quarterly salary report")
    assert len(a_result["results"]) >= 1
    assert all(r["visibility"] == "private" for r in a_result["results"])
    assert a_result["private_collection_hit"] is True


async def test_tc_iso_api_02_user_b_direct_access_is_denied(
    async_client, test_user: User, test_user_2: User, db_session: AsyncSession
):
    """User B queries A's doc directly -> 404 (no existence leak) and 0 rows."""
    doc = await _upload(async_client, test_user, SECRET_TEXT)
    doc_id = doc["id"]

    get_resp = await async_client.get(f"/api/v1/documents/{doc_id}", headers=_headers(test_user_2))
    assert get_resp.status_code == 404

    list_resp = await async_client.get("/api/v1/documents", headers=_headers(test_user_2))
    assert list_resp.status_code == 200
    assert list_resp.json() == []

    del_resp = await async_client.delete(f"/api/v1/documents/{doc_id}", headers=_headers(test_user_2))
    assert del_resp.status_code == 404

    # A's document and vectors are untouched by B's attempts.
    assert await db_session.get(Document, uuid.UUID(doc_id)) is not None
    assert get_private_collection(test_user.id).count() >= 1

    own = await async_client.get(f"/api/v1/documents/{doc_id}", headers=_headers(test_user))
    assert own.status_code == 200


async def test_tc_iso_api_03_retrieval_response_hides_owner_ids(
    async_client, test_user: User, test_user_2: User
):
    """Public hits must not reveal who uploaded them."""
    await _upload(async_client, test_user, b"alpha shared employee handbook", visibility="public", name="handbook.txt")

    b_result = await _search(async_client, test_user_2, "alpha shared employee handbook")
    assert len(b_result["results"]) >= 1
    for hit in b_result["results"]:
        assert hit["visibility"] == "public"
        assert "user_id" not in hit["metadata"]
        assert str(test_user.id) not in str(hit)
        assert test_user.id.hex not in str(hit)


async def test_tc_iso_api_04_deleting_user_removes_only_their_private_collection(
    async_client, test_user: User, test_user_2: User
):
    await _upload(async_client, test_user, SECRET_TEXT)
    await _upload(async_client, test_user_2, b"bravo private travel notes", name="notes.txt")
    await _upload(async_client, test_user, b"alpha shared employee handbook", visibility="public", name="handbook.txt")
    public_count = get_public_collection().count()

    resp = await async_client.delete("/api/v1/users/me", headers=_headers(test_user))
    assert resp.status_code == 200

    assert get_private_collection(test_user.id) is None
    assert get_private_collection(test_user_2.id).count() >= 1
    assert get_public_collection().count() == public_count

    b_result = await _search(async_client, test_user_2, "bravo private travel notes")
    assert any(r["visibility"] == "private" for r in b_result["results"])
