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
from backend.app.services import audit_log
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


def test_task_cleans_up_and_audits_near_duplicate(
    mock_task_session,
    monkeypatch,
    tmp_path,
    caplog,
):
    document_id = uuid.uuid4()
    user_id = uuid.uuid4()
    file_path = tmp_path / "duplicate.txt"
    file_path.write_text("duplicate content")
    document = _document(document_id)
    document.user_id = user_id
    document.filename = "duplicate.txt"
    document.file_path = str(file_path)
    document.visibility = "private"
    mock_task_session(document)

    cleanup_calls = []
    audit_calls = []

    async def reject_duplicate(db, actual_document):
        raise NearDuplicateError(
            NearDuplicateMatch(uuid.uuid4(), "existing.txt", "own", 0.9)
        )

    async def record_audit(**kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(document_tasks, "index_document", reject_duplicate)
    monkeypatch.setattr(
        document_tasks,
        "resolve_document_file_path",
        lambda owner_id, stored_path, visibility, must_exist=False: Path(stored_path),
    )
    monkeypatch.setattr(
        document_tasks,
        "remove_image_files",
        lambda owner_id, doc_id, visibility: cleanup_calls.append(
            ("images", owner_id, doc_id)
        ),
    )
    monkeypatch.setattr(
        document_tasks,
        "delete_document_chunks",
        lambda **kwargs: cleanup_calls.append(("vectors", kwargs)),
    )
    monkeypatch.setattr(audit_log, "record_security_event", record_audit)

    result = document_tasks.process_document.apply(
        args=[str(document_id), str(user_id), "user"]
    )

    assert result.get() is None
    assert document.status == "failed"
    assert document.error_message == "A similar document already exists."
    assert file_path.exists() is False
    assert cleanup_calls == [
        ("images", user_id, document_id),
        (
            "vectors",
            {
                "user_id": user_id,
                "document_id": document_id,
                "visibility": "private",
            },
        ),
    ]
    assert audit_calls == [
        {
            "event_type": audit_log.UPLOAD_REJECTED,
            "user_id": user_id,
            "filename": "duplicate.txt",
            "reason": "Near-duplicate of an existing document",
            "ip_address": None,
        }
    ]
    assert "NearDuplicateError" in caplog.text


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
async def test_near_duplicate_upload_is_queued_for_worker(
    require_postgres,
    async_client,
    test_user,
    monkeypatch,
):
    dispatches = []
    request_side_effects = []

    def record_dispatch(*args):
        dispatches.append(args)

    async def record_index(*args, **kwargs):
        request_side_effects.append("index")

    def record_cleanup(name):
        def cleanup(*args, **kwargs):
            request_side_effects.append(name)

        return cleanup

    monkeypatch.setattr(documents_api, "index_document", record_index)
    monkeypatch.setattr(
        documents_api,
        "remove_image_files",
        record_cleanup("images"),
    )
    monkeypatch.setattr(
        documents_api,
        "delete_document_chunks",
        record_cleanup("vectors"),
    )
    monkeypatch.setattr(
        documents_api,
        "log_security_event",
        record_cleanup("audit"),
    )
    monkeypatch.setattr(document_tasks.process_document, "delay", record_dispatch)
    token = create_access_token(test_user.id, role=test_user.role)
    headers = {"Authorization": f"Bearer {token}"}

    response = await async_client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("duplicate.txt", io.BytesIO(b"duplicate text"), "text/plain")},
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "pending"
    assert dispatches == [
        (response.json()["id"], str(test_user.id), test_user.role)
    ]
    assert request_side_effects == []


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


def test_task_marks_document_failed_when_retries_run_out(mock_task_session, monkeypatch):
    """Ollama down for every attempt: the document must end 'failed', never stay 'processing'."""
    from backend.app.services.embedding import EmbeddingError

    document_id = uuid.uuid4()
    document = _document(document_id)
    document.status = "processing"
    mock_task_session(document)
    attempts = []

    async def embedding_down(db, actual_document):
        attempts.append(1)
        raise EmbeddingError("Ollama unavailable")

    monkeypatch.setattr(document_tasks, "index_document", embedding_down)

    result = document_tasks.process_document.apply(args=[str(document_id), str(uuid.uuid4()), "user"])

    assert result.failed()
    assert len(attempts) == 1 + document_tasks.process_document.max_retries
    assert document.status == "failed"
    assert document.error_message == document_tasks.GAVE_UP_MESSAGE
    assert "Ollama" not in document.error_message


def test_give_up_never_overwrites_a_finished_document(mock_task_session):
    import asyncio

    document = _document(uuid.uuid4())
    document.status = "indexed"
    mock_task_session(document)
    asyncio.run(document_tasks._mark_failed(document.id, uuid.uuid4(), "user"))
    assert document.status == "indexed"


def test_worker_takes_one_job_at_a_time():
    from backend.app.core.celery_app import celery_app

    assert celery_app.conf.worker_prefetch_multiplier == 1
    compose = Path(__file__).resolve().parents[2].joinpath("docker-compose.yml").read_text(encoding="utf-8")
    import re

    worker = re.split(r"\n  (?=\S)", compose.split("container_name: rag_worker", 1)[1], maxsplit=1)[0]
    assert "--concurrency=${WORKER_CONCURRENCY:-1}" in worker
    assert "memory: ${WORKER_MEMORY:-4g}" in worker
