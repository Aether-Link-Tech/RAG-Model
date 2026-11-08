"""create roles and user_roles tables (FR-004 / AL-123)

`users.role` stays the value the app reads (JWT claim, RLS `app.user_role`).
`user_roles` is kept in sync with it by a trigger, so the two can never drift.

Revision ID: a7c3e5f2b801
Revises: f4a7d91c3e21
Create Date: 2026-11-07 20:00:00.000000

"""
import os
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a7c3e5f2b801"
down_revision: Union[str, Sequence[str], None] = "f4a7d91c3e21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Guest is the role of an unauthenticated caller; it is seeded for completeness
# but never assigned to a `users` row (check_user_role only allows admin/user).
SEEDED_ROLES = (
    (1, "admin", "Administrator: can list and manage all users."),
    (2, "user", "Registered user: can manage their own documents and conversations."),
    (3, "guest", "Unauthenticated visitor: may only register and log in."),
)


def _app_role() -> str:
    role_name = os.getenv("APP_DB_USER", "rag_app").strip() or "rag_app"
    if not ROLE_IDENTIFIER_RE.match(role_name):
        raise ValueError(f"Invalid APP_DB_USER identifier: {role_name!r}")
    return role_name


def upgrade() -> None:
    app_role = _app_role()

    op.create_table(
        "roles",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("name", sa.String(length=20), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
    )
    op.bulk_insert(
        sa.table(
            "roles",
            sa.column("id", sa.SmallInteger()),
            sa.column("name", sa.String()),
            sa.column("description", sa.String()),
        ),
        [{"id": i, "name": n, "description": d} for i, n, d in SEEDED_ROLES],
    )

    op.create_table(
        "user_roles",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "role_id",
            sa.SmallInteger(),
            sa.ForeignKey("roles.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_user_roles_role_id", "user_roles", ["role_id"])

    # Backfill existing users.
    op.execute(
        """
        INSERT INTO user_roles (user_id, role_id)
        SELECT u.id, r.id FROM users u JOIN roles r ON r.name = u.role
        ON CONFLICT DO NOTHING;
        """
    )

    # Keep user_roles in sync with users.role. SECURITY DEFINER so the write is
    # done by the table owner: rag_app only gets read access to user_roles.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sync_user_roles()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
            v_role_id smallint;
        BEGIN
            SELECT id INTO v_role_id FROM roles WHERE name = NEW.role;
            IF v_role_id IS NULL THEN
                RAISE EXCEPTION 'Unknown role %', NEW.role USING ERRCODE = '23503';
            END IF;
            DELETE FROM user_roles WHERE user_id = NEW.id AND role_id <> v_role_id;
            INSERT INTO user_roles (user_id, role_id) VALUES (NEW.id, v_role_id)
            ON CONFLICT DO NOTHING;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION sync_user_roles() FROM PUBLIC;")
    op.execute(
        """
        CREATE TRIGGER trg_users_sync_user_roles
        AFTER INSERT OR UPDATE OF role ON users
        FOR EACH ROW
        EXECUTE FUNCTION sync_user_roles();
        """
    )

    # Grants: read-only for the app role.
    op.execute(f"GRANT SELECT ON TABLE roles TO {app_role};")
    op.execute(f"GRANT SELECT ON TABLE user_roles TO {app_role};")

    # RLS: roles is a public reference table; user_roles rows are visible to
    # their own user, and to admins.
    op.execute("ALTER TABLE roles ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY;")
    op.execute("CREATE POLICY roles_select_policy ON roles FOR SELECT USING (true);")

    op.execute("ALTER TABLE user_roles ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE user_roles FORCE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY user_roles_select_policy ON user_roles
        FOR SELECT
        USING (
            NULLIF(current_setting('app.user_id', true), '') IS NOT NULL
            AND (
                user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                OR NULLIF(current_setting('app.user_role', true), '') = 'admin'
            )
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_users_sync_user_roles ON users;")
    op.execute("DROP FUNCTION IF EXISTS sync_user_roles();")
    op.drop_index("ix_user_roles_role_id", table_name="user_roles")
    op.drop_table("user_roles")
    op.drop_table("roles")
