import hashlib
import os
import shutil
import subprocess
import uuid
import pytest
import pytest_asyncio
import httpx
from typing import AsyncGenerator
from sqlalchemy import delete
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.app.api.v1 import documents as documents_api
from backend.app.api.v1 import users as users_api
from backend.app.core.config import settings
from backend.app.db.rls import clear_rls_context, configure_rls_context
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.models.document import Document
from backend.app.models.user import User

# Test engine with NullPool using the non-superuser `rag_app` LOGIN role
test_engine = create_async_engine(
    settings.app_database_url,
    poolclass=NullPool,
    future=True,
)

TestingSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# Privileged teardown-only engine (since `rag_app` is strictly denied hard DELETE on `users`)
_cleanup_engine = create_async_engine(
    settings.migration_database_url,
    poolclass=NullPool,
    future=True,
)
_CleanupSessionLocal = async_sessionmaker(
    bind=_cleanup_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def purge_test_users(*user_ids: uuid.UUID) -> None:
    """Teardown-only helper to hard-delete temporary test users via migration credentials."""
    if not user_ids:
        return
    async with _CleanupSessionLocal() as cleanup_session:
        await cleanup_session.execute(delete(User).where(User.id.in_(user_ids)))
        await cleanup_session.commit()


def _docker_engine_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


_DOCKER_AVAILABLE = _docker_engine_available()
# Modules that exercise the Docker sandbox themselves and must keep the real setting.
_SANDBOX_TEST_MODULES = ("test_sandboxed_parser", "test_al165_malicious_files")


@pytest.fixture(autouse=True)
def parser_sandbox_mode(request, monkeypatch):
    """Keep the suite runnable on machines without Docker (FR-067).

    In CI, or wherever a Docker engine is running, every upload is parsed in the
    real sandbox. On a laptop without Docker, non-sandbox tests fall back to
    in-process parsing instead of failing closed; sandbox tests keep their own setup.
    """
    if request.module.__name__.rsplit(".", 1)[-1] in _SANDBOX_TEST_MODULES:
        return
    if not _DOCKER_AVAILABLE and not os.getenv("CI"):
        monkeypatch.setattr(settings, "PARSER_SANDBOX_ENABLED", False)


@pytest.fixture(autouse=True)
def isolate_storage(tmp_path, monkeypatch):
    """Isolate file storage per test using a temporary directory."""
    test_storage = tmp_path / "test_uploads"
    test_storage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(test_storage))
    yield str(test_storage)


@pytest.fixture(autouse=True)
def stub_rag_indexing(monkeypatch):
    """Decouple upload/deduplication tests from external Ollama/Chroma inference."""
    async def _noop_index_document(db: AsyncSession, document: Document) -> Document:
        return document

    def _noop_delete_document_chunks(*args, **kwargs) -> None:
        return None

    def _noop_delete_user_private_knowledge(*args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(documents_api, "index_document", _noop_index_document)
    monkeypatch.setattr(documents_api, "delete_document_chunks", _noop_delete_document_chunks)
    monkeypatch.setattr(
        users_api,
        "delete_user_private_knowledge",
        _noop_delete_user_private_knowledge,
    )


@pytest.fixture(autouse=True)
def stub_clamav_scan(request, monkeypatch):
    """Upload tests must not need a live ClamAV daemon (FR-066).

    The scan is a no-op unless a test is marked `clamav_scan`, which keeps the
    real scan_and_quarantine (tests then fake the clamd client or need ClamAV).
    `raising=False` keeps this harmless before FR-066 is merged.
    """
    if request.node.get_closest_marker("clamav_scan"):
        return
    monkeypatch.setattr(documents_api, "scan_and_quarantine", lambda path: None, raising=False)


@pytest.fixture(autouse=True)
def override_db_dependency():
    """Ensure every FastAPI request gets a fresh AsyncSession with NullPool."""
    async def _test_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with TestingSessionLocal() as session:
            try:
                yield session
            finally:
                clear_rls_context(session)
                await session.close()

    app.dependency_overrides[get_db] = _test_get_db
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yields a dedicated async database session for test setup and verification."""
    async with TestingSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def test_user(db_session: AsyncSession) -> AsyncGenerator[User, None]:
    """Creates a primary test user in PostgreSQL and cleans up after the test."""
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=f"user-{user_id.hex[:8]}@example.com",
        hashed_password="test_hashed_password_123",
        role="user",
        is_deleted=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    await configure_rls_context(db_session, user_id, "user")

    yield user

    try:
        clear_rls_context(db_session)
        await db_session.rollback()
    except Exception:
        pass
    await purge_test_users(user_id)


@pytest_asyncio.fixture
async def test_user_2(db_session: AsyncSession) -> AsyncGenerator[User, None]:
    """Creates a second test user to verify multi-user isolation and public pool deduplication."""
    prior_rls_uid = db_session.sync_session.info.get("rls_user_id")
    prior_rls_role = db_session.sync_session.info.get("rls_user_role", "user")
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=f"user2-{user_id.hex[:8]}@example.com",
        hashed_password="test_hashed_password_456",
        role="user",
        is_deleted=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    if prior_rls_uid:
        await configure_rls_context(db_session, prior_rls_uid, prior_rls_role)
    else:
        await configure_rls_context(db_session, user_id, "user")

    yield user

    try:
        clear_rls_context(db_session)
        await db_session.rollback()
    except Exception:
        pass
    await purge_test_users(user_id)


FAKE_EMBEDDING_DIM = 16


def fake_embed(text: str) -> list[float]:
    """Deterministic bag-of-words embedding: shared words => nearby vectors."""
    vector = [0.0] * FAKE_EMBEDDING_DIM
    for word in text.lower().split():
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        vector[digest[0] % FAKE_EMBEDDING_DIM] += 1.0
    if not any(vector):
        vector[0] = 1.0
    return vector


@pytest.fixture
def chroma_client(monkeypatch):
    """In-memory ChromaDB replacing the HTTP client; emptied after each test."""
    import chromadb
    from backend.app.db import chroma as chroma_module

    client = chromadb.EphemeralClient()
    monkeypatch.setattr(chroma_module, "get_chroma_client", lambda: client)
    yield client
    for collection in client.list_collections():
        client.delete_collection(getattr(collection, "name", collection))


@pytest.fixture
def fake_embeddings(monkeypatch):
    """Replace Ollama embedding calls with the deterministic fake_embed."""
    from backend.app.api.v1 import retrieval as retrieval_api
    from backend.app.services import document_ingest

    async def _embed_texts(texts):
        return [fake_embed(t) for t in texts]

    async def _embed_query(text):
        return fake_embed(text)

    monkeypatch.setattr(document_ingest, "embed_texts", _embed_texts)
    monkeypatch.setattr(retrieval_api, "embed_query", _embed_query)


@pytest_asyncio.fixture
async def async_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Asynchronous HTTP test client bound directly to the FastAPI app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
