"""Public landing page API: guests (not signed in) ask questions over showcase documents.

GET  /api/v1/public/documents  -> names of the showcase documents guests can ask about
GET  /api/v1/public/limits     -> free questions left today for this visitor
POST /api/v1/public/ask        -> ChatAnswer + free_questions_left / free_questions_limit (JSON only)

Rules:
- Only documents an admin marked `showcase` (public, indexed) are searched. PostgreSQL RLS enforces it
  (guest context: empty app.user_id, app.user_role = 'guest').
- Ask-only: no files, no downloads, no ids of uploaders. Sources are short excerpts around the
  highlighted sentence (PUBLIC_SOURCE_MAX_CHARS).
- Open 24/7, but limited: PUBLIC_ASK_PER_DAY free questions per visitor (IP) per day and a burst limit
  (PUBLIC_ASK_PER_MINUTE); after that the visitor is asked to sign in. Questions that fail on our side are
  not counted. Fails closed if Redis is down.
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
SLOW_DOWN = "You are asking very quickly. Please wait a moment, or sign in to keep asking."
FREE_LIMIT_REACHED = ("You have used your {limit} free questions for today. "
                      "Sign in or create a free account to keep asking.")
RATE_PREFIX = "public_ask:"


class ShowcaseDocument(BaseModel):
    name: str


class FreeQuestions(BaseModel):
    free_questions_left: int
    free_questions_limit: int


class PublicAnswer(ChatAnswer):
    free_questions_left: int
    free_questions_limit: int


def _client_ip(request: Request) -> str:
    # Behind Caddy + nginx, uvicorn --proxy-headers resolves the real client address.
    return request.client.host if request.client else "unknown"


def _day_key(ip: str) -> str:
    return f"{RATE_PREFIX}d:{ip}"


async def _check_rate_limit(ip: str) -> int:
    """Free questions per visitor (IP): a burst limit per minute and a daily allowance.

    Returns the free questions left today. Fails closed (503) when Redis is unavailable.
    """
    try:
        redis = get_redis()
        minute_key = f"{RATE_PREFIX}m:{ip}"
        count = await redis.incr(minute_key)
        if count == 1:
            await redis.expire(minute_key, 60)
        if count > settings.PUBLIC_ASK_PER_MINUTE:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail=SLOW_DOWN,
                headers={"Retry-After": str(max(1, await redis.ttl(minute_key)))},
            )
        day_key = _day_key(ip)
        used = await redis.incr(day_key)
        if used == 1:
            await redis.expire(day_key, 86_400)
        if used > settings.PUBLIC_ASK_PER_DAY:
            await redis.decr(day_key)  # refused questions do not count
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail=FREE_LIMIT_REACHED.format(limit=settings.PUBLIC_ASK_PER_DAY),
                headers={"Retry-After": str(max(1, await redis.ttl(day_key)))},
            )
        return settings.PUBLIC_ASK_PER_DAY - used
    except RedisError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from exc


async def _refund(ip: str) -> None:
    """A question that failed on our side (search or model unavailable) is not counted."""
    try:
        await get_redis().decr(_day_key(ip))
    except RedisError:
        logger.warning("Could not refund a free question for a failed request")


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


@router.get("/limits", response_model=FreeQuestions)
async def free_questions(request: Request) -> FreeQuestions:
    """How many free questions this visitor has left today (does not use one)."""
    _require_enabled()
    try:
        used = int(await get_redis().get(_day_key(_client_ip(request))) or 0)
    except RedisError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from exc
    limit = settings.PUBLIC_ASK_PER_DAY
    return FreeQuestions(free_questions_left=max(0, limit - used), free_questions_limit=limit)


@router.post("/ask", response_model=PublicAnswer)
async def public_ask(payload: PublicAskRequest, request: Request, db: DbSession) -> PublicAnswer:
    _require_enabled()
    question = payload.question.strip()
    if not question:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Question is empty.")
    if len(question) > settings.PUBLIC_QUESTION_MAX_CHARS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Questions are limited to {settings.PUBLIC_QUESTION_MAX_CHARS} characters.",
        )
    ip = _client_ip(request)
    left = await _check_rate_limit(ip)

    await configure_guest_rls_context(db)
    try:
        prepared = await qa.prepare_showcase(db, question)
        result = await qa.answer(prepared)
    except EmbeddingError as exc:
        await _refund(ip)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from exc
    except (VectorStoreError, DatabaseAuthorizationError) as exc:
        await _refund(ip)
        logger.exception("Public Q&A retrieval failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from exc
    except LLMError as exc:
        await _refund(ip)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    answer = source_highlight.guest_view(result, settings.PUBLIC_SOURCE_MAX_CHARS)
    return PublicAnswer(**answer.model_dump(), free_questions_left=left,
                        free_questions_limit=settings.PUBLIC_ASK_PER_DAY)
