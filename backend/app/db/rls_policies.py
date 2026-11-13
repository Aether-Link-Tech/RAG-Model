"""Reusable Row-Level Security SQL for migrations (FR-072 / AL-258).

Every table that holds per-user data must be owner-isolated in PostgreSQL itself, so
that even raw SQL on the application role cannot read another user's rows. Use these
helpers in a migration instead of hand-writing the policy:

    from backend.app.db.rls_policies import owner_rls_sql, drop_owner_rls_sql

    def upgrade():
        op.create_table("document_chunks", ...)
        for statement in owner_rls_sql("document_chunks"):
            op.execute(statement)

    def downgrade():
        for statement in drop_owner_rls_sql("document_chunks"):
            op.execute(statement)
        op.drop_table("document_chunks")

`backend/tests/test_rls_guard.py` fails CI if any table is created without RLS.
"""
from __future__ import annotations

import os
import re

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

# Transaction-local identity set by backend/app/db/rls.py for every authenticated request.
CURRENT_USER = "NULLIF(current_setting('app.user_id', true), '')"
CURRENT_ROLE = "NULLIF(current_setting('app.user_role', true), '')"


def _ident(name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def app_role() -> str:
    return _ident(os.getenv("APP_DB_USER", "rag_app").strip() or "rag_app")


def owner_condition(owner_column: str = "user_id") -> str:
    """True only for the authenticated caller's own rows; false when unauthenticated."""
    column = _ident(owner_column)
    return f"{CURRENT_USER} IS NOT NULL AND {column} = {CURRENT_USER}::uuid"


def owner_rls_sql(
    table: str,
    *,
    owner_column: str = "user_id",
    privileges: str = "SELECT, INSERT, UPDATE, DELETE",
    admin_can_read: bool = False,
) -> list[str]:
    """GRANT + ENABLE/FORCE RLS + an owner-only policy for every command.

    admin_can_read adds a separate SELECT policy for `app.user_role = 'admin'`; leave it
    False for private content (documents' text, conversations), which admins never read.
    """
    t = _ident(table)
    if not re.fullmatch(r"[A-Z, ]+", privileges):
        raise ValueError(f"Invalid privilege list: {privileges!r}")
    condition = owner_condition(owner_column)
    statements = [
        f"GRANT {privileges} ON TABLE {t} TO {app_role()};",
        f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY;",
        f"CREATE POLICY {t}_owner_policy ON {t} FOR ALL USING ({condition}) WITH CHECK ({condition});",
    ]
    if admin_can_read:
        statements.append(
            f"CREATE POLICY {t}_admin_read_policy ON {t} FOR SELECT "
            f"USING ({CURRENT_USER} IS NOT NULL AND {CURRENT_ROLE} = 'admin');"
        )
    return statements


def drop_owner_rls_sql(table: str) -> list[str]:
    """Reverse of owner_rls_sql, for downgrade()."""
    t = _ident(table)
    return [
        f"DROP POLICY IF EXISTS {t}_admin_read_policy ON {t};",
        f"DROP POLICY IF EXISTS {t}_owner_policy ON {t};",
        f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY;",
        f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY;",
        f"REVOKE ALL ON TABLE {t} FROM {app_role()};",
    ]
