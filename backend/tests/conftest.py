import uuid
import pytest
import pytest_asyncio
import httpx
from typing import AsyncGenerator
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.app.api.v1 import documents as documents_api
from backend.app.core.config import settings
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.models.document import Document
from backend.app.models.user import User

# Test engine with NullPool to prevent connection pooling across pytest event loops
test_engine = create_async_engine(
    settings.DATABASE_URL,
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

    monkeypatch.setattr(documents_api, "index_document", _noop_index_document)


@pytest.fixture(autouse=True)
def override_db_dependency():
    """Ensure every FastAPI request gets a fresh AsyncSession with NullPool."""
    async def _test_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with TestingSessionLocal() as session:
            yield session

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
    user = User(
        id=uuid.uuid4(),
        email=f"user-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="test_hashed_password_123",
        role="user",
        is_deleted=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    yield user

    try:
        user_to_delete = await db_session.get(User, user.id)
        if user_to_delete:
            await db_session.delete(user_to_delete)
            await db_session.commit()
    except Exception:
        await db_session.rollback()


@pytest_asyncio.fixture
async def test_user_2(db_session: AsyncSession) -> AsyncGenerator[User, None]:
    """Creates a second test user to verify multi-user isolation and public pool deduplication."""
    user = User(
        id=uuid.uuid4(),
        email=f"user2-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="test_hashed_password_456",
        role="user",
        is_deleted=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    yield user

    try:
        user_to_delete = await db_session.get(User, user.id)
        if user_to_delete:
            await db_session.delete(user_to_delete)
            await db_session.commit()
    except Exception:
        await db_session.rollback()


@pytest_asyncio.fixture
async def async_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Asynchronous HTTP test client bound directly to the FastAPI app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
