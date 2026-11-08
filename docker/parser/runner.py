# pyright: reportMissingImports=false
"""Sandboxed document parser entrypoint (FR-067 / AL-163).

Executed inside an isolated Docker container with no network access,
read-only rootfs, and non-root user. Reads the mounted input file and
outputs extracted text to stdout.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path


def extract_txt(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def extract_pdf(path: Path) -> str:
    try:
        from PyPDF2 import PdfReader  # type: ignore
    except ImportError:
        try:
            pypdf_mod = importlib.import_module("pypdf")
            PdfReader = getattr(pypdf_mod, "PdfReader")
        except ImportError as exc:
            raise RuntimeError("PDF parsing library not available") from exc

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def extract_docx(path: Path) -> str:
    import docx  # type: ignore

    document = docx.Document(str(path))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


MAX_STDIN_BYTES = 30 * 1024 * 1024  # above the 25 MB upload limit
STDIN_SUFFIXES = {".txt", ".pdf", ".docx"}


def _read_stdin_to_tmp(suffix: str) -> Path | None:
    """Stdin mode: copy the streamed document into the tmpfs scratch area."""
    if suffix not in STDIN_SUFFIXES:
        sys.stderr.write(f"Unsupported document type: suffix={suffix}\n")
        return None
    data = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(data) > MAX_STDIN_BYTES:
        sys.stderr.write("Input exceeds maximum size\n")
        return None
    target = Path("/tmp") / f"input{suffix}"
    target.write_bytes(data)
    return target


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: runner.py <file_path|-> [mime_type] [suffix]\n")
        return 1

    mime_type = sys.argv[2] if len(sys.argv) > 2 else ""
    if sys.argv[1] == "-":
        suffix = (sys.argv[3] if len(sys.argv) > 3 else "").lower()
        streamed = _read_stdin_to_tmp(suffix)
        if streamed is None:
            return 2 if suffix not in STDIN_SUFFIXES else 1
        file_path = streamed
    else:
        file_path = Path(sys.argv[1])

    if not file_path.exists() or not file_path.is_file():
        sys.stderr.write(f"File not found: {file_path}\n")
        return 1

    suffix = file_path.suffix.lower()

    try:
        if mime_type == "text/plain" or suffix == ".txt":
            text = extract_txt(file_path)
        elif mime_type == "application/pdf" or suffix == ".pdf":
            text = extract_pdf(file_path)
        elif (
            mime_type
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            or suffix == ".docx"
        ):
            text = extract_docx(file_path)
        else:
            sys.stderr.write(f"Unsupported document type: suffix={suffix}, mime={mime_type}\n")
            return 2

        # Emit extracted text cleanly to stdout
        sys.stdout.write(text)
        sys.stdout.flush()
        return 0
    except Exception as exc:
        sys.stderr.write(f"Extraction failed: {type(exc).__name__}: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
