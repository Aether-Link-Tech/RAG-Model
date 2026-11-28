"""Persistent conversation and message store (FR-006 / FR-037).

All reads and writes run under the caller's PostgreSQL RLS context (`SET LOCAL ROLE rag_app`,
`app.user_id`, `app.user_role`) AND carry explicit `user_id == current_user.id` filters for
defense-in-depth. Unknown or foreign conversation IDs always surface as `ConversationNotFoundError`
(HTTP 404), never leaking whether another user owns that UUID.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.rls import configure_rls_context
from backend.app.models.conversation import Conversation, Message
from backend.app.schemas.chat import (
    ChatAnswer,
    ChatSource,
    ClaimCheck,
    Comparison,
    ConversationDetail,
    ConversationMessage,
    ConversationSummary,
)

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 200
DEFAULT_TITLE = "New conversation"
NOT_FOUND = "Conversation not found"
PERSISTENCE_UNAVAILABLE = "Conversation history is unavailable right now. Please try again in a moment."


class ConversationNotFoundError(LookupError):
    """Raised when a conversation does not exist or belongs to another user (maps to 404)."""


class ConversationPersistenceError(RuntimeError):
    """User-safe failure when saving or reading conversation history."""


def derive_title(first_message: str) -> str:
    """Build a concise conversation title from the first user message (<= 200 chars)."""
    collapsed = " ".join((first_message or "").split()).strip()
    if not collapsed:
        return DEFAULT_TITLE
    if len(collapsed) <= MAX_TITLE_LENGTH:
        return collapsed
    return collapsed[: MAX_TITLE_LENGTH - 1].rstrip() + "…"


def serialize_answer_metadata(answer: ChatAnswer) -> dict[str, Any]:
    """Extract only safe UI-reconstruction metadata from a completed ChatAnswer.

    Never stores user_id, internal filesystem paths, or raw storage identifiers.
    """
    return {
        "sources": [s.model_dump(mode="json") for s in answer.sources],
        "insufficient": bool(answer.insufficient),
        "uncertain": bool(answer.uncertain),
        "uncertain_reason": answer.uncertain_reason,
        "claims": [c.model_dump(mode="json") for c in answer.claims],
        "removed_claims": int(answer.removed_claims),
        "injection_detected": bool(answer.injection_detected),
        "comparison": answer.comparison.model_dump(mode="json") if answer.comparison else None,
    }


def _parse_sources(raw_sources: Any) -> list[ChatSource]:
    if not isinstance(raw_sources, list):
        return []
    parsed: list[ChatSource] = []
    for idx, item in enumerate(raw_sources, start=1):
        if not isinstance(item, dict):
            continue
        try:
            if "marker" in item and "document_name" in item and "text" in item:
                parsed.append(ChatSource.model_validate(item))
            elif "document_id" in item:
                # Backwards-compatible handling for minimal citation dicts [{document_id, filename, ...}].
                parsed.append(
                    ChatSource(
                        marker=int(item.get("marker") or idx),
                        document_id=str(item.get("document_id") or ""),
                        document_name=str(item.get("document_name") or item.get("filename") or "Document"),
                        page=item.get("page") if isinstance(item.get("page"), int) else None,
                        section=item.get("section") if isinstance(item.get("section"), str) else None,
                        source_type="ocr" if item.get("source_type") == "ocr" else "text",
                        text=str(item.get("text") or ""),
                        score=float(item.get("score") if isinstance(item.get("score"), (int, float)) else 1.0),
                        highlight=item.get("highlight") if isinstance(item.get("highlight"), str) else None,
                    )
                )
        except Exception:
            logger.debug("Skipping malformed stored citation entry", exc_info=True)
    return parsed


def unpack_message_metadata(raw: Any) -> dict[str, Any]:
    """Reconstruct answer metadata from `Message.citations` JSONB (dict, legacy list, or None)."""
    defaults: dict[str, Any] = {
        "sources": [],
        "insufficient": False,
        "uncertain": False,
        "uncertain_reason": None,
        "claims": [],
        "removed_claims": 0,
        "injection_detected": False,
        "comparison": None,
    }
    if raw is None:
        return defaults
    if isinstance(raw, list):
        defaults["sources"] = _parse_sources(raw)
        return defaults
    if not isinstance(raw, dict):
        return defaults

    defaults["sources"] = _parse_sources(raw.get("sources"))
    defaults["insufficient"] = bool(raw.get("insufficient", False))
    defaults["uncertain"] = bool(raw.get("uncertain", False))
    reason = raw.get("uncertain_reason")
    defaults["uncertain_reason"] = str(reason) if isinstance(reason, str) and reason else None
    defaults["removed_claims"] = int(raw.get("removed_claims") or 0)
    defaults["injection_detected"] = bool(raw.get("injection_detected", False))

    raw_claims = raw.get("claims")
    if isinstance(raw_claims, list):
        claims: list[ClaimCheck] = []
        for item in raw_claims:
            if isinstance(item, dict):
                try:
                    claims.append(ClaimCheck.model_validate(item))
                except Exception:
                    logger.debug("Skipping malformed stored claim check", exc_info=True)
        defaults["claims"] = claims

    raw_comp = raw.get("comparison")
    if isinstance(raw_comp, dict):
        try:
            defaults["comparison"] = Comparison.model_validate(raw_comp)
        except Exception:
            logger.debug("Skipping malformed stored comparison", exc_info=True)

    return defaults


def _to_message_schema(msg: Message) -> ConversationMessage:
    meta = unpack_message_metadata(msg.citations) if msg.role == "assistant" else unpack_message_metadata(None)
    role = msg.role if msg.role in ("user", "assistant", "system") else "assistant"
    return ConversationMessage(
        id=msg.id,
        conversation_id=msg.conversation_id,
        role=role,
        content=msg.content,
        sources=meta["sources"],
        insufficient=meta["insufficient"],
        uncertain=meta["uncertain"],
        uncertain_reason=meta["uncertain_reason"],
        claims=meta["claims"],
        removed_claims=meta["removed_claims"],
        injection_detected=meta["injection_detected"],
        comparison=meta["comparison"],
        created_at=msg.created_at,
    )


async def get_owned_conversation(
    db: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    user_role: str = "user",
) -> Conversation | None:
    await configure_rls_context(db, user_id, user_role)
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def require_owned_conversation(
    db: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    user_role: str = "user",
) -> Conversation:
    conversation = await get_owned_conversation(db, user_id, conversation_id, user_role=user_role)
    if conversation is None:
        raise ConversationNotFoundError(NOT_FOUND)
    return conversation


async def create_conversation(
    db: AsyncSession,
    user_id: uuid.UUID,
    title: str | None = None,
    *,
    user_role: str = "user",
    conversation_id: uuid.UUID | None = None,
) -> Conversation:
    """Create and persist an empty conversation owned by `user_id`."""
    try:
        await configure_rls_context(db, user_id, user_role)
        now = datetime.now(timezone.utc)
        conversation = Conversation(
            id=conversation_id or uuid.uuid4(),
            user_id=user_id,
            title=derive_title(title or ""),
            created_at=now,
            updated_at=now,
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
        return conversation
    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to create conversation for user %s", user_id)
        raise ConversationPersistenceError(PERSISTENCE_UNAVAILABLE) from exc


async def save_turn(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    user_role: str = "user",
    question: str,
    answer: ChatAnswer,
    create_if_missing: bool,
) -> tuple[Conversation, Message, Message]:
    """Atomically persist a completed (user question, assistant answer) turn.

    When `create_if_missing` is True (initial message of a new conversation), the `Conversation`
    row and both `Message` rows are inserted in one transaction. When False, `answer.conversation_id`
    must belong to `user_id` or `ConversationNotFoundError` is raised.
    """
    try:
        await configure_rls_context(db, user_id, user_role)
        user_ts = datetime.now(timezone.utc)
        assistant_ts = user_ts + timedelta(microseconds=1)

        conversation = await get_owned_conversation(
            db, user_id, answer.conversation_id, user_role=user_role
        )
        if conversation is None:
            if not create_if_missing:
                raise ConversationNotFoundError(NOT_FOUND)
            conversation = Conversation(
                id=answer.conversation_id,
                user_id=user_id,
                title=derive_title(question),
                created_at=user_ts,
                updated_at=assistant_ts,
            )
            db.add(conversation)
            await db.flush()
        else:
            # Ensure monotonic activity timestamps even if system clock ticks within the same microsecond.
            prev_ts = conversation.updated_at or conversation.created_at
            if prev_ts is not None and user_ts <= prev_ts:
                user_ts = prev_ts + timedelta(microseconds=1)
                assistant_ts = user_ts + timedelta(microseconds=1)
            conversation.updated_at = assistant_ts
            if not conversation.title or conversation.title == DEFAULT_TITLE:
                conversation.title = derive_title(question)
            await db.flush()

        user_message = Message(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            user_id=user_id,
            role="user",
            content=question,
            citations=None,
            created_at=user_ts,
        )
        assistant_message = Message(
            id=answer.message_id,
            conversation_id=conversation.id,
            user_id=user_id,
            role="assistant",
            content=answer.answer,
            citations=serialize_answer_metadata(answer),
            created_at=assistant_ts,
        )
        db.add(user_message)
        db.add(assistant_message)
        await db.commit()
        return conversation, user_message, assistant_message
    except ConversationNotFoundError:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to persist turn for conversation %s", answer.conversation_id)
        raise ConversationPersistenceError(PERSISTENCE_UNAVAILABLE) from exc


async def list_conversations(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    user_role: str = "user",
) -> list[ConversationSummary]:
    """List the caller's conversations ordered by most recent activity descending."""
    await configure_rls_context(db, user_id, user_role)
    last_activity = func.coalesce(Conversation.updated_at, Conversation.created_at)
    msg_count = (
        select(func.count(Message.id))
        .where(
            Message.conversation_id == Conversation.id,
            Message.user_id == user_id,
        )
        .correlate(Conversation)
        .scalar_subquery()
    )
    result = await db.execute(
        select(Conversation, msg_count.label("message_count"))
        .where(Conversation.user_id == user_id)
        .order_by(last_activity.desc(), Conversation.created_at.desc(), Conversation.id.desc())
    )
    summaries: list[ConversationSummary] = []
    for conv, count in result.all():
        summaries.append(
            ConversationSummary(
                id=conv.id,
                title=conv.title,
                created_at=conv.created_at,
                updated_at=conv.updated_at or conv.created_at,
                message_count=int(count or 0),
            )
        )
    return summaries


async def get_conversation_detail(
    db: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    user_role: str = "user",
) -> ConversationDetail:
    """Load one owned conversation and all of its messages in chronological order."""
    conversation = await require_owned_conversation(db, user_id, conversation_id, user_role=user_role)
    role_order = case(
        (Message.role == "user", 0),
        (Message.role == "assistant", 1),
        else_=2,
    )
    msg_result = await db.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.user_id == user_id,
        )
        .order_by(Message.created_at.asc(), role_order.asc(), Message.id.asc())
    )
    messages = [_to_message_schema(m) for m in msg_result.scalars().all()]
    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at or conversation.created_at,
        message_count=len(messages),
        messages=messages,
    )


async def delete_conversation(
    db: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    user_role: str = "user",
) -> None:
    """Delete an owned conversation (`fk_messages_conversation_owner` cascades messages)."""
    conversation = await require_owned_conversation(db, user_id, conversation_id, user_role=user_role)
    try:
        await db.delete(conversation)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to delete conversation %s", conversation_id)
        raise ConversationPersistenceError(PERSISTENCE_UNAVAILABLE) from exc
