"""Document text extraction service (FR-067 / AL-163 & AL-164).

Parses PDF, DOCX, and TXT files inside a hardened, isolated Docker container
with no network access, a read-only root filesystem, strict resource limits
(512 MB RAM, 1 CPU), a 60-second timeout, and bounded extracted output size.
"""
from __future__ import annotations

import logging
import os
import selectors
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".txt", ".pdf", ".docx"}
SUPPORTED_MIME_TYPES = {
    "text/plain",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class DocumentParserError(Exception):
    """Base exception for all document parser errors."""


class UnsupportedDocumentError(DocumentParserError, ValueError):
    """Raised when an uploaded file is not an allowed document type."""


class ParserSandboxError(DocumentParserError, RuntimeError):
    """Raised when sandboxed parsing fails, encounters execution errors, or exits abnormally."""


class ParserTimeoutError(ParserSandboxError, TimeoutError):
    """Raised when sandboxed parsing exceeds the maximum execution timeout (60s)."""


class ParserOutputLimitExceededError(ParserSandboxError, ValueError):
    """Raised when extracted document text exceeds the configured maximum byte limit."""


class ParserCancelledError(ParserSandboxError):
    """Raised when a sandboxed parsing task is cancelled by the caller."""


class ContainerCleanupError(DocumentParserError, RuntimeError):
    """Raised or logged when an active sandbox container fails to be removed."""


def _is_supported_suffix(suffix: str, mime_type: str) -> bool:
    return suffix in SUPPORTED_EXTENSIONS or mime_type in SUPPORTED_MIME_TYPES


def build_docker_run_command(
    *,
    container_name: str,
    host_file_path: str,
    container_input_path: str,
    mime_type: Optional[str] = None,
    docker_image: Optional[str] = None,
    memory_limit: Optional[str] = None,
    cpu_limit: Optional[str] = None,
) -> list[str]:
    """Construct hardened docker run invocation for document parsing (AL-163 & AL-164).

    Security constraints applied:
    - --network none: complete network isolation (no internet, no LAN, no host access).
    - --read-only: immutable root filesystem.
    - -v host_path:container_path:ro: input file mounted strictly read-only.
    - --tmpfs /tmp:rw,noexec,nosuid,size=64m: isolated in-memory scratch space.
    - --security-opt no-new-privileges:true: blocks setuid/setgid privilege escalation.
    - --cap-drop ALL: drops all Linux capabilities.
    - --user 10001:10001: runs as unprivileged non-root user.
    - --memory / --memory-swap: enforces 512 MB memory boundary.
    - --cpus: limits execution to 1 CPU core.
    - --pids-limit 64: prevents process fork bombs.
    - --rm: automatic container cleanup on completion.
    """
    image = docker_image or settings.PARSER_DOCKER_IMAGE
    mem = memory_limit or settings.PARSER_MEMORY_LIMIT
    cpus = str(cpu_limit or settings.PARSER_CPU_LIMIT)

    cmd = [
        "docker",
        "run",
        "--name",
        container_name,
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "--user",
        "10001:10001",
        "--memory",
        mem,
        "--memory-swap",
        mem,
        "--cpus",
        cpus,
        "--pids-limit",
        "64",
        "-v",
        f"{host_file_path}:{container_input_path}:ro",
        image,
        container_input_path,
    ]
    if mime_type:
        cmd.append(mime_type)
    return cmd


def _kill_and_remove_container(container_name: str, *, raise_on_failure: bool = False) -> bool:
    """Explicitly terminate and remove the sandbox container if still running.

    Returns True if the container was removed or was already non-existent.
    Returns False (and optionally raises ContainerCleanupError) if removal failed,
    ensuring cleanup failures are clearly surfaced operationally.
    """
    try:
        res = subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if res.returncode == 0:
            return True

        err = res.stderr.strip()
        # If already removed by --rm or prior call, that counts as clean
        if "no such container" in err.lower():
            return True

        logger.critical(
            "OPERATIONAL FAILURE: Failed to force-remove container %s (exit %d): %s",
            container_name,
            res.returncode,
            err,
        )
        if raise_on_failure:
            raise ContainerCleanupError(
                f"Failed to force-remove container {container_name}: {err}"
            )
        return False
    except Exception as exc:
        logger.critical(
            "OPERATIONAL FAILURE: Exception during force-remove of container %s: %s",
            container_name,
            exc,
        )
        if raise_on_failure:
            raise ContainerCleanupError(
                f"Exception during force-remove of container {container_name}: {exc}"
            ) from exc
        return False


def _communicate_with_limits(
    proc: subprocess.Popen,
    max_output_bytes: int,
    timeout_seconds: int,
    cancel_event: Optional[threading.Event] = None,
    max_stderr_bytes: int = 65536,
) -> tuple[str, str]:
    """Read process stdout and stderr while enforcing timeout, output size limit, and cancellation.

    Prevents unbounded memory consumption on the host backend and ensures rapid
    interruption when cancelled.
    """
    if proc.stdout is None or proc.stderr is None:
        raise ParserSandboxError("Subprocess pipes are unavailable")

    os.set_blocking(proc.stdout.fileno(), False)
    os.set_blocking(proc.stderr.fileno(), False)

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    sel.register(proc.stderr, selectors.EVENT_READ)

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_bytes = 0
    stderr_bytes = 0
    deadline = time.monotonic() + timeout_seconds

    try:
        while sel.get_map():
            if cancel_event and cancel_event.is_set():
                raise ParserCancelledError("Parsing operation was cancelled by caller")

            now = time.monotonic()
            if now >= deadline:
                raise subprocess.TimeoutExpired(cmd=proc.args, timeout=timeout_seconds)

            timeout_quantum = min(0.2, max(0.01, deadline - now))
            events = sel.select(timeout=timeout_quantum)

            for key, _ in events:
                try:
                    chunk = os.read(key.fd, 65536)
                except (BlockingIOError, InterruptedError):
                    continue

                if not chunk:
                    sel.unregister(key.fileobj)
                    continue

                if key.fileobj is proc.stdout:
                    stdout_bytes += len(chunk)
                    if stdout_bytes > max_output_bytes:
                        raise ParserOutputLimitExceededError(
                            f"Extracted document text exceeded maximum size limit of {max_output_bytes} bytes"
                        )
                    stdout_chunks.append(chunk)
                elif key.fileobj is proc.stderr:
                    if stderr_bytes < max_stderr_bytes:
                        stderr_chunks.append(chunk[: max_stderr_bytes - stderr_bytes])
                    stderr_bytes += len(chunk)

        remaining = max(0.1, deadline - time.monotonic())
        proc.wait(timeout=remaining)
    finally:
        sel.close()

    stdout_text = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return stdout_text, stderr_text


def extract_text_sandboxed(
    file_path: str | Path,
    mime_type: str,
    filename: str,
    *,
    timeout_seconds: Optional[int] = None,
    docker_image: Optional[str] = None,
    memory_limit: Optional[str] = None,
    cpu_limit: Optional[str] = None,
    max_output_bytes: Optional[int] = None,
    cancel_event: Optional[threading.Event] = None,
) -> str:
    """Execute text extraction inside an isolated Docker container with resource limits."""
    resolved_path = Path(file_path).resolve()
    if not resolved_path.exists() or not resolved_path.is_file():
        raise ParserSandboxError(f"Document file does not exist: {resolved_path.name}")

    timeout = timeout_seconds or settings.PARSER_TIMEOUT_SECONDS
    max_bytes = max_output_bytes or settings.PARSER_MAX_OUTPUT_BYTES
    container_name = f"rag-parser-{uuid.uuid4().hex[:12]}"
    safe_name = resolved_path.name or Path(filename).name or "document"
    container_input_path = f"/input/{safe_name}"

    cmd = build_docker_run_command(
        container_name=container_name,
        host_file_path=str(resolved_path),
        container_input_path=container_input_path,
        mime_type=mime_type,
        docker_image=docker_image,
        memory_limit=memory_limit,
        cpu_limit=cpu_limit,
    )

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = _communicate_with_limits(
            proc=proc,
            max_output_bytes=max_bytes,
            timeout_seconds=timeout,
            cancel_event=cancel_event,
        )
    except ParserOutputLimitExceededError as exc:
        _kill_and_remove_container(container_name)
        if proc:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        logger.error("Sandbox parsing output limit exceeded for %s: %s", filename, exc)
        raise
    except ParserCancelledError as exc:
        _kill_and_remove_container(container_name)
        if proc:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        logger.warning("Sandbox parsing cancelled for %s (container %s)", filename, container_name)
        raise
    except subprocess.TimeoutExpired as exc:
        _kill_and_remove_container(container_name)
        if proc:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        logger.error(
            "Sandbox parsing timed out after %s seconds for %s (container %s)",
            timeout,
            filename,
            container_name,
        )
        raise ParserTimeoutError(
            f"Parsing exceeded timeout of {timeout} seconds"
        ) from exc
    except FileNotFoundError as exc:
        logger.exception("Docker binary not found on host system")
        raise ParserSandboxError("Docker sandbox runtime is not installed or available") from exc
    except BaseException:
        _kill_and_remove_container(container_name)
        if proc:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        raise

    if proc.returncode != 0:
        _kill_and_remove_container(container_name)
        if proc.returncode == 137:
            logger.error("Sandbox container %s terminated by OOM/SIGKILL", container_name)
            raise ParserSandboxError("Parser container terminated due to resource limit (OOM)")
        if proc.returncode == 2:
            raise UnsupportedDocumentError(
                "Only PDF, DOCX, and TXT files can be indexed in this sprint"
            )

        meaningful_lines = [
            line.strip()
            for line in stderr.splitlines()
            if line.strip()
            and not line.strip().startswith(("What's next:", "Debug", "docker ai"))
        ]
        err_snippet = (
            meaningful_lines[0]
            if meaningful_lines
            else (stderr.strip().splitlines()[-1] if stderr.strip() else "Unknown parser error")
        )
        logger.error("Sandbox parsing failed with exit code %d: %s", proc.returncode, err_snippet)
        raise ParserSandboxError(f"Failed to parse document inside sandbox: {err_snippet}")

    return stdout


def extract_text_in_process(path: Path, mime_type: str, filename: str) -> str:
    """In-process text extraction used strictly in isolated tests when sandbox is disabled."""
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


def extract_text(
    file_path: str,
    mime_type: str,
    filename: str,
    *,
    cancel_event: Optional[threading.Event] = None,
    max_output_bytes: Optional[int] = None,
) -> str:
    """Extract text from a document.

    When `settings.PARSER_SANDBOX_ENABLED` is True (default for production),
    extraction MUST execute inside an isolated Docker container. If Docker is
    unavailable or sandbox execution fails, it fails safely without falling back
    to in-process parsing.
    In-process parsing is permitted only when `PARSER_SANDBOX_ENABLED` is
    explicitly False (for unit testing environments without Docker).
    """
    path = Path(file_path)
    suffix = path.suffix.lower() or Path(filename).suffix.lower()

    if not _is_supported_suffix(suffix, mime_type):
        raise UnsupportedDocumentError(
            "Only PDF, DOCX, and TXT files can be indexed in this sprint"
        )

    if settings.PARSER_SANDBOX_ENABLED:
        return extract_text_sandboxed(
            file_path,
            mime_type,
            filename,
            cancel_event=cancel_event,
            max_output_bytes=max_output_bytes,
        )

    # In-process parsing allowed ONLY when sandbox is explicitly disabled (test mode)
    return extract_text_in_process(path, mime_type, filename)


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
