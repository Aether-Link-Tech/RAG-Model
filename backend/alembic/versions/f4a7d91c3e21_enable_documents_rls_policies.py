"""enable row level security and policies on documents

Revision ID: f4a7d91c3e21
Revises: e8b4c2d19f03
Create Date: 2026-10-30 22:00:00.000000

"""
import os
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from backend.app.core.config import DEFAULT_DEV_APP_DB_PASSWORD


revision: str = "f4a7d91c3e21"
down_revision: Union[str, Sequence[str], None] = "e8b4c2d19f03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _get_app_role_credentials() -> tuple[str, str]:
    role_name = os.getenv("APP_DB_USER", "rag_app").strip() or "rag_app"
    if not ROLE_IDENTIFIER_RE.match(role_name):
        raise ValueError(f"Invalid APP_DB_USER identifier: {role_name!r}")
    role_password = os.getenv("APP_DB_PASSWORD", DEFAULT_DEV_APP_DB_PASSWORD)
    if not role_password:
        raise ValueError("APP_DB_PASSWORD must not be empty when provisioning application role.")
    return role_name, role_password


def upgrade() -> None:
    conn = op.get_bind()
    role_name, role_password = _get_app_role_credentials()

    # 1. Provision dedicated non-superuser LOGIN role (`rag_app`) with NOBYPASSRLS
    # so normal application connections authenticate directly as `rag_app` and can
    # never bypass PostgreSQL Row-Level Security or escalate privileges via `RESET ROLE`.
    conn.execute(
        sa.text(
            "SELECT set_config('app.provision_role', :role_name, true), "
            "set_config('app.provision_pass', :role_password, true)"
        ),
        {"role_name": role_name, "role_password": role_password},
    )
    op.execute(
        """
        DO $$
        DECLARE
            v_role text := current_setting('app.provision_role', true);
            v_pass text := current_setting('app.provision_pass', true);
            v_parent text;
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
                EXECUTE format(
                    'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION NOINHERIT',
                    v_role,
                    v_pass
                );
            ELSE
                EXECUTE format(
                    'ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION NOINHERIT',
                    v_role,
                    v_pass
                );
            END IF;

            -- Ensure the application role never belongs to or inherits from any other role
            FOR v_parent IN
                SELECT r.rolname
                FROM pg_auth_members am
                JOIN pg_roles r ON r.oid = am.roleid
                JOIN pg_roles m ON m.oid = am.member
                WHERE m.rolname = v_role
            LOOP
                EXECUTE format('REVOKE %I FROM %I', v_parent, v_role);
            END LOOP;

            EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), v_role);
        END
        $$;
        """
    )
    conn.execute(
        sa.text(
            "SELECT set_config('app.provision_role', '', true), "
            "set_config('app.provision_pass', '', true)"
        )
    )

    # Apply least-privilege schema and table grants:
    # - `documents`: SELECT, INSERT, UPDATE, DELETE (strictly governed by FORCE ROW LEVEL SECURITY)
    # - `users`: SELECT, INSERT, UPDATE only (no hard DELETE, preventing ON DELETE CASCADE
    #   from ever deleting or corrupting public documents via the application role)
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {role_name};")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {role_name};")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role_name};")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE documents TO {role_name};")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE users TO {role_name};")

    # 2. Enable and force Row-Level Security on `documents`
    op.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE documents FORCE ROW LEVEL SECURITY;")

    # 3. SELECT policy:
    # - Requires an authenticated transaction (`app.user_id` set and non-empty).
    # - Private documents: visible strictly to their owner (`user_id = app.user_id`).
    # - Public documents: visible to any authenticated user.
    op.execute(
        """
        CREATE POLICY documents_select_policy ON documents
        FOR SELECT
        USING (
            NULLIF(current_setting('app.user_id', true), '') IS NOT NULL
            AND (
                visibility = 'public'
                OR (
                    visibility = 'private'
                    AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                )
            )
        );
        """
    )

    # 4. INSERT policy:
    # - Callers may only insert documents owned by their authenticated `app.user_id`.
    op.execute(
        """
        CREATE POLICY documents_insert_policy ON documents
        FOR INSERT
        WITH CHECK (
            NULLIF(current_setting('app.user_id', true), '') IS NOT NULL
            AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
            AND visibility IN ('private', 'public')
        );
        """
    )

    # 5. UPDATE policy + immutability trigger:
    # - Private documents: only the owner may update.
    # - Public documents: only the original uploader or an admin may update mutable fields.
    # - Neither owners nor admins may mutate `user_id` (ownership) or `visibility` (scope).
    op.execute(
        """
        CREATE POLICY documents_update_policy ON documents
        FOR UPDATE
        USING (
            NULLIF(current_setting('app.user_id', true), '') IS NOT NULL
            AND (
                (
                    visibility = 'private'
                    AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                )
                OR (
                    visibility = 'public'
                    AND (
                        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                        OR NULLIF(current_setting('app.user_role', true), '') = 'admin'
                    )
                )
            )
        )
        WITH CHECK (
            NULLIF(current_setting('app.user_id', true), '') IS NOT NULL
            AND (
                (
                    visibility = 'private'
                    AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                )
                OR (
                    visibility = 'public'
                    AND (
                        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                        OR NULLIF(current_setting('app.user_role', true), '') = 'admin'
                    )
                )
            )
        );
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_document_ownership_or_scope_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.user_id IS DISTINCT FROM OLD.user_id THEN
                RAISE EXCEPTION 'Document owner (user_id) is immutable and cannot be reassigned.'
                    USING ERRCODE = '42501';
            END IF;

            IF NEW.visibility IS DISTINCT FROM OLD.visibility THEN
                RAISE EXCEPTION 'Document visibility scope is immutable after creation.'
                    USING ERRCODE = '42501';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_documents_immutable_owner_and_visibility
        BEFORE UPDATE ON documents
        FOR EACH ROW
        EXECUTE FUNCTION prevent_document_ownership_or_scope_change();
        """
    )

    # 6. DELETE policy:
    # - Private documents: only the owner may delete.
    # - Public documents: only the original uploader or an admin may delete.
    op.execute(
        """
        CREATE POLICY documents_delete_policy ON documents
        FOR DELETE
        USING (
            NULLIF(current_setting('app.user_id', true), '') IS NOT NULL
            AND (
                (
                    visibility = 'private'
                    AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                )
                OR (
                    visibility = 'public'
                    AND (
                        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                        OR NULLIF(current_setting('app.user_role', true), '') = 'admin'
                    )
                )
            )
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_documents_immutable_owner_and_visibility ON documents;")
    op.execute("DROP FUNCTION IF EXISTS prevent_document_ownership_or_scope_change();")
    op.execute("DROP POLICY IF EXISTS documents_delete_policy ON documents;")
    op.execute("DROP POLICY IF EXISTS documents_update_policy ON documents;")
    op.execute("DROP POLICY IF EXISTS documents_insert_policy ON documents;")
    op.execute("DROP POLICY IF EXISTS documents_select_policy ON documents;")
    op.execute("ALTER TABLE documents NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE documents DISABLE ROW LEVEL SECURITY;")
