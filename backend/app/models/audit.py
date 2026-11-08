import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.crypto import EncryptedText
from backend.app.db.base import Base


class SecurityAuditLog(Base):
    """Append-only record of rejected uploads and denied actions (FR-069 / AL-166).

    Only admins can read it (PostgreSQL RLS). Rows older than
    AUDIT_LOG_RETENTION_DAYS are purged. The client IP is encrypted at rest (FR-007).
    """

    __tablename__ = "security_audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    ip_address: Mapped[Optional[str]] = mapped_column(EncryptedText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_security_audit_log_created_at", "created_at"),
        Index("ix_security_audit_log_user_id", "user_id"),
    )
