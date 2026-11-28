import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from backend.app.core.security import create_access_token
from backend.app.models.document import Document
from backend.app.services.storage_paths import build_document_storage_path
from backend.app.tasks import document_tasks
from backend.tests.conftest import test_engine


@pytest_asyncio.fixture
async def require_postgres():
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        if os.getenv("CI"):
            pytest.fail(f"PostgreSQL required in CI but unreachable: {exc}")
        pytest.skip(f"PostgreSQL unreachable: {exc}")


def _headers(user):
    token = create_access_token(user.id, role=user.role)
    return {"Authorization": f"Bearer {token}"}


async def _create_document(db_session, user, *, visibility="private", status="failed"):
    document_id = uuid.uuid4()
    file_path = Path(
        build_document_storage_path(
            user.id,
            document_id,
            "retry.txt",
            visibility=visibility,
        )
    )
    file_path.write_text("already uploaded file", encoding="utf-8")
    document = Document(
        id=document_id,
        user_id=user.id,
        filename="retry.txt",
        file_path=str(file_path),
        mime_type="text/plain",
        file_size=file_path.stat().st_size,
        file_hash=uuid.uuid4().hex * 2,
        status=status,
        visibility=visibility,
        error_message="Previous processing failure",
    )
    db_session.add(document)
    await db_session.commit()
    await db_session.refresh(document)
    return document, file_path


@pytest.mark.asyncio
@pytest.mark.isolation
async def test_owner_retries_failed_document_and_queues_existing_file(
    require_postgres,
    async_client,
    db_session,
    test_user,
    monkeypatch,
):
    document, file_path = await _create_document(db_session, test_user)
    dispatches = []
    monkeypatch.setattr(
        document_tasks.process_document,
        "delay",
        lambda *args: dispatches.append(args),
    )

    response = await async_client.post(
        f"/api/v1/documents/{document.id}/retry",
        headers=_headers(test_user),
    )

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "processing"
    assert response.json()["error_message"] is None
    assert dispatches == [(str(document.id), str(test_user.id), test_user.role)]
    assert file_path.read_text(encoding="utf-8") == "already uploaded file"


@pytest.mark.asyncio
@pytest.mark.isolation
async def test_non_owner_cannot_retry_public_document(
    require_postgres,
    async_client,
    db_session,
    test_user,
    test_user_2,
    monkeypatch,
):
    document, _ = await _create_document(db_session, test_user, visibility="public")
    dispatches = []
    monkeypatch.setattr(
        document_tasks.process_document,
        "delay",
        lambda *args: dispatches.append(args),
    )

    response = await async_client.post(
        f"/api/v1/documents/{document.id}/retry",
        headers=_headers(test_user_2),
    )

    assert response.status_code == 404
    assert dispatches == []


@pytest.mark.asyncio
@pytest.mark.isolation
async def test_retry_queue_failure_keeps_document_failed_with_safe_message(
    require_postgres,
    async_client,
    db_session,
    test_user,
    monkeypatch,
):
    document, _ = await _create_document(db_session, test_user)

    def fail_dispatch(*args):
        raise RuntimeError("Redis credential leaked")

    monkeypatch.setattr(document_tasks.process_document, "delay", fail_dispatch)

    response = await async_client.post(
        f"/api/v1/documents/{document.id}/retry",
        headers=_headers(test_user),
    )
    detail = response.json()["detail"]
    stored = await async_client.get(
        f"/api/v1/documents/{document.id}",
        headers=_headers(test_user),
    )

    assert response.status_code == 503
    assert detail == "Document retry could not be queued. Please try again."
    assert "Redis credential" not in detail
    assert stored.status_code == 200
    assert stored.json()["status"] == "failed"
    assert stored.json()["error_message"] == "Could not queue the indexing job"


@pytest.mark.asyncio
@pytest.mark.isolation
async def test_retry_rejects_documents_that_are_not_failed(
    require_postgres,
    async_client,
    db_session,
    test_user,
    monkeypatch,
):
    document, _ = await _create_document(db_session, test_user, status="indexed")
    dispatches = []
    monkeypatch.setattr(
        document_tasks.process_document,
        "delay",
        lambda *args: dispatches.append(args),
    )

    response = await async_client.post(
        f"/api/v1/documents/{document.id}/retry",
        headers=_headers(test_user),
    )

    assert response.status_code == 409
    assert dispatches == []
