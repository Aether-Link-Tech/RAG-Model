"""document_fingerprints + LSH bands for near-duplicate detection (FR-081 / AL-279, AL-281)

RLS: SELECT returns the caller's own rows and rows of public documents (so the check can
compare against both); INSERT / UPDATE / DELETE are owner-only. Another user's private
fingerprints are invisible, so they can never match or be named.

Revision ID: 9c3d4e5f6a7b
Revises: 26d21e70da5d
Create Date: 2026-11-09 10:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

from backend.app.db.rls_policies import CURRENT_USER, app_role, owner_condition

revision: str = "9c3d4e5f6a7b"
down_revision: Union[str, Sequence[str], None] = "26d21e70da5d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("document_fingerprints", "document_fingerprint_bands")


def upgrade() -> None:
    op.create_table(
        "document_fingerprints",
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False),
        sa.Column("num_perm", sa.SmallInteger(), nullable=False),
        sa.Column("signature", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("visibility IN ('private', 'public')", name="check_fingerprint_visibility"),
    )
    op.create_index("ix_document_fingerprints_user_id", "document_fingerprints", ["user_id"])
    op.create_table(
        "document_fingerprint_bands",
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("band", sa.SmallInteger(), primary_key=True),
        sa.Column("hash", sa.BigInteger(), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False),
        sa.CheckConstraint("visibility IN ('private', 'public')", name="check_fingerprint_band_visibility"),
    )
    op.create_index("ix_document_fingerprint_bands_lookup", "document_fingerprint_bands", ["band", "hash"])

    owner = owner_condition()
    for table in TABLES:
        op.execute(f"GRANT SELECT, INSERT, DELETE ON TABLE {table} TO {app_role()};")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY {table}_select_policy ON {table} FOR SELECT "
            f"USING ({CURRENT_USER} IS NOT NULL AND (visibility = 'public' OR {owner}));"
        )
        op.execute(f"CREATE POLICY {table}_insert_policy ON {table} FOR INSERT WITH CHECK ({owner});")
        op.execute(f"CREATE POLICY {table}_delete_policy ON {table} FOR DELETE USING ({owner});")


def downgrade() -> None:
    op.drop_table("document_fingerprint_bands")
    op.drop_table("document_fingerprints")
