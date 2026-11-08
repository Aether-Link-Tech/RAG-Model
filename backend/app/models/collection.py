"""Per-user document collections (FR-012 / AL-147)."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base

# Documents with no collection are shown under this name; it cannot be used
# for a real collection.
UNCATEGORIZED = "Uncategorized"


class Collection(Base):
    __tablename__ = "collections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_collections_user_name"),
        # Target of the documents (collection_id, user_id) foreign key.
        UniqueConstraint("id", "user_id", name="uq_collections_id_user"),
    )

    def __repr__(self) -> str:
        return f"<Collection id={self.id} name={self.name!r}>"
