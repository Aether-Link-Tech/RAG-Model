"""OCR clients (FR-059 / AL-239, AL-240). No cloud APIs: every model runs locally.

OCR_BACKEND chooses where the model runs:
  * "local" (default): LocalOCRClient loads the model inside the backend process (no extra
    service; the model is downloaded once into the Hugging Face cache).
  * "worker": HttpOCRClient calls the docker/ocr worker on an internal network.
OCR_ENGINE chooses the model ("florence" or "easyocr", see ocr_engines.py).

Any failure is reported per image as OCRFailure, never raised for a single image, so the
pipeline can skip it (FR-063).
"""
from __future__ import annotations

import asyncio
import base64
import threading
from dataclasses import dataclass
from typing import Protocol, Sequence, Union

import httpx

from backend.app.core.config import settings


@dataclass(frozen=True)
class OCRText:
    text: str
    confidence: float  # 0..1, mean token probability (Florence-2) or detector score (EasyOCR)
    engine: str


@dataclass(frozen=True)
class OCRFailure:
    reason: str


OCROutcome = Union[OCRText, OCRFailure]


class OCRClient(Protocol):
    async def recognize(self, images: Sequence[bytes], timeout: float) -> list[OCROutcome]: ...


class HttpOCRClient:
    """Talks to the OCR worker's POST /ocr endpoint."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def recognize(self, images: Sequence[bytes], timeout: float) -> list[OCROutcome]:
        payload = {"images": [base64.b64encode(data).decode("ascii") for data in images],
                   "timeout_seconds": max(1.0, timeout)}
        try:
            async with httpx.AsyncClient(timeout=timeout + 5) as client:
                response = await client.post(f"{self.base_url}/ocr", json=payload)
                response.raise_for_status()
                results = response.json()["results"]
        except httpx.TimeoutException:
            return [OCRFailure("OCR timed out")] * len(images)
        except (httpx.HTTPError, KeyError, ValueError):
            return [OCRFailure("OCR service unavailable")] * len(images)
        if len(results) != len(images):
            return [OCRFailure("OCR service returned an invalid response")] * len(images)
        outcomes: list[OCROutcome] = []
        for item in results:
            if item.get("error"):
                outcomes.append(OCRFailure(str(item["error"])[:300]))
            else:
                outcomes.append(OCRText(str(item.get("text") or ""), float(item.get("confidence") or 0.0),
                                        str(item.get("engine") or "unknown")))
        return outcomes


class LocalOCRClient:
    """Runs the OCR model in-process (in a thread, one inference at a time)."""

    def __init__(self, engine: str):
        from backend.app.services.ocr_engines import build_service

        self.service = build_service(engine)

    async def recognize(self, images: Sequence[bytes], timeout: float) -> list[OCROutcome]:
        results = await asyncio.to_thread(self.service.recognize, list(images), timeout)
        return [OCRFailure(r.error) if r.error else OCRText(r.text, r.confidence, r.engine) for r in results]


_client: OCRClient | None = None
_local_clients: dict[str, LocalOCRClient] = {}
_local_lock = threading.Lock()


def get_ocr_client() -> OCRClient:
    if _client is not None:
        return _client
    if settings.OCR_BACKEND == "worker":
        return HttpOCRClient(settings.OCR_SERVICE_URL)
    with _local_lock:  # load each model once per process
        if settings.OCR_ENGINE not in _local_clients:
            _local_clients[settings.OCR_ENGINE] = LocalOCRClient(settings.OCR_ENGINE)
        return _local_clients[settings.OCR_ENGINE]


def set_ocr_client(client: OCRClient | None) -> None:
    """Tests replace the worker with a fake engine."""
    global _client
    _client = client
