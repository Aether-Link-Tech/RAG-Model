"""Public landing page API: guests (not signed in) ask questions over showcase documents.

GET  /api/v1/public/documents  -> names of the showcase documents guests can ask about
GET  /api/v1/public/limits     -> free questions left today for this visitor
POST /api/v1/public/ask        -> ChatAnswer + free_questions_left / free_questions_limit (JSON only)

Rules:
- Only documents an admin marked `showcase` (public, indexed) are searched. PostgreSQL RLS enforces it
  (guest context: empty app.user_id, app.user_role = 'guest').
- Ask-only: no files, no downloads, no ids of uploaders. Sources are short excerpts around the
  highlighted sentence (PUBLIC_SOURCE_MAX_CHARS).
- Open 24/7, but limited: PUBLIC_ASK_PER_DAY free questions per browser per day (anonymous random-id
  cookie, so people sharing one network each get their own) and a burst limit (PUBLIC_ASK_PER_MINUTE), plus a
  higher safety ceiling per IP (PUBLIC_ASK_PER_IP_DAY / PUBLIC_ASK_PER_IP_MINUTE). After that the visitor is
  asked to sign in. Questions that fail on our side are not counted. Fails closed if Redis is down.
- Nothing is stored (no conversation, no message history).
"""
from __future__ import annotations

import logging
import re
import secrets

from fastapi import APIRouter, HTTPException, Request, Response, status
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
NETWORK_LIMIT_REACHED = ("Too many free questions have been asked from your network today. "
                         "Sign in or create a free account to keep asking.")
RATE_PREFIX = "public_ask:"
VISITOR_COOKIE = "aether_guest"
VISITOR_COOKIE_MAX_AGE = 365 * 86_400
_VISITOR_RE = re.compile(r"[0-9a-f]{32}")


class ShowcaseDocument(BaseModel):
    name: str


class FreeQuestions(BaseModel):
    free_questions_left: int
    free_questions_limit: int


class PublicAnswer(ChatAnswer):
    free_questions_left: int
    free_questions_limit: int


def _client_ip(request: Request) -> str:
    # Behind Cloudflare Tunnel -> Caddy -> nginx, Caddy passes the real visitor IP (CF-Connecting-IP)
    # in X-Forwarded-For and uvicorn --proxy-headers resolves it.
    return request.client.host if request.client else "unknown"


def _visitor_id(request: Request) -> tuple[str, bool]:
    """(anonymous browser id, is_new). The cookie holds only a random id, no personal data."""
    value = request.cookies.get(VISITOR_COOKIE, "")
    if _VISITOR_RE.fullmatch(value):
        return value, False
    return secrets.token_hex(16), True


def _remember_visitor(response: Response, request: Request, visitor: str) -> None:
    response.set_cookie(
        VISITOR_COOKIE, visitor, max_age=VISITOR_COOKIE_MAX_AGE, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/api/v1/public",
    )


def _keys(visitor: str, ip: str) -> dict[str, str]:
    return {
        "visitor_minute": f"{RATE_PREFIX}m:v:{visitor}",
        "visitor_day": f"{RATE_PREFIX}d:v:{visitor}",
        "ip_minute": f"{RATE_PREFIX}m:ip:{ip}",
        "ip_day": f"{RATE_PREFIX}d:ip:{ip}",
    }


async def _hit(redis, key: str, window: int) -> int:
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window)
    return count


async def _check_rate_limit(visitor: str, ip: str) -> int:
    """Free questions per browser, with a much higher safety ceiling per IP.

    Many people can share one public IP (a university or office network), so the free allowance
    is counted per browser (anonymous cookie). Clearing cookies gives a new allowance, so the IP
    ceiling (PUBLIC_ASK_PER_IP_DAY / _PER_IP_MINUTE) caps abuse from one network.
    Returns the free questions left today for this browser. Fails closed (503) if Redis is down.
    """
    keys = _keys(visitor, ip)
    try:
        redis = get_redis()
        minute = await _hit(redis, keys["visitor_minute"], 60)
        ip_minute = await _hit(redis, keys["ip_minute"], 60)
        if minute > settings.PUBLIC_ASK_PER_MINUTE or ip_minute > settings.PUBLIC_ASK_PER_IP_MINUTE:
            key = keys["visitor_minute"] if minute > settings.PUBLIC_ASK_PER_MINUTE else keys["ip_minute"]
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail=SLOW_DOWN,
                headers={"Retry-After": str(max(1, await redis.ttl(key)))},
            )
        used = await _hit(redis, keys["visitor_day"], 86_400)
        ip_used = await _hit(redis, keys["ip_day"], 86_400)
        if used > settings.PUBLIC_ASK_PER_DAY or ip_used > settings.PUBLIC_ASK_PER_IP_DAY:
            await redis.decr(keys["visitor_day"])  # refused questions do not count
            await redis.decr(keys["ip_day"])
            detail = (FREE_LIMIT_REACHED.format(limit=settings.PUBLIC_ASK_PER_DAY)
                      if used > settings.PUBLIC_ASK_PER_DAY else NETWORK_LIMIT_REACHED)
            key = keys["visitor_day"] if used > settings.PUBLIC_ASK_PER_DAY else keys["ip_day"]
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail=detail,
                headers={"Retry-After": str(max(1, await redis.ttl(key)))},
            )
        return max(0, min(settings.PUBLIC_ASK_PER_DAY - used, settings.PUBLIC_ASK_PER_IP_DAY - ip_used))
    except RedisError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from exc


async def _refund(visitor: str, ip: str) -> None:
    """A question that failed on our side (search or model unavailable) is not counted."""
    keys = _keys(visitor, ip)
    try:
        redis = get_redis()
        await redis.decr(keys["visitor_day"])
        await redis.decr(keys["ip_day"])
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
async def free_questions(request: Request, response: Response) -> FreeQuestions:
    """How many free questions this browser has left today (does not use one)."""
    _require_enabled()
    visitor, new = _visitor_id(request)
    keys = _keys(visitor, _client_ip(request))
    try:
        redis = get_redis()
        used = int(await redis.get(keys["visitor_day"]) or 0)
        ip_used = int(await redis.get(keys["ip_day"]) or 0)
    except RedisError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from exc
    if new:
        _remember_visitor(response, request, visitor)
    limit = settings.PUBLIC_ASK_PER_DAY
    left = min(limit - used, settings.PUBLIC_ASK_PER_IP_DAY - ip_used)
    return FreeQuestions(free_questions_left=max(0, left), free_questions_limit=limit)


@router.post("/ask", response_model=PublicAnswer)
async def public_ask(payload: PublicAskRequest, request: Request, response: Response,
                     db: DbSession) -> PublicAnswer:
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
    visitor, new = _visitor_id(request)
    if new:
        _remember_visitor(response, request, visitor)
    left = await _check_rate_limit(visitor, ip)

    await configure_guest_rls_context(db)
    try:
        prepared = await qa.prepare_showcase(db, question)
        result = await qa.answer(prepared)
    except EmbeddingError as exc:
        await _refund(visitor, ip)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from exc
    except (VectorStoreError, DatabaseAuthorizationError) as exc:
        await _refund(visitor, ip)
        logger.exception("Public Q&A retrieval failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from exc
    except LLMError as exc:
        await _refund(visitor, ip)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    answer = source_highlight.guest_view(result, settings.PUBLIC_SOURCE_MAX_CHARS)
    return PublicAnswer(**answer.model_dump(), free_questions_left=left,
                        free_questions_limit=settings.PUBLIC_ASK_PER_DAY)
