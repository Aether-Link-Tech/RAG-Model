"""collections table + documents.collection_id (FR-012 / AL-147)

Collections are strictly per user (RLS on owner). documents.collection_id references
collections(id, user_id) together with the document's own user_id, so a document can
only be filed in its owner's collection. Deleting a collection sets only
collection_id to NULL ("Uncategorized"); the documents themselves are kept.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-11-08 06:20:00.000000
"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = os.getenv("APP_DB_USER", "rag_app").strip() or "rag_app"
OWNER = "user_id = NULLIF(current_setting('app.user_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "collections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("id", "user_id", name="uq_collections_id_user"),
    )
    op.execute("CREATE UNIQUE INDEX uq_collections_user_name ON collections (user_id, lower(name));")

    op.add_column("documents", sa.Column("collection_id", UUID(as_uuid=True), nullable=True))
    op.create_index("ix_documents_collection_id", "documents", ["collection_id"])
    # PostgreSQL 15+: SET NULL on a column subset keeps documents.user_id intact.
    op.execute(
        """
        ALTER TABLE documents
        ADD CONSTRAINT fk_documents_collection_owner
        FOREIGN KEY (collection_id, user_id) REFERENCES collections (id, user_id)
        ON DELETE SET NULL (collection_id);
        """
    )

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE collections TO {APP_ROLE};")
    op.execute("ALTER TABLE collections ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE collections FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY collections_owner_policy ON collections
        FOR ALL
        USING (NULLIF(current_setting('app.user_id', true), '') IS NOT NULL AND {OWNER})
        WITH CHECK (NULLIF(current_setting('app.user_id', true), '') IS NOT NULL AND {OWNER});
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS fk_documents_collection_owner;")
    op.drop_index("ix_documents_collection_id", table_name="documents")
    op.drop_column("documents", "collection_id")
    op.drop_table("collections")
