"""document_images: embedded images and their OCR results (FR-058 / AL-237, FR-059, FR-063)

Owner-only FORCE RLS through the shared helper (FR-072).

Revision ID: 9d4e5f6a7b8c
Revises: 9c3d4e5f6a7b
Create Date: 2026-11-09 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from backend.app.db.rls_policies import drop_owner_rls_sql, owner_rls_sql

revision: str = "9d4e5f6a7b8c"
down_revision: Union[str, Sequence[str], None] = "9c3d4e5f6a7b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_images",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("image_index", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("bbox", JSONB(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.String(1024), nullable=False),
        sa.Column("ocr_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("ocr_engine", sa.String(40), nullable=True),
        sa.Column("ocr_error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("document_id", "image_index", name="uq_document_images_index"),
        sa.CheckConstraint("ocr_status IN ('pending', 'done', 'skipped')", name="check_document_image_ocr_status"),
    )
    op.create_index("ix_document_images_document_id", "document_images", ["document_id"])
    for statement in owner_rls_sql("document_images"):
        op.execute(statement)


def downgrade() -> None:
    for statement in drop_owner_rls_sql("document_images"):
        op.execute(statement)
    op.drop_table("document_images")
