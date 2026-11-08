"""create collections and documents.collection_id (FR-012 / AL-147)

Revision ID: c8e2a4d6f103
Revises: b5d1f8a3c902
Create Date: 2026-11-07 22:00:00.000000

"""
import os
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c8e2a4d6f103"
down_revision: Union[str, Sequence[str], None] = "b5d1f8a3c902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

OWNER_CHECK = (
    "NULLIF(current_setting('app.user_id', true), '') IS NOT NULL "
    "AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid"
)


def _app_role() -> str:
    role_name = os.getenv("APP_DB_USER", "rag_app").strip() or "rag_app"
    if not ROLE_IDENTIFIER_RE.match(role_name):
        raise ValueError(f"Invalid APP_DB_USER identifier: {role_name!r}")
    return role_name


def upgrade() -> None:
    app_role = _app_role()

    op.create_table(
        "collections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "name", name="uq_collections_user_name"),
        sa.UniqueConstraint("id", "user_id", name="uq_collections_id_user"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="check_collection_name_not_blank"),
    )
    op.create_index("ix_collections_user_id", "collections", ["user_id"])

    op.add_column(
        "documents",
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_documents_collection_id", "documents", ["collection_id"])
    # Composite FK: a document can only sit in a collection of its own owner.
    # Deleting a collection clears only collection_id ("Uncategorized").
    op.execute(
        """
        ALTER TABLE documents
        ADD CONSTRAINT fk_documents_collection_owner
        FOREIGN KEY (collection_id, user_id)
        REFERENCES collections (id, user_id)
        ON DELETE SET NULL (collection_id);
        """
    )

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE collections TO {app_role};")

    # RLS: collections are strictly per-user, admins included.
    op.execute("ALTER TABLE collections ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE collections FORCE ROW LEVEL SECURITY;")
    op.execute(f"CREATE POLICY collections_select_policy ON collections FOR SELECT USING ({OWNER_CHECK});")
    op.execute(f"CREATE POLICY collections_insert_policy ON collections FOR INSERT WITH CHECK ({OWNER_CHECK});")
    op.execute(
        f"CREATE POLICY collections_update_policy ON collections FOR UPDATE "
        f"USING ({OWNER_CHECK}) WITH CHECK ({OWNER_CHECK});"
    )
    op.execute(f"CREATE POLICY collections_delete_policy ON collections FOR DELETE USING ({OWNER_CHECK});")


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS fk_documents_collection_owner;")
    op.drop_index("ix_documents_collection_id", table_name="documents")
    op.drop_column("documents", "collection_id")
    op.drop_index("ix_collections_user_id", table_name="collections")
    op.drop_table("collections")
