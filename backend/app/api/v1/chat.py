"""Q&A endpoint (FR-028 / AL-286, AL-287).

POST /api/v1/chat/ask
  Accept: application/json  -> ChatAnswer
  Accept: text/event-stream -> SSE: "token" {"text"} ..., then "done" {ChatAnswer} or "error" {"detail"}

Only the caller's private passages and public passages can be used (query_authorized_chunks).
Error responses never contain internal exception text.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from backend.app.api.deps import CurrentUser, DbSession
from backend.app.schemas.chat import ChatAnswer, ChatAskRequest
from backend.app.services import qa
from backend.app.services.embedding import EmbeddingError
from backend.app.services.llm import UNAVAILABLE, LLMError
from backend.app.services.vector_store import DatabaseAuthorizationError, VectorStoreError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["Chat"])

SEARCH_UNAVAILABLE = "Search is unavailable right now. Please try again in a moment."


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _events(prepared: qa.PreparedQuestion) -> AsyncIterator[str]:
    try:
        async for kind, value in qa.stream_answer(prepared):
            if kind == "token":
                yield _sse("token", {"text": value})
            else:
                yield _sse("done", value.model_dump(mode="json"))
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
            _events(prepared),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        return await qa.answer(prepared)
    except LLMError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
