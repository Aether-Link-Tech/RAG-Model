"""Client for the OCR worker (FR-059 / AL-239, AL-240).

The OCR models (Florence-2-base, EasyOCR fallback) run in a separate container
(docker/ocr) on an internal network without internet access. The backend sends image
bytes in batches and gets text + confidence per image. Any failure is reported per
image as OCRFailure, never raised for a single image, so the pipeline can skip it
(FR-063).
"""
from __future__ import annotations

import base64
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


_client: OCRClient | None = None


def get_ocr_client() -> OCRClient:
    return _client or HttpOCRClient(settings.OCR_SERVICE_URL)


def set_ocr_client(client: OCRClient | None) -> None:
    """Tests replace the worker with a fake engine."""
    global _client
    _client = client
