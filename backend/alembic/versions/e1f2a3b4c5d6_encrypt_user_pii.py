"""encrypt user PII at rest (FR-007 / AL-136)

- users.email becomes AES-256-GCM ciphertext; users.email_hash (keyed HMAC blind
  index) replaces the plaintext unique index for lookup and duplicate detection.
- users.full_name is encrypted as well.

Keys: PII_ENCRYPTION_KEY / PII_INDEX_KEY (derived from SECRET_KEY in local dev).

Revision ID: e1f2a3b4c5d6
Revises: c8e2a4d6f103
Create Date: 2026-11-08 09:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from backend.app.core.crypto import decrypt_text, email_index, encrypt_text, normalize_email

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "c8e2a4d6f103"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_hash", sa.String(64), nullable=True))
    op.drop_index("ix_users_email", table_name="users")
    op.alter_column("users", "email", type_=sa.Text(), existing_nullable=False)
    op.alter_column("users", "full_name", type_=sa.Text(), existing_nullable=True)

    conn = op.get_bind()
    for user_id, email, full_name in conn.execute(sa.text("SELECT id, email, full_name FROM users")).fetchall():
        normalised = normalize_email(email)
        conn.execute(
            sa.text("UPDATE users SET email = :email, email_hash = :hash, full_name = :name WHERE id = :id"),
            {
                "email": encrypt_text(normalised),
                "hash": email_index(normalised),
                "name": encrypt_text(full_name) if full_name is not None else None,
                "id": user_id,
            },
        )

    op.alter_column("users", "email_hash", nullable=False)
    op.create_index("ix_users_email_hash", "users", ["email_hash"], unique=True)


def downgrade() -> None:
    conn = op.get_bind()
    for user_id, email, full_name in conn.execute(sa.text("SELECT id, email, full_name FROM users")).fetchall():
        conn.execute(
            sa.text("UPDATE users SET email = :email, full_name = :name WHERE id = :id"),
            {
                "email": decrypt_text(email),
                "name": decrypt_text(full_name) if full_name is not None else None,
                "id": user_id,
            },
        )
    op.drop_index("ix_users_email_hash", table_name="users")
    op.alter_column("users", "full_name", type_=sa.String(255), existing_nullable=True)
    op.alter_column("users", "email", type_=sa.String(255), existing_nullable=False)
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.drop_column("users", "email_hash")
