"""Document text extraction service (FR-067 / AL-163 & AL-164).

Parses PDF, DOCX, and TXT files inside a hardened, isolated Docker container
with no network access, a read-only root filesystem, strict resource limits
(512 MB RAM, 1 CPU), a 60-second timeout, and bounded extracted output size.
"""
from __future__ import annotations

import logging
import os
import queue
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from backend.app.core.config import settings
from backend.app.services.storage_paths import (
    UnsafeStoragePathError,
    resolve_user_file_path,
    validate_safe_filename,
)

_UNSCOPED_PARSER_LOCAL_ENVS = frozenset({"development", "dev", "local"})

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
    input_mode: Optional[str] = None,
    parser_mode: str = "text",
) -> list[str]:
    """Construct hardened docker run invocation for document parsing (AL-163 & AL-164).

    parser_mode "layout" (FR-058) makes the runner return JSON with text blocks and
    embedded images instead of plain text; every sandbox flag stays the same.

    Security constraints applied:
    - --network none: complete network isolation (no internet, no LAN, no host access).
    - --read-only: immutable root filesystem.
    - input_mode "mount" (default): -v host_path:container_path:ro, input file
      mounted strictly read-only.
    - input_mode "stdin": no host mount at all; the file is streamed through
      stdin (-i). Needed when the backend itself runs in a container, where
      its file paths don't exist on the Docker host.
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
    ]
    if parser_mode not in ("text", "layout"):
        raise ValueError(f"Unsupported parser mode: {parser_mode!r}")
    if parser_mode != "text":
        cmd += ["-e", f"PARSER_MODE={parser_mode}"]
    mode = (input_mode or settings.PARSER_INPUT_MODE or "mount").lower()
    if mode == "stdin":
        suffix = Path(container_input_path).suffix.lower()
        cmd += ["-i", image, "-", mime_type or "", suffix]
        return cmd
    if mode != "mount":
        raise ValueError(f"Unsupported PARSER_INPUT_MODE: {mode!r}")

    cmd += [
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

    Cross-platform: one reader thread per pipe feeds a queue. (`selectors` only
    supports sockets on Windows and `os.set_blocking` is missing there before
    Python 3.12, so the previous implementation failed on Windows hosts.)
    """
    if proc.stdout is None or proc.stderr is None:
        raise ParserSandboxError("Subprocess pipes are unavailable")

    chunks: "queue.Queue[tuple[str, Optional[bytes]]]" = queue.Queue()

    def _pump(stream, tag: str) -> None:
        reader = getattr(stream, "read1", stream.read)
        try:
            while True:
                data = reader(65536)
                if not data:
                    break
                chunks.put((tag, data))
        except (OSError, ValueError):
            pass  # pipe closed because the container was killed
        finally:
            chunks.put((tag, None))

    for stream, tag in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        threading.Thread(target=_pump, args=(stream, tag), daemon=True).start()

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_bytes = 0
    stderr_bytes = 0
    open_streams = 2
    deadline = time.monotonic() + timeout_seconds

    while open_streams:
        if cancel_event and cancel_event.is_set():
            raise ParserCancelledError("Parsing operation was cancelled by caller")

        now = time.monotonic()
        if now >= deadline:
            raise subprocess.TimeoutExpired(cmd=proc.args, timeout=timeout_seconds)

        try:
            tag, chunk = chunks.get(timeout=min(0.2, max(0.01, deadline - now)))
        except queue.Empty:
            continue

        if chunk is None:
            open_streams -= 1
            continue

        if tag == "stdout":
            stdout_bytes += len(chunk)
            if stdout_bytes > max_output_bytes:
                raise ParserOutputLimitExceededError(
                    f"Extracted document text exceeded maximum size limit of {max_output_bytes} bytes"
                )
            stdout_chunks.append(chunk)
        else:
            if stderr_bytes < max_stderr_bytes:
                stderr_chunks.append(chunk[: max_stderr_bytes - stderr_bytes])
            stderr_bytes += len(chunk)

    remaining = max(0.1, deadline - time.monotonic())
    proc.wait(timeout=remaining)

    stdout_text = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return stdout_text, stderr_text


def _validate_parser_input_path(
    file_path: str | Path,
    filename: str,
    owner_id: Optional[uuid.UUID | str] = None,
) -> tuple[Path, str]:
    """Validate parser input path (and owner containment when ``owner_id`` is given)."""
    raw = str(file_path)
    if "\x00" in raw:
        raise UnsafeStoragePathError("Parser input path contains NUL byte.")

    if owner_id is not None:
        resolved = resolve_user_file_path(owner_id, raw, must_exist=False)
    else:
        env_normalized = (settings.ENV or "").lower().strip()
        if env_normalized not in _UNSCOPED_PARSER_LOCAL_ENVS:
            raise UnsafeStoragePathError(
                "owner_id is required for document parsing outside local development environments."
            )
        candidate = Path(raw)
        if candidate.is_symlink():
            raise UnsafeStoragePathError("Parser input path must not be a symlink.")
        resolved = candidate.resolve()
        if resolved.is_symlink():
            raise UnsafeStoragePathError("Parser input path must not be a symlink.")

    safe_name = resolved.name or Path(filename).name or "document"
    validate_safe_filename(safe_name)
    return resolved, safe_name


def _start_stdin_feeder(proc: subprocess.Popen, path: Path) -> None:
    """Stream the document into the sandbox's stdin from a background thread."""

    def _feed() -> None:
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise UnsafeStoragePathError("Parser input is not a regular file.")
                source = os.fdopen(fd, "rb")
            except BaseException:
                os.close(fd)
                raise
            with source:
                while chunk := source.read(65536):
                    proc.stdin.write(chunk)
        except (BrokenPipeError, OSError, ValueError):
            pass  # container exited or was killed; the reader reports the error
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

    threading.Thread(target=_feed, daemon=True).start()


def extract_text_sandboxed(
    file_path: str | Path,
    mime_type: str,
    filename: str,
    *,
    owner_id: Optional[uuid.UUID | str] = None,
    timeout_seconds: Optional[int] = None,
    docker_image: Optional[str] = None,
    memory_limit: Optional[str] = None,
    cpu_limit: Optional[str] = None,
    max_output_bytes: Optional[int] = None,
    cancel_event: Optional[threading.Event] = None,
    parser_mode: str = "text",
) -> str:
    """Execute text extraction inside an isolated Docker container with resource limits."""
    resolved_path, safe_name = _validate_parser_input_path(
        file_path, filename, owner_id=owner_id
    )
    if not resolved_path.exists() or not resolved_path.is_file():
        raise ParserSandboxError(f"Document file does not exist: {resolved_path.name}")

    timeout = timeout_seconds or settings.PARSER_TIMEOUT_SECONDS
    max_bytes = max_output_bytes or settings.PARSER_MAX_OUTPUT_BYTES
    container_name = f"rag-parser-{uuid.uuid4().hex[:12]}"
    container_input_path = f"/input/{safe_name}"

    use_stdin = (settings.PARSER_INPUT_MODE or "mount").lower() == "stdin"
    cmd = build_docker_run_command(
        container_name=container_name,
        host_file_path=str(resolved_path),
        container_input_path=container_input_path,
        mime_type=mime_type,
        docker_image=docker_image,
        memory_limit=memory_limit,
        cpu_limit=cpu_limit,
        parser_mode=parser_mode,
    )

    if not use_stdin:
        # Reduce mount-mode TOCTOU: verify the target is a non-symlink regular file right before launch.
        fd = os.open(resolved_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise UnsafeStoragePathError("Parser input is not a regular file.")
        finally:
            os.close(fd)

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if use_stdin else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if use_stdin:
            _start_stdin_feeder(proc, resolved_path)
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
    owner_id: Optional[uuid.UUID | str] = None,
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
    resolved_path, _ = _validate_parser_input_path(file_path, filename, owner_id=owner_id)
    suffix = resolved_path.suffix.lower() or Path(filename).suffix.lower()

    if not _is_supported_suffix(suffix, mime_type):
        raise UnsupportedDocumentError(
            "Only PDF, DOCX, and TXT files can be indexed in this sprint"
        )

    if settings.PARSER_SANDBOX_ENABLED:
        return extract_text_sandboxed(
            str(resolved_path),
            mime_type,
            filename,
            owner_id=owner_id,
            cancel_event=cancel_event,
            max_output_bytes=max_output_bytes,
        )

    # In-process parsing allowed ONLY when sandbox is explicitly disabled (test mode)
    return extract_text_in_process(resolved_path, mime_type, filename)


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


# --- Layout extraction with embedded images (FR-058 / AL-236) ---------------------------

_RUNNER_PATH = Path(__file__).resolve().parents[3] / "docker" / "parser" / "runner.py"


def _load_runner_module():
    """The sandbox runner, imported in-process (tests only, when the sandbox is disabled)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("rag_parser_runner", _RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise UnsupportedDocumentError("Parser runner is not available for in-process layout extraction")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extract_layout(
    file_path: str,
    mime_type: str,
    filename: str,
    *,
    owner_id: Optional[uuid.UUID | str] = None,
    cancel_event: Optional[threading.Event] = None,
) -> dict:
    """Text blocks and embedded images in reading order (see docker/parser/runner.py).

    Runs in the same hardened sandbox as extract_text; images come back base64-encoded in
    the JSON output, capped by the runner (40 images, 20 MB) and by
    PARSER_LAYOUT_MAX_OUTPUT_BYTES. Fails closed like extract_text.
    """
    import json

    resolved_path, _ = _validate_parser_input_path(file_path, filename, owner_id=owner_id)
    suffix = resolved_path.suffix.lower() or Path(filename).suffix.lower()
    if not _is_supported_suffix(suffix, mime_type):
        raise UnsupportedDocumentError("Only PDF, DOCX, and TXT files can be indexed in this sprint")

    if settings.PARSER_SANDBOX_ENABLED:
        raw = extract_text_sandboxed(
            str(resolved_path),
            mime_type,
            filename,
            owner_id=owner_id,
            cancel_event=cancel_event,
            max_output_bytes=settings.PARSER_LAYOUT_MAX_OUTPUT_BYTES,
            parser_mode="layout",
        )
        try:
            layout = json.loads(raw)
        except ValueError as exc:
            raise ParserSandboxError("Parser returned an invalid layout") from exc
    else:
        layout = _load_runner_module().extract_layout(resolved_path, mime_type)
    if not isinstance(layout, dict) or layout.get("version") != 1:
        raise ParserSandboxError("Parser returned an unsupported layout version")
    return layout
