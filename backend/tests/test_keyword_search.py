"""FR-023 Keyword Search (BM25).

AL-219: BM25 over the user's authorized chunks (POST /api/v1/retrieval/keyword).
AL-220: tokenizer and stopword removal.
AL-221: the keyword index is rebuilt when chunks are added, deleted or renamed.
AL-222: ranking, isolation and latency (latency with RUN_BENCHMARKS=1).

API tests need PostgreSQL (DATABASE_URL, migrated to head). Chroma and Ollama are
replaced by an in-memory client and deterministic fake embeddings.
"""
from __future__ import annotations

import io
import os
import random
import statistics
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from backend.app.api.v1 import documents as documents_api
from backend.app.api.v1 import users as users_api
from backend.app.core.config import settings
from backend.app.core.security import create_access_token
from backend.app.models.user import User
from backend.app.services import keyword_search as keyword_module
from backend.app.services import vector_store
from backend.app.services.document_ingest import index_document
from backend.app.services.keyword_search import Bm25Index, clear_keyword_index_cache
from backend.app.services.keyword_tokenizer import tokenize
from backend.app.services.vector_store import RetrievedChunk
from backend.tests.conftest import fake_embed, test_engine

SEARCH = "/api/v1/retrieval/keyword"


# ============================================================ AL-220 tokenizer (no database)

def test_tokenizer_lowercases_and_removes_stopwords():
    assert tokenize("The Quick brown FOX jumps over the lazy dog") == [
        "quick", "brown", "fox", "jumps", "lazy", "dog",
    ]


def test_tokenizer_keeps_numbers_and_joined_terms():
    assert tokenize("COVID-19 cases rose 12% in 2024 (v1.5 report)") == [
        "covid-19", "cases", "rose", "12", "2024", "v1.5", "report",
    ]


def test_tokenizer_handles_punctuation_possessives_and_unicode():
    assert tokenize("Employee’s handbook: “leave”, sick-leave; café!") == [
        "employee", "handbook", "leave", "sick-leave", "café",
    ]


def test_tokenizer_drops_single_letters_and_underscores():
    assert tokenize("a b c x_y __init__ 7") == ["init", "7"]


def test_tokenizer_only_stopwords_gives_nothing():
    assert tokenize("what is the and of it") == []
    assert tokenize("") == []


# ============================================================ BM25 index (no database)

def _chunk(chroma_id: str, text_: str, **metadata) -> RetrievedChunk:
    return RetrievedChunk(chroma_id, text_, 0.0, metadata, "private")


def test_bm25_ranks_more_relevant_chunk_first():
    index = Bm25Index([
        _chunk("a", "solar panel installation guide for rooftops"),
        _chunk("b", "solar solar panel efficiency and solar inverter sizing"),
        _chunk("c", "kitchen renovation budget"),
    ])
    ranked = index.search(tokenize("solar inverter"))
    assert [index.chunks[p].chroma_id for p, _, _ in ranked] == ["b", "a"]
    assert ranked[0][2] == ("solar", "inverter")
    assert all(score > 0 for _, score, _ in ranked)


def test_bm25_rare_term_outweighs_common_term():
    index = Bm25Index([
        _chunk("common", "policy policy policy document"),
        _chunk("rare", "policy about quarantine"),
        _chunk("other", "policy summary"),
    ])
    ranked = index.search(tokenize("policy quarantine"))
    assert index.chunks[ranked[0][0]].chroma_id == "rare"


def test_bm25_scores_stay_positive_for_tiny_corpus():
    index = Bm25Index([_chunk("only", "single chunk about glaciers")])
    ((_, score, _),) = index.search(["glaciers"])
    assert score > 0


def test_bm25_section_heading_is_searchable():
    index = Bm25Index([_chunk("s", "twenty days per year", section="Annual Leave")])
    assert index.search(["leave"])


def test_bm25_no_match_returns_empty():
    index = Bm25Index([_chunk("a", "apples and pears")])
    assert index.search(["bananas"]) == []


# ============================================================ API (PostgreSQL)

@pytest_asyncio.fixture
async def require_postgres():
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        if os.getenv("CI"):
            pytest.fail(f"PostgreSQL required in CI but unreachable: {exc}")
        pytest.skip(f"PostgreSQL unreachable, skipping keyword search API tests: {exc}")


@pytest.fixture
def api(require_postgres, monkeypatch, chroma_client, fake_embeddings):
    monkeypatch.setattr(documents_api, "index_document", index_document)
    monkeypatch.setattr(documents_api, "delete_document_chunks", vector_store.delete_document_chunks)
    monkeypatch.setattr(documents_api, "rename_document_chunks", vector_store.rename_document_chunks)
    monkeypatch.setattr(users_api, "delete_user_private_knowledge", vector_store.delete_user_private_knowledge)
    clear_keyword_index_cache()
    yield
    clear_keyword_index_cache()


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


async def _search(client, user: User, query: str, **extra):
    response = await client.post(SEARCH, headers=_headers(user), json={"query": query, **extra})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "body",
    [{"query": ""}, {"query": "   "}, {"query": "x" * 4001}, {"query": "ok", "top_k": 0}, {"query": "ok", "top_k": 51}],
)
async def test_invalid_requests_are_rejected(api, async_client, test_user: User, body):
    response = await async_client.post(SEARCH, headers=_headers(test_user), json=body)
    assert response.status_code == 422


async def test_requires_authentication(api, async_client):
    assert (await async_client.post(SEARCH, json={"query": "anything"})).status_code == 401


async def test_exact_keyword_match_ranks_first(api, async_client, test_user: User):
    await _upload(async_client, test_user, b"invoice number INV-20931 for consulting services", name="invoice.txt")
    await _upload(async_client, test_user, b"consulting services agreement and payment terms", name="contract.txt")
    await _upload(async_client, test_user, b"team offsite agenda and travel plans", name="offsite.txt")

    body = await _search(async_client, test_user, "INV-20931 consulting", top_k=5)
    assert body["query_terms"] == ["inv-20931", "consulting"]
    results = body["results"]
    assert [hit["filename"] for hit in results] == ["invoice.txt", "contract.txt"]
    assert results[0]["matched_terms"] == ["inv-20931", "consulting"]
    assert results[1]["matched_terms"] == ["consulting"]
    assert [hit["rank"] for hit in results] == [1, 2]
    assert results[0]["score"] > results[1]["score"] > 0
    assert "user_id" not in results[0]["metadata"]
    assert body["took_ms"] >= 0


async def test_top_k_limits_results(api, async_client, test_user: User):
    for index in range(5):
        await _upload(async_client, test_user, f"telescope mirror coating batch {index} {'x' * index}".encode(), name=f"t{index}.txt")
    body = await _search(async_client, test_user, "telescope mirror", top_k=3)
    assert len(body["results"]) == 3
    assert body["top_k"] == 3


async def test_stopword_only_query_returns_message(api, async_client, test_user: User):
    await _upload(async_client, test_user, b"the and of", name="stop.txt")
    body = await _search(async_client, test_user, "what is the")
    assert body["results"] == []
    assert body["query_terms"] == []
    assert "keywords" in body["message"]


async def test_no_match_returns_message(api, async_client, test_user: User):
    await _upload(async_client, test_user, b"photosynthesis in leaves", name="bio.txt")
    body = await _search(async_client, test_user, "quantum chromodynamics")
    assert body["results"] == []
    assert body["message"] == "No matching passages found."


# ------------------------------------------------------------ isolation (AL-222)

async def test_other_users_private_chunks_never_returned(api, async_client, test_user: User, test_user_2: User):
    await _upload(async_client, test_user, b"zephyr project acquisition target list", name="zephyr.txt")

    other = await _search(async_client, test_user_2, "zephyr acquisition", top_k=50)
    assert other["results"] == []
    own = await _search(async_client, test_user, "zephyr acquisition")
    assert own["results"][0]["filename"] == "zephyr.txt"


async def test_public_chunks_visible_to_everyone_and_flags_respected(
    api, async_client, test_user: User, test_user_2: User
):
    await _upload(async_client, test_user, b"cafeteria opening hours weekdays", name="cafe.txt", visibility="public")
    await _upload(async_client, test_user_2, b"cafeteria private budget notes", name="mine.txt")

    both = await _search(async_client, test_user_2, "cafeteria")
    assert {hit["visibility"] for hit in both["results"]} == {"public", "private"}
    assert {hit["visibility"] for hit in (await _search(async_client, test_user_2, "cafeteria", include_private=False))["results"]} == {"public"}
    assert {hit["visibility"] for hit in (await _search(async_client, test_user_2, "cafeteria", include_public=False))["results"]} == {"private"}


async def test_stale_index_still_drops_deleted_document(api, async_client, test_user: User, monkeypatch):
    """Even if the index were not invalidated, PostgreSQL authorization hides deleted documents."""
    uploaded = await _upload(async_client, test_user, b"obsolete nebula survey results", name="nebula.txt")
    assert (await _search(async_client, test_user, "nebula"))["results"]

    monkeypatch.setattr(vector_store, "mark_chunks_changed", lambda *args, **kwargs: None)
    monkeypatch.setattr(settings, "KEYWORD_INDEX_TTL_SECONDS", 3600)
    # Delete only the database row; the stale index still holds the chunk.
    from backend.tests.conftest import TestingSessionLocal
    from backend.app.db.rls import configure_rls_context
    async with TestingSessionLocal() as session:
        await configure_rls_context(session, test_user.id, test_user.role)
        await session.execute(text("UPDATE documents SET status = 'failed' WHERE id = :id"), {"id": uploaded["id"]})
        await session.commit()

    assert (await _search(async_client, test_user, "nebula"))["results"] == []


# ------------------------------------------------------------ index invalidation (AL-221)

async def test_new_document_is_searchable_immediately(api, async_client, test_user: User):
    await _upload(async_client, test_user, b"first document about turbines", name="one.txt")
    assert len((await _search(async_client, test_user, "turbines"))["results"]) == 1

    # The index for this user is now cached; the new chunk must invalidate it.
    await _upload(async_client, test_user, b"second document about wind turbines", name="two.txt")
    assert len((await _search(async_client, test_user, "turbines"))["results"]) == 2


async def test_deleted_document_leaves_index(api, async_client, test_user: User):
    uploaded = await _upload(async_client, test_user, b"meteor shower viewing guide", name="meteor.txt")
    assert (await _search(async_client, test_user, "meteor"))["results"]

    response = await async_client.delete(f"/api/v1/documents/{uploaded['id']}", headers=_headers(test_user))
    assert response.status_code == 204
    assert (await _search(async_client, test_user, "meteor"))["results"] == []


async def test_index_rebuilt_once_and_reused_until_chunks_change(api, async_client, test_user: User, monkeypatch):
    builds = []
    original = keyword_module.Bm25Index

    def counting_index(*args, **kwargs):
        builds.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(keyword_module, "Bm25Index", counting_index)
    await _upload(async_client, test_user, b"aurora borealis photography", name="aurora.txt")
    await _search(async_client, test_user, "aurora")
    await _search(async_client, test_user, "borealis")
    assert len(builds) == 1  # second query reused the cached index

    vector_store.mark_chunks_changed(test_user.id, "private")
    await _search(async_client, test_user, "aurora")
    assert len(builds) == 2  # a chunk change forces a rebuild


async def test_write_from_another_process_is_detected_by_count(api, async_client, test_user: User, monkeypatch):
    await _upload(async_client, test_user, b"lighthouse keeper diary", name="diary.txt")
    assert len((await _search(async_client, test_user, "lighthouse"))["results"]) == 1

    # Simulate another worker indexing: no generation bump in this process.
    monkeypatch.setattr(vector_store, "mark_chunks_changed", lambda *args, **kwargs: None)
    await _upload(async_client, test_user, b"lighthouse lens restoration", name="lens.txt")
    assert len((await _search(async_client, test_user, "lighthouse"))["results"]) == 2


async def test_rename_updates_filename_in_results(api, async_client, test_user: User):
    uploaded = await _upload(async_client, test_user, b"orchid greenhouse humidity log", name="old-name.txt")
    assert (await _search(async_client, test_user, "orchid"))["results"][0]["filename"] == "old-name.txt"

    response = await async_client.put(
        f"/api/v1/documents/{uploaded['id']}", headers=_headers(test_user), data={"filename": "orchids.txt"}
    )
    assert response.status_code == 202, response.text
    assert (await _search(async_client, test_user, "orchid"))["results"][0]["filename"] == "orchids.txt"


async def test_deleting_private_collection_clears_results(api, async_client, test_user: User):
    await _upload(async_client, test_user, b"sapphire mining permits", name="sapphire.txt")
    assert (await _search(async_client, test_user, "sapphire"))["results"]

    response = await async_client.delete("/api/v1/users/me/private-collection", headers=_headers(test_user))
    assert response.status_code == 200
    assert (await _search(async_client, test_user, "sapphire"))["results"] == []


# ------------------------------------------------------------ latency (AL-222)

@pytest.mark.skipif(
    os.getenv("RUN_BENCHMARKS") != "1",
    reason="set RUN_BENCHMARKS=1 to run the keyword search latency benchmark",
)
async def test_keyword_search_p95_latency(api, async_client, test_user: User):
    """BM25 over ~2,000 chunks: first query builds the index, the rest must be fast."""
    rng = random.Random(7)
    words = [f"word{n}" for n in range(5000)]
    ids, docs, metas = [], [], []
    document = await _upload(async_client, test_user, b"benchmark anchor document", name="bench.txt")
    for index in range(2000):
        chunk_id = f"{document['id']}:{index + 1}"
        ids.append(chunk_id)
        docs.append(" ".join(rng.choice(words) for _ in range(120)))
        metas.append({"document_id": document["id"], "chunk_id": chunk_id, "filename": "bench.txt", "chunk_index": index + 1})
    vector_store.upsert_chunks(
        user_id=test_user.id, ids=ids, embeddings=[fake_embed(doc) for doc in docs],
        documents=docs, metadatas=metas, visibility="private",
    )

    started = time.perf_counter()
    await _search(async_client, test_user, "word1 word2", top_k=10)
    build_ms = (time.perf_counter() - started) * 1000

    timings = []
    for index in range(50):
        query = f"{rng.choice(words)} {rng.choice(words)} {rng.choice(words)}"
        started = time.perf_counter()
        await _search(async_client, test_user, query, top_k=10)
        timings.append((time.perf_counter() - started) * 1000)

    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    print(f"\nkeyword search: index build {build_ms:.0f} ms, median {statistics.median(timings):.1f} ms, p95 {p95:.1f} ms")
    assert p95 < float(os.getenv("KEYWORD_P95_BUDGET_MS", "300"))
