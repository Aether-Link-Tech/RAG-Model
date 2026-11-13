"""Build contextual embedding text and metadata for document chunks."""
from __future__ import annotations

from typing import Any


def build_context_summary(
    section: str | None,
    parent_heading: str | None = None,
) -> str:
    """Return a readable heading path, omitting missing titles safely."""
    section_title = (section or "").strip()
    parent_title = (parent_heading or "").strip()
    if parent_title and section_title:
        return f"{parent_title} > {section_title}"
    return section_title or parent_title


def build_embedding_text(
    chunk_text: str,
    document_name: str,
    section: str | None,
) -> str:
    """Prefix the original chunk text with its document and section context."""
    return f"Document: {document_name} | Section: {section or ''}\n{chunk_text}"


def build_chunk_metadata(
    chunk: dict[str, Any],
    document_id: str,
    document_name: str,
    parent_heading: str | None = None,
) -> dict[str, str | int | float | bool]:
    """Build scalar-only ChromaDB metadata for a chunk."""
    section_value = chunk.get("section")
    section = section_value if isinstance(section_value, str) else ""
    page_value = chunk.get("page")
    position_value = chunk.get("position")
    page = page_value if isinstance(page_value, int) and not isinstance(page_value, bool) else 0
    position = (
        position_value
        if isinstance(position_value, int) and not isinstance(position_value, bool)
        else 0
    )
    return {
        "document_id": document_id,
        "document_name": document_name,
        "section": section,
        "page": page,
        "position": position,
        "context_summary": build_context_summary(section, parent_heading),
    }
