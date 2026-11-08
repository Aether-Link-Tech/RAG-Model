"""Tests for Sandboxed File Parsing (FR-067 / AL-163 & AL-164).

Covers:
- AL-163: Docker sandbox execution, network isolation, read-only rootfs,
  unprivileged user, dropped capabilities, and read-only volume mounting.
- AL-164: Resource limits (512 MB RAM, 1 CPU, 64 PIDs), 60-second execution
  timeout, bounded extracted output size, fail-closed security, and explicit container termination/cleanup.
"""
from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.app.core.config import settings
from backend.app.models.document import Document
from backend.app.services.document_ingest import index_document
from backend.app.services.document_parser import (
    ContainerCleanupError,
    DocumentParserError,
    ParserCancelledError,
    ParserOutputLimitExceededError,
    ParserSandboxError,
    ParserTimeoutError,
    UnsupportedDocumentError,
    _kill_and_remove_container,
    build_docker_run_command,
    extract_text,
    extract_text_in_process,
    extract_text_sandboxed,
)
import docker.parser.runner as runner


# ---------------------------------------------------------------------------
# AL-163 & AL-164: Command Line Construction & Hardening Flags
# ---------------------------------------------------------------------------

def test_build_docker_run_command_hardening_flags():
    """Verify all mandated security hardening and resource limit flags are present."""
    cmd = build_docker_run_command(
        container_name="test-parser-1234",
        host_file_path="/host/data/report.pdf",
        container_input_path="/input/report.pdf",
        mime_type="application/pdf",
        docker_image="rag-parser-sandbox:latest",
        memory_limit="512m",
        cpu_limit="1.0",
    )

    # Core execution
    assert cmd[0] == "docker"
    assert cmd[1] == "run"
    assert "--name" in cmd
    assert cmd[cmd.index("--name") + 1] == "test-parser-1234"
    assert "--rm" in cmd

    # AL-163 Security Hardening
    assert "--network" in cmd
    assert cmd[cmd.index("--network") + 1] == "none"
    assert "--read-only" in cmd
    assert "--security-opt" in cmd
    assert cmd[cmd.index("--security-opt") + 1] == "no-new-privileges:true"
    assert "--cap-drop" in cmd
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert "--user" in cmd
    assert cmd[cmd.index("--user") + 1] == "10001:10001"
    assert "--tmpfs" in cmd
    assert cmd[cmd.index("--tmpfs") + 1] == "/tmp:rw,noexec,nosuid,size=64m"

    # Volume mount strictly read-only
    assert "-v" in cmd
    v_arg = cmd[cmd.index("-v") + 1]
    assert v_arg == "/host/data/report.pdf:/input/report.pdf:ro"
    assert v_arg.endswith(":ro")

    # AL-164 Resource Limits
    assert "--memory" in cmd
    assert cmd[cmd.index("--memory") + 1] == "512m"
    assert "--memory-swap" in cmd
    assert cmd[cmd.index("--memory-swap") + 1] == "512m"
    assert "--cpus" in cmd
    assert cmd[cmd.index("--cpus") + 1] == "1.0"
    assert "--pids-limit" in cmd
    assert cmd[cmd.index("--pids-limit") + 1] == "64"

    # Image and runner args
    assert "rag-parser-sandbox:latest" in cmd
    assert "/input/report.pdf" in cmd
    assert "application/pdf" in cmd


def test_build_docker_run_command_defaults_from_settings(monkeypatch):
    """Verify build_docker_run_command falls back to settings defaults."""
    monkeypatch.setattr(settings, "PARSER_DOCKER_IMAGE", "custom-parser:tag")
    monkeypatch.setattr(settings, "PARSER_MEMORY_LIMIT", "256m")
    monkeypatch.setattr(settings, "PARSER_CPU_LIMIT", "0.5")

    cmd = build_docker_run_command(
        container_name="auto-defaults",
        host_file_path="/tmp/test.txt",
        container_input_path="/input/test.txt",
    )
    assert cmd[cmd.index("--memory") + 1] == "256m"
    assert cmd[cmd.index("--cpus") + 1] == "0.5"
    assert "custom-parser:tag" in cmd


# ---------------------------------------------------------------------------
# Container Cleanup & Operational Failure Surfacing
# ---------------------------------------------------------------------------

def test_kill_and_remove_container_invokes_docker_rm():
    """Verify _kill_and_remove_container executes docker rm -f <container_name>."""
    mock_res = MagicMock()
    mock_res.returncode = 0
    with patch("subprocess.run", return_value=mock_res) as mock_run:
        result = _kill_and_remove_container("stale-container-123")
        assert result is True
        mock_run.assert_called_once_with(
            ["docker", "rm", "-f", "stale-container-123"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )


def test_kill_and_remove_container_surfaces_cleanup_failure():
    """Verify failed cleanup is operationally surfaced, not treated as successful."""
    mock_res = MagicMock()
    mock_res.returncode = 1
    mock_res.stderr = "Error response from daemon: permission denied"

    with patch("subprocess.run", return_value=mock_res):
        # Default returns False without masking failure
        success = _kill_and_remove_container("failing-container")
        assert success is False

        # With raise_on_failure=True, explicitly raises ContainerCleanupError
        with pytest.raises(ContainerCleanupError, match="Failed to force-remove container"):
            _kill_and_remove_container("failing-container", raise_on_failure=True)


def test_kill_and_remove_container_handles_already_removed():
    """Verify container already removed by --rm is treated as cleanly absent."""
    mock_res = MagicMock()
    mock_res.returncode = 1
    mock_res.stderr = "Error: No such container: stale-123"

    with patch("subprocess.run", return_value=mock_res):
        assert _kill_and_remove_container("stale-123") is True


# ---------------------------------------------------------------------------
# Sandboxed Extraction (Mocked Subprocess & Logic)
# ---------------------------------------------------------------------------

def test_extract_text_sandboxed_success(tmp_path):
    """Verify successful sandboxed extraction captures stdout."""
    sample_file = tmp_path / "notes.txt"
    sample_file.write_text("Confidential Research Notes", encoding="utf-8")

    mock_proc = MagicMock()
    mock_proc.returncode = 0

    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("backend.app.services.document_parser._communicate_with_limits",
               return_value=("Confidential Research Notes\nExtracted", "")):
        text = extract_text_sandboxed(sample_file, "text/plain", "notes.txt")
        assert "Confidential Research Notes" in text
        assert "Extracted" in text


def test_extract_text_sandboxed_missing_file():
    """Verify error when attempting to parse a non-existent file path."""
    with pytest.raises(ParserSandboxError, match="Document file does not exist"):
        extract_text_sandboxed("/non/existent/file.pdf", "application/pdf", "file.pdf")


def test_extract_text_sandboxed_timeout_kills_container(tmp_path):
    """AL-164: When parsing exceeds timeout, kill container and raise ParserTimeoutError."""
    sample_file = tmp_path / "hang.pdf"
    sample_file.write_text("dummy", encoding="utf-8")

    mock_proc = MagicMock()

    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("backend.app.services.document_parser._communicate_with_limits",
               side_effect=subprocess.TimeoutExpired(cmd="docker run", timeout=60)), \
         patch("backend.app.services.document_parser._kill_and_remove_container") as mock_cleanup:

        with pytest.raises(ParserTimeoutError, match="Parsing exceeded timeout of 60 seconds"):
            extract_text_sandboxed(sample_file, "application/pdf", "hang.pdf", timeout_seconds=60)

        mock_cleanup.assert_called_once()
        mock_proc.kill.assert_called_once()


def test_extract_text_sandboxed_output_limit_exceeded(tmp_path):
    """AL-164: Verify output size protection kills container and raises ParserOutputLimitExceededError."""
    sample_file = tmp_path / "huge.txt"
    sample_file.write_text("dummy", encoding="utf-8")

    mock_proc = MagicMock()

    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("backend.app.services.document_parser._communicate_with_limits",
               side_effect=ParserOutputLimitExceededError("Extracted text exceeded maximum size limit of 1000 bytes")), \
         patch("backend.app.services.document_parser._kill_and_remove_container") as mock_cleanup:

        with pytest.raises(ParserOutputLimitExceededError, match="exceeded maximum size limit"):
            extract_text_sandboxed(sample_file, "text/plain", "huge.txt", max_output_bytes=1000)

        mock_cleanup.assert_called_once()
        mock_proc.kill.assert_called_once()


def test_extract_text_sandboxed_cancellation_kills_and_cleans_container(tmp_path):
    """Verify that task cancellation via cancel_event triggers container cleanup and terminates subprocess."""
    sample_file = tmp_path / "cancel.pdf"
    sample_file.write_text("dummy", encoding="utf-8")

    mock_proc = MagicMock()
    cancel_event = threading.Event()
    cancel_event.set()

    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("backend.app.services.document_parser._communicate_with_limits",
               side_effect=ParserCancelledError("Parsing operation was cancelled by caller")), \
         patch("backend.app.services.document_parser._kill_and_remove_container") as mock_cleanup:

        with pytest.raises(ParserCancelledError):
            extract_text_sandboxed(sample_file, "application/pdf", "cancel.pdf", cancel_event=cancel_event)

        mock_cleanup.assert_called_once()
        mock_proc.kill.assert_called_once()


def test_extract_text_sandboxed_oom_killed_exit_code_137(tmp_path):
    """AL-164: Container exit code 137 indicates OOM / resource limit enforcement."""
    sample_file = tmp_path / "bomb.pdf"
    sample_file.write_text("dummy", encoding="utf-8")

    mock_proc = MagicMock()
    mock_proc.returncode = 137

    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("backend.app.services.document_parser._communicate_with_limits", return_value=("", "Killed\n")), \
         patch("backend.app.services.document_parser._kill_and_remove_container") as mock_cleanup:

        with pytest.raises(ParserSandboxError, match="resource limit \\(OOM\\)"):
            extract_text_sandboxed(sample_file, "application/pdf", "bomb.pdf")

        mock_cleanup.assert_called_once()


def test_extract_text_sandboxed_unsupported_exit_code_2(tmp_path):
    """AL-163: Container exit code 2 maps to UnsupportedDocumentError."""
    sample_file = tmp_path / "archive.zip"
    sample_file.write_text("dummy", encoding="utf-8")

    mock_proc = MagicMock()
    mock_proc.returncode = 2

    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("backend.app.services.document_parser._communicate_with_limits",
               return_value=("", "Unsupported document type: suffix=.zip\n")), \
         patch("backend.app.services.document_parser._kill_and_remove_container") as mock_cleanup:

        with pytest.raises(UnsupportedDocumentError, match="Only PDF, DOCX, and TXT files"):
            extract_text_sandboxed(sample_file, "application/zip", "archive.zip")

        mock_cleanup.assert_called_once()


def test_extract_text_sandboxed_general_failure_with_stderr(tmp_path):
    """Verify non-zero exit code cleans container and extracts clean error snippet."""
    sample_file = tmp_path / "corrupt.pdf"
    sample_file.write_text("corrupted", encoding="utf-8")

    mock_proc = MagicMock()
    mock_proc.returncode = 1

    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("backend.app.services.document_parser._communicate_with_limits",
               return_value=("", "Extraction failed: PdfReadError: EOF marker not found\nWhat's next:\nDebug container\n")), \
         patch("backend.app.services.document_parser._kill_and_remove_container") as mock_cleanup:

        with pytest.raises(ParserSandboxError, match="Extraction failed: PdfReadError: EOF marker not found"):
            extract_text_sandboxed(sample_file, "application/pdf", "corrupt.pdf")

        mock_cleanup.assert_called_once()


def test_extract_text_sandboxed_missing_docker_runtime(tmp_path):
    """Verify clear error when Docker CLI is not installed or accessible."""
    sample_file = tmp_path / "valid.txt"
    sample_file.write_text("valid", encoding="utf-8")

    with patch("subprocess.Popen", side_effect=FileNotFoundError("No such file: 'docker'")):
        with pytest.raises(ParserSandboxError, match="Docker sandbox runtime is not installed or available"):
            extract_text_sandboxed(sample_file, "text/plain", "valid.txt")


# ---------------------------------------------------------------------------
# Fail-Closed Security Policy & In-Process Fallback
# ---------------------------------------------------------------------------

def test_fail_closed_never_falls_back_in_production(tmp_path, monkeypatch):
    """CRITICAL SECURITY TEST: When PARSER_SANDBOX_ENABLED=True, failures must fail closed.

    Under no circumstances should the system silently fall back to parsing untrusted
    files in the FastAPI host process.
    """
    monkeypatch.setattr(settings, "PARSER_SANDBOX_ENABLED", True)

    sample_file = tmp_path / "untrusted.pdf"
    sample_file.write_text("dummy", encoding="utf-8")

    with patch("backend.app.services.document_parser.extract_text_sandboxed",
               side_effect=ParserSandboxError("Sandbox failure")):
        with patch("backend.app.services.document_parser.extract_text_in_process") as mock_in_process:
            with pytest.raises(ParserSandboxError, match="Sandbox failure"):
                extract_text(str(sample_file), "application/pdf", "untrusted.pdf")

            # Must NEVER execute in host process
            mock_in_process.assert_not_called()


def test_in_process_fallback_when_sandbox_explicitly_disabled(tmp_path, monkeypatch):
    """Verify in-process parsing is allowed ONLY when PARSER_SANDBOX_ENABLED=False (test mode)."""
    monkeypatch.setattr(settings, "PARSER_SANDBOX_ENABLED", False)

    sample_file = tmp_path / "test.txt"
    sample_file.write_text("In process test text", encoding="utf-8")

    text = extract_text(str(sample_file), "text/plain", "test.txt")
    assert text == "In process test text"


def test_unsupported_extension_rejected_immediately(tmp_path):
    """Verify unsupported extensions are rejected before Docker execution."""
    sample_file = tmp_path / "payload.exe"
    sample_file.write_text("binary", encoding="utf-8")

    with pytest.raises(UnsupportedDocumentError, match="Only PDF, DOCX, and TXT files"):
        extract_text(str(sample_file), "application/x-msdownload", "payload.exe")


# ---------------------------------------------------------------------------
# Ingestion Pipeline Integration: Document Status & Async Cancellation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_index_document_marks_failed_on_parser_sandbox_error():
    """Verify index_document marks document as 'failed' on sandbox failure."""
    mock_db = MagicMock()
    async def _async_noop(*args, **kwargs):
        pass
    mock_db.commit = _async_noop
    mock_db.refresh = _async_noop

    doc = Document(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        filename="corrupted.pdf",
        file_path="/tmp/corrupted.pdf",
        mime_type="application/pdf",
        file_size=1024,
        file_hash="dummy_hash",
        status="pending",
        visibility="private",
    )

    with patch("backend.app.services.document_ingest.extract_text",
               side_effect=ParserSandboxError("Failed to parse document inside sandbox: EOF error")):
        result = await index_document(mock_db, doc)
        assert result.status == "failed"
        assert "Failed to parse document inside sandbox" in result.error_message


@pytest.mark.asyncio
async def test_index_document_marks_failed_on_output_limit_exceeded():
    """Verify index_document marks document as 'failed' on output size exceeded."""
    mock_db = MagicMock()
    async def _async_noop(*args, **kwargs):
        pass
    mock_db.commit = _async_noop
    mock_db.refresh = _async_noop

    doc = Document(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        filename="huge.pdf",
        file_path="/tmp/huge.pdf",
        mime_type="application/pdf",
        file_size=2048,
        file_hash="dummy_hash_huge",
        status="pending",
        visibility="private",
    )

    with patch("backend.app.services.document_ingest.extract_text",
               side_effect=ParserOutputLimitExceededError("Extracted text exceeded maximum size limit")):
        result = await index_document(mock_db, doc)
        assert result.status == "failed"
        assert "exceeded maximum size limit" in result.error_message


@pytest.mark.asyncio
async def test_index_document_actual_asyncio_cancellation_marks_failed():
    """Verify that cancelling an active asyncio task marks document as failed and propagates CancelledError."""
    mock_db = MagicMock()
    async def _async_noop(*args, **kwargs):
        pass
    mock_db.commit = _async_noop
    mock_db.refresh = _async_noop

    doc = Document(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        filename="cancelled.pdf",
        file_path="/tmp/cancelled.pdf",
        mime_type="application/pdf",
        file_size=1024,
        file_hash="dummy_hash_cancel",
        status="pending",
        visibility="private",
    )

    # Simulate slow extract_text that responds to cancel_event
    def _slow_extract(*args, **kwargs):
        cancel_event = kwargs.get("cancel_event")
        for _ in range(50):
            if cancel_event and cancel_event.is_set():
                raise ParserCancelledError("Cancelled")
            import time
            time.sleep(0.05)
        return "Done"

    with patch("backend.app.services.document_ingest.extract_text", side_effect=_slow_extract):
        task = asyncio.create_task(index_document(mock_db, doc))
        await asyncio.sleep(0.02)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert doc.status == "failed"
        assert "cancelled" in doc.error_message.lower()


# ---------------------------------------------------------------------------
# Unit Tests for Sandbox Runner Script (docker/parser/runner.py)
# ---------------------------------------------------------------------------

def test_runner_extract_txt(tmp_path):
    """Verify runner.extract_txt extracts plain text."""
    txt_file = tmp_path / "sample.txt"
    txt_file.write_text("Hello runner test", encoding="utf-8")
    assert runner.extract_txt(txt_file) == "Hello runner test"


def test_runner_main_txt_stdout(tmp_path, monkeypatch, capsys):
    """Verify runner main() outputs extracted text to stdout and exits with code 0."""
    txt_file = tmp_path / "input.txt"
    txt_file.write_text("Extracted via runner", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["runner.py", str(txt_file), "text/plain"])
    ret = runner.main()
    assert ret == 0
    captured = capsys.readouterr()
    assert captured.out == "Extracted via runner"


def test_runner_main_missing_args(monkeypatch, capsys):
    """Verify runner main() returns 1 when arguments are missing."""
    monkeypatch.setattr(sys, "argv", ["runner.py"])
    ret = runner.main()
    assert ret == 1
    captured = capsys.readouterr()
    assert "Usage: runner.py" in captured.err


def test_runner_main_nonexistent_file(monkeypatch, capsys):
    """Verify runner main() returns 1 when file does not exist."""
    monkeypatch.setattr(sys, "argv", ["runner.py", "/non/existent/doc.pdf"])
    ret = runner.main()
    assert ret == 1
    captured = capsys.readouterr()
    assert "File not found" in captured.err


def test_runner_main_unsupported_type(tmp_path, monkeypatch, capsys):
    """Verify runner main() returns exit code 2 on unsupported file types."""
    unsupported_file = tmp_path / "script.py"
    unsupported_file.write_text("print('hello')", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["runner.py", str(unsupported_file), "text/x-python"])
    ret = runner.main()
    assert ret == 2
    captured = capsys.readouterr()
    assert "Unsupported document type" in captured.err


# ---------------------------------------------------------------------------
# Real Docker Container Integration Tests
# (AL-163 & AL-164 end-to-end verification against real Docker Desktop engine)
# ---------------------------------------------------------------------------

_docker_available = shutil.which("docker") is not None


@pytest.mark.skipif(not _docker_available, reason="Docker CLI not installed on host")
class TestDockerSandboxIntegration:
    """Live integration tests running real Docker containers with rag-parser-sandbox."""

    @pytest.fixture(autouse=True)
    def check_daemon_and_image(self):
        """Skip live Docker tests if daemon is not running or image is missing."""
        try:
            res = subprocess.run(
                ["docker", "image", "inspect", settings.PARSER_DOCKER_IMAGE],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if res.returncode != 0:
                pytest.skip(f"Docker image {settings.PARSER_DOCKER_IMAGE} not built or found")
        except Exception as exc:
            pytest.skip(f"Docker daemon unreachable: {exc}")

    def test_live_container_parses_txt_file(self, tmp_path):
        """Live sandbox execution extracts text from a .txt file."""
        test_file = tmp_path / "live_test.txt"
        test_file.write_text("Real Docker Sandbox Text Extraction", encoding="utf-8")

        extracted = extract_text_sandboxed(
            file_path=test_file,
            mime_type="text/plain",
            filename="live_test.txt",
        )
        assert extracted == "Real Docker Sandbox Text Extraction"

    def test_live_container_parses_docx_file(self, tmp_path):
        """Live sandbox execution extracts text from a .docx file."""
        import docx

        doc_file = tmp_path / "live_sample.docx"
        doc = docx.Document()
        doc.add_paragraph("Real Docker DOCX Parsing Verification")
        doc.save(str(doc_file))

        extracted = extract_text_sandboxed(
            file_path=doc_file,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="live_sample.docx",
        )
        assert "Real Docker DOCX Parsing Verification" in extracted

    def test_live_container_parses_pdf_file(self, tmp_path):
        """Live sandbox execution extracts text from a valid .pdf file."""
        header = b"%PDF-1.4\n"
        obj1 = b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        obj2 = b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        obj3 = b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
        obj4 = b"4 0 obj\n<< /Length 51 >>\nstream\nBT /F1 24 Tf 100 700 Td (Live Sandbox PDF Text) Tj ET\nendstream\nendobj\n"
        obj5 = b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"

        off1 = len(header)
        off2 = off1 + len(obj1)
        off3 = off2 + len(obj2)
        off4 = off3 + len(obj3)
        off5 = off4 + len(obj4)
        xref_off = off5 + len(obj5)

        xref = (
            f"xref\n0 6\n0000000000 65535 f \n"
            f"{off1:010d} 00000 n \n"
            f"{off2:010d} 00000 n \n"
            f"{off3:010d} 00000 n \n"
            f"{off4:010d} 00000 n \n"
            f"{off5:010d} 00000 n \n"
        ).encode()

        trailer = (
            f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_off}\n%%EOF\n"
        ).encode()

        pdf_bytes = header + obj1 + obj2 + obj3 + obj4 + obj5 + xref + trailer

        pdf_file = tmp_path / "live_sample.pdf"
        pdf_file.write_bytes(pdf_bytes)

        extracted = extract_text_sandboxed(
            file_path=pdf_file,
            mime_type="application/pdf",
            filename="live_sample.pdf",
        )
        assert "Live Sandbox PDF Text" in extracted

    def test_live_container_output_size_limit_kills_container(self, tmp_path):
        """Live container output exceeding max_output_bytes terminates container and raises error."""
        test_file = tmp_path / "big_output.txt"
        test_file.write_text("A" * 50000, encoding="utf-8")

        # Set strict 10 KB limit
        with pytest.raises(ParserOutputLimitExceededError, match="exceeded maximum size limit"):
            extract_text_sandboxed(
                file_path=test_file,
                mime_type="text/plain",
                filename="big_output.txt",
                max_output_bytes=10000,
            )

        # Confirm container was removed
        ps_check = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=rag-parser", "--format", "{{.Names}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        assert ps_check.stdout.strip() == ""

    def test_live_container_network_isolation(self):
        """Live container has no network access (--network none)."""
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python",
                settings.PARSER_DOCKER_IMAGE,
                "-c",
                "import socket; s = socket.socket(); s.settimeout(1); s.connect(('8.8.8.8', 53))",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        assert proc.returncode != 0
        assert "Network is unreachable" in proc.stderr or "OSError" in proc.stderr

    def test_live_container_rootfs_read_only(self):
        """Live container has immutable root filesystem (--read-only)."""
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "python",
                settings.PARSER_DOCKER_IMAGE,
                "-c",
                "open('/test_write.txt', 'w').write('fail')",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        assert proc.returncode != 0
        assert "Read-only file system" in proc.stderr

    def test_live_container_unprivileged_user(self):
        """Live container runs as unprivileged non-root user (UID 10001)."""
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "python",
                settings.PARSER_DOCKER_IMAGE,
                "-c",
                "import os; print(os.getuid(), os.getgid())",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        assert proc.returncode == 0
        assert proc.stdout.strip() == "10001 10001"

    def test_live_container_cleanup_after_completion(self, tmp_path):
        """Verify no stopped container is left behind after execution (--rm)."""
        test_file = tmp_path / "cleanup_test.txt"
        test_file.write_text("Cleanup verification", encoding="utf-8")

        unique_container_name = f"test-cleanup-{uuid.uuid4().hex[:8]}"
        cmd = build_docker_run_command(
            container_name=unique_container_name,
            host_file_path=str(test_file.resolve()),
            container_input_path="/input/cleanup_test.txt",
            mime_type="text/plain",
        )
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False)
        assert proc.returncode == 0

        # Verify container does not exist in docker ps -a
        ps_check = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={unique_container_name}", "--format", "{{.Names}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        assert unique_container_name not in ps_check.stdout.split()

    def test_live_container_cleanup_on_parser_failure(self, tmp_path):
        """Verify container is removed after parser failure (non-zero exit)."""
        corrupt_file = tmp_path / "corrupt_sample.pdf"
        corrupt_file.write_bytes(b"%PDF-1.4\nNOT_A_VALID_PDF_BODY\n")

        with pytest.raises(ParserSandboxError):
            extract_text_sandboxed(
                file_path=corrupt_file,
                mime_type="application/pdf",
                filename="corrupt_sample.pdf",
            )

        # Check no rag-parser containers remain in docker ps -a
        ps_check = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=rag-parser", "--format", "{{.Names}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        assert ps_check.stdout.strip() == ""

    def test_live_container_cleanup_on_live_timeout(self, tmp_path):
        """Verify container is force-killed and removed when live execution exceeds timeout."""
        slow_file = tmp_path / "slow.txt"
        slow_file.write_text("slow test", encoding="utf-8")

        unique_name = f"rag-parser-timeout-{uuid.uuid4().hex[:8]}"
        cmd = build_docker_run_command(
            container_name=unique_name,
            host_file_path=str(slow_file.resolve()),
            container_input_path="/input/slow.txt",
            mime_type="text/plain",
        )
        cmd.insert(cmd.index("rag-parser-sandbox:latest"), "--entrypoint")
        cmd.insert(cmd.index("rag-parser-sandbox:latest"), "sleep")
        cmd[cmd.index("/input/slow.txt")] = "10"
        if "text/plain" in cmd:
            cmd.remove("text/plain")

        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_and_remove_container(unique_name)
            if proc:
                try:
                    proc.kill()
                    proc.communicate(timeout=2)
                except Exception:
                    pass

        ps_check = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={unique_name}", "--format", "{{.Names}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        assert unique_name not in ps_check.stdout.split()

    def test_security_container_configuration_and_mount_isolation(self, tmp_path):
        """Security: Inspect actual container configuration to verify complete mount isolation."""
        test_file = tmp_path / "inspect_test.txt"
        test_file.write_text("Inspect Content", encoding="utf-8")

        unique_name = f"rag-inspect-{uuid.uuid4().hex[:8]}"
        cmd = build_docker_run_command(
            container_name=unique_name,
            host_file_path=str(test_file.resolve()),
            container_input_path="/input/inspect_test.txt",
            mime_type="text/plain",
        )
        # Add sleep so we can inspect the running container configuration
        cmd.insert(cmd.index("rag-parser-sandbox:latest"), "--entrypoint")
        cmd.insert(cmd.index("rag-parser-sandbox:latest"), "sleep")
        cmd[cmd.index("/input/inspect_test.txt")] = "5"
        if "text/plain" in cmd:
            cmd.remove("text/plain")

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            # Wait for container to start
            inspect_data = None
            for _ in range(20):
                inspect_res = subprocess.run(
                    ["docker", "inspect", unique_name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                if inspect_res.returncode == 0:
                    inspect_data = json.loads(inspect_res.stdout)[0]
                    break
                import time
                time.sleep(0.1)

            assert inspect_data is not None, "Failed to inspect container"

            # 1. Verify ReadonlyRootfs is true
            host_config = inspect_data.get("HostConfig", {})
            assert host_config.get("ReadonlyRootfs") is True

            # 2. Verify NetworkMode is none
            assert host_config.get("NetworkMode") == "none"

            # 3. Verify SecurityOpt has no-new-privileges
            security_opts = host_config.get("SecurityOpt", [])
            assert "no-new-privileges:true" in security_opts

            # 4. Verify CapDrop has ALL
            cap_drop = host_config.get("CapDrop", [])
            assert "ALL" in cap_drop

            # 5. Verify Memory & CPU limits
            assert host_config.get("Memory") == 512 * 1024 * 1024
            assert host_config.get("NanoCpus") == 1000000000  # 1.0 CPU

            # 6. Verify Mounts: only the input file is mounted from host and Mode is ro
            mounts = inspect_data.get("Mounts", [])
            bind_mounts = [m for m in mounts if m.get("Type") == "bind"]
            assert len(bind_mounts) == 1

            input_mount = bind_mounts[0]
            assert input_mount.get("Source") == str(test_file.resolve())
            assert input_mount.get("Destination") == "/input/inspect_test.txt"
            assert input_mount.get("Mode") == "ro"
            assert input_mount.get("RW") is False

            # 7. Verify NO docker.sock mount exists in any mount
            for m in mounts:
                assert "docker.sock" not in m.get("Source", "")
                assert "docker.sock" not in m.get("Destination", "")
        finally:
            _kill_and_remove_container(unique_name)
            try:
                proc.kill()
                proc.communicate(timeout=2)
            except Exception:
                pass

    def test_security_read_only_input_prevents_file_modification(self, tmp_path):
        """Security: Container cannot modify or overwrite the mounted input file."""
        test_file = tmp_path / "immutable_target.txt"
        test_file.write_text("ORIGINAL_CONTENT", encoding="utf-8")

        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "-v",
                f"{test_file.resolve()}:/input/target.txt:ro",
                "--entrypoint",
                "python",
                settings.PARSER_DOCKER_IMAGE,
                "-c",
                "open('/input/target.txt', 'w').write('HACKED')",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
        assert "Read-only file system" in proc.stderr
        # Verify host file remains completely untouched
        assert test_file.read_text(encoding="utf-8") == "ORIGINAL_CONTENT"

    def test_security_cannot_access_host_files(self):
        """Security: Container cannot traverse or read files from the host outside mount."""
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "python",
                settings.PARSER_DOCKER_IMAGE,
                "-c",
                "import os; os.listdir('/Users')",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
        assert "FileNotFoundError" in proc.stderr

    def test_security_command_injection_attempt_with_real_metacharacter_filename(self, tmp_path):
        """Security: File with real shell metacharacters in its actual on-disk filename cannot trigger shell execution."""
        proof_file = Path("rag_injection_proof.txt")
        if proof_file.exists():
            proof_file.unlink()

        try:
            # Real file on disk with shell metacharacters in its actual filename
            malicious_filename = "report;touch rag_injection_proof.txt;.txt"
            real_malicious_file = tmp_path / malicious_filename
            real_malicious_file.write_text("Legitimate Parsed Content", encoding="utf-8")

            extracted = extract_text_sandboxed(
                file_path=real_malicious_file,
                mime_type="text/plain",
                filename=malicious_filename,
            )
            # Text extracted normally via clean argument vector passing
            assert extracted == "Legitimate Parsed Content"

            # Host file must NEVER have been created by injected command
            assert not proof_file.exists()
        finally:
            if proof_file.exists():
                proof_file.unlink()
