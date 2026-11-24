"""Public landing page API: guests (not signed in) ask questions over showcase documents.

GET  /api/v1/public/documents  -> names of the showcase documents guests can ask about
POST /api/v1/public/ask        -> ChatAnswer (JSON only)

Rules:
- Only documents an admin marked `showcase` (public, indexed) are searched. PostgreSQL RLS enforces it
  (guest context: empty app.user_id, app.user_role = 'guest').
- Ask-only: no files, no downloads, no ids of uploaders. Sources are short excerpts around the
  highlighted sentence (PUBLIC_SOURCE_MAX_CHARS).
- Rate limited per client IP (PUBLIC_ASK_PER_MINUTE / PUBLIC_ASK_PER_DAY); fails closed if Redis is down.
- Nothing is stored (no conversation, no message history).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy import select

from backend.app.api.deps import DbSession
from backend.app.core.config import settings
from backend.app.core.redis import get_redis
from backend.app.db.rls import configure_guest_rls_context
from backend.app.models.document import Document
from backend.app.schemas.chat import ChatAnswer, PublicAskRequest
from backend.app.services import qa, source_highlight
from backend.app.services.embedding import EmbeddingError
from backend.app.services.llm import LLMError
from backend.app.services.vector_store import DatabaseAuthorizationError, VectorStoreError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/public", tags=["Public"])

SEARCH_UNAVAILABLE = "Search is unavailable right now. Please try again in a moment."
RATE_LIMITED = "Too many questions. Please wait a moment, or sign in to keep asking."
RATE_PREFIX = "public_ask:"


class ShowcaseDocument(BaseModel):
    name: str


def _client_ip(request: Request) -> str:
    # Behind Caddy + nginx, uvicorn --proxy-headers resolves the real client address.
    return request.client.host if request.client else "unknown"


async def _check_rate_limit(ip: str) -> None:
    """Fixed windows per IP: one minute and one day. Fails closed (503) when Redis is unavailable."""
    limits = ((f"{RATE_PREFIX}m:{ip}", 60, settings.PUBLIC_ASK_PER_MINUTE),
              (f"{RATE_PREFIX}d:{ip}", 86_400, settings.PUBLIC_ASK_PER_DAY))
    try:
        redis = get_redis()
        for key, window, limit in limits:
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, window)
            if count > limit:
                ttl = await redis.ttl(key)
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=RATE_LIMITED,
                    headers={"Retry-After": str(max(1, ttl))},
                )
    except RedisError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from exc


def _require_enabled() -> None:
    if not settings.PUBLIC_ASK_ENABLED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not Found")


@router.get("/documents", response_model=list[ShowcaseDocument])
async def showcase_documents(db: DbSession) -> list[ShowcaseDocument]:
    _require_enabled()
    await configure_guest_rls_context(db)
    result = await db.execute(
        select(Document.filename)
        .where(Document.showcase.is_(True), Document.visibility == "public", Document.status == "indexed")
        .order_by(Document.filename)
    )
    return [ShowcaseDocument(name=name) for name in result.scalars().all()]


@router.post("/ask", response_model=ChatAnswer)
async def public_ask(payload: PublicAskRequest, request: Request, db: DbSession) -> ChatAnswer:
    _require_enabled()
    question = payload.question.strip()
    if not question:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Question is empty.")
    if len(question) > settings.PUBLIC_QUESTION_MAX_CHARS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Questions are limited to {settings.PUBLIC_QUESTION_MAX_CHARS} characters.",
        )
    await _check_rate_limit(_client_ip(request))

    await configure_guest_rls_context(db)
    try:
        prepared = await qa.prepare_showcase(db, question)
    except EmbeddingError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from exc
    except (VectorStoreError, DatabaseAuthorizationError) as exc:
        logger.exception("Public Q&A retrieval failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from exc
    try:
        result = await qa.answer(prepared)
    except LLMError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return source_highlight.guest_view(result, settings.PUBLIC_SOURCE_MAX_CHARS)
