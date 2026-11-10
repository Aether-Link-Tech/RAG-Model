"""OCR worker HTTP API (FR-059 / AL-239). Internal network only, no internet access.

POST /ocr     {"images": ["<base64>", ...], "timeout_seconds": 30}
          ->  {"results": [{"text", "confidence", "engine", "error"}, ...]}   (same order)
GET  /health  {"status": "ok", "primary": ..., "fallback": ...}
"""
from __future__ import annotations

import base64
import binascii
import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ocr_engines import build_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

MAX_IMAGES_PER_REQUEST = int(os.getenv("OCR_MAX_IMAGES_PER_REQUEST", "16"))
MAX_TIMEOUT = float(os.getenv("OCR_MAX_TIMEOUT_SECONDS", "60"))

app = FastAPI(title="Aether Link OCR worker", docs_url=None, redoc_url=None)
service = build_service(os.getenv("OCR_ENGINE", "florence"))


class OCRRequest(BaseModel):
    images: list[str] = Field(..., min_length=1, max_length=MAX_IMAGES_PER_REQUEST)
    timeout_seconds: float = Field(30.0, gt=0, le=MAX_TIMEOUT)


@app.on_event("startup")
async def warm_up() -> None:
    await run_in_threadpool(service._engine, "primary")  # load Florence-2 once at start


@app.get("/health")
def health() -> dict:
    return {"status": "ok", **service.status()}


@app.post("/ocr")
async def ocr(request: OCRRequest) -> dict:
    payloads = []
    for item in request.images:
        try:
            payloads.append(base64.b64decode(item, validate=True))
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Images must be base64-encoded") from exc
    results = await run_in_threadpool(service.recognize, payloads, request.timeout_seconds)
    return {"results": [r.as_dict() for r in results]}
