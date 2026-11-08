"""security_audit_log table with admin-only read access (FR-069 / AL-166)

The application role may INSERT any row (rejections are logged even when the request
fails) and SELECT only when `app.user_role = 'admin'`. There is no UPDATE grant, so
entries are append-only; DELETE is allowed only for rows past the retention period.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-11-08 06:10:00.000000
"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = os.getenv("APP_DB_USER", "rag_app").strip() or "rag_app"
RETENTION_DAYS = int(os.getenv("AUDIT_LOG_RETENTION_DAYS", "90"))


def upgrade() -> None:
    op.create_table(
        "security_audit_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_security_audit_log_created_at", "security_audit_log", ["created_at"])
    op.create_index("ix_security_audit_log_user_id", "security_audit_log", ["user_id"])

    op.execute(f"GRANT SELECT, INSERT, DELETE ON TABLE security_audit_log TO {APP_ROLE};")
    op.execute("ALTER TABLE security_audit_log ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE security_audit_log FORCE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY security_audit_log_insert_policy ON security_audit_log
        FOR INSERT WITH CHECK (true);
        """
    )
    op.execute(
        """
        CREATE POLICY security_audit_log_select_policy ON security_audit_log
        FOR SELECT USING (NULLIF(current_setting('app.user_role', true), '') = 'admin');
        """
    )
    op.execute(
        f"""
        CREATE POLICY security_audit_log_retention_delete_policy ON security_audit_log
        FOR DELETE USING (created_at < now() - interval '{RETENTION_DAYS} days');
        """
    )


def downgrade() -> None:
    op.drop_table("security_audit_log")
