"""AL-205 (document_chunks table with RLS) and AL-284 (OCR columns).

Everything runs as the application role with raw SQL and app.user_id / app.user_role set per
transaction, so PostgreSQL itself is shown to enforce the rules.
"""
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.tests.conftest import test_engine

pytestmark = pytest.mark.isolation


@pytest_asyncio.fixture(autouse=True)
async def require_postgres():
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM document_chunks LIMIT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        if os.getenv("CI"):
            pytest.fail(f"PostgreSQL (migrated to head) required in CI: {exc}")
        pytest.skip(f"PostgreSQL / document_chunks unavailable: {exc}")


async def _as(user, sql, params=None, *, role=None, result="scalar"):
    """One statement as rag_app with the user's identity bound for the transaction (None = anonymous)."""
    async with test_engine.connect() as conn:
        async with conn.begin():
            if user is not None:
                await conn.execute(
                    text("SELECT set_config('app.user_id', :uid, true), set_config('app.user_role', :role, true)"),
                    {"uid": str(user.id), "role": role or user.role},
                )
            res = await conn.execute(text(sql), params or {})
            if result == "rowcount":
                return res.rowcount
            if result == "none":
                return None
            return res.scalar_one()


async def _document(user, visibility="private") -> uuid.UUID:
    doc_id = uuid.uuid4()
    await _as(user, """
        INSERT INTO documents (id, user_id, filename, file_path, mime_type, file_size, file_hash, status, visibility)
        VALUES (:id, :uid, 'policy.txt', '/tmp/policy.txt', 'text/plain', 10, :hash, 'indexed', :vis)
    """, {"id": doc_id, "uid": user.id, "hash": uuid.uuid4().hex * 2, "vis": visibility}, result="none")
    return doc_id


async def _chunk(user, doc_id, visibility="private", index=0, content="Report incidents within 24 hours.",
                 **ocr) -> uuid.UUID:
    chunk_id = uuid.uuid4()
    columns = ["id", "document_id", "user_id", "visibility", "chunk_index", "content", "chroma_id", *ocr]
    values = {"id": chunk_id, "document_id": doc_id, "user_id": user.id, "visibility": visibility,
              "chunk_index": index, "content": content, "chroma_id": f"{doc_id}:{index}", **ocr}
    placeholders = ", ".join(f"CAST(:{c} AS JSONB)" if c == "bbox" else f":{c}" for c in columns)
    await _as(user, f"INSERT INTO document_chunks ({', '.join(columns)}) VALUES ({placeholders})",
              values, result="none")
    return chunk_id


COUNT = "SELECT count(*) FROM document_chunks WHERE document_id = :d"


# ---------------------------------------------------------------- AL-205 visibility
async def test_private_chunks_are_visible_only_to_their_owner(test_user, test_user_2, admin_user):
    doc = await _document(test_user)
    await _chunk(test_user, doc)
    assert await _as(test_user, COUNT, {"d": doc}) == 1
    assert await _as(test_user_2, COUNT, {"d": doc}) == 0
    assert await _as(admin_user, COUNT, {"d": doc}) == 0  # admins never read private content
    assert await _as(None, COUNT, {"d": doc}) == 0       # unauthenticated sees nothing


async def test_public_chunks_are_visible_to_every_signed_in_user(test_user, test_user_2):
    doc = await _document(test_user, "public")
    await _chunk(test_user, doc, "public")
    assert await _as(test_user_2, COUNT, {"d": doc}) == 1
    assert await _as(None, COUNT, {"d": doc}) == 0


# ---------------------------------------------------------------- AL-205 insert rules
async def test_cannot_insert_chunks_as_someone_else(test_user, test_user_2):
    doc = await _document(test_user)
    with pytest.raises(DBAPIError):  # user B writes a row that claims to be A's
        await _as(test_user_2, """INSERT INTO document_chunks (id, document_id, user_id, visibility, chunk_index,
                 content, chroma_id) VALUES (:id, :d, :a, 'private', 0, 'x', :c)""",
                  {"id": uuid.uuid4(), "d": doc, "a": test_user.id, "c": f"{doc}:0"}, result="none")


async def test_cannot_attach_chunks_to_another_users_document(test_user, test_user_2):
    doc = await _document(test_user)
    with pytest.raises(DBAPIError):  # B claims the chunk is B's, but the document is A's
        await _chunk(test_user_2, doc)


async def test_chunk_visibility_must_match_its_document(test_user):
    doc = await _document(test_user, "private")
    with pytest.raises(DBAPIError):
        await _chunk(test_user, doc, "public")


async def test_chunks_cannot_be_updated(test_user):
    doc = await _document(test_user)
    await _chunk(test_user, doc)
    with pytest.raises(DBAPIError):  # no UPDATE privilege: chunks are replaced on re-index
        await _as(test_user, "UPDATE document_chunks SET content = 'tampered' WHERE document_id = :d",
                  {"d": doc}, result="none")


# ---------------------------------------------------------------- AL-205 delete rules
async def test_delete_rules_mirror_documents(test_user, test_user_2, admin_user):
    private_doc, public_doc = await _document(test_user), await _document(test_user, "public")
    await _chunk(test_user, private_doc)
    await _chunk(test_user, public_doc, "public")
    delete = "DELETE FROM document_chunks WHERE document_id = :d"
    assert await _as(test_user_2, delete, {"d": private_doc}, result="rowcount") == 0
    assert await _as(test_user_2, delete, {"d": public_doc}, result="rowcount") == 0
    assert await _as(admin_user, delete, {"d": private_doc}, result="rowcount") == 0
    assert await _as(admin_user, delete, {"d": public_doc}, result="rowcount") == 1
    assert await _as(test_user, delete, {"d": private_doc}, result="rowcount") == 1


async def test_deleting_the_document_removes_its_chunks(test_user):
    doc = await _document(test_user)
    await _chunk(test_user, doc, index=0)
    await _chunk(test_user, doc, index=1, content="Second chunk.")
    await _as(test_user, "DELETE FROM documents WHERE id = :d", {"d": doc}, result="none")
    assert await _as(test_user, COUNT, {"d": doc}) == 0


async def test_chunk_index_is_unique_per_document(test_user):
    doc = await _document(test_user)
    await _chunk(test_user, doc, index=0)
    with pytest.raises(DBAPIError):
        await _as(test_user, """INSERT INTO document_chunks (id, document_id, user_id, visibility, chunk_index,
                 content, chroma_id) VALUES (:id, :d, :u, 'private', 0, 'dup', :c)""",
                  {"id": uuid.uuid4(), "d": doc, "u": test_user.id, "c": f"{doc}:dup"}, result="none")


# ---------------------------------------------------------------- AL-205 keyword search
async def test_search_vector_supports_keyword_search_within_rls(test_user, test_user_2):
    doc = await _document(test_user)
    await _chunk(test_user, doc, content="Suspected data incidents are reported to the security desk.")
    query = ("SELECT count(*) FROM document_chunks WHERE document_id = :d "
             "AND search_vector @@ plainto_tsquery('english', 'security incident')")
    assert await _as(test_user, query, {"d": doc}) == 1    # stemming: incident ~ incidents
    assert await _as(test_user_2, query, {"d": doc}) == 0  # still filtered by RLS


# ---------------------------------------------------------------- AL-284 OCR columns
async def test_source_type_defaults_to_text(test_user):
    doc = await _document(test_user)
    chunk = await _chunk(test_user, doc)
    row = await _as(test_user, "SELECT source_type || ':' || coalesce(ocr_confidence::text, 'null') "
                               "FROM document_chunks WHERE id = :c", {"c": chunk})
    assert row == "text:null"


async def test_ocr_chunk_keeps_image_position_and_confidence(test_user, test_user_2):
    doc = await _document(test_user)
    chunk = await _chunk(test_user, doc, source_type="ocr", bbox="[10, 20, 300, 120]", ocr_confidence=0.93)
    row = await _as(test_user, "SELECT source_type || '|' || bbox::text || '|' || ocr_confidence::text "
                               "FROM document_chunks WHERE id = :c", {"c": chunk})
    assert row == "ocr|[10, 20, 300, 120]|0.93"
    assert await _as(test_user_2, "SELECT count(*) FROM document_chunks WHERE id = :c", {"c": chunk}) == 0


@pytest.mark.parametrize("bad", [{"source_type": "image"}, {"source_type": "ocr", "ocr_confidence": 1.5}])
async def test_invalid_ocr_values_are_rejected(test_user, bad):
    doc = await _document(test_user)
    with pytest.raises(DBAPIError):
        await _chunk(test_user, doc, **bad)


async def test_deleting_an_image_keeps_the_chunk(test_user):
    doc = await _document(test_user)
    image = uuid.uuid4()
    await _as(test_user, """INSERT INTO document_images (id, document_id, user_id, image_index, size_bytes, file_path, ocr_status)
              VALUES (:i, :d, :u, 0, 100, '/tmp/img.png', 'done')""", {"i": image, "d": doc, "u": test_user.id},
              result="none")
    chunk = await _chunk(test_user, doc, source_type="ocr", image_id=image, ocr_confidence=0.9)
    await _as(test_user, "DELETE FROM document_images WHERE id = :i", {"i": image}, result="none")
    assert await _as(test_user, "SELECT image_id IS NULL FROM document_chunks WHERE id = :c", {"c": chunk}) is True
