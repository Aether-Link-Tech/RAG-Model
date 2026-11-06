import uuid
from datetime import datetime
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, Integer, Text, DateTime, ForeignKey, CheckConstraint, Index, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.app.db.base import Base

if TYPE_CHECKING:
    from backend.app.models.user import User

class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )
    file_path: Mapped[str] = mapped_column(
        String(1024),
        nullable=False
    )
    mime_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )
    file_size: Mapped[int] = mapped_column(
        Integer,
        nullable=False
    )
    # SHA-256 hash: 64-char hex digest; scoped uniqueness enforced via partial unique indexes
    file_hash: Mapped[str] = mapped_column(
        String(64),
        index=True,
        nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(30),
        default="pending",
        nullable=False
    )
    visibility: Mapped[str] = mapped_column(
        String(20),
        default="private",
        server_default="private",
        nullable=False
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        onupdate=func.now(),
        nullable=True
    )

    # Relationships
    owner: Mapped["User"] = relationship(
        "User",
        back_populates="documents"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'indexed', 'failed')",
            name="check_document_status"
        ),
        CheckConstraint(
            "visibility IN ('private', 'public')",
            name="check_document_visibility"
        ),
        Index(
            "uq_documents_private_user_hash",
            "user_id",
            "file_hash",
            unique=True,
            postgresql_where=text("visibility = 'private'"),
        ),
        Index(
            "uq_documents_public_hash",
            "file_hash",
            unique=True,
            postgresql_where=text("visibility = 'public'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Document id={self.id} filename={self.filename} hash={self.file_hash[:8]} status={self.status}>"
