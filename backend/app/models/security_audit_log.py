import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class SecurityAuditLog(Base):
    __tablename__ = "security_audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    filename: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    reason: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    ip_address: Mapped[Optional[str]] = mapped_column(
        String(45),
        nullable=True,
    )
    timestamp: Mapped[datetime] = mapped_column(
        "timestamp",
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_security_audit_log_user_id_timestamp", "user_id", "timestamp"),
        Index("ix_security_audit_log_timestamp", "timestamp"),
        {"implicit_returning": False},
    )

    def __repr__(self) -> str:
        return (
            f"<SecurityAuditLog id={self.id} user_id={self.user_id} "
            f"filename={self.filename!r} reason={self.reason!r}>"
        )
