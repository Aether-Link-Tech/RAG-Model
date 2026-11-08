"""scope file_hash uniqueness by private and public visibility

Revision ID: e8b4c2d19f03
Revises: c3a91f0e4b12
Create Date: 2026-10-29 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8b4c2d19f03"
down_revision: Union[str, Sequence[str], None] = "c3a91f0e4b12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Safety check: never silently delete or overwrite duplicate records
    dup_private = conn.execute(
        sa.text(
            """
            SELECT user_id, file_hash
            FROM documents
            WHERE visibility = 'private'
            GROUP BY user_id, file_hash
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if dup_private is not None:
        raise RuntimeError(
            "Cannot apply uq_documents_private_user_hash: existing private duplicate "
            "(user_id, file_hash) rows require manual resolution."
        )

    dup_public = conn.execute(
        sa.text(
            """
            SELECT file_hash
            FROM documents
            WHERE visibility = 'public'
            GROUP BY file_hash
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if dup_public is not None:
        raise RuntimeError(
            "Cannot apply uq_documents_public_hash: existing public duplicate "
            "file_hash rows require manual resolution."
        )

    # Replace global unique index on file_hash with non-unique lookup index
    op.drop_index(op.f("ix_documents_file_hash"), table_name="documents")
    op.create_index(
        op.f("ix_documents_file_hash"),
        "documents",
        ["file_hash"],
        unique=False,
    )

    # Private scope: per-user duplicate detection
    op.create_index(
        "uq_documents_private_user_hash",
        "documents",
        ["user_id", "file_hash"],
        unique=True,
        postgresql_where=sa.text("visibility = 'private'"),
    )

    # Public scope: global duplicate detection within the shared public pool
    op.create_index(
        "uq_documents_public_hash",
        "documents",
        ["file_hash"],
        unique=True,
        postgresql_where=sa.text("visibility = 'public'"),
    )


def downgrade() -> None:
    conn = op.get_bind()

    dup_global = conn.execute(
        sa.text(
            """
            SELECT file_hash
            FROM documents
            GROUP BY file_hash
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if dup_global is not None:
        raise RuntimeError(
            "Cannot downgrade to global UNIQUE(file_hash): multiple documents share "
            "the same file_hash across different users or visibility scopes."
        )

    op.drop_index("uq_documents_public_hash", table_name="documents")
    op.drop_index("uq_documents_private_user_hash", table_name="documents")
    op.drop_index(op.f("ix_documents_file_hash"), table_name="documents")
    op.create_index(
        op.f("ix_documents_file_hash"),
        "documents",
        ["file_hash"],
        unique=True,
    )
