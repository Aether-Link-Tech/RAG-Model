"""OCR engines (FR-059 / AL-239, AL-240 / AL-283), run inside the backend process (no external API).

Primary: Florence-2-base (MIT licence) via transformers' native Florence2 support, CPU friendly.
The model is downloaded once into the Hugging Face cache, then loaded from disk.

Fallback (AL-283): RapidOCR (PaddleOCR detection + recognition models in ONNX, Apache-2.0). The
models ship inside the wheel (~15 MB, nothing downloaded at run time), it loads in ~0.5 s and
reads an image in ~1-3 s on CPU. It is used for an image when Florence-2 cannot be loaded, fails
on that image, returns nothing, or is less confident than OCR_FALLBACK_MIN_CONFIDENCE.

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
FALLBACK_ENABLED = os.getenv("OCR_FALLBACK_ENABLED", "True").lower() in ("true", "1")
FALLBACK_MIN_CONFIDENCE = float(os.getenv("OCR_FALLBACK_MIN_CONFIDENCE", "0.5"))


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


def load_florence(processor_cls, model_cls, torch):
    """Load Florence-2 from the local Hugging Face cache first, so a cached model never touches
    the network (offline-first; an unreachable hub otherwise burns the whole OCR time budget).
    Only when the model is not cached yet is it downloaded once from the hub."""
    try:
        processor = processor_cls.from_pretrained(FLORENCE_REPO, local_files_only=True)
        model = model_cls.from_pretrained(FLORENCE_REPO, dtype=torch.float32, local_files_only=True)
    except OSError:
        logger.info("Florence-2 not in the local cache; downloading %s once", FLORENCE_REPO)
        processor = processor_cls.from_pretrained(FLORENCE_REPO)
        model = model_cls.from_pretrained(FLORENCE_REPO, dtype=torch.float32)
    return processor, model.eval()


class Florence2Engine:
    name = "florence-2-base"

    def __init__(self) -> None:
        import torch
        from transformers import AutoProcessor, Florence2ForConditionalGeneration

        self._torch = torch
        self.processor, self.model = load_florence(AutoProcessor, Florence2ForConditionalGeneration, torch)

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


class RapidOCREngine:
    """Fallback engine (AL-283): PaddleOCR models in ONNX via rapidocr_onnxruntime."""

    name = "rapidocr"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR

        self._ocr = RapidOCR()

    @staticmethod
    def reading_order(boxes: list) -> list:
        """Sort detected text boxes top-to-bottom, then left-to-right within a line."""
        def top(b):
            return min(pt[1] for pt in b[0])

        def left(b):
            return min(pt[0] for pt in b[0])

        def height(b):
            return max(pt[1] for pt in b[0]) - top(b)

        lines: list[list] = []
        for box in sorted(boxes, key=top):
            if lines and abs(top(box) - top(lines[-1][0])) <= max(4.0, 0.5 * height(lines[-1][0])):
                lines[-1].append(box)
            else:
                lines.append([box])
        return [b for line in lines for b in sorted(line, key=left)]

    def recognize(self, images: Sequence[Image.Image]) -> list[tuple[str, float]]:
        import numpy as np

        results = []
        for image in images:
            boxes, _ = self._ocr(np.array(image))
            boxes = self.reading_order(boxes or [])
            text = " ".join(str(b[1]).strip() for b in boxes if str(b[1]).strip())
            confidence = sum(float(b[2]) for b in boxes) / len(boxes) if boxes else 0.0
            results.append((text, confidence))
        return results


class _Lazy:
    """Loads an engine once, on first use; remembers a load failure instead of retrying."""

    def __init__(self, factory) -> None:
        self.factory = factory
        self.engine: Optional[Engine] = None
        self.error: Optional[str] = None

    def get(self) -> Optional[Engine]:
        if self.factory is None:
            return None
        if self.engine is None and self.error is None:
            try:
                self.engine = self.factory()
            except Exception as exc:  # model files missing, out of memory, package missing...
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                logger.error("OCR engine %s unavailable: %s", getattr(self.factory, "name", self.factory), self.error)
        return self.engine


class OCRService:
    """Florence-2 OCR with a RapidOCR fallback (AL-283); engines load lazily, one inference at a time.

    1. Primary pass: all images in one batch; if the batch fails, each image is retried alone, so
       a single bad image cannot sink the others.
    2. Fallback pass (AL-283): images the primary could not read, read empty, or read with low
       confidence go to the fallback engine. The better of the two results is kept.
    Everything stops at the per-document deadline; images left over get "OCR timed out" and are
    skipped by FR-063.
    """

    def __init__(self, engine_factory=Florence2Engine, fallback_factory=None) -> None:
        if fallback_factory is None and FALLBACK_ENABLED and engine_factory is Florence2Engine:
            fallback_factory = RapidOCREngine
        self._primary = _Lazy(engine_factory)
        self._fallback = _Lazy(fallback_factory)
        self._lock = threading.Lock()

    def engine(self) -> Optional[Engine]:
        return self._primary.get()

    def status(self) -> dict:
        return {
            "engine": getattr(self._primary.engine, "name", None),
            "load_error": self._primary.error,
            "fallback_engine": getattr(self._fallback.engine, "name", None),
            "fallback_load_error": self._fallback.error,
            "fallback_configured": self._fallback.factory is not None,
        }

    @staticmethod
    def _needs_fallback(result: Result) -> bool:
        return bool(result.error) or not result.text.strip() or result.confidence < FALLBACK_MIN_CONFIDENCE

    def _run(self, engine: Engine, images: dict[int, Image.Image], indices: list[int],
             deadline: float, results: list[Result], keep_better: bool) -> None:
        """Recognise `indices` with `engine` into `results` (batch first, then one by one)."""
        def store(i: int, text: str, conf: float) -> None:
            new = Result(text, conf, engine.name)
            if not keep_better or self._better(new, results[i]):
                results[i] = new

        pending = list(indices)
        if pending and time.monotonic() < deadline:
            try:
                for i, (text, conf) in zip(pending, engine.recognize([images[i] for i in pending])):
                    store(i, text, conf)
                pending = []
            except Exception as exc:
                logger.warning("OCR batch failed on %s, retrying images one by one: %s", engine.name, exc)
        for i in pending:
            if time.monotonic() >= deadline:
                if not results[i].text:
                    results[i].error = "OCR timed out"
                continue
            try:
                text, conf = engine.recognize([images[i]])[0]
                store(i, text, conf)
            except Exception as exc:
                if not results[i].text:
                    results[i].error = f"OCR failed: {type(exc).__name__}"

    @staticmethod
    def _better(new: Result, old: Result) -> bool:
        if old.error or not old.text.strip():
            return bool(new.text.strip())
        return bool(new.text.strip()) and new.confidence > old.confidence

    def recognize(self, payloads: Sequence[bytes], timeout_seconds: float) -> list[Result]:
        deadline = time.monotonic() + timeout_seconds
        results = [Result() for _ in payloads]
        images: dict[int, Image.Image] = {}
        for i, data in enumerate(payloads):
            try:
                images[i] = decode(data)
            except ValueError as exc:
                results[i].error = str(exc)
        if not images:
            return results
        with self._lock:
            primary = self._primary.get()
            if primary is not None:
                self._run(primary, images, list(images), deadline, results, keep_better=False)
            else:
                for i in images:
                    results[i].error = "OCR model unavailable"

            retry = [i for i in images if self._needs_fallback(results[i])]
            if retry and time.monotonic() < deadline:
                fallback = self._fallback.get()
                if fallback is not None:
                    logger.info("OCR fallback (%s) for %d image(s)", fallback.name, len(retry))
                    self._run(fallback, images, retry, deadline, results, keep_better=True)
            for i in images:  # a better fallback read clears the primary's error
                if results[i].text.strip() and results[i].engine and results[i].error:
                    results[i].error = None
        return results
