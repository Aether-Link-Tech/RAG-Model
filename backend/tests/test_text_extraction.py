from __future__ import annotations

import io
from pathlib import Path

import pytest

from backend.app.services.text_extraction import TextExtractionError, extract_text


def _pdf_with_pages(page_texts: list[str]) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{3 + index * 2} 0 R".encode() for index in range(len(page_texts)))
        + f"] /Count {len(page_texts)} >>".encode(),
    ]
    for index, text in enumerate(page_texts):
        page_id = 3 + index * 2
        content_id = page_id + 1
        escaped_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped_text}) Tj ET".encode()
        objects.extend(
            [
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {3 + len(page_texts) * 2} 0 R >> >> "
                    f"/Contents {content_id} 0 R >>"
                ).encode(),
                f"<< /Length {len(stream)} >>\nstream\n".encode()
                + stream
                + b"\nendstream",
            ]
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    output = io.BytesIO()
    output.write(b"%PDF-1.4\n")
    offsets = [0]
    for object_id, body in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{object_id} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_offset = output.tell()
    output.write(f"xref\n0 {len(offsets)}\n".encode())
    output.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return output.getvalue()


def test_extract_pdf_preserves_one_based_page_numbers(tmp_path: Path) -> None:
    pdf_path = tmp_path / "pages.pdf"
    pdf_path.write_bytes(_pdf_with_pages(["First page text", "Second page text"]))

    result = extract_text(pdf_path)

    assert result == [
        {"page": 1, "section": "", "text": "First page text"},
        {"page": 2, "section": "", "text": "Second page text"},
    ]


def test_extract_docx_preserves_heading_sections(tmp_path: Path) -> None:
    from docx import Document

    document = Document()
    document.add_heading("Introduction", level=1)
    document.add_paragraph("Opening paragraph.")
    document.add_heading("Details", level=2)
    document.add_paragraph("Supporting paragraph.")
    path = tmp_path / "sections.docx"
    document.save(path)

    assert extract_text(path) == [
        {"page": 1, "section": "Introduction", "text": "Opening paragraph."},
        {"page": 1, "section": "Details", "text": "Supporting paragraph."},
    ]


def test_extract_txt_as_utf8(tmp_path: Path) -> None:
    path = tmp_path / "utf8.txt"
    path.write_text("Résumé — café", encoding="utf-8")

    assert extract_text(path) == [
        {"page": 1, "section": "", "text": "Résumé — café"}
    ]


def test_extract_txt_with_detected_fallback_encoding(tmp_path: Path) -> None:
    path = tmp_path / "legacy.txt"
    path.write_bytes("Résumé — café".encode("cp1252"))

    result = extract_text(path)

    assert result == [{"page": 1, "section": "", "text": "Résumé — café"}]


def test_empty_file_raises_specific_error(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")

    with pytest.raises(TextExtractionError, match="Document is empty"):
        extract_text(path)


def test_corrupted_pdf_raises_specific_error(tmp_path: Path) -> None:
    path = tmp_path / "corrupted.pdf"
    path.write_bytes(b"not a PDF")

    with pytest.raises(TextExtractionError, match="Corrupted or unreadable PDF"):
        extract_text(path)
