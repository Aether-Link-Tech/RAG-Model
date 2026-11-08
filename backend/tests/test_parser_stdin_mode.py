"""PARSER_INPUT_MODE=stdin: the document is streamed into the sandbox (no host mount).

Needed when the backend runs in a container, where its file paths don't exist
on the Docker host. Hardening flags must be identical to the mount mode.
"""
import io
import shutil
import subprocess
import zipfile

import pytest

from backend.app.core.config import settings
from backend.app.services.document_parser import build_docker_run_command, extract_text_sandboxed

HARDENING = [
    ("--network", "none"),
    ("--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"),
    ("--security-opt", "no-new-privileges:true"),
    ("--cap-drop", "ALL"),
    ("--user", "10001:10001"),
    ("--pids-limit", "64"),
]


def _cmd(mode):
    return build_docker_run_command(
        container_name="c1",
        host_file_path="/host/uploads/u/doc.pdf",
        container_input_path="/input/doc.pdf",
        mime_type="application/pdf",
        input_mode=mode,
    )


def test_stdin_mode_has_no_host_mount_and_streams_input():
    cmd = _cmd("stdin")
    assert "-v" not in cmd and not any("/host/uploads" in part for part in cmd)
    assert "-i" in cmd
    image_at = cmd.index(settings.PARSER_DOCKER_IMAGE)
    assert cmd[image_at + 1:] == ["-", "application/pdf", ".pdf"]


@pytest.mark.parametrize("mode", ["stdin", "mount"])
def test_both_modes_keep_every_hardening_flag(mode):
    cmd = _cmd(mode)
    for flag, value in HARDENING:
        assert cmd[cmd.index(flag) + 1] == value, flag
    assert "--read-only" in cmd and "--rm" in cmd


def test_mount_mode_is_unchanged_default():
    cmd = _cmd("mount")
    assert cmd[cmd.index("-v") + 1] == "/host/uploads/u/doc.pdf:/input/doc.pdf:ro"
    assert "-i" not in cmd


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        _cmd("network")


# ------------------------------------------------------------------ live sandbox

def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "image", "inspect", settings.PARSER_DOCKER_IMAGE], capture_output=True, timeout=20
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


live = pytest.mark.skipif(not _docker_ready(), reason="Docker / rag-parser-sandbox image not available")


def _docx_bytes(text: str) -> bytes:
    import docx  # python-docx

    buffer = io.BytesIO()
    document = docx.Document()
    document.add_paragraph(text)
    document.save(buffer)
    return buffer.getvalue()


def _pdf_bytes(text: str) -> bytes:
    import fitz  # PyMuPDF

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    return document.tobytes()


@live
@pytest.mark.parametrize(
    "name,mime,make",
    [
        ("notes.txt", "text/plain", lambda t: t.encode("utf-8")),
        ("report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", _docx_bytes),
        ("paper.pdf", "application/pdf", _pdf_bytes),
    ],
)
def test_live_stdin_mode_extracts_each_type(tmp_path, monkeypatch, name, mime, make):
    monkeypatch.setattr(settings, "PARSER_INPUT_MODE", "stdin")
    source = tmp_path / name
    source.write_bytes(make("Streamed sandbox extraction works"))

    text = extract_text_sandboxed(source, mime, name)

    assert "Streamed sandbox extraction works" in text


@live
def test_live_stdin_mode_rejects_unsupported_type(tmp_path, monkeypatch):
    from backend.app.services.document_parser import UnsupportedDocumentError

    monkeypatch.setattr(settings, "PARSER_INPUT_MODE", "stdin")
    source = tmp_path / "evil.sh"
    source.write_bytes(b"echo nope")
    with pytest.raises(UnsupportedDocumentError):
        extract_text_sandboxed(source, "text/x-shellscript", "evil.sh")
