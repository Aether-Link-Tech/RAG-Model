"""Merge OCR text into the document's reading order (FR-060 / AL-243) and build chunks (FR-061).

The layout from the parser lists text blocks and images per page in reading order. OCR
text replaces each image at its exact position, inside the surrounding section. Text
chunks are marked source_type "text", OCR chunks source_type "ocr" with page, bbox, image
id and confidence, so search results can show "source: image on page X".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from backend.app.services.chunking import split_text
from backend.app.services.document_images import ImageOCR


@dataclass(frozen=True)
class Segment:
    source_type: str  # "text" | "ocr"
    text: str
    page: Optional[int] = None
    image_id: Optional[str] = None
    bbox: Optional[list] = None
    confidence: Optional[float] = None


def merge_layout(layout: dict, ocr: dict[int, ImageOCR]) -> list[Segment]:
    segments: list[Segment] = []
    for page in layout.get("pages", []):
        number = page.get("page")
        buffer: list[str] = []

        def flush() -> None:
            if buffer:
                segments.append(Segment("text", "\n".join(buffer), number))
                buffer.clear()

        for block in page.get("blocks", []):
            if block.get("type") == "text":
                if block.get("text"):
                    buffer.append(block["text"])
                continue
            result = ocr.get(block.get("image"))
            if result is None or result.status != "done" or not result.text:
                continue  # skipped images leave no gap in the text (FR-063)
            flush()
            segments.append(Segment("ocr", result.text, number, str(result.image_id), block.get("bbox"),
                                    result.confidence))
        flush()
    return segments


def merged_text(segments: list[Segment]) -> str:
    return "\n\n".join(s.text for s in segments)


def chunks_from_segments(segments: list[Segment]) -> list[tuple[str, dict[str, Any]]]:
    """(chunk text, extra metadata) in document order; text never spans an OCR block."""
    chunks: list[tuple[str, dict[str, Any]]] = []
    for segment in segments:
        meta: dict[str, Any] = {"source_type": segment.source_type}
        if segment.page is not None:
            meta["page"] = int(segment.page)
        if segment.source_type == "ocr":
            meta["image_id"] = segment.image_id
            if segment.bbox is not None:
                meta["bbox"] = json.dumps(segment.bbox)  # Chroma metadata values must be scalars
            if segment.confidence is not None:
                meta["ocr_confidence"] = float(segment.confidence)
        for piece in split_text(segment.text):
            chunks.append((piece, dict(meta)))
    return chunks
