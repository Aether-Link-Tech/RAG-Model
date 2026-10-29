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
    """Stable, Chroma-safe name for a user's private collection."""
    return f"{settings.CHROMA_PRIVATE_COLLECTION_PREFIX}{_normalize_user_id(user_id)}"


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


def get_or_create_private_collection(user_id: uuid.UUID | str) -> Any:
    """Return the caller's private collection, creating it on first use."""
    owner = _normalize_user_id(user_id)
    return get_chroma_client().get_or_create_collection(
        name=private_collection_name(user_id),
        metadata={
            "hnsw:space": "cosine",
            "visibility": "private",
            "user_id": owner,
        },
    )


def get_private_collection(user_id: uuid.UUID | str) -> Optional[Any]:
    """Return an existing private collection, or None if the user has none."""
    try:
        return get_chroma_client().get_collection(name=private_collection_name(user_id))
    except Exception:
        return None


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
