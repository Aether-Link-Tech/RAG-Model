from typing import Sequence

import httpx

from backend.app.core.config import settings


class EmbeddingError(RuntimeError):
    pass


async def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    if not texts:
        return []

    payload_texts = list(texts)
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{settings.OLLAMA_BASE_URL}/api/embed",
            json={"model": settings.EMBEDDING_MODEL, "input": payload_texts},
        )
        if response.status_code == 200:
            data = response.json()
            embeddings = data.get("embeddings")
            if embeddings and len(embeddings) == len(payload_texts):
                return embeddings

        embeddings: list[list[float]] = []
        for text in payload_texts:
            single = await client.post(
                f"{settings.OLLAMA_BASE_URL}/api/embeddings",
                json={"model": settings.EMBEDDING_MODEL, "prompt": text},
            )
            if single.status_code != 200:
                raise EmbeddingError(
                    f"Ollama embedding failed ({single.status_code}): {single.text}"
                )
            vector = single.json().get("embedding")
            if not vector:
                raise EmbeddingError("Ollama returned an empty embedding")
            embeddings.append(vector)
        return embeddings


async def embed_query(text: str) -> list[float]:
    vectors = await embed_texts([text])
    return vectors[0]
