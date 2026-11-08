"""FR-012 user-specific collections (AL-147, AL-149)."""
import io
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from backend.app.db.rls import configure_rls_context
from backend.app.models.collection import Collection
from backend.tests.conftest import TestingSessionLocal, bearer


async def _upload(client, user, name="notes.txt", visibility="private"):
    res = await client.post(
        "/api/v1/documents",
        headers=bearer(user),
        data={"visibility": visibility},
        files={"file": (name, io.BytesIO(f"{name} {uuid.uuid4()}".encode()), "text/plain")},
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _create(client, user, name):
    return await client.post("/api/v1/collections", json={"name": name}, headers=bearer(user))


@pytest.mark.asyncio
async def test_collection_crud_and_counts(async_client, test_user):
    assert (await async_client.get("/api/v1/collections")).status_code == 401

    created = await _create(async_client, test_user, "  Research   papers ")
    assert created.status_code == 201
    collection = created.json()
    assert collection["name"] == "Research papers"
    assert collection["document_count"] == 0

    dup = await _create(async_client, test_user, "research PAPERS")
    assert dup.status_code == 409

    renamed = await async_client.patch(
        f"/api/v1/collections/{collection['id']}", json={"name": "Papers"}, headers=bearer(test_user)
    )
    assert renamed.status_code == 200 and renamed.json()["name"] == "Papers"

    doc_id = await _upload(async_client, test_user)
    moved = await async_client.put(
        f"/api/v1/documents/{doc_id}/collection",
        json={"collection_id": collection["id"]},
        headers=bearer(test_user),
    )
    assert moved.status_code == 200 and moved.json()["collection_id"] == collection["id"]

    listing = (await async_client.get("/api/v1/collections", headers=bearer(test_user))).json()
    assert listing == [{"id": collection["id"], "name": "Papers", "document_count": 1}]

    in_collection = await async_client.get(
        "/api/v1/documents", params={"collection": collection["id"]}, headers=bearer(test_user)
    )
    assert [d["id"] for d in in_collection.json()] == [doc_id]
    uncategorized = await async_client.get(
        "/api/v1/documents", params={"collection": "none"}, headers=bearer(test_user)
    )
    assert doc_id not in {d["id"] for d in uncategorized.json()}


@pytest.mark.asyncio
async def test_deleting_collection_moves_documents_to_uncategorized(async_client, test_user):
    collection = (await _create(async_client, test_user, "Temp")).json()
    doc_id = await _upload(async_client, test_user)
    await async_client.put(
        f"/api/v1/documents/{doc_id}/collection", json={"collection_id": collection["id"]}, headers=bearer(test_user)
    )

    deleted = await async_client.delete(f"/api/v1/collections/{collection['id']}", headers=bearer(test_user))
    assert deleted.status_code == 204

    doc = await async_client.get(f"/api/v1/documents/{doc_id}", headers=bearer(test_user))
    assert doc.status_code == 200  # document kept
    assert doc.json()["collection_id"] is None
    uncategorized = await async_client.get(
        "/api/v1/documents", params={"collection": "none"}, headers=bearer(test_user)
    )
    assert doc_id in {d["id"] for d in uncategorized.json()}


@pytest.mark.asyncio
async def test_collections_are_per_user(async_client, test_user, test_user_2):
    mine = (await _create(async_client, test_user, "Mine")).json()
    # The same name is fine for another user
    assert (await _create(async_client, test_user_2, "Mine")).status_code == 201

    theirs = (await async_client.get("/api/v1/collections", headers=bearer(test_user_2))).json()
    assert mine["id"] not in {c["id"] for c in theirs}
    for method, kwargs in (
        ("patch", {"json": {"name": "Hijack"}}),
        ("delete", {}),
    ):
        res = await getattr(async_client, method)(
            f"/api/v1/collections/{mine['id']}", headers=bearer(test_user_2), **kwargs
        )
        assert res.status_code == 404

    # User 2 cannot file their document into user 1's collection
    other_doc = await _upload(async_client, test_user_2, "theirs.txt")
    res = await async_client.put(
        f"/api/v1/documents/{other_doc}/collection", json={"collection_id": mine["id"]}, headers=bearer(test_user_2)
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_shared_documents_cannot_be_filed_by_other_users(async_client, test_user, test_user_2):
    public_doc = await _upload(async_client, test_user, "handbook.txt", visibility="public")
    owner_collection = (await _create(async_client, test_user, "Shared stuff")).json()
    await async_client.put(
        f"/api/v1/documents/{public_doc}/collection",
        json={"collection_id": owner_collection["id"]},
        headers=bearer(test_user),
    )
    other_collection = (await _create(async_client, test_user_2, "Mine")).json()

    res = await async_client.put(
        f"/api/v1/documents/{public_doc}/collection",
        json={"collection_id": other_collection["id"]},
        headers=bearer(test_user_2),
    )
    assert res.status_code == 404
    # The owner's filing is not visible to other users
    seen = await async_client.get(f"/api/v1/documents/{public_doc}", headers=bearer(test_user_2))
    assert seen.json()["collection_id"] is None


@pytest.mark.asyncio
async def test_database_enforces_collection_isolation(async_client, test_user, test_user_2):
    mine = (await _create(async_client, test_user, "RLS")).json()
    other_doc = await _upload(async_client, test_user_2, "x.txt")

    async with TestingSessionLocal() as session:
        await configure_rls_context(session, test_user_2.id, "user")
        visible = (await session.execute(select(Collection))).scalars().all()
        assert mine["id"] not in {str(c.id) for c in visible}
        # Composite FK: a document can only reference a collection of the same owner
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.execute(
                text("UPDATE documents SET collection_id = :c WHERE id = :d"),
                {"c": mine["id"], "d": other_doc},
            )
            await session.flush()
        await session.rollback()

    async with TestingSessionLocal() as session:
        await configure_rls_context(session, test_user_2.id, "user")
        with pytest.raises(DBAPIError):
            session.add(Collection(user_id=test_user.id, name="Planted"))
            await session.flush()
        await session.rollback()
