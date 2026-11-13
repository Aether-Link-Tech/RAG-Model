"""ClamAV streaming scan and quarantine helpers."""
from __future__ import annotations

import errno
import os
import shutil
import uuid
from pathlib import Path

import clamd

from backend.app.core.config import settings
from backend.app.services.storage_paths import (
    build_quarantine_path,
    open_user_file_no_follow,
    resolve_user_file_path,
)


class ClamAVUnavailableError(RuntimeError):
    """The scanner is unavailable or returned an unusable response."""


class InfectedFileError(RuntimeError):
    """An uploaded file was detected and moved to quarantine."""

    def __init__(self, signature: str) -> None:
        self.signature = signature
        super().__init__(signature)


def _move_to_quarantine_no_overwrite(
    owner_id: uuid.UUID,
    source: Path,
    destination: Path,
) -> None:
    """Move ``source`` to ``destination`` without overwriting an existing quarantine file."""
    try:
        os.link(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(destination, flags, 0o600)
        try:
            dst_file = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            destination.unlink(missing_ok=True)
            raise
        try:
            with dst_file, open_user_file_no_follow(owner_id, source) as src_file:
                shutil.copyfileobj(src_file, dst_file)
                dst_file.flush()
                os.fsync(dst_file.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    try:
        os.unlink(source)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def scan_and_quarantine(
    path: str,
    user_id: uuid.UUID | str,
    document_id: uuid.UUID | str,
) -> None:
    """Scan a staged upload through INSTREAM, moving detections to quarantine.

    Streaming keeps ClamAV independent of the API host's filesystem layout.
    Failures to reach ClamAV are fail-closed: callers must not accept the file.
    """
    owner_id = uuid.UUID(str(user_id))
    doc_id = uuid.UUID(str(document_id))
    resolved_source = resolve_user_file_path(owner_id, path, must_exist=True)
    # Validate the quarantine destination before connecting or moving anything.
    build_quarantine_path(owner_id, doc_id, create_dir=False)

    try:
        client = clamd.ClamdNetworkSocket(
            host=settings.CLAMAV_HOST,
            port=settings.CLAMAV_PORT,
            timeout=settings.CLAMAV_TIMEOUT_SECONDS,
        )
        if client.ping() != "PONG":
            raise ClamAVUnavailableError("ClamAV did not respond to ping")
        with open_user_file_no_follow(owner_id, resolved_source) as upload_stream:
            result = client.instream(upload_stream)
    except ClamAVUnavailableError:
        raise
    except Exception as exc:
        raise ClamAVUnavailableError("ClamAV scan request failed") from exc

    verdict = (result or {}).get("stream")
    if not verdict or len(verdict) != 2:
        raise ClamAVUnavailableError("ClamAV returned an invalid scan result")
    state, signature = verdict
    if state == "OK":
        return
    if state == "FOUND":
        resolved_source = resolve_user_file_path(owner_id, path, must_exist=True)
        quarantine_path = build_quarantine_path(owner_id, doc_id, create_dir=True)
        try:
            _move_to_quarantine_no_overwrite(owner_id, resolved_source, quarantine_path)
        except OSError as exc:
            raise ClamAVUnavailableError("Detected file could not be quarantined") from exc
        raise InfectedFileError(str(signature or "malware detected"))
    raise ClamAVUnavailableError(f"ClamAV returned unexpected status: {state}")
