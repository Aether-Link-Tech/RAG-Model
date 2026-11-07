"""create security_audit_log table (AL-166)

Revision ID: 057f400f6227
Revises: f4a7d91c3e21
Create Date: 2026-11-07 16:33:19.463956

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "057f400f6227"
down_revision: Union[str, Sequence[str], None] = "f4a7d91c3e21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE security_audit_log (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NULL,
            filename varchar(255) NULL,
            reason varchar(255) NOT NULL,
            ip_address varchar(45) NULL,
            "timestamp" timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        'CREATE INDEX ix_security_audit_log_user_id_timestamp ON security_audit_log (user_id, "timestamp");'
    )
    op.execute(
        'CREATE INDEX ix_security_audit_log_timestamp ON security_audit_log ("timestamp");'
    )
    op.execute("ALTER TABLE security_audit_log ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE security_audit_log FORCE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY security_audit_log_insert_policy ON security_audit_log
        FOR INSERT TO rag_app
        WITH CHECK (true);
        """
    )
    op.execute(
        """
        CREATE POLICY security_audit_log_select_admin_policy ON security_audit_log
        FOR SELECT TO rag_app
        USING (NULLIF(current_setting('app.user_role', true), '') = 'admin');
        """
    )
    op.execute("GRANT SELECT, INSERT ON security_audit_log TO rag_app;")
    op.execute("REVOKE UPDATE, DELETE ON security_audit_log FROM rag_app;")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS security_audit_log;")
