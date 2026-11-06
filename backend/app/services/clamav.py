"""ClamAV streaming scan and quarantine helpers."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import clamd

from backend.app.core.config import settings


class ClamAVUnavailableError(RuntimeError):
    """The scanner is unavailable or returned an unusable response."""


class InfectedFileError(RuntimeError):
    """An uploaded file was detected and moved to quarantine."""

    def __init__(self, signature: str) -> None:
        self.signature = signature
        super().__init__(signature)


def scan_and_quarantine(
    path: str,
    user_id: uuid.UUID | str,
    document_id: uuid.UUID | str,
) -> None:
    """Scan a staged upload through INSTREAM, moving detections to quarantine.

    Streaming keeps ClamAV independent of the API host's filesystem layout.
    Failures to reach ClamAV are fail-closed: callers must not accept the file.
    """
    try:
        client = clamd.ClamdNetworkSocket(
            host=settings.CLAMAV_HOST,
            port=settings.CLAMAV_PORT,
            timeout=settings.CLAMAV_TIMEOUT_SECONDS,
        )
        if client.ping() != "PONG":
            raise ClamAVUnavailableError("ClamAV did not respond to ping")
        with open(path, "rb") as upload_stream:
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
        quarantine_dir = Path(settings.CLAMAV_QUARANTINE_DIR) / str(user_id)
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        quarantine_path = quarantine_dir / str(document_id)
        try:
            os.replace(path, quarantine_path)
        except OSError as exc:
            raise ClamAVUnavailableError("Detected file could not be quarantined") from exc
        raise InfectedFileError(str(signature or "malware detected"))
    raise ClamAVUnavailableError(f"ClamAV returned unexpected status: {state}")
