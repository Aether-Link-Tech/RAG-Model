"""AL-155 — FR-064 double-extension rejection tests.

Spec (Jira AL-155): report.xml.pdf -> reject, file.exe.pdf -> reject,
normal.pdf -> accept, archive.tar.gz.pdf -> flag. Blocklist (AL-154):
.exe .js .xml .bat .cmd .vbs .ps1 .scr .jar before the final extension.
"""
import pytest
from fastapi import HTTPException

documents_api = pytest.importorskip("backend.app.api.v1.documents")
if not hasattr(documents_api, "_validate_extension_chain"):
    pytest.skip("FR-064 (_validate_extension_chain) not merged yet", allow_module_level=True)

validate = documents_api._validate_extension_chain


def _rejected(filename: str) -> HTTPException:
    with pytest.raises(HTTPException) as info:
        validate(filename)
    assert info.value.status_code == 400
    return info.value


@pytest.mark.parametrize("filename", ["report.xml.pdf", "file.exe.pdf"])
def test_spec_dangerous_double_extensions_are_rejected(filename):
    _rejected(filename)


@pytest.mark.parametrize("filename", ["normal.pdf", "notes.txt", "thesis.docx"])
def test_spec_normal_filenames_are_accepted(filename):
    validate(filename)


def test_spec_long_extension_chain_is_flagged():
    detail = _rejected("archive.tar.gz.pdf").detail
    assert "review" in detail.lower() or "extension" in detail.lower()


@pytest.mark.parametrize("hidden", [".exe", ".js", ".xml", ".bat", ".cmd", ".vbs", ".ps1", ".scr", ".jar"])
def test_every_blocklisted_extension_is_rejected(hidden):
    _rejected(f"invoice{hidden}.pdf")


@pytest.mark.parametrize("filename", ["INVOICE.EXE.PDF", "Invoice.Exe.Pdf", "report.XmL.docx"])
def test_blocklist_is_case_insensitive(filename):
    _rejected(filename)


@pytest.mark.parametrize("filename", ["../../evil.exe.pdf", "C:\\temp\\evil.bat.txt", "dir/sub/evil.ps1.docx"])
def test_path_components_do_not_hide_the_double_extension(filename):
    _rejected(filename)
