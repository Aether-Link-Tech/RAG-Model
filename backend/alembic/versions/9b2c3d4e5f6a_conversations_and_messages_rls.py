"""conversations + messages with owner-only RLS (FR-006 / AL-133)

Conversation memory must never leak between users: both tables are FORCE RLS with
an owner-only policy keyed on transaction-local `app.user_id`. messages.user_id is
tied to the conversation owner by a composite FK, so a message can never be attached
to someone else's conversation. Admins get no extra read access to conversations.

Revision ID: 9b2c3d4e5f6a
Revises: 9a1b2c3d4e5f
Create Date: 2026-11-08 06:30:00.000000
"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "9b2c3d4e5f6a"
down_revision: Union[str, Sequence[str], None] = "9a1b2c3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = os.getenv("APP_DB_USER", "rag_app").strip() or "rag_app"
OWNER = (
    "NULLIF(current_setting('app.user_id', true), '') IS NOT NULL "
    "AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False, server_default="New conversation"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("id", "user_id", name="uq_conversations_id_user"),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])

    op.create_table(
        "messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id", "user_id"],
            ["conversations.id", "conversations.user_id"],
            ondelete="CASCADE",
            name="fk_messages_conversation_owner",
        ),
        sa.CheckConstraint("role IN ('user', 'assistant', 'system')", name="check_message_role"),
    )
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])

    for table in ("conversations", "messages"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {APP_ROLE};")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY {table}_owner_policy ON {table}
            FOR ALL USING ({OWNER}) WITH CHECK ({OWNER});
            """
        )


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("conversations")
