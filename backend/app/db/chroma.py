"""ChromaDB client and collection management.

Public knowledge lives in one shared collection. Each user gets a separate
private collection so other tenants cannot query their chunks, and deleting a
user only drops that user's private collection.
"""
from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Any, Optional

import chromadb
from chromadb.api import ClientAPI

from backend.app.core.config import settings

PUBLIC_COLLECTION_METADATA = {
    "hnsw:space": "cosine",
    "visibility": "public",
}


def _normalize_user_id(user_id: uuid.UUID | str) -> str:
    parsed = uuid.UUID(str(user_id))
    return parsed.hex


def private_collection_name(user_id: uuid.UUID | str) -> str:
    """Stable, Chroma-safe name for a user's private collection: user_{user_id}_private."""
    return settings.CHROMA_PRIVATE_COLLECTION_TEMPLATE.format(
        user_id=_normalize_user_id(user_id)
    )


def _is_owned_by(collection: Any, owner: str) -> bool:
    """A private collection is usable only if its stamped owner matches the caller."""
    metadata = getattr(collection, "metadata", None) or {}
    return metadata.get("visibility") == "private" and metadata.get("user_id") == owner


@lru_cache(maxsize=1)
def get_chroma_client() -> ClientAPI:
    return chromadb.HttpClient(
        host=settings.CHROMA_HOST,
        port=settings.CHROMA_PORT,
    )


def get_public_collection() -> Any:
    """Shared collection for documents that every authenticated user may retrieve."""
    return get_chroma_client().get_or_create_collection(
        name=settings.CHROMA_PUBLIC_COLLECTION,
        metadata=PUBLIC_COLLECTION_METADATA,
    )


class PrivateCollectionOwnershipError(PermissionError):
    """Raised when a private collection's stamped owner does not match the caller."""


def get_or_create_private_collection(user_id: uuid.UUID | str) -> Any:
    """Return the caller's private collection, creating it on first use."""
    owner = _normalize_user_id(user_id)
    collection = get_chroma_client().get_or_create_collection(
        name=private_collection_name(user_id),
        metadata={
            "hnsw:space": "cosine",
            "visibility": "private",
            "user_id": owner,
        },
    )
    if not _is_owned_by(collection, owner):
        raise PrivateCollectionOwnershipError(
            "Private collection owner does not match the requesting user"
        )
    return collection


def get_private_collection(user_id: uuid.UUID | str) -> Optional[Any]:
    """Return the caller's existing private collection, or None.

    Returns None when the collection is missing or when its stamped owner does
    not match, so a mis-stamped collection is never read on another user's behalf.
    """
    try:
        collection = get_chroma_client().get_collection(name=private_collection_name(user_id))
    except Exception:
        return None
    if not _is_owned_by(collection, _normalize_user_id(user_id)):
        return None
    return collection


def delete_private_collection(user_id: uuid.UUID | str) -> bool:
    """Drop only this user's private collection. Public knowledge is left intact."""
    name = private_collection_name(user_id)
    client = get_chroma_client()
    try:
        client.get_collection(name=name)
    except Exception:
        return False
    client.delete_collection(name=name)
    return True
