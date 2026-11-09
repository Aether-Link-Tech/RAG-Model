"""Content fingerprints for near-duplicate detection (FR-081 / AL-279, AL-281)."""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, LargeBinary, SmallInteger, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class DocumentFingerprint(Base):
    """MinHash signature of a document's extracted text (one row per indexed document).

    RLS: a user can read their own fingerprints and those of public documents; writes
    are owner-only. Another user's private document can therefore never be matched.
    """

    __tablename__ = "document_fingerprints"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    visibility: Mapped[str] = mapped_column(String(20), nullable=False)
    num_perm: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    signature: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)  # num_perm × uint64, little-endian
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DocumentFingerprintBand(Base):
    """LSH band hashes: documents sharing any (band, hash) are candidate near-duplicates."""

    __tablename__ = "document_fingerprint_bands"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    band: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    hash: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    visibility: Mapped[str] = mapped_column(String(20), nullable=False)

    __table_args__ = (Index("ix_document_fingerprint_bands_lookup", "band", "hash"),)
