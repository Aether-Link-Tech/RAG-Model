"""ChromaDB indexing and owner-scoped vector search.

Private chunks are stored only in the owner's collection and queried with an
explicit user_id metadata filter. Callers never pass a target user other than
the authenticated owner. All user-facing retrievals use PostgreSQL RLS-backed
chunk authorization (AL-128), validating retrieved vector IDs against PostgreSQL
document Row-Level Security policies.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.chroma import (
    delete_private_collection,
    get_or_create_private_collection,
    get_private_collection,
    get_public_collection,
)
from backend.app.models.document import Document

logger = logging.getLogger(__name__)

Visibility = Literal["private", "public"]


class VectorStoreError(RuntimeError):
    """Raised when ChromaDB operations fail unexpectedly."""


class DatabaseAuthorizationError(RuntimeError):
    """Raised when PostgreSQL authorization query fails."""


@dataclass(frozen=True)
class RetrievedChunk:
    chroma_id: str
    document: str
    distance: float
    metadata: dict[str, Any]
    visibility: Visibility


def _owner_hex(user_id: uuid.UUID | str) -> str:
    return uuid.UUID(str(user_id)).hex


def _owner_filter(user_id: uuid.UUID | str) -> dict[str, Any]:
    return {"user_id": {"$eq": _owner_hex(user_id)}}


def private_owner_filter(user_id: uuid.UUID | str) -> dict[str, Any]:
    """Mandatory filter for every private-collection read: user_id AND visibility='private'."""
    return {
        "$and": [
            _owner_filter(user_id),
            {"visibility": {"$eq": "private"}},
        ]
    }


def _chunk_metadata(
    user_id: uuid.UUID | str,
    visibility: Visibility,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = dict(extra) if extra else {}
    metadata["user_id"] = _owner_hex(user_id)
    metadata["visibility"] = visibility
    return metadata


def _collection_for_write(user_id: uuid.UUID | str, visibility: Visibility) -> Any:
    if visibility == "private":
        return get_or_create_private_collection(user_id)
    return get_public_collection()


def upsert_chunks(
    *,
    user_id: uuid.UUID | str,
    ids: Sequence[str],
    embeddings: Sequence[Sequence[float]],
    documents: Sequence[str],
    metadatas: Optional[Sequence[dict[str, Any]]] = None,
    visibility: Visibility = "private",
) -> None:
    """Index chunks into the public collection or the owner's private collection."""
    if not (len(ids) == len(embeddings) == len(documents)):
        raise ValueError("ids, embeddings, and documents must be the same length")
    if metadatas is not None and len(metadatas) != len(ids):
        raise ValueError("metadatas must be the same length as ids")

    collection = _collection_for_write(user_id, visibility)
    merged_metadata = [
        _chunk_metadata(user_id, visibility, metadatas[i] if metadatas else None)
        for i in range(len(ids))
    ]
    collection.upsert(
        ids=list(ids),
        embeddings=list(embeddings),
        documents=list(documents),
        metadatas=merged_metadata,
    )


def query_for_user(
    *,
    user_id: uuid.UUID | str,
    query_embedding: Sequence[float],
    n_results: int = 10,
    include_public: bool = True,
    include_private: bool = True,
) -> list[RetrievedChunk]:
    """Return the top chunks the owner is allowed to see.

    Public hits come from the shared collection. Private hits come only from
    this user's own collection (never another user's) and are additionally
    filtered by user_id AND visibility='private' so another tenant's private
    chunks cannot leak even if collection routing fails.
    """
    if n_results < 1:
        raise ValueError("n_results must be at least 1")

    owner = _owner_hex(user_id)
    hits: list[RetrievedChunk] = []

    if include_public:
        hits.extend(
            _query_collection(
                collection=get_public_collection(),
                query_embedding=query_embedding,
                n_results=n_results,
                visibility="public",
                where={"visibility": {"$eq": "public"}},
            )
        )

    if include_private:
        private = get_private_collection(user_id)
        if private is not None:
            hits.extend(
                _query_collection(
                    collection=private,
                    query_embedding=query_embedding,
                    n_results=n_results,
                    visibility="private",
                    where=private_owner_filter(user_id),
                )
            )

    hits = [hit for hit in hits if _is_visible_to_owner(hit, owner)]
    hits.sort(key=lambda hit: hit.distance)
    return hits[:n_results]


def is_valid_chunk_metadata(
    metadata: Any,
    expected_visibility: Visibility,
    expected_owner_hex: Optional[str] = None,
) -> bool:
    """Verify that chunk metadata has valid document_id, visibility, and user_id.

    Missing, malformed, or inconsistent metadata must never grant access.
    """
    if not isinstance(metadata, dict):
        return False

    # 1. document_id must be present and a valid UUID
    doc_id = metadata.get("document_id")
    if not doc_id:
        return False
    try:
        uuid.UUID(str(doc_id))
    except (ValueError, TypeError, AttributeError):
        return False

    # 2. visibility in metadata must match expected collection visibility
    vis = metadata.get("visibility")
    if vis != expected_visibility or vis not in ("private", "public"):
        return False

    # 3. If private, user_id must be present and match expected owner hex
    if expected_visibility == "private":
        owner = metadata.get("user_id")
        if not owner or (expected_owner_hex and owner != expected_owner_hex):
            return False

    return True


def _is_visible_to_owner(hit: RetrievedChunk, owner: str) -> bool:
    return is_valid_chunk_metadata(hit.metadata, hit.visibility, owner)


def _query_collection(
    *,
    collection: Any,
    query_embedding: Sequence[float],
    n_results: int,
    visibility: Visibility,
    where: dict[str, Any],
) -> list[RetrievedChunk]:
    try:
        count = collection.count()
        if not count:
            return []
        raw = collection.query(
            query_embeddings=[list(query_embedding)],
            n_results=min(n_results, count),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:
        logger.exception("ChromaDB query failed on %s collection", visibility)
        raise VectorStoreError(f"ChromaDB query failed: {exc}") from exc
    ids = (raw.get("ids") or [[]])[0]
    documents = (raw.get("documents") or [[]])[0]
    metadatas = (raw.get("metadatas") or [[]])[0]
    distances = (raw.get("distances") or [[]])[0]

    chunks: list[RetrievedChunk] = []
    for chroma_id, document, metadata, distance in zip(
        ids, documents, metadatas, distances
    ):
        chunks.append(
            RetrievedChunk(
                chroma_id=chroma_id,
                document=document or "",
                distance=float(distance),
                metadata=dict(metadata or {}),
                visibility=visibility,
            )
        )
    return chunks


async def authorize_retrieved_chunks(
    *,
    db: AsyncSession,
    user_id: uuid.UUID | str,
    chunks: Sequence[RetrievedChunk],
) -> list[RetrievedChunk]:
    """Authorize retrieved ChromaDB vector chunks against PostgreSQL document RLS (AL-128).

    Provides PostgreSQL RLS-backed chunk authorization: ChromaDB retrieves candidate
    vector hits, and their parent document IDs are validated in batch against PostgreSQL
    under transaction-local Row-Level Security (`app.user_id`).
    Ensures every returned chunk belongs to an active, accessible document in PostgreSQL
    visible to the authenticated user under RLS policies, and whose metadata is strictly
    consistent with database records.
    """
    if not chunks:
        return []

    owner_uuid = uuid.UUID(str(user_id)) if isinstance(user_id, str) else user_id
    owner_hex = owner_uuid.hex

    valid_candidates: list[tuple[RetrievedChunk, uuid.UUID]] = []
    doc_ids_to_query: set[uuid.UUID] = set()

    for chunk in chunks:
        if not is_valid_chunk_metadata(chunk.metadata, chunk.visibility, owner_hex):
            continue
        try:
            doc_uuid = uuid.UUID(str(chunk.metadata["document_id"]))
        except (ValueError, TypeError, AttributeError):
            continue
        valid_candidates.append((chunk, doc_uuid))
        doc_ids_to_query.add(doc_uuid)

    if not valid_candidates or not doc_ids_to_query:
        return []

    try:
        stmt = select(
            Document.id,
            Document.user_id,
            Document.visibility,
            Document.status,
        ).where(Document.id.in_(list(doc_ids_to_query)))
        result = await db.execute(stmt)
        authorized_docs = {row.id: row for row in result.all()}
    except Exception as exc:
        logger.exception("Database authorization check failed under RLS")
        raise DatabaseAuthorizationError(
            "Failed to verify document access in PostgreSQL"
        ) from exc

    authorized_chunks: list[RetrievedChunk] = []
    for chunk, doc_uuid in valid_candidates:
        doc = authorized_docs.get(doc_uuid)
        if doc is None:
            # Document either does not exist, was deleted, or is blocked by PostgreSQL RLS
            continue

        if doc.status != "indexed":
            continue

        if doc.visibility != chunk.visibility:
            continue

        if chunk.visibility == "private":
            if doc.user_id != owner_uuid:
                continue
            if chunk.metadata.get("user_id") != owner_hex:
                continue
        elif chunk.visibility == "public":
            if chunk.metadata.get("visibility") != "public":
                continue
        else:
            continue

        authorized_chunks.append(chunk)

    return authorized_chunks


async def query_authorized_chunks(
    *,
    db: AsyncSession,
    user_id: uuid.UUID | str,
    query_embedding: Sequence[float],
    n_results: int = 10,
    include_public: bool = True,
    include_private: bool = True,
) -> list[RetrievedChunk]:
    """Retrieve and authorize top-k chunks for an authenticated user (AL-128).

    Queries ChromaDB collections and filters candidates through PostgreSQL RLS authorization.
    """
    if n_results < 1:
        raise ValueError("n_results must be at least 1")

    fetch_count = max(n_results * 2, 10)
    candidates = query_for_user(
        user_id=user_id,
        query_embedding=query_embedding,
        n_results=fetch_count,
        include_public=include_public,
        include_private=include_private,
    )

    authorized = await authorize_retrieved_chunks(
        db=db,
        user_id=user_id,
        chunks=candidates,
    )

    authorized.sort(key=lambda hit: hit.distance)
    return authorized[:n_results]


def delete_document_chunks(
    *,
    user_id: uuid.UUID | str,
    document_id: uuid.UUID | str,
    visibility: Visibility = "private",
) -> None:
    """Remove one document's vectors from the collection they were indexed into."""
    where = {
        "$and": [
            _owner_filter(user_id),
            {"visibility": {"$eq": visibility}},
            {"document_id": {"$eq": str(document_id)}},
        ]
    }
    if visibility == "private":
        collection = get_private_collection(user_id)
        if collection is None:
            return
    else:
        collection = get_public_collection()
    collection.delete(where=where)


def delete_user_private_knowledge(user_id: uuid.UUID | str) -> bool:
    """Delete a user's private collection only. Public chunks stay indexed."""
    return delete_private_collection(user_id)
