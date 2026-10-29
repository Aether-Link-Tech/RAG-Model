"""ChromaDB indexing and owner-scoped vector search.

Private chunks are stored only in the owner's collection and queried with an
explicit user_id metadata filter. Callers never pass a target user other than
the authenticated owner.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, Optional, Sequence

from backend.app.db.chroma import (
    delete_private_collection,
    get_or_create_private_collection,
    get_private_collection,
    get_public_collection,
)

Visibility = Literal["private", "public"]


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
    this user's collection and are additionally filtered by user_id metadata so
    another tenant's private chunks cannot leak even if collection routing fails.
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
                    where=_owner_filter(user_id),
                )
            )

    hits = [hit for hit in hits if _is_visible_to_owner(hit, owner)]
    hits.sort(key=lambda hit: hit.distance)
    return hits[:n_results]


def _is_visible_to_owner(hit: RetrievedChunk, owner: str) -> bool:
    if hit.visibility == "public":
        return hit.metadata.get("visibility") == "public"
    return (
        hit.metadata.get("visibility") == "private"
        and hit.metadata.get("user_id") == owner
    )


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
    except Exception:
        return []
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
