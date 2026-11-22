"""FR-022 Semantic Vector Search through POST /api/v1/retrieval/query.

AL-216: similarity scores, top_k setting and request validation.
AL-217: page, section and file name on every result.
AL-218: latency benchmark (set RUN_BENCHMARKS=1).

Needs PostgreSQL (DATABASE_URL, migrated to head). Chroma and Ollama are replaced
by an in-memory client and deterministic fake embeddings.
"""
from __future__ import annotations

import io
import os
import random
import statistics
import time

import pytest
import pytest_asyncio
from sqlalchemy import text

from backend.app.api.v1 import documents as documents_api
from backend.app.core.config import settings
from backend.app.core.security import create_access_token
from backend.app.models.user import User
from backend.app.services import vector_store
from backend.app.services.document_ingest import index_document
from backend.tests.conftest import fake_embed, test_engine

SEARCH = "/api/v1/retrieval/query"


@pytest_asyncio.fixture(autouse=True)
async def require_postgres():
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        if os.getenv("CI"):
            pytest.fail(f"PostgreSQL required in CI but unreachable: {exc}")
        pytest.skip(f"PostgreSQL unreachable, skipping semantic search tests: {exc}")


@pytest.fixture(autouse=True)
def real_vector_ops(monkeypatch, chroma_client, fake_embeddings):
    monkeypatch.setattr(documents_api, "index_document", index_document)
    monkeypatch.setattr(documents_api, "delete_document_chunks", vector_store.delete_document_chunks)


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, role=user.role)}"}


async def _upload(client, user: User, content: bytes, *, name: str, visibility: str = "private"):
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


async def _search(client, user: User, body: dict):
    return await client.post(SEARCH, headers=_headers(user), json=body)


# ---------------------------------------------------------------- AL-216 validation

@pytest.mark.parametrize(
    "body",
    [
        {"query": ""},
        {"query": "    \n\t "},
        {"query": "x" * 4001},
        {"query": "ok", "top_k": 0},
        {"query": "ok", "top_k": 51},
        {"query": "ok", "top_k": "many"},
        {"query": "ok", "top_k": 3, "n_results": 4},
    ],
)
async def test_invalid_requests_are_rejected(async_client, test_user: User, body):
    response = await _search(async_client, test_user, body)
    assert response.status_code == 422, response.text


async def test_search_requires_authentication(async_client):
    response = await async_client.post(SEARCH, json={"query": "anything"})
    assert response.status_code == 401


async def test_top_k_above_configured_maximum_is_rejected(async_client, test_user: User, monkeypatch):
    monkeypatch.setattr(settings, "RETRIEVAL_MAX_TOP_K", 10)
    response = await _search(async_client, test_user, {"query": "ok", "top_k": 11})
    assert response.status_code == 422
    assert (await _search(async_client, test_user, {"query": "ok", "top_k": 10})).status_code == 200


# ---------------------------------------------------------------- AL-216 scores and top_k

async def test_results_are_ranked_by_score_with_requested_top_k(async_client, test_user: User):
    for index in range(6):
        await _upload(
            async_client,
            test_user,
            f"orbital mechanics lecture {index} ".encode() + b"planet " * index,
            name=f"notes-{index}.txt",
        )

    response = await _search(async_client, test_user, {"query": "orbital mechanics lecture", "top_k": 4})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["top_k"] == 4
    assert len(body["results"]) == 4
    scores = [hit["score"] for hit in body["results"]]
    assert scores == sorted(scores, reverse=True)
    assert [hit["rank"] for hit in body["results"]] == [1, 2, 3, 4]
    for hit in body["results"]:
        assert 0.0 <= hit["score"] <= 1.0
        assert hit["score"] == pytest.approx(max(0.0, min(1.0, 1.0 - hit["distance"])), abs=1e-4)
    assert body["took_ms"] >= 0


async def test_default_top_k_comes_from_settings(async_client, test_user: User, monkeypatch):
    monkeypatch.setattr(settings, "RETRIEVAL_DEFAULT_TOP_K", 2)
    for index in range(4):
        await _upload(async_client, test_user, f"glacier survey report {index}".encode(), name=f"g{index}.txt")

    body = (await _search(async_client, test_user, {"query": "glacier survey report"})).json()
    assert body["top_k"] == 2
    assert len(body["results"]) == 2


async def test_legacy_n_results_still_works(async_client, test_user: User):
    for index in range(3):
        await _upload(async_client, test_user, f"coral reef study {index}".encode(), name=f"c{index}.txt")

    body = (await _search(async_client, test_user, {"query": "coral reef study", "n_results": 2})).json()
    assert body["top_k"] == 2
    assert len(body["results"]) == 2


async def test_best_match_is_first(async_client, test_user: User):
    await _upload(async_client, test_user, b"volcano eruption ash cloud aviation", name="volcano.txt")
    await _upload(async_client, test_user, b"bakery sourdough starter recipe", name="bread.txt")

    body = (await _search(async_client, test_user, {"query": "volcano eruption ash", "top_k": 2})).json()
    assert body["results"][0]["filename"] == "volcano.txt"
    assert body["results"][0]["score"] > body["results"][1]["score"]


async def test_no_documents_returns_empty_list_with_message(async_client, test_user: User):
    body = (await _search(async_client, test_user, {"query": "nothing indexed yet"})).json()
    assert body["results"] == []
    assert body["message"]


# ---------------------------------------------------------------- AL-217 source location

async def test_results_include_filename_section_and_page(async_client, test_user: User):
    uploaded = await _upload(async_client, test_user, b"intro paragraph for the handbook", name="handbook.txt")
    chunk_text = "annual leave policy allows twenty days"
    vector_store.upsert_chunks(
        user_id=test_user.id,
        ids=[f"{uploaded['id']}:99"],
        embeddings=[fake_embed(chunk_text)],
        documents=[chunk_text],
        metadatas=[{
            "document_id": uploaded["id"],
            "chunk_id": f"{uploaded['id']}:99",
            "filename": "handbook.txt",
            "chunk_index": 99,
            "section": "Leave > Annual leave",
            "page": 4,
        }],
        visibility="private",
    )

    body = (await _search(async_client, test_user, {"query": chunk_text, "top_k": 1})).json()
    hit = body["results"][0]
    assert hit["document"] == chunk_text
    assert hit["filename"] == "handbook.txt"
    assert hit["section"] == "Leave > Annual leave"
    assert hit["page"] == 4
    assert hit["chunk_index"] == 99
    assert hit["document_id"] == uploaded["id"]
    assert "user_id" not in hit["metadata"]


async def test_missing_section_and_page_are_null(async_client, test_user: User):
    await _upload(async_client, test_user, b"plain text without headings", name="plain.txt")
    hit = (await _search(async_client, test_user, {"query": "plain text without headings"})).json()["results"][0]
    assert hit["filename"] == "plain.txt"
    assert hit["section"] is None
    assert hit["page"] is None


# ---------------------------------------------------------------- isolation still holds

async def test_other_users_private_chunks_never_appear(async_client, test_user: User, test_user_2: User):
    await _upload(async_client, test_user, b"falcon project secret launch codes", name="falcon.txt")

    body = (await _search(async_client, test_user_2, {"query": "falcon project secret launch codes", "top_k": 50})).json()
    assert body["results"] == []
    assert body["private_collection_hit"] is False


# ---------------------------------------------------------------- AL-218 latency benchmark

@pytest.mark.skipif(
    os.getenv("RUN_BENCHMARKS") != "1",
    reason="set RUN_BENCHMARKS=1 to run the semantic search latency benchmark",
)
async def test_semantic_search_p95_latency(async_client, test_user: User):
    """End-to-end API latency (fake embeddings) over 20 documents; budget from SEARCH_P95_BUDGET_MS."""
    rng = random.Random(42)
    words = [f"term{n}" for n in range(2000)]
    for index in range(20):
        # Distinct vocabulary per document so near-duplicate detection accepts every upload.
        content = " ".join(rng.choice(words) for _ in range(400))
        await _upload(async_client, test_user, content.encode(), name=f"bench-{index}.txt")

    timings = []
    for index in range(50):
        query = f"{words[index % len(words)]} {words[(index + 3) % len(words)]}"
        started = time.perf_counter()
        response = await _search(async_client, test_user, {"query": query, "top_k": 10})
        timings.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200

    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    print(f"\nsemantic search: median {statistics.median(timings):.1f} ms, p95 {p95:.1f} ms")
    assert p95 < float(os.getenv("SEARCH_P95_BUDGET_MS", "500"))
