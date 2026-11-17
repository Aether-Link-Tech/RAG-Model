"""OCR columns on document_chunks (FR-061 / AL-284, deferred from AL-245)

A chunk records whether its text came from the page or from an image (OCR), which image, where
on the page (bbox) and how confident OCR was, so search results and citations can show it.
Existing RLS policies apply unchanged.

Revision ID: 6f7a8b9c0d1e
Revises: 5e6f7a8b9c0d
Create Date: 2026-11-17 10:05:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "6f7a8b9c0d1e"
down_revision: Union[str, Sequence[str], None] = "5e6f7a8b9c0d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "document_chunks"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("source_type", sa.String(10), nullable=False, server_default="text"))
    op.add_column(TABLE, sa.Column("image_id", UUID(as_uuid=True),
                                   sa.ForeignKey("document_images.id", ondelete="SET NULL"), nullable=True))
    op.add_column(TABLE, sa.Column("bbox", JSONB(), nullable=True))
    op.add_column(TABLE, sa.Column("ocr_confidence", sa.Float(), nullable=True))
    op.create_check_constraint("check_chunk_source_type", TABLE, "source_type IN ('text', 'ocr')")
    op.create_check_constraint("check_chunk_ocr_confidence", TABLE,
                               "ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 1)")
    op.create_index("ix_document_chunks_image_id", TABLE, ["image_id"])


def downgrade() -> None:
    op.drop_index("ix_document_chunks_image_id", table_name=TABLE)
    op.drop_constraint("check_chunk_ocr_confidence", TABLE, type_="check")
    op.drop_constraint("check_chunk_source_type", TABLE, type_="check")
    for column in ("ocr_confidence", "bbox", "image_id", "source_type"):
        op.drop_column(TABLE, column)
