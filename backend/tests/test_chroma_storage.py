import uuid
from types import SimpleNamespace

import pytest

from backend.app.core.config import settings
from backend.app.db.chroma import get_private_collection, get_public_collection
from backend.app.services import document_ingest
from backend.app.services.vector_store import (
    cleanup_stale_document_chunks,
    delete_document_chunks,
    upsert_chunks,
)
from backend.tests.conftest import fake_embed


def _insert(user_id, document_id, index, text, *, visibility="private"):
    chunk_id = f"{document_id}:{index}"
    upsert_chunks(
        user_id=user_id,
        ids=[chunk_id],
        embeddings=[fake_embed(text)],
        documents=[text],
        metadatas=[
            {
                "document_id": str(document_id),
                "chunk_id": chunk_id,
                "chunk_index": index,
            }
        ],
        visibility=visibility,
    )
    return chunk_id


def test_upsert_overwrites_existing_chunk(chroma_client):
    user_id, document_id = uuid.uuid4(), uuid.uuid4()
    chunk_id = _insert(user_id, document_id, 0, "old content")
    _insert(user_id, document_id, 0, "replacement content")

    stored = get_private_collection(user_id).get(ids=[chunk_id], include=["documents"])
    assert stored["documents"] == ["replacement content"]


async def test_reindex_with_fewer_chunks_removes_stale_chunks(
    chroma_client, monkeypatch
):
    from pathlib import Path
    from backend.app.services.storage_paths import build_storage_path

    user_id, document_id = uuid.uuid4(), uuid.uuid4()
    file_path = build_storage_path(user_id, document_id, "document.txt")
    Path(file_path).write_bytes(b"content")
    document = SimpleNamespace(
        user_id=user_id,
        id=document_id,
        status="uploaded",
        error_message=None,
        file_path=file_path,
        mime_type="text/plain",
        filename="document.txt",
        visibility="private",
    )

    extracted_text = iter(("long version", "short version"))
    monkeypatch.setattr(
        document_ingest,
        "extract_text",
        lambda *args, **kwargs: next(extracted_text),
    )
    monkeypatch.setattr(
        document_ingest,
        "split_text",
        lambda text: (
            ["first chunk", "second chunk"]
            if text == "long version"
            else ["first chunk"]
        ),
    )

    async def fake_embed_texts(texts):
        return _fake_embeddings(texts)

    monkeypatch.setattr(document_ingest, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(document_ingest.near_duplicate, "shingles", lambda text: set())
    monkeypatch.setattr(settings, "OCR_ENABLED", False)

    class FakeSession:
        async def commit(self):
            pass

        async def refresh(self, instance):
            pass

    await document_ingest.index_document(FakeSession(), document)
    collection = get_private_collection(user_id)
    assert set(collection.get()["ids"]) == {
        f"{document_id}:0",
        f"{document_id}:1",
    }

    await document_ingest.index_document(FakeSession(), document)
    stored = collection.get(include=["metadatas", "documents"])
    assert stored["ids"] == [f"{document_id}:0"]
    assert stored["metadatas"][0]["chunk_id"] == stored["ids"][0]


def _fake_embeddings(texts):
    return [fake_embed(text) for text in texts]


@pytest.mark.parametrize("visibility", ["private", "public"])
def test_stale_cleanup_is_scoped_to_owner_document_and_visibility(
    chroma_client, visibility
):
    user_id, other_user_id = uuid.uuid4(), uuid.uuid4()
    document_id, other_document_id, other_user_document_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    kept_id = _insert(user_id, document_id, 0, "kept", visibility=visibility)
    _insert(user_id, document_id, 1, "stale", visibility=visibility)
    other_document_chunk = _insert(
        user_id, other_document_id, 0, "other document", visibility=visibility
    )
    other_user_chunk = _insert(
        other_user_id,
        other_user_document_id,
        0,
        "other owner",
        visibility=visibility,
    )

    cleanup_stale_document_chunks(
        user_id=user_id,
        document_id=document_id,
        chunk_ids=[kept_id],
        visibility=visibility,
    )

    collection = (
        get_private_collection(user_id)
        if visibility == "private"
        else get_public_collection()
    )
    remaining = set(collection.get()["ids"])
    expected_ids = {kept_id, other_document_chunk}
    if visibility == "public":
        expected_ids.add(other_user_chunk)
    assert remaining == expected_ids
    kept = collection.get(ids=[kept_id], include=["metadatas"])
    assert kept["metadatas"][0]["user_id"] == user_id.hex

    if visibility == "private":
        assert get_private_collection(other_user_id).get()["ids"] == [other_user_chunk]


@pytest.mark.parametrize("visibility", ["private", "public"])
def test_delete_document_chunks_removes_all_document_chunks(
    chroma_client, visibility
):
    user_id = uuid.uuid4()
    document_id, other_document_id = uuid.uuid4(), uuid.uuid4()
    target_ids = {
        _insert(user_id, document_id, 0, "first", visibility=visibility),
        _insert(user_id, document_id, 1, "second", visibility=visibility),
    }
    other_id = _insert(user_id, other_document_id, 0, "keep", visibility=visibility)

    delete_document_chunks(
        user_id=user_id,
        document_id=document_id,
        visibility=visibility,
    )

    collection = (
        get_private_collection(user_id)
        if visibility == "private"
        else get_public_collection()
    )
    assert set(collection.get()["ids"]) == {other_id}
    assert not (target_ids & set(collection.get()["ids"]))


async def test_ingested_chunk_id_metadata_matches_chroma_id(chroma_client, monkeypatch):
    from pathlib import Path
    from backend.app.services.storage_paths import build_storage_path

    user_id, document_id = uuid.uuid4(), uuid.uuid4()
    file_path = build_storage_path(user_id, document_id, "document.txt")
    Path(file_path).write_bytes(b"content")
    document = SimpleNamespace(
        user_id=user_id,
        id=document_id,
        status="uploaded",
        error_message=None,
        file_path=file_path,
        mime_type="text/plain",
        filename="document.txt",
        visibility="private",
    )
    monkeypatch.setattr(document_ingest, "extract_text", lambda *args, **kwargs: "content")
    monkeypatch.setattr(document_ingest, "split_text", lambda text: ["chunk"])

    async def fake_embed_texts(texts):
        return _fake_embeddings(texts)

    monkeypatch.setattr(document_ingest, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(document_ingest.near_duplicate, "shingles", lambda text: set())
    monkeypatch.setattr(settings, "OCR_ENABLED", False)

    class FakeSession:
        async def commit(self):
            pass

        async def refresh(self, instance):
            pass

    await document_ingest.index_document(FakeSession(), document)

    stored = get_private_collection(user_id).get(include=["metadatas"])
    assert stored["metadatas"][0]["chunk_id"] == stored["ids"][0]
    assert stored["metadatas"][0]["document_id"] == str(document_id)
    assert stored["metadatas"][0]["chunk_index"] == 0
