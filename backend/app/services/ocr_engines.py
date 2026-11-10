"""OCR engines (FR-059 / AL-239, AL-240), used in two ways:

  * OCR_BACKEND=local  (default) the backend loads the model in-process: no extra service.
  * OCR_BACKEND=worker the docker/ocr worker container imports this same file.

OCR_ENGINE selects the model: "florence" (Florence-2-base, EasyOCR fallback) or "easyocr"
(EasyOCR only: smaller and faster, less accurate on difficult text).

Primary: Florence-2-base (MIT licence) via transformers' native Florence2 support.
Fallback: EasyOCR (Apache-2.0), CPU friendly. Jira named MonkeyOCRv2-B as the fallback;
it is a ~3B-parameter model that needs a GPU server, so EasyOCR is used on this CPU-only
stack (decision recorded on AL-240).

Every image gets its own result: a corrupt image, an engine error or a timeout affects
only that image, never the batch.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger("ocr.engines")

FLORENCE_REPO = os.getenv("OCR_FLORENCE_REPO", "florence-community/Florence-2-base")
MAX_SIDE = int(os.getenv("OCR_MAX_IMAGE_SIDE", "2048"))


@dataclass
class Result:
    text: str = ""
    confidence: float = 0.0
    engine: str = ""
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {"text": self.text, "confidence": round(self.confidence, 4), "engine": self.engine, "error": self.error}


class Engine(Protocol):
    name: str

    def recognize(self, images: Sequence[Image.Image]) -> list[tuple[str, float]]: ...


def decode(data: bytes) -> Image.Image:
    """Bytes -> RGB image, downscaled to MAX_SIDE. Raises ValueError for corrupt data."""
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Corrupt or unsupported image") from exc
    image = image.convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    return image


class Florence2Engine:
    name = "florence-2-base"

    def __init__(self) -> None:
        import torch
        from transformers import AutoProcessor, Florence2ForConditionalGeneration

        self._torch = torch
        self.processor = AutoProcessor.from_pretrained(FLORENCE_REPO)
        self.model = Florence2ForConditionalGeneration.from_pretrained(FLORENCE_REPO, dtype=torch.float32).eval()

    def recognize(self, images: Sequence[Image.Image]) -> list[tuple[str, float]]:
        torch = self._torch
        inputs = self.processor(text=["<OCR>"] * len(images), images=list(images), return_tensors="pt")
        with torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=512, num_beams=1, do_sample=False,
                                      output_scores=True, return_dict_in_generate=True)
        texts = self.processor.batch_decode(out.sequences, skip_special_tokens=True)
        scores = self.model.compute_transition_scores(out.sequences, out.scores, normalize_logits=True)
        results = []
        pad = self.processor.tokenizer.pad_token_id
        generated = out.sequences[:, -scores.shape[1]:]
        for text, row, tokens in zip(texts, scores, generated):
            mask = tokens != pad
            probs = torch.exp(row[mask]) if mask.any() else torch.tensor([0.0])
            results.append((text.strip(), float(probs.mean())))
        return results


class EasyOCREngine:
    name = "easyocr"

    def __init__(self) -> None:
        import easyocr

        storage = os.getenv("EASYOCR_MODULE_PATH")
        self.reader = easyocr.Reader(["en"], gpu=False, model_storage_directory=storage,
                                     download_enabled=os.getenv("HF_HUB_OFFLINE") != "1", verbose=False)

    def recognize(self, images: Sequence[Image.Image]) -> list[tuple[str, float]]:
        import numpy as np

        results = []
        for image in images:
            found = self.reader.readtext(np.asarray(image), detail=1, paragraph=False)
            found.sort(key=lambda item: (round(item[0][0][1] / 20), item[0][0][0]))  # rows, then left to right
            text = " ".join(item[1] for item in found).strip()
            confidence = sum(float(item[2]) for item in found) / len(found) if found else 0.0
            results.append((text, confidence))
        return results


class OCRService:
    """Primary engine with a lazily loaded fallback; thread-safe, one inference at a time."""

    def __init__(self, primary_factory=Florence2Engine, fallback_factory=EasyOCREngine) -> None:
        self._primary_factory, self._fallback_factory = primary_factory, fallback_factory
        self._primary: Optional[Engine] = None
        self._fallback: Optional[Engine] = None
        self._primary_error: Optional[str] = None
        self._lock = threading.Lock()

    def _engine(self, which: str) -> Optional[Engine]:
        if which == "primary":
            if self._primary is None and self._primary_error is None:
                try:
                    self._primary = self._primary_factory()
                except Exception as exc:  # model missing offline, out of memory, ...
                    self._primary_error = f"{type(exc).__name__}: {exc}"[:200]
                    logger.error("Primary OCR engine unavailable: %s", self._primary_error)
            return self._primary
        if self._fallback is None:
            if self._fallback_factory is None:
                return None
            try:
                self._fallback = self._fallback_factory()
            except Exception as exc:
                logger.error("Fallback OCR engine unavailable: %s", exc)
                return None
        return self._fallback

    def status(self) -> dict:
        return {"primary": getattr(self._primary, "name", None), "primary_error": self._primary_error,
                "fallback": getattr(self._fallback, "name", None)}

    def recognize(self, payloads: Sequence[bytes], timeout_seconds: float) -> list[Result]:
        deadline = time.monotonic() + timeout_seconds
        results = [Result() for _ in payloads]
        images: dict[int, Image.Image] = {}
        for i, data in enumerate(payloads):
            try:
                images[i] = decode(data)
            except ValueError as exc:
                results[i].error = str(exc)
        with self._lock:
            pending = list(images)
            primary = self._engine("primary")
            if primary is not None and pending and time.monotonic() < deadline:
                try:
                    for i, (text, conf) in zip(pending, primary.recognize([images[i] for i in pending])):
                        results[i] = Result(text, conf, primary.name)
                    pending = []
                except Exception as exc:
                    logger.warning("Primary OCR failed, trying fallback: %s", exc)
            for i in pending:  # fallback image by image, so one bad image cannot sink the rest
                if time.monotonic() >= deadline:
                    results[i].error = "OCR timed out"
                    continue
                fallback = self._engine("fallback")
                if fallback is None:
                    results[i].error = "No OCR engine available"
                    continue
                try:
                    text, conf = fallback.recognize([images[i]])[0]
                    results[i] = Result(text, conf, fallback.name)
                except Exception as exc:
                    results[i].error = f"OCR failed: {type(exc).__name__}"
        return results


ENGINES = ("florence", "easyocr")


def build_service(engine: str = "florence") -> OCRService:
    """OCRService for OCR_ENGINE: "florence" (with EasyOCR fallback) or "easyocr" alone."""
    if engine == "florence":
        return OCRService(Florence2Engine, EasyOCREngine)
    if engine == "easyocr":
        return OCRService(EasyOCREngine, None)
    raise ValueError(f"Unknown OCR engine {engine!r}; choose one of {ENGINES}")
