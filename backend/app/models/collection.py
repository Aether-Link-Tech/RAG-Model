import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class Collection(Base):
    """A user's folder for grouping their own documents (FR-012 / AL-147).

    Per-user only (PostgreSQL RLS). Deleting a collection moves its documents to
    "Uncategorized" (documents.collection_id is set to NULL), never deletes them.
    """

    __tablename__ = "collections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    __table_args__ = (
        # Target of the composite FK from documents, so a document can only be filed
        # in a collection owned by the same user.
        UniqueConstraint("id", "user_id", name="uq_collections_id_user"),
        Index("uq_collections_user_name", "user_id", text("lower(name)"), unique=True),
    )
