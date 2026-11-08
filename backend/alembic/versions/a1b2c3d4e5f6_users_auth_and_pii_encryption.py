"""users: registration status, email verification, encrypted PII (FR-001 / FR-007)

- status ('pending' | 'active'): self-registered accounts stay pending until the
  email is verified. Existing accounts become 'active'.
- email_verification_hash / _expires_at / email_verified_at: single-use 24 h link.
- email is now AES-256-GCM ciphertext; email_hash (HMAC blind index) replaces the
  plaintext unique index for lookups and duplicate detection.
- full_name (encrypted) for registration.

Revision ID: a1b2c3d4e5f6
Revises: f4a7d91c3e21
Create Date: 2026-11-08 06:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from backend.app.core.crypto import decrypt_text, email_index, encrypt_text, normalize_email

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f4a7d91c3e21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_hash", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("full_name", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
    )
    op.add_column("users", sa.Column("email_verification_hash", sa.String(64), nullable=True))
    op.add_column(
        "users",
        sa.Column("email_verification_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("check_user_status", "users", "status IN ('pending', 'active')")

    # Encrypt existing emails in place and fill the blind index.
    op.drop_index("ix_users_email", table_name="users")
    op.alter_column("users", "email", type_=sa.Text(), existing_nullable=False)
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, email FROM users")).fetchall()
    for user_id, email in rows:
        normalised = normalize_email(email)
        conn.execute(
            sa.text("UPDATE users SET email = :email, email_hash = :hash WHERE id = :id"),
            {"email": encrypt_text(normalised), "hash": email_index(normalised), "id": user_id},
        )

    op.alter_column("users", "email_hash", nullable=False)
    op.create_index("ix_users_email_hash", "users", ["email_hash"], unique=True)
    op.create_index("ix_users_email_verification_hash", "users", ["email_verification_hash"])


def downgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, email FROM users")).fetchall()
    for user_id, stored in rows:
        conn.execute(
            sa.text("UPDATE users SET email = :email WHERE id = :id"),
            {"email": decrypt_text(stored), "id": user_id},
        )
    op.drop_index("ix_users_email_verification_hash", table_name="users")
    op.drop_index("ix_users_email_hash", table_name="users")
    op.alter_column("users", "email", type_=sa.String(255), existing_nullable=False)
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.drop_constraint("check_user_status", "users", type_="check")
    for column in (
        "email_verified_at",
        "email_verification_expires_at",
        "email_verification_hash",
        "status",
        "full_name",
        "email_hash",
    ):
        op.drop_column("users", column)
