"""Q&A and persistent conversation endpoints (FR-028 / FR-037).

POST   /api/v1/chat/ask                           -> ChatAnswer (JSON or SSE)
GET    /api/v1/chat/conversations                 -> list[ConversationSummary]
GET    /api/v1/chat/conversations/{conversation_id} -> ConversationDetail
DELETE /api/v1/chat/conversations/{conversation_id} -> 204 No Content

Only the caller's private passages and public passages can be used (`query_authorized_chunks`).
Conversations and messages are strictly owner-isolated via PostgreSQL FORCE RLS and explicit
`user_id == current_user.id` checks; foreign or missing conversation IDs always return 404.
Error responses never contain internal exception text.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
import logging
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import CurrentUser, DbSession
from backend.app.db.rls import configure_rls_context
from backend.app.db.session import get_db
from backend.app.schemas.chat import (
    ChatAnswer,
    ChatAskRequest,
    ConversationDetail,
    ConversationSummary,
)
from backend.app.services import conversation_store, qa
from backend.app.services.conversation_store import (
    ConversationNotFoundError,
    ConversationPersistenceError,
)
from backend.app.services.embedding import EmbeddingError
from backend.app.services.llm import UNAVAILABLE, LLMError
from backend.app.services.vector_store import DatabaseAuthorizationError, VectorStoreError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["Chat"])

SEARCH_UNAVAILABLE = "Search is unavailable right now. Please try again in a moment."


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@asynccontextmanager
async def _open_rls_session(
    request: Request,
    user_id: uuid.UUID,
    user_role: str,
) -> AsyncIterator[AsyncSession]:
    """Open a fresh AsyncSession safe to use inside a StreamingResponse generator.

    Respects `app.dependency_overrides[get_db]` so test NullPool sessions and production
    sessions follow the exact same lifecycle and RLS cleanup guarantees.
    """
    provider = request.app.dependency_overrides.get(get_db, get_db)
    gen = provider()
    try:
        session = await anext(gen)
        await configure_rls_context(session, user_id, user_role)
        yield session
    finally:
        try:
            await anext(gen)
        except StopAsyncIteration:
            pass


async def _events(
    request: Request,
    prepared: qa.PreparedQuestion,
    *,
    user_id: uuid.UUID,
    user_role: str,
    create_if_missing: bool,
) -> AsyncIterator[str]:
    try:
        async for kind, value in qa.stream_answer(prepared):
            if kind == "token":
                yield _sse("token", {"text": value})
            else:
                assert isinstance(value, ChatAnswer)
                async with _open_rls_session(request, user_id, user_role) as stream_db:
                    await conversation_store.save_turn(
                        stream_db,
                        user_id=user_id,
                        user_role=user_role,
                        question=prepared.question,
                        answer=value,
                        create_if_missing=create_if_missing,
                    )
                yield _sse("done", value.model_dump(mode="json"))
    except ConversationNotFoundError as exc:
        yield _sse("error", {"detail": str(exc)})
    except ConversationPersistenceError as exc:
        yield _sse("error", {"detail": str(exc)})
    except LLMError as exc:
        yield _sse("error", {"detail": str(exc)})
    except Exception:
        logger.exception("Unexpected error while streaming answer %s", prepared.message_id)
        yield _sse("error", {"detail": UNAVAILABLE})


@router.post("/ask", response_model=ChatAnswer)
async def ask(payload: ChatAskRequest, request: Request, current_user: CurrentUser, db: DbSession):
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Question is empty.")

    create_if_missing = payload.conversation_id is None
    if payload.conversation_id is not None:
        try:
            await conversation_store.require_owned_conversation(
                db,
                current_user.id,
                payload.conversation_id,
                user_role=current_user.role,
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        prepared = await qa.prepare(
            db,
            current_user.id,
            question,
            conversation_id=payload.conversation_id,
            include_public=payload.include_public,
            include_private=payload.include_private,
        )
    except EmbeddingError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from exc
    except (VectorStoreError, DatabaseAuthorizationError) as exc:
        logger.exception("Q&A retrieval failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from exc

    if "text/event-stream" in request.headers.get("accept", ""):
        return StreamingResponse(
            _events(
                request,
                prepared,
                user_id=current_user.id,
                user_role=current_user.role,
                create_if_missing=create_if_missing,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        result = await qa.answer(prepared)
        await conversation_store.save_turn(
            db,
            user_id=current_user.id,
            user_role=current_user.role,
            question=question,
            answer=result,
            create_if_missing=create_if_missing,
        )
        return result
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ConversationPersistenceError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(current_user: CurrentUser, db: DbSession) -> list[ConversationSummary]:
    return await conversation_store.list_conversations(
        db,
        current_user.id,
        user_role=current_user.role,
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> ConversationDetail:
    try:
        return await conversation_store.get_conversation_detail(
            db,
            current_user.id,
            conversation_id,
            user_role=current_user.role,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> Response:
    try:
        await conversation_store.delete_conversation(
            db,
            current_user.id,
            conversation_id,
            user_role=current_user.role,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ConversationPersistenceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
