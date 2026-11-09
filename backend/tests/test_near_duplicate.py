"""FR-081 / AL-282: near-duplicate detection (>= 50 % similar) with privacy-safe messages."""
import io
import os
import random
import time
import uuid

import numpy as np
import pytest
from sqlalchemy import text

from backend.app.api.v1 import documents as documents_api
from backend.app.core.config import settings
from backend.app.services import document_ingest
from backend.app.services import near_duplicate as nd
from backend.tests.conftest import TestingSessionLocal, _CleanupSessionLocal, bearer, test_engine

VOCAB_RNG = random.Random(81)


def _words(n: int) -> list[str]:
    return [f"w{VOCAB_RNG.randrange(10**9)}" for _ in range(n)]


def pair_with_jaccard(target: float, common: int = 600) -> tuple[str, str]:
    """Two texts whose word-3-gram shingle sets have Jaccard similarity close to `target`."""
    unique = max(1, round(common * (1 - target) / (2 * target)))
    shared = _words(common + 2)
    a = " ".join(_words(unique) + shared)
    b = " ".join(_words(unique) + shared)
    return a, b


def exact_jaccard(a: str, b: str) -> float:
    sa, sb = nd.shingles(a), nd.shingles(b)
    return len(sa & sb) / len(sa | sb)


# --- unit level -----------------------------------------------------------------------


def test_signature_is_deterministic_and_identical_text_scores_one():
    content = "Quarterly revenue grew in every region except the north. " * 20
    assert np.array_equal(nd.signature(content), nd.signature(content))
    assert nd.similarity(nd.signature(content), nd.signature(content)) == 1.0
    assert len(nd.signature(content)) == nd.NUM_PERM == 128


def test_shingles_ignore_case_and_punctuation():
    assert nd.shingles("Hello, World! How are you?") == nd.shingles("hello world how ARE you")


@pytest.mark.parametrize("target", [0.2, 0.4, 0.6, 0.8])
def test_estimate_tracks_exact_jaccard(target):
    a, b = pair_with_jaccard(target)
    exact = exact_jaccard(a, b)
    assert abs(exact - target) < 0.03
    assert abs(nd.similarity(nd.signature(a), nd.signature(b)) - exact) < 0.12


def test_threshold_boundary_49_vs_51_percent():
    assert nd.is_near_duplicate(0.49) is False
    assert nd.is_near_duplicate(0.50) is True
    assert nd.is_near_duplicate(0.51) is True


def test_lsh_finds_pairs_at_or_above_threshold():
    """Pairs >= 50 % similar must share at least one band (b=42, r=3 -> p >= 0.996)."""
    found = 0
    for _ in range(40):
        a, b = pair_with_jaccard(0.55)
        found += bool(set(nd.band_hashes(nd.signature(a))) & set(nd.band_hashes(nd.signature(b))))
    assert found >= 39


def test_signature_roundtrip_bytes():
    sig = nd.signature("some document text for the round trip test")
    assert np.array_equal(nd.from_bytes(nd.to_bytes(sig)), sig)


# --- through the API (real indexing pipeline) ------------------------------------------


@pytest.fixture
def real_indexing(monkeypatch, chroma_client, fake_embeddings):
    """Use the real index_document (conftest stubs it) with in-process parsing."""
    monkeypatch.setattr(documents_api, "index_document", document_ingest.index_document)
    monkeypatch.setattr(settings, "PARSER_SANDBOX_ENABLED", False)
    monkeypatch.setattr(document_ingest, "AsyncSessionLocal", TestingSessionLocal)


async def _upload(client, user, name, content, visibility="private"):
    return await client.post(
        "/api/v1/documents",
        headers=bearer(user),
        data={"visibility": visibility},
        files={"file": (name, io.BytesIO(content.encode()), "text/plain")},
    )


async def _privileged(sql, **params):
    async with _CleanupSessionLocal() as session:
        result = await session.execute(text(sql), params)
        await session.commit()
        return result


async def test_public_match_is_rejected_and_named(async_client, test_user, test_user_2, real_indexing):
    original, near = pair_with_jaccard(0.75)
    first = await _upload(async_client, test_user, "employee-handbook.txt", original, "public")
    assert first.status_code == 201 and first.json()["status"] == "indexed", first.text

    res = await _upload(async_client, test_user_2, "my-copy.txt", near)
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert "employee-handbook.txt" in detail and "public" in detail and "use that document" in detail

    # Rejected upload removed cleanly: no row, no file, no vectors, and the rejection is audited.
    listing = (await async_client.get("/api/v1/documents", headers=bearer(test_user_2))).json()
    assert [d["filename"] for d in listing if d["is_owner"]] == []
    b_folder = document_ingest.user_storage_dir(test_user_2.id)
    assert not b_folder.exists() or not any(b_folder.iterdir())
    from backend.app.db.chroma import get_private_collection

    collection = get_private_collection(test_user_2.id)
    assert collection is None or collection.count() == 0
    audit = (await _privileged(
        "SELECT reason FROM security_audit_log WHERE user_id = :u", u=test_user_2.id)).scalars().all()
    assert "Near-duplicate of an existing document" in audit
    await _privileged("DELETE FROM security_audit_log WHERE user_id = :u", u=test_user_2.id)


async def test_own_private_match_is_named(async_client, test_user, real_indexing):
    original, near = pair_with_jaccard(0.8)
    assert (await _upload(async_client, test_user, "board-notes.txt", original)).status_code == 201
    res = await _upload(async_client, test_user, "board-notes-v2.txt", near)
    assert res.status_code == 409
    assert "You already uploaded" in res.json()["detail"] and "board-notes.txt" in res.json()["detail"]
    await _privileged("DELETE FROM security_audit_log WHERE user_id = :u", u=test_user.id)


async def test_other_users_private_document_is_never_revealed(async_client, test_user, test_user_2, real_indexing):
    original, near = pair_with_jaccard(0.9)
    assert (await _upload(async_client, test_user, "secret-merger-plan.txt", original)).status_code == 201
    res = await _upload(async_client, test_user_2, "similar.txt", near)
    assert res.status_code == 201, res.text  # treated as no match: nothing about A is revealed
    assert "secret-merger-plan" not in res.text


async def test_dissimilar_upload_is_accepted_and_fingerprinted(async_client, test_user, real_indexing):
    a, b = pair_with_jaccard(0.3)
    first = await _upload(async_client, test_user, "one.txt", a)
    second = await _upload(async_client, test_user, "two.txt", b)
    assert first.status_code == second.status_code == 201
    ids = [first.json()["id"], second.json()["id"]]
    count = (await _privileged(
        "SELECT count(*) FROM document_fingerprints WHERE document_id = ANY(:ids)", ids=[uuid.UUID(i) for i in ids]
    )).scalar_one()
    bands = (await _privileged(
        "SELECT count(*) FROM document_fingerprint_bands WHERE document_id = :d", d=uuid.UUID(ids[0])
    )).scalar_one()
    assert count == 2 and bands == nd.BANDS

    deleted = await async_client.delete(f"/api/v1/documents/{ids[0]}", headers=bearer(test_user))
    assert deleted.status_code == 204
    left = (await _privileged(
        "SELECT count(*) FROM document_fingerprint_bands WHERE document_id = :d", d=uuid.UUID(ids[0])
    )).scalar_one()
    assert left == 0  # removed with the document


async def test_replacement_that_duplicates_another_document_fails_with_reason(async_client, test_user, real_indexing):
    x, x_near = pair_with_jaccard(0.8)
    y, _ = pair_with_jaccard(0.2)
    assert (await _upload(async_client, test_user, "report.txt", x)).status_code == 201
    other = (await _upload(async_client, test_user, "draft.txt", y)).json()
    res = await async_client.put(
        f"/api/v1/documents/{other['id']}", headers=bearer(test_user),
        files={"file": ("draft.txt", io.BytesIO(x_near.encode()), "text/plain")},
    )
    assert res.status_code == 202, res.text
    doc = (await async_client.get(f"/api/v1/documents/{other['id']}", headers=bearer(test_user))).json()
    assert doc["status"] == "failed" and "report.txt" in doc["error_message"]


async def test_rls_hides_other_users_private_fingerprints(async_client, test_user, test_user_2, real_indexing):
    private = (await _upload(async_client, test_user, "private.txt", pair_with_jaccard(0.5)[0])).json()
    public = (await _upload(async_client, test_user, "public.txt", pair_with_jaccard(0.5)[0], "public")).json()

    async def count_as(user_id, doc_id):
        async with test_engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id)})
                return (await conn.execute(text(
                    "SELECT count(*) FROM document_fingerprint_bands WHERE document_id = :d"), {"d": doc_id})).scalar_one()

    assert await count_as(test_user_2.id, private["id"]) == 0
    assert await count_as(test_user_2.id, public["id"]) == nd.BANDS
    assert await count_as(test_user.id, private["id"]) == nd.BANDS
    async with test_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(test_user_2.id)})
            removed = await conn.execute(text("DELETE FROM document_fingerprints WHERE document_id = :d"),
                                         {"d": public["id"]})
            assert removed.rowcount == 0  # readable, but only the owner can delete


# --- performance (AL-281) ---------------------------------------------------------------


async def test_lookup_stays_fast_with_many_documents(test_user):
    """Seed N fingerprints and time one check (CI: 2,000 under 1 s).

    Full benchmark for AL-281: NEAR_DUP_BENCH_N=10000 pytest tests/test_near_duplicate.py -k fast -s
    (requirement: under 5 s for 10,000 documents).
    """
    n = int(os.getenv("NEAR_DUP_BENCH_N", "2000"))
    limit = 5.0 if n >= 10000 else 1.0
    rng = np.random.default_rng(7)
    doc_ids = [uuid.uuid4() for _ in range(n)]
    async with _CleanupSessionLocal() as session:
        await session.execute(text(
            "INSERT INTO documents (id, user_id, filename, file_path, mime_type, file_size, file_hash, status, visibility) "
            "SELECT d, :u, 'bulk.txt', '/x', 'text/plain', 1, md5(d::text) || md5(d::text), 'indexed', 'private' "
            "FROM unnest(CAST(:ids AS uuid[])) AS d"), {"u": test_user.id, "ids": doc_ids})
        sigs, band_docs, band_nums, band_hashes = [], [], [], []
        for d in doc_ids:
            sig = rng.integers(0, 2**63, size=nd.NUM_PERM, dtype=np.uint64)
            sigs.append(nd.to_bytes(sig))
            for b, h in nd.band_hashes(sig):
                band_docs.append(d)
                band_nums.append(b)
                band_hashes.append(h)
        # One set-based INSERT per table (row-by-row inserts take minutes at 10K documents).
        await session.execute(text(
            "INSERT INTO document_fingerprints (document_id, user_id, visibility, num_perm, signature) "
            "SELECT d, :u, 'private', 128, s FROM unnest(CAST(:ids AS uuid[]), CAST(:sigs AS bytea[])) AS t(d, s)"),
            {"u": test_user.id, "ids": doc_ids, "sigs": sigs})
        await session.execute(text(
            "INSERT INTO document_fingerprint_bands (document_id, band, hash, user_id, visibility) "
            "SELECT d, b, h, :u, 'private' FROM unnest(CAST(:docs AS uuid[]), CAST(:bands AS smallint[]), "
            "CAST(:hashes AS bigint[])) AS t(d, b, h)"),
            {"u": test_user.id, "docs": band_docs, "bands": band_nums, "hashes": band_hashes})
        await session.commit()

    from backend.app.db.rls import configure_rls_context
    from backend.app.models.document import Document

    probe = Document(id=uuid.uuid4(), user_id=test_user.id)
    async with TestingSessionLocal() as db:
        await configure_rls_context(db, test_user.id, "user")
        start = time.perf_counter()
        match = await nd.find_near_duplicate(db, probe, nd.signature(" ".join(_words(500))))
        elapsed = time.perf_counter() - start
    assert match is None
    print(f"near-duplicate check: {elapsed * 1000:.0f} ms against {n} fingerprinted documents")
    assert elapsed < limit, f"near-duplicate check took {elapsed:.2f}s for {n} documents"
