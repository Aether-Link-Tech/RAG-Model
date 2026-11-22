import io
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from backend.app.api.v1 import documents as documents_api
from backend.app.core.security import create_access_token
from backend.app.services.near_duplicate import NearDuplicateError, NearDuplicateMatch
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
        monkeypatch.setattr(document_tasks, "WorkerSessionLocal", lambda: session)
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


def test_worker_engine_uses_null_pool():
    assert isinstance(document_tasks.worker_engine.sync_engine.pool, NullPool)


def test_task_disposes_worker_engine(monkeypatch):
    events = []

    async def process_document(*args):
        events.append("process")

    async def dispose_engine():
        events.append("dispose")

    monkeypatch.setattr(document_tasks, "_process_document", process_document)
    monkeypatch.setattr(
        document_tasks,
        "worker_engine",
        SimpleNamespace(dispose=dispose_engine),
    )

    document_tasks.process_document.apply(
        args=[str(uuid.uuid4()), str(uuid.uuid4()), "user"]
    )

    assert events == ["process", "dispose"]


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
    assert document.error_message == "Processing failed. Please retry or contact support."
    assert "Parser service unavailable" not in document.error_message
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
async def test_near_duplicate_upload_is_discarded_before_celery_dispatch(
    require_postgres,
    async_client,
    test_user,
    monkeypatch,
):
    events = []
    dispatches = []
    rejected_paths = []
    document_id = uuid.uuid4()

    async def reject_duplicate(db, document):
        events.append("check")
        raise NearDuplicateError(
            NearDuplicateMatch(document_id, "existing.txt", "own", 0.9)
        )

    original_discard = documents_api._discard_rejected_upload

    async def record_cleanup(db, document):
        events.append("cleanup")
        rejected_paths.append(Path(document.file_path))
        await original_discard(db, document)

    async def record_audit(*args, **kwargs):
        events.append("audit")

    def record_dispatch(*args):
        dispatches.append(args)
        events.append("dispatch")

    monkeypatch.setattr(
        documents_api, "check_document_for_near_duplicate", reject_duplicate
    )
    monkeypatch.setattr(
        documents_api, "_discard_rejected_upload", record_cleanup
    )
    monkeypatch.setattr(documents_api, "log_security_event", record_audit)
    monkeypatch.setattr(document_tasks.process_document, "delay", record_dispatch)
    token = create_access_token(test_user.id, role=test_user.role)
    headers = {"Authorization": f"Bearer {token}"}

    response = await async_client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("duplicate.txt", io.BytesIO(b"duplicate text"), "text/plain")},
    )

    assert response.status_code == 409, response.text
    assert "existing.txt" in response.json()["detail"]
    assert events == ["check", "cleanup", "audit"]
    assert dispatches == []
    assert len(rejected_paths) == 1
    assert not rejected_paths[0].exists()


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
