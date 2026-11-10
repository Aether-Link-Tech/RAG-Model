"""add users.status active/quarantined (AL-265)

Revision ID: 49619796f3b1
Revises: 26d21e70da5d
Create Date: 2026-11-10 08:15:27.583556

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '49619796f3b1'
down_revision: Union[str, Sequence[str], None] = '9d4e5f6a7b8c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'active';")
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT check_user_status CHECK (status IN ('active', 'quarantined'));"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS check_user_status;")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS status;")

