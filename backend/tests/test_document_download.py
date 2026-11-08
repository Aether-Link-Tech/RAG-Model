"""AL-182 — GET /api/v1/documents/{id}/download (FR-009)."""
import io
import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.security import create_access_token
from backend.app.models.document import Document
from backend.app.models.user import User
from backend.tests.conftest import _CleanupSessionLocal


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, role=user.role)}"}


async def _upload(client, user: User, body: bytes, *, visibility="private", name="notes.txt") -> str:
    response = await client.post(
        "/api/v1/documents",
        headers=_headers(user),
        files={"file": (name, io.BytesIO(body), "text/plain")},
        data={"visibility": visibility},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _url(doc_id: str) -> str:
    return f"/api/v1/documents/{doc_id}/download"


async def test_owner_downloads_exact_bytes_as_attachment(async_client, test_user: User):
    doc_id = await _upload(async_client, test_user, b"private body bytes", name="report.txt")

    response = await async_client.get(_url(doc_id), headers=_headers(test_user))

    assert response.status_code == 200
    assert response.content == b"private body bytes"
    disposition = response.headers.get("content-disposition", "")
    assert "attachment" in disposition and "report.txt" in disposition
    assert response.headers["content-type"].startswith("text/plain")


async def test_other_users_private_document_is_404(async_client, test_user: User, test_user_2: User):
    doc_id = await _upload(async_client, test_user, b"secret body")

    response = await async_client.get(_url(doc_id), headers=_headers(test_user_2))

    assert response.status_code == 404
    assert b"secret body" not in response.content


async def test_other_users_public_document_is_downloadable(async_client, test_user: User, test_user_2: User):
    doc_id = await _upload(async_client, test_user, b"public handbook body", visibility="public", name="handbook.txt")

    response = await async_client.get(_url(doc_id), headers=_headers(test_user_2))

    assert response.status_code == 200
    assert response.content == b"public handbook body"


async def test_unknown_document_is_404(async_client, test_user: User):
    response = await async_client.get(_url(str(uuid.uuid4())), headers=_headers(test_user))
    assert response.status_code == 404


async def test_missing_token_is_401(async_client, test_user: User):
    doc_id = await _upload(async_client, test_user, b"x body")
    response = await async_client.get(_url(doc_id))
    assert response.status_code == 401


async def test_file_missing_on_disk_is_404(async_client, test_user: User, db_session: AsyncSession):
    doc_id = await _upload(async_client, test_user, b"will be removed")
    document = await db_session.get(Document, uuid.UUID(doc_id))
    import os
    os.remove(document.file_path)

    response = await async_client.get(_url(doc_id), headers=_headers(test_user))
    assert response.status_code == 404


async def test_stored_path_outside_upload_dir_is_never_served(async_client, test_user: User, tmp_path):
    """Defence in depth: even a tampered file_path outside UPLOAD_DIR is refused."""
    doc_id = await _upload(async_client, test_user, b"normal body")
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("must never be served", encoding="utf-8")

    # file_path is set server-side only; simulate DB tampering with the privileged role.
    async with _CleanupSessionLocal() as admin_session:
        await admin_session.execute(
            update(Document).where(Document.id == uuid.UUID(doc_id)).values(file_path=str(outside))
        )
        await admin_session.commit()

    response = await async_client.get(_url(doc_id), headers=_headers(test_user))
    assert response.status_code == 404
    assert b"must never be served" not in response.content
