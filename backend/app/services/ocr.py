"""OCR client (FR-059 / AL-239, AL-240). No external APIs: the models run inside the backend.

Florence-2-base reads the text (see ocr_engines.py).
The model is loaded once per process, on first use, from the local Hugging Face cache.
Any failure is reported per image as OCRFailure, never raised for a single image, so the
pipeline can skip it (FR-063).
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Protocol, Sequence, Union


@dataclass(frozen=True)
class OCRText:
    text: str
    confidence: float  # 0..1, mean token probability from Florence-2
    engine: str


@dataclass(frozen=True)
class OCRFailure:
    reason: str


OCROutcome = Union[OCRText, OCRFailure]


class OCRClient(Protocol):
    async def recognize(self, images: Sequence[bytes], timeout: float) -> list[OCROutcome]: ...


class LocalOCRClient:
    """Runs the OCR models in-process (in a thread, one inference at a time)."""

    def __init__(self) -> None:
        from backend.app.services.ocr_engines import OCRService

        self.service = OCRService()

    async def recognize(self, images: Sequence[bytes], timeout: float) -> list[OCROutcome]:
        results = await asyncio.to_thread(self.service.recognize, list(images), timeout)
        return [OCRFailure(r.error) if r.error else OCRText(r.text, r.confidence, r.engine) for r in results]


_client: OCRClient | None = None
_local: LocalOCRClient | None = None
_lock = threading.Lock()


def get_ocr_client() -> OCRClient:
    global _local
    if _client is not None:
        return _client
    with _lock:  # load the models once per process
        if _local is None:
            _local = LocalOCRClient()
        return _local


def set_ocr_client(client: OCRClient | None) -> None:
    """Tests replace the models with a fake engine."""
    global _client
    _client = client
