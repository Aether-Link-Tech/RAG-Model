import io
import os
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text

from backend.app.api.v1 import documents as documents_api
from backend.app.core.security import create_access_token
from backend.tests.conftest import test_engine
from backend.app.tasks import document_tasks


class FakeSession:
    def __init__(self, document):
        self.document = document

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    async def get(self, model, document_id):
        if self.document is not None and self.document.id == document_id:
            return self.document
        return None

    async def rollback(self):
        return None

    async def commit(self):
        return None


@pytest.fixture
def mock_task_session(monkeypatch):
    def configure(document):
        session = FakeSession(document)
        monkeypatch.setattr(document_tasks, "AsyncSessionLocal", lambda: session)
        monkeypatch.setattr(
            document_tasks,
            "configure_rls_context",
            _async_noop,
        )
        monkeypatch.setattr(document_tasks, "clear_rls_context", lambda db: None)
        return session

    return configure


async def _async_noop(*args, **kwargs):
    return None


def _document(document_id: uuid.UUID):
    return SimpleNamespace(
        id=document_id,
        status="pending",
        error_message=None,
    )


def test_task_calls_index_document_with_requested_document(mock_task_session, monkeypatch):
    document_id = uuid.uuid4()
    user_id = uuid.uuid4()
    document = _document(document_id)
    mock_task_session(document)
    indexed = []

    async def fake_index(db, actual_document):
        indexed.append(actual_document.id)
        actual_document.status = "indexed"
        return actual_document

    monkeypatch.setattr(document_tasks, "index_document", fake_index)

    result = document_tasks.process_document.apply(
        args=[str(document_id), str(user_id), "user"]
    )

    assert result.get() is None
    assert indexed == [document_id]
    assert document.status == "indexed"


def test_task_records_and_logs_unexpected_failure(
    mock_task_session,
    monkeypatch,
    caplog,
):
    document_id = uuid.uuid4()
    user_id = uuid.uuid4()
    document = _document(document_id)
    mock_task_session(document)

    async def fail_index(db, actual_document):
        raise RuntimeError("Parser service unavailable")

    monkeypatch.setattr(document_tasks, "index_document", fail_index)

    result = document_tasks.process_document.apply(
        args=[str(document_id), str(user_id), "user"]
    )

    assert result.get() is None
    assert document.status == "failed"
    assert document.error_message == "Parser service unavailable"
    assert "Unexpected indexing failure" in caplog.text
    assert "RuntimeError: Parser service unavailable" in caplog.text


def test_task_skips_missing_document_without_crashing(mock_task_session):
    user_id = uuid.uuid4()
    mock_task_session(None)

    result = document_tasks.process_document.apply(
        args=[str(uuid.uuid4()), str(user_id), "user"]
    )

    assert result.get() is None


@pytest_asyncio.fixture
async def require_postgres():
    try:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        if os.getenv("CI"):
            pytest.fail(f"PostgreSQL required in CI but unreachable: {exc}")
        pytest.skip(f"PostgreSQL unreachable: {exc}")


@pytest.mark.asyncio
@pytest.mark.isolation
async def test_replace_cleans_old_chunks_before_dispatching_task(
    require_postgres,
    async_client,
    test_user,
    monkeypatch,
):
    events = []
    dispatches = []

    def record_dispatch(*args):
        events.append("dispatch")
        dispatches.append(args)

    monkeypatch.setattr(document_tasks.process_document, "delay", record_dispatch)
    monkeypatch.setattr(
        documents_api,
        "delete_document_chunks",
        lambda **kwargs: events.append("cleanup"),
    )
    token = create_access_token(test_user.id, role=test_user.role)
    headers = {"Authorization": f"Bearer {token}"}

    upload = await async_client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("replace.txt", io.BytesIO(b"first version"), "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    events.clear()
    dispatches.clear()

    response = await async_client.put(
        f"/api/v1/documents/{upload.json()['id']}",
        headers=headers,
        files={"file": ("replace.txt", io.BytesIO(b"second version"), "text/plain")},
    )

    assert response.status_code == 202, response.text
    assert events == ["cleanup", "dispatch"]
    assert dispatches == [
        (upload.json()["id"], str(test_user.id), test_user.role)
    ]


@pytest.mark.asyncio
@pytest.mark.isolation
async def test_upload_returns_503_and_marks_document_failed_when_queueing_fails(
    require_postgres,
    async_client,
    test_user,
    monkeypatch,
):
    def fail_dispatch(*args):
        raise RuntimeError("Redis unavailable")

    monkeypatch.setattr(document_tasks.process_document, "delay", fail_dispatch)
    token = create_access_token(test_user.id, role=test_user.role)
    headers = {"Authorization": f"Bearer {token}"}

    response = await async_client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("queue-failure.txt", io.BytesIO(b"upload payload"), "text/plain")},
    )

    assert response.status_code == 503
    assert "indexing job could not be queued" in response.json()["detail"]
    listing = await async_client.get("/api/v1/documents", headers=headers)
    assert listing.status_code == 200, listing.text
    document = next(item for item in listing.json() if item["filename"] == "queue-failure.txt")
    assert document["status"] == "failed"
    assert document["error_message"] == "Could not queue the indexing job"


@pytest.mark.asyncio
@pytest.mark.isolation
async def test_replace_returns_503_and_marks_document_failed_when_queueing_fails(
    require_postgres,
    async_client,
    test_user,
    monkeypatch,
):
    monkeypatch.setattr(document_tasks.process_document, "delay", lambda *args: None)
    token = create_access_token(test_user.id, role=test_user.role)
    headers = {"Authorization": f"Bearer {token}"}
    upload = await async_client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("replace-queue-failure.txt", io.BytesIO(b"first version"), "text/plain")},
    )
    assert upload.status_code == 201, upload.text

    def fail_dispatch(*args):
        raise RuntimeError("Redis unavailable")

    monkeypatch.setattr(document_tasks.process_document, "delay", fail_dispatch)
    response = await async_client.put(
        f"/api/v1/documents/{upload.json()['id']}",
        headers=headers,
        files={
            "file": (
                "replace-queue-failure.txt",
                io.BytesIO(b"replacement version"),
                "text/plain",
            )
        },
    )

    assert response.status_code == 503
    assert "indexing job could not be queued" in response.json()["detail"]
    document_response = await async_client.get(
        f"/api/v1/documents/{upload.json()['id']}",
        headers=headers,
    )
    assert document_response.status_code == 200, document_response.text
    assert document_response.json()["status"] == "failed"
    assert document_response.json()["error_message"] == "Could not queue the indexing job"
