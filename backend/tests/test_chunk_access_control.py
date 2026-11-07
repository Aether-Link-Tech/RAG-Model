"""Automated tests for AL-128: PostgreSQL RLS-Backed Chunk Retrieval Authorization.

Verifies that PostgreSQL document Row-Level Security (RLS) serves as the authoritative
backend authorization mechanism for chunks retrieved from ChromaDB, preventing
cross-tenant leakage, stale chunk exposure, and metadata tampering without requiring
a separate PostgreSQL chunks table.
"""
from __future__ import annotations

import io
import uuid
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from backend.app.api.v1 import documents as documents_api
from backend.app.api.v1 import users as users_api
from backend.app.core.config import settings
from backend.app.core.security import create_access_token
from backend.app.db.chroma import (
    get_or_create_private_collection,
    get_private_collection,
    get_public_collection,
)
from backend.app.models.document import Document
from backend.app.models.user import User
from backend.app.services import vector_store
from backend.app.services.document_ingest import index_document
from backend.app.services.vector_store import (
    DatabaseAuthorizationError,
    VectorStoreError,
    authorize_retrieved_chunks,
    delete_document_chunks,
    query_authorized_chunks,
    upsert_chunks,
)
from backend.tests.conftest import fake_embed, test_engine

# Engine with migration privileges for direct fixture manipulation/cleanup
_migration_engine = create_async_engine(
    settings.migration_database_url,
    poolclass=NullPool,
    future=True,
)
_MigrationSessionLocal = async_sessionmaker(
    bind=_migration_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

USER_A_PRIVATE_TEXT = b"Quarterly secret financial audit for User A exclusive review."
USER_B_PRIVATE_TEXT = b"Confidential internal product roadmap for User B eyes only."
PUBLIC_SHARED_TEXT = b"Enterprise open employee benefit handbook for everyone."


@pytest_asyncio.fixture(autouse=True)
async def require_postgres():
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"PostgreSQL unreachable: {exc}")


@pytest.fixture(autouse=True)
def real_vector_ops(monkeypatch, chroma_client, fake_embeddings):
    """Enable real indexing and vector ops with in-memory Chroma client."""
    monkeypatch.setattr(documents_api, "index_document", index_document)
    monkeypatch.setattr(documents_api, "delete_document_chunks", delete_document_chunks)
    monkeypatch.setattr(
        users_api, "delete_user_private_knowledge", vector_store.delete_user_private_knowledge
    )


def _auth_headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, role=user.role)}"}


async def _upload_doc(
    client,
    user: User,
    content: bytes,
    *,
    visibility: str = "private",
    name: str = "test_doc.txt",
) -> dict:
    resp = await client.post(
        "/api/v1/documents",
        headers=_auth_headers(user),
        files={"file": (name, io.BytesIO(content), "text/plain")},
        data={"visibility": visibility},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "indexed"
    return body


async def _query_retrieval(client, user: User | None, query: str, **kwargs) -> tuple[int, dict]:
    headers = _auth_headers(user) if user else {}
    resp = await client.post(
        "/api/v1/retrieval/query",
        headers=headers,
        json={"query": query, "n_results": 10, **kwargs},
    )
    return resp.status_code, resp.json() if resp.status_code != 204 else {}


# ==============================================================================
# 1. User Retrieval Authorization (Private & Public)
# ==============================================================================

async def test_user_a_retrieves_own_private_chunks(
    async_client, test_user: User
):
    """User A retrieves their own private chunks -> Allowed."""
    await _upload_doc(async_client, test_user, USER_A_PRIVATE_TEXT, name="a_private.txt")

    status, data = await _query_retrieval(async_client, test_user, "Quarterly secret financial audit")
    assert status == 200
    assert len(data["results"]) >= 1
    assert all(r["visibility"] == "private" for r in data["results"])
    assert data["private_collection_hit"] is True
    assert any("Quarterly secret financial" in r["document"] for r in data["results"])


async def test_user_b_searches_user_a_private_chunks_denied(
    async_client, test_user: User, test_user_2: User
):
    """User B searches User A's private chunks -> Denied (0 chunks returned)."""
    await _upload_doc(async_client, test_user, USER_A_PRIVATE_TEXT, name="a_private.txt")

    status, data = await _query_retrieval(async_client, test_user_2, "Quarterly secret financial audit")
    assert status == 200
    assert data["results"] == []
    assert data["private_collection_hit"] is False


async def test_user_a_retrieves_public_chunks(
    async_client, test_user: User
):
    """User A retrieves public chunks -> Allowed."""
    await _upload_doc(
        async_client,
        test_user,
        PUBLIC_SHARED_TEXT,
        visibility="public",
        name="public_handbook.txt",
    )

    status, data = await _query_retrieval(async_client, test_user, "Enterprise open employee benefit")
    assert status == 200
    assert len(data["results"]) >= 1
    assert any("Enterprise open employee" in r["document"] for r in data["results"])
    assert any(r["visibility"] == "public" for r in data["results"])


async def test_user_b_retrieves_public_chunks(
    async_client, test_user: User, test_user_2: User
):
    """User B retrieves public chunks uploaded by User A -> Allowed."""
    await _upload_doc(
        async_client,
        test_user,
        PUBLIC_SHARED_TEXT,
        visibility="public",
        name="public_handbook.txt",
    )

    status, data = await _query_retrieval(async_client, test_user_2, "Enterprise open employee benefit")
    assert status == 200
    assert len(data["results"]) >= 1
    assert any("Enterprise open employee" in r["document"] for r in data["results"])
    assert all(r["visibility"] == "public" for r in data["results"])
    # Public hits must not reveal the original uploader's user_id
    for r in data["results"]:
        assert "user_id" not in r["metadata"]


# ==============================================================================
# 2. Authentication Checks (Missing & Invalid JWT)
# ==============================================================================

async def test_missing_jwt_returns_401(async_client):
    """Missing JWT -> HTTP 401."""
    resp = await async_client.post(
        "/api/v1/retrieval/query",
        json={"query": "test query"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"


async def test_invalid_jwt_returns_401(async_client):
    """Invalid JWT -> HTTP 401."""
    resp = await async_client.post(
        "/api/v1/retrieval/query",
        headers={"Authorization": "Bearer invalid.token.value"},
        json={"query": "test query"},
    )
    assert resp.status_code == 401


# ==============================================================================
# 3. Metadata Integrity & Anti-Tampering (AL-128 Phase 2 B & C)
# ==============================================================================

async def test_missing_document_id_metadata_rejected(
    async_client, test_user: User
):
    """Chunk in ChromaDB missing document_id metadata -> Chunk rejected."""
    collection = get_or_create_private_collection(test_user.id)
    collection.add(
        ids=["chunk_no_doc_id"],
        embeddings=[fake_embed("Malicious orphaned chunk with no document id")],
        documents=["Malicious orphaned chunk with no document id"],
        metadatas=[{"user_id": test_user.id.hex, "visibility": "private"}],
    )

    status, data = await _query_retrieval(
        async_client, test_user, "Malicious orphaned chunk with no document id"
    )
    assert status == 200
    assert data["results"] == []


async def test_invalid_document_uuid_metadata_rejected(
    async_client, test_user: User
):
    """Chunk with invalid document UUID -> Chunk rejected."""
    collection = get_or_create_private_collection(test_user.id)
    collection.add(
        ids=["chunk_bad_uuid"],
        embeddings=[fake_embed("Malformed UUID chunk")],
        documents=["Malformed UUID chunk"],
        metadatas=[{
            "document_id": "not-a-valid-uuid",
            "user_id": test_user.id.hex,
            "visibility": "private",
        }],
    )

    status, data = await _query_retrieval(
        async_client, test_user, "Malformed UUID chunk"
    )
    assert status == 200
    assert data["results"] == []


async def test_chunk_metadata_ownership_mismatch_rejected(
    async_client, test_user: User, test_user_2: User
):
    """Chunk metadata user_id mismatch with authenticated owner -> Chunk rejected."""
    collection = get_or_create_private_collection(test_user.id)
    fake_doc_id = str(uuid.uuid4())
    collection.add(
        ids=["chunk_owner_mismatch"],
        embeddings=[fake_embed("Planted chunk belonging to another user")],
        documents=["Planted chunk belonging to another user"],
        metadatas=[{
            "document_id": fake_doc_id,
            "user_id": test_user_2.id.hex,  # User B's hex in User A's collection
            "visibility": "private",
        }],
    )

    status, data = await _query_retrieval(
        async_client, test_user, "Planted chunk belonging to another user"
    )
    assert status == 200
    assert data["results"] == []


async def test_private_chunk_incorrectly_placed_in_public_collection_rejected(
    async_client, test_user: User, test_user_2: User
):
    """Private document's chunk planted into the public collection -> Chunk rejected for all users."""
    # 1. User A creates a real private document in PostgreSQL
    doc_a = await _upload_doc(
        async_client, test_user, b"Classified secret of User A", name="a_classified.txt"
    )
    doc_id = doc_a["id"]

    # 2. Plant chunk pointing to doc_id into the public Chroma collection
    public_col = get_public_collection()
    public_col.add(
        ids=[f"planted_public:{doc_id}"],
        embeddings=[fake_embed("Classified secret of User A leaked to public collection")],
        documents=["Classified secret of User A leaked to public collection"],
        metadatas=[{
            "document_id": doc_id,
            "user_id": test_user.id.hex,
            "visibility": "public",  # Claiming public, but DB has private!
        }],
    )

    # User B queries public collection -> PostgreSQL RLS blocks private doc lookup for B -> Rejected
    status_b, data_b = await _query_retrieval(
        async_client, test_user_2, "Classified secret of User A"
    )
    assert status_b == 200
    assert not any(r["chroma_id"] == f"planted_public:{doc_id}" for r in data_b["results"])

    # User A queries public collection -> PostgreSQL returns doc, but doc.visibility='private' != chunk.visibility='public' -> Rejected
    status_a, data_a = await _query_retrieval(
        async_client, test_user, "Classified secret of User A", include_private=False
    )
    assert status_a == 200
    assert not any(r["chroma_id"] == f"planted_public:{doc_id}" for r in data_a["results"])


async def test_document_removed_from_postgres_but_chunk_remains_in_chromadb(
    async_client, test_user: User
):
    """Document hard-deleted from PostgreSQL but chunk lingers in ChromaDB -> Chunk rejected."""
    doc = await _upload_doc(
        async_client, test_user, b"Temporary confidential document to be purged", name="temp_purge.txt"
    )
    doc_id = uuid.UUID(doc["id"])

    # Verify chunk was indexed in ChromaDB
    user_col = get_private_collection(test_user.id)
    assert user_col is not None
    assert user_col.count() >= 1

    # Remove document directly from PostgreSQL (leaving ChromaDB chunk intact as a stale vector)
    async with _MigrationSessionLocal() as mig_db:
        await mig_db.execute(delete(Document).where(Document.id == doc_id))
        await mig_db.commit()

    # User A searches -> PostgreSQL authorization finds no document row -> Chunk rejected!
    status, data = await _query_retrieval(
        async_client, test_user, "Temporary confidential document to be purged"
    )
    assert status == 200
    assert data["results"] == []


# ==============================================================================
# 4. Fail-Closed Error Handling (AL-128 Phase 2 C & D)
# ==============================================================================

async def test_database_authorization_failure_fails_closed(
    async_client, test_user: User, monkeypatch
):
    """Database failure during authorization check -> Fail closed (HTTP 503, never return unverified data)."""
    await _upload_doc(async_client, test_user, USER_A_PRIVATE_TEXT, name="a_auth_fail.txt")

    async def _failing_authorize(*args, **kwargs):
        raise DatabaseAuthorizationError("Simulated database connection failure")

    monkeypatch.setattr(vector_store, "authorize_retrieved_chunks", _failing_authorize)

    status, data = await _query_retrieval(async_client, test_user, "Quarterly secret financial")
    assert status == 503
    assert data["detail"] == "Vector store unavailable"


async def test_chromadb_connection_failure_controlled_error(
    async_client, test_user: User, monkeypatch
):
    """ChromaDB connection failure -> Controlled HTTP 503 error, not unauthorized data."""
    await _upload_doc(async_client, test_user, USER_A_PRIVATE_TEXT, name="a_chroma_fail.txt")

    def _failing_query_for_user(*args, **kwargs):
        raise VectorStoreError("Simulated ChromaDB connection timeout")

    monkeypatch.setattr(vector_store, "query_for_user", _failing_query_for_user)

    status, data = await _query_retrieval(async_client, test_user, "Quarterly secret financial")
    assert status == 503
    assert data["detail"] == "Vector store unavailable"


# ==============================================================================
# 5. Multi-Tenant Request Isolation (No RLS Leakage Across Requests)
# ==============================================================================

async def test_consecutive_requests_from_different_users_no_identity_leakage(
    async_client, test_user: User, test_user_2: User
):
    """Consecutive requests from different users in the same process do not leak identity or chunks."""
    await _upload_doc(async_client, test_user, USER_A_PRIVATE_TEXT, name="a_doc.txt")
    await _upload_doc(async_client, test_user_2, USER_B_PRIVATE_TEXT, name="b_doc.txt")

    # Request 1: User A
    status_a1, data_a1 = await _query_retrieval(async_client, test_user, "financial roadmap audit")
    assert status_a1 == 200
    assert any("Quarterly secret financial" in r["document"] for r in data_a1["results"])
    assert not any("Confidential internal product" in r["document"] for r in data_a1["results"])

    # Request 2: User B immediately following User A
    status_b, data_b = await _query_retrieval(async_client, test_user_2, "financial roadmap audit")
    assert status_b == 200
    assert any("Confidential internal product" in r["document"] for r in data_b["results"])
    assert not any("Quarterly secret financial" in r["document"] for r in data_b["results"])

    # Request 3: User A again
    status_a2, data_a2 = await _query_retrieval(async_client, test_user, "financial roadmap audit")
    assert status_a2 == 200
    assert any("Quarterly secret financial" in r["document"] for r in data_a2["results"])
    assert not any("Confidential internal product" in r["document"] for r in data_a2["results"])


# ==============================================================================
# 6. Direct Service Unit Tests (authorize_retrieved_chunks)
# ==============================================================================

async def test_authorize_retrieved_chunks_filters_non_indexed_status(
    test_user: User, db_session: AsyncSession
):
    """Documents with status != 'indexed' (e.g. 'failed' or 'pending') are rejected."""
    # Create document in 'failed' status
    doc_id = uuid.uuid4()
    doc = Document(
        id=doc_id,
        user_id=test_user.id,
        filename="failed_doc.txt",
        file_path="/tmp/failed_doc.txt",
        mime_type="text/plain",
        file_size=100,
        file_hash="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        status="failed",
        visibility="private",
    )
    db_session.add(doc)
    await db_session.commit()

    chunk = vector_store.RetrievedChunk(
        chroma_id="test_chunk:0",
        document="Failed document chunk",
        distance=0.1,
        metadata={
            "document_id": str(doc_id),
            "user_id": test_user.id.hex,
            "visibility": "private",
        },
        visibility="private",
    )

    authorized = await authorize_retrieved_chunks(
        db=db_session,
        user_id=test_user.id,
        chunks=[chunk],
    )
    assert authorized == []

