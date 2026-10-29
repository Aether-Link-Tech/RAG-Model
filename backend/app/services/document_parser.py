from pathlib import Path


class UnsupportedDocumentError(ValueError):
    pass


def extract_text(file_path: str, mime_type: str, filename: str) -> str:
    path = Path(file_path)
    suffix = path.suffix.lower() or Path(filename).suffix.lower()

    if mime_type == "text/plain" or suffix == ".txt":
        return path.read_text(encoding="utf-8", errors="ignore")

    if mime_type == "application/pdf" or suffix == ".pdf":
        return _extract_pdf(path)

    if (
        mime_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or suffix == ".docx"
    ):
        return _extract_docx(path)

    raise UnsupportedDocumentError(
        "Only PDF, DOCX, and TXT files can be indexed in this sprint"
    )


def _extract_pdf(path: Path) -> str:
    try:
        from PyPDF2 import PdfReader
    except ImportError as exc:
        raise UnsupportedDocumentError("PyPDF2 is required to parse PDF files") from exc

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def _extract_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise UnsupportedDocumentError("python-docx is required to parse DOCX files") from exc

    document = Document(str(path))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)
