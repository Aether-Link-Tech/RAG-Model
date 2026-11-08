"""AL-158 — FR-065 MIME-type spoofing tests.

Spec (Jira AL-158): .exe renamed to .pdf -> reject. Real PDF -> accept.
ZIP renamed to .pdf -> reject. DOCX -> accept (zip under the hood).
"""
import io
import zipfile

import pytest
from fastapi import HTTPException, UploadFile

from backend.app.api.v1 import documents as documents_api

validate = documents_api._validate_file_magic_bytes

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<\n>>\nendobj\ntrailer\n<<\n>>\n%%EOF"
EXE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" + b"\x00" * 64 + b"This program cannot be run in DOS mode."


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


REAL_DOCX = _zip({
    "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
    "word/document.xml": b'<?xml version="1.0"?><w:document/>',
})
PLAIN_ZIP = _zip({"payload.bin": b"\x00\x01\x02", "readme.txt": b"not a word document"})


def _upload(data: bytes, filename: str) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename)


async def _assert_rejected(data: bytes, filename: str, suffix: str) -> None:
    with pytest.raises(HTTPException) as info:
        await validate(_upload(data, filename), suffix)
    assert info.value.status_code == 400


# --- Spec cases ---------------------------------------------------------------

async def test_spec_exe_renamed_to_pdf_is_rejected():
    await _assert_rejected(EXE_BYTES, "invoice.pdf", ".pdf")


async def test_spec_real_pdf_is_accepted():
    await validate(_upload(PDF_BYTES, "invoice.pdf"), ".pdf")


async def test_spec_zip_renamed_to_pdf_is_rejected():
    await _assert_rejected(PLAIN_ZIP, "archive.pdf", ".pdf")


async def test_spec_real_docx_is_accepted():
    await validate(_upload(REAL_DOCX, "thesis.docx"), ".docx")


# --- Additional spoofing cases ----------------------------------------------

async def test_plain_zip_renamed_to_docx_is_rejected():
    """A ZIP without word/document.xml must not pass as DOCX (also covers AL-152)."""
    await _assert_rejected(PLAIN_ZIP, "fake.docx", ".docx")


async def test_exe_renamed_to_docx_is_rejected():
    await _assert_rejected(EXE_BYTES, "fake.docx", ".docx")


async def test_exe_renamed_to_txt_is_rejected():
    await _assert_rejected(EXE_BYTES, "notes.txt", ".txt")


async def test_pdf_renamed_to_txt_with_binary_body_is_rejected():
    await _assert_rejected(PDF_BYTES + b"\x00\xff\xfe binary stream", "notes.txt", ".txt")


async def test_valid_utf8_text_is_accepted():
    await validate(_upload("Résumé — plain UTF-8 text.\n".encode("utf-8"), "notes.txt"), ".txt")


async def test_validation_rewinds_the_stream_for_later_hashing():
    upload = _upload(PDF_BYTES, "invoice.pdf")
    await validate(upload, ".pdf")
    assert await upload.read() == PDF_BYTES
