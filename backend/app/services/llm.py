"""Local LLM client (FR-031 / AL-294): Ollama chat API, model settings.LLM_MODEL (qwen3:4b).

Documents never leave the machine: Ollama runs on the host (http://host.docker.internal:11434
from the backend container). Network or model failures surface as LLMError with a message that
is safe to show to users.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Sequence

import httpx

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

UNAVAILABLE = "The answer service is unavailable right now. Please try again in a moment."


class LLMError(RuntimeError):
    """User-safe failure of the answer model."""


def _payload(messages: Sequence[dict[str, str]], stream: bool) -> dict:
    return {
        "model": settings.LLM_MODEL,
        "messages": list(messages),
        "stream": stream,
        "think": False,  # qwen3: no hidden reasoning block, straight to the answer
        "options": {"temperature": settings.LLM_TEMPERATURE},
    }


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(settings.LLM_TIMEOUT_SECONDS, connect=10.0)


async def complete(messages: Sequence[dict[str, str]], retries: int = 2) -> str:
    """Whole answer in one response."""
    url = f"{settings.OLLAMA_BASE_URL}/api/chat"
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                response = await client.post(url, json=_payload(messages, stream=False))
            if response.status_code == 404:
                logger.error("LLM model %s is not pulled in Ollama", settings.LLM_MODEL)
                raise LLMError(UNAVAILABLE)
            response.raise_for_status()
            return (response.json().get("message") or {}).get("content", "")
        except LLMError:
            raise
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            if attempt == retries:
                logger.warning("LLM unreachable after %d retries: %s", retries, exc)
                raise LLMError(UNAVAILABLE) from exc
            await asyncio.sleep(0.5 * 2**attempt)
        except (httpx.HTTPError, ValueError) as exc:
            logger.exception("LLM request failed")
            raise LLMError(UNAVAILABLE) from exc
    raise LLMError(UNAVAILABLE)


async def stream(messages: Sequence[dict[str, str]]) -> AsyncIterator[str]:
    """Answer text piece by piece as Ollama produces it (newline-delimited JSON)."""
    url = f"{settings.OLLAMA_BASE_URL}/api/chat"
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            async with client.stream("POST", url, json=_payload(messages, stream=True)) as response:
                if response.status_code != 200:
                    logger.error("LLM stream failed with HTTP %s", response.status_code)
                    raise LLMError(UNAVAILABLE)
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    piece = (data.get("message") or {}).get("content", "")
                    if piece:
                        yield piece
                    if data.get("done"):
                        return
    except LLMError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("LLM stream interrupted: %s", exc)
        raise LLMError(UNAVAILABLE) from exc
