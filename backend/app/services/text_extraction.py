"""Extract page- and section-aware text from supported document formats."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import TypeAlias


TextBlock: TypeAlias = dict[str, int | str]


class TextExtractionError(ValueError):
    """Raised when a document is empty, corrupted, or cannot be decoded."""


def extract_text(file_path: str | Path) -> list[TextBlock]:
    """Extract text blocks from a PDF, DOCX, or TXT file."""
    path = Path(file_path)
    suffix = path.suffix.lower()
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise TextExtractionError(f"Unable to read document: {path.name}.") from exc

    if not content:
        raise TextExtractionError(f"Document is empty: {path.name}.")

    if suffix == ".pdf":
        return _extract_pdf(content, path.name)
    if suffix == ".docx":
        return _extract_docx(content, path.name)
    if suffix == ".txt":
        return _extract_txt(content, path.name)
    raise TextExtractionError(f"Unsupported document type: {suffix or '(no extension)'}.")


def _extract_pdf(content: bytes, filename: str) -> list[TextBlock]:
    try:
        from PyPDF2 import PdfReader
    except ImportError as exc:
        raise TextExtractionError("PyPDF2 is required to extract PDF files.") from exc

    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise TextExtractionError(f"Encrypted PDF cannot be extracted: {filename}.")
        page_count = len(reader.pages)
        fallback_text = [page.extract_text() or "" for page in reader.pages]
    except TextExtractionError:
        raise
    except Exception as exc:
        raise TextExtractionError(f"Corrupted or unreadable PDF: {filename}.") from exc

    try:
        import pdfplumber
    except ImportError as exc:
        raise TextExtractionError("pdfplumber is required to extract PDF files.") from exc

    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            if len(pdf.pages) != page_count:
                raise TextExtractionError(f"Corrupted PDF page structure: {filename}.")
            page_texts = [page.extract_text() or "" for page in pdf.pages]
    except TextExtractionError:
        raise
    except Exception as exc:
        raise TextExtractionError(f"Corrupted or unreadable PDF: {filename}.") from exc

    blocks: list[TextBlock] = []
    for page_number, text in enumerate(page_texts, start=1):
        page_text = (text or fallback_text[page_number - 1]).strip()
        if page_text:
            blocks.append({"page": page_number, "section": "", "text": page_text})
    if not blocks:
        raise TextExtractionError(f"PDF contains no extractable text: {filename}.")
    return blocks


def _extract_docx(content: bytes, filename: str) -> list[TextBlock]:
    try:
        from docx import Document
    except ImportError as exc:
        raise TextExtractionError("python-docx is required to extract DOCX files.") from exc

    try:
        document = Document(BytesIO(content))
        blocks: list[TextBlock] = []
        section = ""
        section_text: list[str] = []

        def append_section() -> None:
            if section_text or section:
                blocks.append(
                    {"page": 1, "section": section, "text": "\n".join(section_text)}
                )
                section_text.clear()

        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            if paragraph.style and paragraph.style.name.startswith("Heading"):
                append_section()
                section = text
            else:
                section_text.append(text)
        append_section()
    except TextExtractionError:
        raise
    except Exception as exc:
        raise TextExtractionError(f"Corrupted or unreadable DOCX: {filename}.") from exc

    if not any(block["text"] or block["section"] for block in blocks):
        raise TextExtractionError(f"DOCX contains no extractable text: {filename}.")
    return blocks


def _extract_txt(content: bytes, filename: str) -> list[TextBlock]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            import chardet

            encoding = chardet.detect(content).get("encoding")
            if not encoding:
                raise TextExtractionError(
                    f"Unable to detect text encoding: {filename}."
                )
            text = content.decode(encoding)
        except TextExtractionError:
            raise
        except Exception as exc:
            raise TextExtractionError(
                f"Unable to detect text encoding: {filename}."
            ) from exc

    if not text.strip():
        raise TextExtractionError(f"TXT file contains no extractable text: {filename}.")
    return [{"page": 1, "section": "", "text": text}]
