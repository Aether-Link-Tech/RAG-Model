"""Showcase documents for the public landing page (guest Q&A).

Revision ID: 7a8b9c0d1e2f
Revises: 6f7a8b9c0d1e
Create Date: 2026-11-23 18:00:00.000000

- documents.showcase: an admin marks a public document as usable by guests (not signed in).
- Only public documents can be showcase documents (CHECK).
- Only admins can change the flag (trigger; the UPDATE policy already limits who can update).
- Guests (app.user_id empty, app.user_role = 'guest') can SELECT only indexed showcase documents.
  Every other policy still requires app.user_id, so guests see nothing else.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "7a8b9c0d1e2f"
down_revision: Union[str, Sequence[str], None] = "6f7a8b9c0d1e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE documents ADD COLUMN showcase BOOLEAN NOT NULL DEFAULT false")
    op.execute(
        "ALTER TABLE documents ADD CONSTRAINT ck_documents_showcase_public "
        "CHECK (NOT showcase OR visibility = 'public')"
    )
    op.execute("CREATE INDEX ix_documents_showcase ON documents (id) WHERE showcase")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_non_admin_showcase_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.showcase IS DISTINCT FROM OLD.showcase
               AND COALESCE(NULLIF(current_setting('app.user_role', true), ''), '') <> 'admin' THEN
                RAISE EXCEPTION 'Only an admin can change the showcase flag.' USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER documents_showcase_admin_only BEFORE UPDATE OF showcase ON documents "
        "FOR EACH ROW EXECUTE FUNCTION prevent_non_admin_showcase_change()"
    )

    op.execute(
        """
        CREATE POLICY documents_guest_select_policy ON documents
        FOR SELECT
        USING (
            NULLIF(current_setting('app.user_id', true), '') IS NULL
            AND current_setting('app.user_role', true) = 'guest'
            AND visibility = 'public'
            AND showcase
            AND status = 'indexed'
        );
        """
    )
    # rag_app already holds table-level SELECT/UPDATE on documents, which covers the new column.


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS documents_guest_select_policy ON documents")
    op.execute("DROP TRIGGER IF EXISTS documents_showcase_admin_only ON documents")
    op.execute("DROP FUNCTION IF EXISTS prevent_non_admin_showcase_change()")
    op.execute("DROP INDEX IF EXISTS ix_documents_showcase")
    op.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS ck_documents_showcase_public")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS showcase")
