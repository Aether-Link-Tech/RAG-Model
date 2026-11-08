import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from backend.app.core.crypto import EncryptedText, email_index, normalize_email
from backend.app.core.roles import UserRole
from backend.app.db.base import Base

if TYPE_CHECKING:
    from backend.app.models.document import Document


class UserStatus:
    PENDING = "pending"  # registered, email not verified yet (FR-001)
    ACTIVE = "active"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    # Email and full name are personal data: encrypted at rest with AES-256-GCM (FR-007).
    # `email_hash` is a keyed HMAC blind index used for login lookup and uniqueness.
    email: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    email_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(EncryptedText, nullable=True)
    hashed_password: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )
    role: Mapped[str] = mapped_column(
        String(20),
        default=UserRole.USER.value,
        nullable=False
    )
    # Accounts created through self-registration start as "pending"; registration sets it
    # explicitly. Admin-created and pre-existing accounts are "active".
    status: Mapped[str] = mapped_column(
        String(20),
        default=UserStatus.ACTIVE,
        server_default=UserStatus.ACTIVE,
        nullable=False,
    )
    # SHA-256 of the single-use verification token; the raw token only exists in the email.
    email_verification_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    email_verification_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
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
        CheckConstraint("status IN ('pending', 'active')", name="check_user_status"),
    )

    @validates("email")
    def _normalise_email(self, _key: str, value: str) -> str:
        normalised = normalize_email(value)
        self.email_hash = email_index(normalised)
        return normalised

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    @property
    def is_active(self) -> bool:
        return self.status == UserStatus.ACTIVE

    def __repr__(self) -> str:
        return f"<User id={self.id} role={self.role} status={self.status}>"
