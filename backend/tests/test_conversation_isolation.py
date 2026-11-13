"""FR-006 conversation memory isolation (AL-133, AL-183: TC-ISO-03 / TC-ISO-04).

TC-ISO-03: conversation history only loads the caller's conversations and messages.
TC-ISO-04: citations stored on messages never leak to another user.
"""
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from backend.app.db.rls import configure_rls_context
from backend.app.models.conversation import Conversation, Message
from backend.tests.conftest import TestingSessionLocal


async def _seed(owner_id) -> tuple[uuid.UUID, uuid.UUID]:
    async with TestingSessionLocal() as session:
        await configure_rls_context(session, owner_id, "user")
        conversation = Conversation(user_id=owner_id, title="Quarterly plan")
        session.add(conversation)
        await session.flush()
        message = Message(
            conversation_id=conversation.id,
            user_id=owner_id,
            role="assistant",
            content="Revenue grew 12% according to the board deck.",
            citations=[{"document_id": str(uuid.uuid4()), "chunk_id": "c-1", "filename": "board.pdf"}],
        )
        session.add(message)
        await session.commit()
        return conversation.id, message.id


@pytest.mark.asyncio
async def test_tc_iso_03_conversation_history_is_owner_only(test_user, test_user_2):
    conversation_id, message_id = await _seed(test_user.id)

    async with TestingSessionLocal() as owner:
        await configure_rls_context(owner, test_user.id, "user")
        assert (await owner.execute(select(func.count()).select_from(Message))).scalar_one() >= 1
        await owner.rollback()

    async with TestingSessionLocal() as other:
        await configure_rls_context(other, test_user_2.id, "user")
        assert (await other.get(Conversation, conversation_id)) is None
        assert (await other.get(Message, message_id)) is None
        leaked = await other.execute(select(Message).where(Message.user_id == test_user.id))
        assert leaked.scalars().all() == []
        # Updates and deletes on someone else's rows affect nothing
        result = await other.execute(
            text("UPDATE conversations SET title = 'pwned' WHERE id = :id"), {"id": conversation_id}
        )
        assert result.rowcount == 0
        result = await other.execute(text("DELETE FROM messages WHERE id = :id"), {"id": message_id})
        assert result.rowcount == 0
        await other.rollback()

    # Admins get no special access to other users' conversations
    async with TestingSessionLocal() as admin:
        await configure_rls_context(admin, test_user_2.id, "admin")
        assert (await admin.get(Conversation, conversation_id)) is None
        await admin.rollback()


@pytest.mark.asyncio
async def test_tc_iso_04_messages_and_citations_cannot_cross_users(test_user, test_user_2):
    conversation_id, _ = await _seed(test_user.id)

    async with TestingSessionLocal() as other:
        await configure_rls_context(other, test_user_2.id, "user")
        # Citations are only reachable through messages, which RLS hides
        citations = await other.execute(text("SELECT citations FROM messages WHERE citations IS NOT NULL"))
        assert all("board.pdf" not in str(row[0]) for row in citations)

        # Cannot inject a message into another user's conversation, under either owner id
        for owner in (test_user.id, test_user_2.id):
            with pytest.raises(DBAPIError):
                async with other.begin_nested():
                    other.add(Message(conversation_id=conversation_id, user_id=owner, role="user", content="hi"))
                    await other.flush()
        await other.rollback()


@pytest.mark.asyncio
async def test_unauthenticated_connections_see_no_conversations(test_user):
    await _seed(test_user.id)
    async with TestingSessionLocal() as anonymous:
        assert (await anonymous.execute(select(func.count()).select_from(Conversation))).scalar_one() == 0
        assert (await anonymous.execute(select(func.count()).select_from(Message))).scalar_one() == 0
