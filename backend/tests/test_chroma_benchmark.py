import os
import time
import uuid

import numpy as np
import pytest

from backend.app.db.chroma import get_or_create_private_collection
from backend.app.services.vector_store import private_owner_filter


@pytest.mark.skipif(
    os.getenv("RUN_BENCHMARKS") != "1",
    reason="set RUN_BENCHMARKS=1 to run the ChromaDB search benchmark",
)
def test_chroma_top_10_search_under_500ms(chroma_client):
    user_id = uuid.uuid4()
    document_id = uuid.uuid4()
    collection = get_or_create_private_collection(user_id)
    ids = [f"{document_id}:{index}" for index in range(10_000)]
    embeddings = np.random.default_rng(42).random((10_000, 768), dtype=np.float32)
    metadatas = [
        {
            "document_id": str(document_id),
            "chunk_id": chunk_id,
            "chunk_index": index,
            "user_id": user_id.hex,
            "visibility": "private",
        }
        for index, chunk_id in enumerate(ids)
    ]
    query = np.random.default_rng(43).random(768, dtype=np.float32)
    where = private_owner_filter(user_id)

    batch_size = 5_000
    for start in range(0, len(ids), batch_size):
        end = start + batch_size
        collection.upsert(
            ids=ids[start:end],
            embeddings=embeddings[start:end],
            metadatas=metadatas[start:end],
        )
    assert collection.count() == 10_000
    collection.query(
        query_embeddings=[query],
        n_results=10,
        where=where,
        include=["distances"],
    )

    started = time.perf_counter()
    result = collection.query(
        query_embeddings=[query],
        n_results=10,
        where=where,
        include=["distances"],
    )
    elapsed = time.perf_counter() - started

    assert len(result["ids"][0]) == 10
    print(f"Chroma top-10 search across 10,000 vectors: {elapsed * 1000:.1f} ms")
    assert elapsed < 0.5, f"Chroma top-10 query took {elapsed * 1000:.1f} ms"
