from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.app.core.config import settings
from backend.app.services import document_ingest, embedding
from backend.app.services.embedding import EmbeddingError, embed_texts


def _vector(value: float = 1.0, size: int | None = None) -> list[float]:
    return [value] * (settings.CHROMA_EMBEDDING_DIM if size is None else size)


def _mock_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        embedding.httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=transport, **kwargs),
    )


@pytest.fixture(autouse=True)
def embedding_settings(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_BATCH_SIZE", 32)
    monkeypatch.setattr(settings, "EMBEDDING_MAX_RETRIES", 3)
    monkeypatch.setattr(settings, "EMBEDDING_RETRY_BASE_DELAY", 0.5)
    monkeypatch.setattr(settings, "CHROMA_EMBEDDING_DIM", 768)
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://ollama.test")


@pytest.mark.asyncio
async def test_seventy_five_texts_use_three_batches_and_preserve_order(monkeypatch) -> None:
    batch_sizes = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = request.read()
        import json

        payload = json.loads(inputs)
        batch_sizes.append(len(payload["input"]))
        return httpx.Response(
            200,
            json={
                "embeddings": [
                    _vector(float(int(text.removeprefix("text-"))))
                    for text in payload["input"]
                ]
            },
        )

    _mock_client(monkeypatch, handler)
    texts = [f"text-{index}" for index in range(75)]

    result = await embed_texts(texts)

    assert batch_sizes == [32, 32, 11]
    assert [vector[0] for vector in result] == [float(i) for i in range(75)]


@pytest.mark.asyncio
async def test_retry_succeeds_after_two_server_failures(monkeypatch) -> None:
    calls = 0
    sleep = AsyncMock()
    monkeypatch.setattr(embedding.asyncio, "sleep", sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"embeddings": [_vector()]})

    _mock_client(monkeypatch, handler)

    assert await embed_texts(["hello"]) == [_vector()]
    assert calls == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]


@pytest.mark.asyncio
async def test_retry_gives_up_after_max_retries(monkeypatch) -> None:
    calls = 0
    monkeypatch.setattr(embedding.asyncio, "sleep", AsyncMock())

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    _mock_client(monkeypatch, handler)
    monkeypatch.setattr(settings, "EMBEDDING_MAX_RETRIES", 2)

    with pytest.raises(EmbeddingError, match="after 2 retries"):
        await embed_texts(["hello"])
    assert calls == 3


@pytest.mark.asyncio
async def test_connection_error_retries_and_can_succeed(monkeypatch) -> None:
    calls = 0
    monkeypatch.setattr(embedding.asyncio, "sleep", AsyncMock())

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"embeddings": [_vector()]})

    _mock_client(monkeypatch, handler)

    assert await embed_texts(["hello"]) == [_vector()]
    assert calls == 3


@pytest.mark.asyncio
async def test_client_error_is_not_retried(monkeypatch) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(400, text="invalid request")

    _mock_client(monkeypatch, handler)
    sleep = AsyncMock()
    monkeypatch.setattr(embedding.asyncio, "sleep", sleep)

    with pytest.raises(EmbeddingError, match="400"):
        await embed_texts(["hello"])

    assert calls == ["/api/embed", "/api/embeddings"]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_vector_dimension_raises_embedding_error(monkeypatch) -> None:
    _mock_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"embeddings": [_vector(size=12)]},
        ),
    )

    with pytest.raises(EmbeddingError, match="12 dimensions; expected 768"):
        await embed_texts(["hello"])


@pytest.mark.asyncio
async def test_document_ingest_metadata_contains_embedding_model(monkeypatch) -> None:
    captured = {}
    document = SimpleNamespace(
        id="document-id",
        user_id="user-id",
        visibility="private",
        file_path="document.txt",
        filename="document.txt",
        mime_type="text/plain",
        status="pending",
        error_message=None,
    )
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())

    monkeypatch.setattr(document_ingest.asyncio, "to_thread", AsyncMock(return_value="text"))
    monkeypatch.setattr(document_ingest.near_duplicate, "shingles", lambda text: [])
    monkeypatch.setattr(document_ingest, "split_text", lambda text: ["chunk"])
    monkeypatch.setattr(document_ingest, "embed_texts", AsyncMock(return_value=[_vector()]))
    monkeypatch.setattr(
        document_ingest,
        "upsert_chunks",
        lambda **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(settings, "EMBEDDING_MODEL_VERSION", "nomic-embed-text:v1.5")

    await document_ingest.index_document(db, document)

    assert captured["metadatas"][0]["embedding_model"] == "nomic-embed-text:v1.5"


@pytest.mark.asyncio
async def test_empty_text_list_returns_empty_without_http(monkeypatch) -> None:
    def unexpected_client(**kwargs):
        raise AssertionError("HTTP client should not be created")

    monkeypatch.setattr(embedding.httpx, "AsyncClient", unexpected_client)

    assert await embed_texts([]) == []
