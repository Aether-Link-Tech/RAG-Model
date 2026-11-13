import uuid
from datetime import datetime
from typing import TYPE_CHECKING
from sqlalchemy import String, Boolean, DateTime, CheckConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from backend.app.core.crypto import EncryptedText, email_index, normalize_email
from backend.app.core.roles import UserRole
from backend.app.db.base import Base

if TYPE_CHECKING:
    from backend.app.models.document import Document

class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    # Email and full name are personal data: encrypted at rest with AES-256-GCM (FR-007).
    # `email_hash` is a keyed HMAC blind index used for lookup and uniqueness.
    email: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    email_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )
    full_name: Mapped[str | None] = mapped_column(
        EncryptedText,
        nullable=True
    )
    # FR-001: self-registered accounts stay pending until the email is verified.
    is_verified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False
    )
    role: Mapped[str] = mapped_column(
        String(20),
        default=UserRole.USER.value,
        nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        server_default="active",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False
    )

    # Relationships
    documents: Mapped[list["Document"]] = relationship(
        "Document",
        back_populates="owner",
        cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("role IN ('admin', 'user')", name="check_user_role"),
        CheckConstraint("status IN ('active', 'quarantined')", name="check_user_status"),
    )

    @validates("email")
    def _normalise_email(self, _key: str, value: str) -> str:
        normalised = normalize_email(value)
        self.email_hash = email_index(normalised)
        return normalised

    @property
    def account_status(self) -> str:
        return "active" if self.is_verified else "pending_verification"

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    def __repr__(self) -> str:
        return f"<User id={self.id} role={self.role}>"
