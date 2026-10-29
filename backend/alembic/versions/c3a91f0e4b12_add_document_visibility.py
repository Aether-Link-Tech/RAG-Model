"""add document visibility for private vs public knowledge

Revision ID: c3a91f0e4b12
Revises: 24737d3d7b01
Create Date: 2026-10-29 06:22:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3a91f0e4b12"
down_revision: Union[str, Sequence[str], None] = "24737d3d7b01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "visibility",
            sa.String(length=20),
            nullable=False,
            server_default="private",
        ),
    )
    op.create_check_constraint(
        "check_document_visibility",
        "documents",
        "visibility IN ('private', 'public')",
    )


def downgrade() -> None:
    op.drop_constraint("check_document_visibility", "documents", type_="check")
    op.drop_column("documents", "visibility")
