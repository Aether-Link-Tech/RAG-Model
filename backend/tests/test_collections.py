"""FR-012 / AL-147: per-user collections; AL-139 storage layer; AL-140 metadata insert."""
import io
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.core.security import create_access_token
from backend.app.db.rls import configure_rls_context
from backend.app.models.collection import Collection
from backend.app.models.document import Document
from backend.app.models.user import User
from backend.app.services.document_ingest import (
    UnsafeStoragePathError,
    build_storage_path,
)


def _h(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, role=user.role)}"}


async def _create(client, user: User, name: str):
    return await client.post("/api/v1/collections", json={"name": name}, headers=_h(user))


async def _upload(client, user: User, *, collection_id: str | None = None, visibility: str = "private"):
    data = {"visibility": visibility}
    if collection_id is not None:
        data["collection_id"] = collection_id
    return await client.post(
        "/api/v1/documents",
        headers=_h(user),
        data=data,
        files={"file": ("notes.txt", io.BytesIO(f"content {uuid.uuid4()}".encode()), "text/plain")},
    )


# --- CRUD ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_list_rename_collection(async_client, test_user: User):
    created = await _create(async_client, test_user, "  Research   Papers ")
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Research Papers"
    assert body["document_count"] == 0
    cid = body["id"]

    listed = await async_client.get("/api/v1/collections", headers=_h(test_user))
    assert listed.status_code == 200
    names = [c["name"] for c in listed.json()]
    assert names == ["Research Papers", "Uncategorized"]
    assert listed.json()[-1]["id"] is None

    renamed = await async_client.put(
        f"/api/v1/collections/{cid}", json={"name": "Papers"}, headers=_h(test_user)
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Papers"

    fetched = await async_client.get(f"/api/v1/collections/{cid}", headers=_h(test_user))
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Papers"


@pytest.mark.asyncio
async def test_collection_name_rules(async_client, test_user: User):
    assert (await _create(async_client, test_user, "Dup")).status_code == 201
    assert (await _create(async_client, test_user, "Dup")).status_code == 409
    for bad in ("", "   ", "Uncategorized", "uncategorized", "x" * 101):
        assert (await _create(async_client, test_user, bad)).status_code == 422, bad

    other = await _create(async_client, test_user, "Other")
    clash = await async_client.put(
        f"/api/v1/collections/{other.json()['id']}", json={"name": "Dup"}, headers=_h(test_user)
    )
    assert clash.status_code == 409


@pytest.mark.asyncio
async def test_same_name_allowed_for_different_users(async_client, test_user: User, test_user_2: User):
    assert (await _create(async_client, test_user, "Shared Name")).status_code == 201
    assert (await _create(async_client, test_user_2, "Shared Name")).status_code == 201


@pytest.mark.asyncio
async def test_collections_require_authentication(async_client):
    assert (await async_client.get("/api/v1/collections")).status_code == 401
    assert (await async_client.post("/api/v1/collections", json={"name": "x"})).status_code == 401


# --- Isolation ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_users_collections_are_invisible(async_client, test_user: User, test_user_2: User):
    cid = (await _create(async_client, test_user, "Private Stuff")).json()["id"]

    listed = await async_client.get("/api/v1/collections", headers=_h(test_user_2))
    assert [c["name"] for c in listed.json()] == ["Uncategorized"]

    for method, kwargs in (
        ("get", {}),
        ("put", {"json": {"name": "Hijacked"}}),
        ("delete", {}),
    ):
        res = await getattr(async_client, method)(
            f"/api/v1/collections/{cid}", headers=_h(test_user_2), **kwargs
        )
        assert res.status_code == 404, method

    # Nor can they put documents into it.
    upload = await _upload(async_client, test_user_2, collection_id=cid)
    assert upload.status_code == 404

    still = await async_client.get(f"/api/v1/collections/{cid}", headers=_h(test_user))
    assert still.json()["name"] == "Private Stuff"


@pytest.mark.asyncio
async def test_collections_rls_blocks_cross_user_rows(
    db_session: AsyncSession, test_user: User, test_user_2: User
):
    owner_id, other_id = test_user.id, test_user_2.id
    await db_session.rollback()
    await configure_rls_context(db_session, owner_id, "user")
    db_session.add(Collection(user_id=owner_id, name="RLS Owned"))
    await db_session.commit()

    await configure_rls_context(db_session, other_id, "admin")
    rows = (await db_session.execute(select(Collection).where(Collection.name == "RLS Owned"))).all()
    assert rows == []  # not even admins see other users' collections

    db_session.add(Collection(user_id=owner_id, name="Forged"))
    with pytest.raises((DBAPIError, IntegrityError)):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_database_rejects_document_in_foreign_collection(
    async_client, db_session: AsyncSession, test_user: User, test_user_2: User
):
    """The composite FK stops a document from pointing at another user's collection."""
    foreign_cid = uuid.UUID((await _create(async_client, test_user_2, "Theirs")).json()["id"])
    doc_id = uuid.UUID((await _upload(async_client, test_user)).json()["id"])
    owner_id = test_user.id

    await db_session.rollback()
    await configure_rls_context(db_session, owner_id, "user")
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("UPDATE documents SET collection_id = :cid WHERE id = :id"),
            {"cid": foreign_cid, "id": doc_id},
        )
    await db_session.rollback()


# --- Documents in collections -----------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_into_collection_and_move_between_collections(async_client, test_user: User):
    a = (await _create(async_client, test_user, "A")).json()["id"]
    b = (await _create(async_client, test_user, "B")).json()["id"]

    up = await _upload(async_client, test_user, collection_id=a)
    assert up.status_code == 201
    doc = up.json()
    assert doc["collection_id"] == a

    moved = await async_client.put(
        f"/api/v1/documents/{doc['id']}", data={"collection_id": b}, headers=_h(test_user)
    )
    assert moved.status_code == 202
    assert moved.json()["collection_id"] == b
    assert moved.json()["filename"] == "notes.txt"

    in_b = await async_client.get("/api/v1/documents", params={"collection_id": b}, headers=_h(test_user))
    assert [d["id"] for d in in_b.json()] == [doc["id"]]
    in_a = await async_client.get("/api/v1/documents", params={"collection_id": a}, headers=_h(test_user))
    assert in_a.json() == []

    uncategorized = await async_client.put(
        f"/api/v1/documents/{doc['id']}", data={"collection_id": "uncategorized"}, headers=_h(test_user)
    )
    assert uncategorized.json()["collection_id"] is None
    loose = await async_client.get(
        "/api/v1/documents", params={"collection_id": "uncategorized"}, headers=_h(test_user)
    )
    assert doc["id"] in [d["id"] for d in loose.json()]


@pytest.mark.asyncio
async def test_move_and_rename_together(async_client, test_user: User):
    cid = (await _create(async_client, test_user, "Target")).json()["id"]
    doc = (await _upload(async_client, test_user)).json()
    res = await async_client.put(
        f"/api/v1/documents/{doc['id']}",
        data={"collection_id": cid, "filename": "renamed.txt"},
        headers=_h(test_user),
    )
    assert res.status_code == 202
    assert res.json()["collection_id"] == cid
    assert res.json()["filename"] == "renamed.txt"


@pytest.mark.asyncio
async def test_bad_collection_ids_are_rejected(async_client, test_user: User):
    doc = (await _upload(async_client, test_user)).json()
    bad = await async_client.put(
        f"/api/v1/documents/{doc['id']}", data={"collection_id": "not-a-uuid"}, headers=_h(test_user)
    )
    assert bad.status_code == 422
    missing = await async_client.put(
        f"/api/v1/documents/{doc['id']}", data={"collection_id": str(uuid.uuid4())}, headers=_h(test_user)
    )
    assert missing.status_code == 404
    assert (await _upload(async_client, test_user, collection_id=str(uuid.uuid4()))).status_code == 404


@pytest.mark.asyncio
async def test_cannot_move_someone_elses_public_document(async_client, test_user: User, test_user_2: User):
    their_doc = (await _upload(async_client, test_user_2, visibility="public")).json()
    mine = (await _create(async_client, test_user, "Mine")).json()["id"]
    res = await async_client.put(
        f"/api/v1/documents/{their_doc['id']}", data={"collection_id": mine}, headers=_h(test_user)
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_collection_id_hidden_on_other_users_public_documents(
    async_client, test_user: User, test_user_2: User
):
    cid = (await _create(async_client, test_user_2, "Their Folder")).json()["id"]
    doc = (await _upload(async_client, test_user_2, collection_id=cid, visibility="public")).json()
    seen = await async_client.get(f"/api/v1/documents/{doc['id']}", headers=_h(test_user))
    assert seen.status_code == 200
    assert seen.json()["collection_id"] is None


@pytest.mark.asyncio
async def test_deleting_collection_moves_documents_to_uncategorized(
    async_client, test_user: User
):
    cid = (await _create(async_client, test_user, "Temporary")).json()["id"]
    d1 = (await _upload(async_client, test_user, collection_id=cid)).json()["id"]
    d2 = (await _upload(async_client, test_user, collection_id=cid, visibility="public")).json()["id"]

    counted = await async_client.get(f"/api/v1/collections/{cid}", headers=_h(test_user))
    assert counted.json()["document_count"] == 2

    deleted = await async_client.delete(f"/api/v1/collections/{cid}", headers=_h(test_user))
    assert deleted.status_code == 204
    assert (await async_client.get(f"/api/v1/collections/{cid}", headers=_h(test_user))).status_code == 404

    for doc_id in (d1, d2):
        doc = await async_client.get(f"/api/v1/documents/{doc_id}", headers=_h(test_user))
        assert doc.status_code == 200  # documents are kept
        assert doc.json()["collection_id"] is None

    listing = await async_client.get("/api/v1/collections", headers=_h(test_user))
    assert listing.json()[-1] == {
        "id": None,
        "name": "Uncategorized",
        "document_count": 2,
        "created_at": None,
        "updated_at": None,
    }


# --- AL-139: per-user storage layer --------------------------------------------------------


def test_storage_path_is_inside_the_users_namespace(isolate_storage):
    user_id, doc_id = uuid.uuid4(), uuid.uuid4()
    path = Path(build_storage_path(user_id, doc_id, "Report.PDF"))
    assert path == Path(settings.UPLOAD_DIR) / "users" / str(user_id) / "documents" / f"{doc_id}.pdf"
    assert path.parent.is_dir()


@pytest.mark.parametrize(
    "filename",
    ["../../etc/passwd", "..\\..\\evil.txt", "a.pdf/../../x", "/abs/path.txt", "evil.txt\x00.pdf"],
)
def test_storage_path_ignores_traversal_in_filenames(isolate_storage, filename):
    user_id, doc_id = uuid.uuid4(), uuid.uuid4()
    user_dir = (Path(settings.UPLOAD_DIR) / "users" / str(user_id) / "documents").resolve()
    try:
        path = Path(build_storage_path(user_id, doc_id, filename)).resolve()
    except UnsafeStoragePathError:
        return
    assert path.parent == user_dir
    assert path.name.startswith(str(doc_id))


@pytest.mark.parametrize("bad_id", ["../other-user", "..", "x/../../y"])
def test_storage_path_rejects_non_uuid_ids(isolate_storage, bad_id):
    with pytest.raises(ValueError):
        build_storage_path(bad_id, uuid.uuid4(), "a.txt")
    with pytest.raises(ValueError):
        build_storage_path(uuid.uuid4(), bad_id, "a.txt")


@pytest.mark.asyncio
async def test_uploaded_file_lands_in_user_namespace(async_client, db_session, test_user: User):
    res = await async_client.post(
        "/api/v1/documents",
        headers=_h(test_user),
        data={"visibility": "private"},
        files={"file": ("../../escape.txt", io.BytesIO(f"x {uuid.uuid4()}".encode()), "text/plain")},
    )
    assert res.status_code == 201
    doc = await db_session.get(Document, uuid.UUID(res.json()["id"]))
    stored = Path(doc.file_path).resolve()
    user_dir = (Path(settings.UPLOAD_DIR) / "users" / str(test_user.id) / "documents").resolve()
    assert stored.parent == user_dir
    assert stored.name == f"{doc.id}.txt"
    assert doc.filename == "escape.txt"


# --- AL-140: metadata row ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_records_full_metadata(async_client, db_session, test_user: User):
    content = f"metadata check {uuid.uuid4()}".encode()
    res = await async_client.post(
        "/api/v1/documents",
        headers=_h(test_user),
        data={"visibility": "private"},
        files={"file": ("meta.txt", io.BytesIO(content), "text/plain")},
    )
    assert res.status_code == 201
    doc = await db_session.get(Document, uuid.UUID(res.json()["id"]))
    import hashlib

    assert doc.user_id == test_user.id
    assert doc.filename == "meta.txt"
    assert doc.mime_type == "text/plain"
    assert doc.file_size == len(content)
    assert doc.file_hash == hashlib.sha256(content).hexdigest()
    assert doc.visibility == "private"
    assert doc.status in ("pending", "processing", "indexed")
    assert doc.created_at is not None


@pytest.mark.asyncio
async def test_failed_db_insert_removes_the_written_file(async_client, test_user: User, monkeypatch):
    """AL-140: the file write is rolled back when the metadata insert fails."""
    from backend.app.api.v1 import documents as documents_api

    original_commit = AsyncSession.commit

    async def _failing_commit(self):
        if any(isinstance(o, Document) for o in self.new):
            raise IntegrityError("INSERT INTO documents", {}, Exception("simulated failure"))
        return await original_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", _failing_commit)
    res = await _upload(async_client, test_user)
    assert res.status_code == 400

    user_dir = Path(settings.UPLOAD_DIR) / "users" / str(test_user.id) / "documents"
    leftovers = list(user_dir.glob("*")) if user_dir.exists() else []
    assert leftovers == []
