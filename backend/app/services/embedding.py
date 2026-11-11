import asyncio
from typing import Any, Sequence

import httpx

from backend.app.core.config import settings


class EmbeddingError(RuntimeError):
    pass


async def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """Embed texts with Ollama in bounded batches, preserving input order."""
    if not texts:
        return []

    batch_size = settings.EMBEDDING_BATCH_SIZE
    if not 10 <= batch_size <= 50:
        raise EmbeddingError("EMBEDDING_BATCH_SIZE must be between 10 and 50.")

    embeddings: list[list[float]] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for start in range(0, len(texts), batch_size):
            batch = list(texts[start : start + batch_size])
            response = await _post_with_retries(
                client,
                "/api/embed",
                {"model": settings.EMBEDDING_MODEL, "input": batch},
            )

            batch_embeddings = _parse_batch_response(response, len(batch))
            if batch_embeddings is None:
                batch_embeddings = await _embed_individually(client, batch)
            embeddings.extend(batch_embeddings)
    return embeddings


async def _post_with_retries(
    client: httpx.AsyncClient,
    endpoint: str,
    payload: dict[str, Any],
) -> httpx.Response:
    retries = settings.EMBEDDING_MAX_RETRIES
    if retries < 0:
        raise EmbeddingError("EMBEDDING_MAX_RETRIES must not be negative.")
    if settings.EMBEDDING_RETRY_BASE_DELAY < 0:
        raise EmbeddingError("EMBEDDING_RETRY_BASE_DELAY must not be negative.")

    for attempt in range(retries + 1):
        try:
            response = await client.post(
                f"{settings.OLLAMA_BASE_URL}{endpoint}",
                json=payload,
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            if attempt == retries:
                raise EmbeddingError(
                    f"Ollama embedding request failed after {retries} retries."
                ) from exc
            await asyncio.sleep(settings.EMBEDDING_RETRY_BASE_DELAY * (2**attempt))
            continue

        if response.status_code >= 500:
            if attempt == retries:
                raise EmbeddingError(
                    f"Ollama embedding request failed with HTTP "
                    f"{response.status_code} after {retries} retries."
                )
            await asyncio.sleep(settings.EMBEDDING_RETRY_BASE_DELAY * (2**attempt))
            continue
        return response

    raise EmbeddingError("Ollama embedding request failed after retries.")


def _validate_vector(vector: Any) -> list[float]:
    if not isinstance(vector, list):
        raise EmbeddingError("Ollama returned an invalid embedding vector.")
    if len(vector) != settings.CHROMA_EMBEDDING_DIM:
        raise EmbeddingError(
            f"Ollama returned an embedding with {len(vector)} dimensions; "
            f"expected {settings.CHROMA_EMBEDDING_DIM}."
        )
    try:
        return [float(value) for value in vector]
    except (TypeError, ValueError) as exc:
        raise EmbeddingError("Ollama returned a non-numeric embedding vector.") from exc


def _parse_batch_response(
    response: httpx.Response,
    expected_count: int,
) -> list[list[float]] | None:
    if response.status_code != 200:
        if response.status_code >= 400 and response.status_code < 500:
            return None
        raise EmbeddingError(
            f"Ollama batch embedding failed ({response.status_code})."
        )
    try:
        vectors = response.json().get("embeddings")
    except (ValueError, AttributeError) as exc:
        raise EmbeddingError("Ollama returned an invalid batch embedding response.") from exc
    if not isinstance(vectors, list) or len(vectors) != expected_count:
        return None
    return [_validate_vector(vector) for vector in vectors]


async def _embed_individually(
    client: httpx.AsyncClient,
    texts: Sequence[str],
) -> list[list[float]]:
    embeddings = []
    for text in texts:
        response = await _post_with_retries(
            client,
            "/api/embeddings",
            {"model": settings.EMBEDDING_MODEL, "prompt": text},
        )
        if response.status_code != 200:
            raise EmbeddingError(
                f"Ollama embedding failed ({response.status_code}): {response.text}"
            )
        try:
            vector = response.json().get("embedding")
        except (ValueError, AttributeError) as exc:
            raise EmbeddingError("Ollama returned an invalid embedding response.") from exc
        embeddings.append(_validate_vector(vector))
    return embeddings


async def embed_query(text: str) -> list[float]:
    vectors = await embed_texts([text])
    return vectors[0]
