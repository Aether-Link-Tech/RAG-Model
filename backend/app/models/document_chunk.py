"""Document chunks in PostgreSQL (FR-019 / AL-205) with OCR metadata (FR-061 / AL-284).

Mirrors the chunks stored in ChromaDB (same `chroma_id`) so keyword search (BM25 via the
generated `search_vector`), enrichment, OCR metadata and citations can use them. RLS (see the
migration) mirrors `documents`: public chunks readable by everyone signed in, private only by
their owner; inserts only for a document you own with the same visibility; no updates.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, Computed, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    visibility: Mapped[str] = mapped_column(String(20), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    section: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    context_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    search_vector = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', coalesce(content, ''))", persisted=True)
    )
    embedding_model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    chroma_id: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # AL-284: where the text came from
    source_type: Mapped[str] = mapped_column(String(10), nullable=False, server_default="text", default="text")
    image_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_images.id", ondelete="SET NULL"), nullable=True, index=True
    )
    bbox: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    ocr_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    __table_args__ = (
        CheckConstraint("visibility IN ('private', 'public')", name="check_chunk_visibility"),
        CheckConstraint("chunk_index >= 0", name="check_chunk_index"),
        CheckConstraint("source_type IN ('text', 'ocr')", name="check_chunk_source_type"),
        CheckConstraint("ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 1)",
                        name="check_chunk_ocr_confidence"),
        UniqueConstraint("chroma_id", name="uq_document_chunks_chroma_id"),
        UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_document_index"),
    )
