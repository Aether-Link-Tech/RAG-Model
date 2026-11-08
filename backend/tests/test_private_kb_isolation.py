"""TC-ISO: private knowledge base isolation at the vector-store layer (FR-079).

Runs against an in-memory ChromaDB with deterministic fake embeddings, so no
PostgreSQL, Chroma server, or Ollama is required.
"""
import uuid

import pytest

from backend.app.db import chroma as chroma_module
from backend.app.db.chroma import (
    PrivateCollectionOwnershipError,
    get_or_create_private_collection,
    get_private_collection,
    private_collection_name,
)
from backend.app.services.vector_store import (
    delete_document_chunks,
    delete_user_private_knowledge,
    private_owner_filter,
    query_for_user,
    upsert_chunks,
)
from backend.tests.conftest import fake_embed

SECRET_TEXT = "alpha quarterly salary report confidential"


@pytest.fixture
def user_a() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def user_b() -> uuid.UUID:
    return uuid.uuid4()


def _index(user_id, text, *, visibility="private", document_id=None, chunk_id=None):
    document_id = document_id or uuid.uuid4()
    upsert_chunks(
        user_id=user_id,
        ids=[chunk_id or f"{document_id}:0"],
        embeddings=[fake_embed(text)],
        documents=[text],
        metadatas=[{"document_id": str(document_id), "filename": "f.txt", "chunk_index": 0}],
        visibility=visibility,
    )
    return document_id


def _search(user_id, text, **kwargs):
    return query_for_user(user_id=user_id, query_embedding=fake_embed(text), n_results=10, **kwargs)


def test_private_collection_name_is_user_scoped(user_a):
    assert private_collection_name(user_a) == f"user_{user_a.hex}_private"
    assert private_collection_name(str(user_a)) == private_collection_name(user_a)


def test_tc_iso_01_private_doc_searchable_only_by_owner(chroma_client, user_a, user_b):
    """User A uploads a private doc -> User B searches -> 0 results; A finds it."""
    _index(user_a, SECRET_TEXT)

    b_hits = _search(user_b, SECRET_TEXT)
    assert b_hits == []

    a_hits = _search(user_a, SECRET_TEXT)
    assert len(a_hits) == 1
    assert a_hits[0].visibility == "private"
    assert a_hits[0].document == SECRET_TEXT


def test_tc_iso_02_search_never_opens_another_users_collection(
    chroma_client, monkeypatch, user_a, user_b
):
    """User B's search must only ever touch B's own private collection name."""
    _index(user_a, SECRET_TEXT)
    _index(user_b, "unrelated grocery list")

    requested: list[str] = []
    real_get = chroma_client.get_collection

    def spy_get_collection(name, *args, **kwargs):
        requested.append(name)
        return real_get(name, *args, **kwargs)

    monkeypatch.setattr(chroma_client, "get_collection", spy_get_collection)

    _search(user_b, SECRET_TEXT)

    assert private_collection_name(user_a) not in requested
    assert private_collection_name(user_b) in requested


def test_tc_iso_03_private_collection_separate_from_public(chroma_client, user_a, user_b):
    """Private chunks live only in the owner's collection, never the public one."""
    _index(user_a, SECRET_TEXT)
    _index(user_b, "shared handbook holiday policy", visibility="public")

    names = {getattr(c, "name", c) for c in chroma_client.list_collections()}
    assert private_collection_name(user_a) in names
    assert private_collection_name(user_b) not in names  # B only indexed public

    public = chroma_module.get_public_collection()
    public_docs = public.get(include=["documents"])["documents"]
    assert SECRET_TEXT not in public_docs

    a_private = get_private_collection(user_a)
    assert a_private.count() == 1


def test_tc_iso_04_public_docs_visible_to_all_private_excluded(chroma_client, user_a, user_b):
    _index(user_a, SECRET_TEXT)
    _index(user_a, "alpha shared handbook", visibility="public")

    b_hits = _search(user_b, "alpha")
    assert [h.visibility for h in b_hits] == ["public"]
    assert all(h.document != SECRET_TEXT for h in b_hits)


def test_tc_iso_05_mis_stamped_collection_is_rejected(chroma_client, user_a, user_b):
    """A collection under B's name but stamped with A's owner is never used for B."""
    chroma_client.create_collection(
        name=private_collection_name(user_b),
        metadata={"hnsw:space": "cosine", "visibility": "private", "user_id": user_a.hex},
    )

    assert get_private_collection(user_b) is None
    with pytest.raises(PrivateCollectionOwnershipError):
        get_or_create_private_collection(user_b)


def test_tc_iso_06_private_filter_requires_owner_and_private_visibility(
    chroma_client, user_a, user_b
):
    """Chunks in A's collection with a foreign owner or non-private flag are filtered out."""
    _index(user_a, SECRET_TEXT)
    collection = get_private_collection(user_a)
    collection.add(
        ids=["planted-foreign-owner", "planted-public-flag"],
        embeddings=[fake_embed(SECRET_TEXT), fake_embed(SECRET_TEXT)],
        documents=["foreign owner chunk", "public flagged chunk"],
        metadatas=[
            {"user_id": user_b.hex, "visibility": "private", "document_id": "x"},
            {"user_id": user_a.hex, "visibility": "public", "document_id": "y"},
        ],
    )

    hits = _search(user_a, SECRET_TEXT, include_public=False)
    assert [h.document for h in hits] == [SECRET_TEXT]

    assert private_owner_filter(user_a) == {
        "$and": [
            {"user_id": {"$eq": user_a.hex}},
            {"visibility": {"$eq": "private"}},
        ]
    }


def test_tc_iso_07_owner_cannot_delete_other_users_chunks(chroma_client, user_a, user_b):
    doc_a = _index(user_a, SECRET_TEXT)

    # B tries to delete A's document id: only B's (missing) collection is touched.
    delete_document_chunks(user_id=user_b, document_id=doc_a, visibility="private")
    assert get_private_collection(user_a).count() == 1

    delete_document_chunks(user_id=user_a, document_id=doc_a, visibility="private")
    assert get_private_collection(user_a).count() == 0


def test_tc_iso_08_deleting_user_removes_only_their_private_collection(
    chroma_client, user_a, user_b
):
    _index(user_a, SECRET_TEXT)
    _index(user_b, "bravo private notes")
    _index(user_a, "alpha shared handbook", visibility="public")

    assert delete_user_private_knowledge(user_a) is True

    assert get_private_collection(user_a) is None
    assert get_private_collection(user_b).count() == 1
    assert chroma_module.get_public_collection().count() == 1
    assert _search(user_a, SECRET_TEXT, include_public=False) == []

    # Idempotent: nothing left to delete.
    assert delete_user_private_knowledge(user_a) is False
