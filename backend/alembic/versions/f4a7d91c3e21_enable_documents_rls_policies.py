"""enable row level security and policies on documents

Revision ID: f4a7d91c3e21
Revises: e8b4c2d19f03
Create Date: 2026-10-30 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "f4a7d91c3e21"
down_revision: Union[str, Sequence[str], None] = "e8b4c2d19f03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Provision non-superuser, NOBYPASSRLS application role (`rag_app`) so that
    # transactions executing `SET LOCAL ROLE rag_app` cannot bypass PostgreSQL RLS
    # even when the connection pool connects as the `postgres` superuser.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
                CREATE ROLE rag_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            ELSE
                ALTER ROLE rag_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            END IF;
            EXECUTE format('GRANT rag_app TO %I', current_user);
        END
        $$;
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO rag_app;")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE documents, users TO rag_app;")

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

    # 5. UPDATE policy:
    # - Private documents: only the owner may update.
    # - Public documents: only the original uploader or an admin may update.
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
    op.execute("DROP POLICY IF EXISTS documents_delete_policy ON documents;")
    op.execute("DROP POLICY IF EXISTS documents_update_policy ON documents;")
    op.execute("DROP POLICY IF EXISTS documents_insert_policy ON documents;")
    op.execute("DROP POLICY IF EXISTS documents_select_policy ON documents;")
    op.execute("ALTER TABLE documents NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE documents DISABLE ROW LEVEL SECURITY;")
