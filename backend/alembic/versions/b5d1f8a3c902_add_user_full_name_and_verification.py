"""add users.full_name and users.is_verified (FR-001 / AL-115, AL-117)

Revision ID: b5d1f8a3c902
Revises: a7c3e5f2b801
Create Date: 2026-11-07 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b5d1f8a3c902"
down_revision: Union[str, Sequence[str], None] = "a7c3e5f2b801"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("full_name", sa.String(length=255), nullable=True))
    # Accounts that already exist were created before email verification, so
    # they are marked verified; new rows default to pending (false).
    op.add_column(
        "users",
        sa.Column("is_verified", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.alter_column("users", "is_verified", server_default=sa.false())


def downgrade() -> None:
    op.drop_column("users", "is_verified")
    op.drop_column("users", "full_name")
