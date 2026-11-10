# pyright: reportMissingImports=false
"""Sandboxed document parser entrypoint (FR-067 / AL-163).

Executed inside an isolated Docker container with no network access,
read-only rootfs, and non-root user. Reads the mounted input file and
outputs extracted text to stdout.
"""
from __future__ import annotations

import importlib
import json
import os
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


# --- Layout mode (FR-058 / AL-236) ---------------------------------------------------
# PARSER_MODE=layout returns JSON with text blocks and embedded images in reading order:
#   {"version": 1, "pages": [{"page": 1, "blocks": [{"type": "text", "bbox": [...], "text": "..."},
#                                                   {"type": "image", "bbox": [...], "image": 0}]}],
#    "images": [{"index": 0, "page": 1, "bbox": [...], "ext": "png", "width": 640, "height": 480,
#                "data": "<base64>"}],
#    "skipped_images": 0}
# Images travel back in the JSON (base64) so the sandbox keeps --network none, --read-only
# and no writable host mount. Caps keep the output bounded.
MAX_IMAGES = 40
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
MIN_IMAGE_SIDE = 24  # icons and bullets carry no text worth OCR
OCR_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "tiff", "tif", "webp"}


class _ImageCollector:
    def __init__(self) -> None:
        self.images: list[dict] = []
        self.total = 0
        self.skipped = 0

    def add(self, data: bytes, ext: str, page, bbox, width, height):
        import base64

        ext = (ext or "").lower().lstrip(".")
        too_small = width is not None and height is not None and min(width, height) < MIN_IMAGE_SIDE
        if (
            ext not in OCR_IMAGE_EXTENSIONS
            or too_small
            or len(data) > MAX_IMAGE_BYTES
            or len(self.images) >= MAX_IMAGES
            or self.total + len(data) > MAX_TOTAL_IMAGE_BYTES
        ):
            self.skipped += 1
            return None
        index = len(self.images)
        self.total += len(data)
        self.images.append({
            "index": index, "page": page, "bbox": bbox, "ext": "jpg" if ext == "jpeg" else ext,
            "width": width, "height": height, "data": base64.b64encode(data).decode("ascii"),
        })
        return index


def _bbox(values) -> list[float]:
    return [round(float(v), 1) for v in values]


def layout_pdf(path: Path) -> dict:
    import fitz  # type: ignore  # PyMuPDF

    collector = _ImageCollector()
    pages = []
    with fitz.open(str(path)) as doc:
        for number, page in enumerate(doc, start=1):
            blocks = []
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") == 0:
                    text = "\n".join(
                        "".join(span["text"] for span in line["spans"]) for line in block.get("lines", [])
                    ).strip()
                    if text:
                        blocks.append({"type": "text", "bbox": _bbox(block["bbox"]), "text": text})
                elif block.get("type") == 1 and block.get("image"):
                    bbox = _bbox(block["bbox"])
                    index = collector.add(block["image"], block.get("ext", ""), number, bbox,
                                          block.get("width"), block.get("height"))
                    if index is not None:
                        blocks.append({"type": "image", "bbox": bbox, "image": index})
            blocks.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))  # top-to-bottom, left-to-right
            pages.append({"page": number, "blocks": blocks})
    return {"version": 1, "pages": pages, "images": collector.images, "skipped_images": collector.skipped}


def _image_size(data: bytes):
    try:
        import fitz  # type: ignore

        pix = fitz.Pixmap(data)
        return pix.width, pix.height
    except Exception:
        return None, None


def layout_docx(path: Path) -> dict:
    import docx  # type: ignore
    from docx.oxml.ns import qn  # type: ignore

    document = docx.Document(str(path))
    collector = _ImageCollector()
    blocks = []
    for paragraph in document.paragraphs:  # body order; DOCX has no fixed pages or coordinates
        text = paragraph.text.strip()
        if text:
            blocks.append({"type": "text", "bbox": None, "text": text})
        for blip in paragraph._p.iter(qn("a:blip")):
            part = document.part.related_parts.get(blip.get(qn("r:embed")))
            if part is None:
                continue
            ext = part.content_type.split("/")[-1]
            width, height = _image_size(part.blob)
            index = collector.add(part.blob, ext, None, None, width, height)
            if index is not None:
                blocks.append({"type": "image", "bbox": None, "image": index})
    return {"version": 1, "pages": [{"page": None, "blocks": blocks}], "images": collector.images,
            "skipped_images": collector.skipped}


def layout_txt(path: Path) -> dict:
    text = extract_txt(path).strip()
    blocks = [{"type": "text", "bbox": None, "text": text}] if text else []
    return {"version": 1, "pages": [{"page": 1, "blocks": blocks}], "images": [], "skipped_images": 0}


def extract_layout(path: Path, mime_type: str = "") -> dict:
    suffix = path.suffix.lower()
    if mime_type == "application/pdf" or suffix == ".pdf":
        return layout_pdf(path)
    if suffix == ".docx" or mime_type.endswith("wordprocessingml.document"):
        return layout_docx(path)
    if mime_type == "text/plain" or suffix == ".txt":
        return layout_txt(path)
    raise ValueError(f"Unsupported document type: suffix={suffix}, mime={mime_type}")


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

    if os.environ.get("PARSER_MODE", "text") == "layout":
        try:
            if suffix not in STDIN_SUFFIXES:
                sys.stderr.write(f"Unsupported document type: suffix={suffix}, mime={mime_type}\n")
                return 2
            json.dump(extract_layout(file_path, mime_type), sys.stdout)
            sys.stdout.flush()
            return 0
        except Exception as exc:
            sys.stderr.write(f"Extraction failed: {type(exc).__name__}: {exc}\n")
            return 1

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
