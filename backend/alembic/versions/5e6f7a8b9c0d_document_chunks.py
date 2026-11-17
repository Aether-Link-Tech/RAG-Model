"""document_chunks table with RLS (FR-019 / AL-205)

Chunks lived only in ChromaDB. BM25 keyword search, enrichment, OCR metadata and citations need
them in PostgreSQL too. RLS mirrors `documents`:

* SELECT: public chunks for any authenticated user; private chunks only for their owner.
* INSERT: only as yourself, and only for a document you own with the same visibility
  (a chunk can never claim a different owner or scope than its document).
* DELETE: private -> owner; public -> uploader or admin.
* No UPDATE: chunks are replaced (delete + insert) on re-index.

Revision ID: 5e6f7a8b9c0d
Revises: 49619796f3b1
Create Date: 2026-11-17 10:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID

from backend.app.db.rls_policies import CURRENT_ROLE, CURRENT_USER, app_role

revision: str = "5e6f7a8b9c0d"
down_revision: Union[str, Sequence[str], None] = "49619796f3b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "document_chunks"
AUTHENTICATED = f"{CURRENT_USER} IS NOT NULL"
OWNER = f"user_id = {CURRENT_USER}::uuid"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(500), nullable=True),
        sa.Column("context_summary", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column(
            "search_vector",
            TSVECTOR(),
            sa.Computed("to_tsvector('english', coalesce(content, ''))", persisted=True),
        ),
        sa.Column("embedding_model", sa.String(100), nullable=True),
        sa.Column("chroma_id", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("visibility IN ('private', 'public')", name="check_chunk_visibility"),
        sa.CheckConstraint("chunk_index >= 0", name="check_chunk_index"),
        sa.UniqueConstraint("chroma_id", name="uq_document_chunks_chroma_id"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_document_index"),
    )
    op.create_index("ix_document_chunks_user_id", TABLE, ["user_id"])
    op.create_index("ix_document_chunks_search_vector", TABLE, ["search_vector"], postgresql_using="gin")

    op.execute(f"GRANT SELECT, INSERT, DELETE ON TABLE {TABLE} TO {app_role()};")
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY {TABLE}_select_policy ON {TABLE} FOR SELECT USING ("
        f"{AUTHENTICATED} AND (visibility = 'public' OR (visibility = 'private' AND {OWNER})));"
    )
    op.execute(
        f"CREATE POLICY {TABLE}_insert_policy ON {TABLE} FOR INSERT WITH CHECK ("
        f"{AUTHENTICATED} AND {OWNER} AND visibility IN ('private', 'public') AND EXISTS ("
        f"SELECT 1 FROM documents d WHERE d.id = document_id "
        f"AND d.user_id = {TABLE}.user_id AND d.visibility = {TABLE}.visibility));"
    )
    op.execute(
        f"CREATE POLICY {TABLE}_delete_policy ON {TABLE} FOR DELETE USING ("
        f"{AUTHENTICATED} AND ((visibility = 'private' AND {OWNER}) OR "
        f"(visibility = 'public' AND ({OWNER} OR {CURRENT_ROLE} = 'admin'))));"
    )


def downgrade() -> None:
    for policy in ("select", "insert", "delete"):
        op.execute(f"DROP POLICY IF EXISTS {TABLE}_{policy}_policy ON {TABLE};")
    op.drop_index("ix_document_chunks_search_vector", table_name=TABLE)
    op.drop_index("ix_document_chunks_user_id", table_name=TABLE)
    op.drop_table(TABLE)
