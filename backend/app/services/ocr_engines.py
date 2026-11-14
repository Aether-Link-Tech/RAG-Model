"""OCR engines (FR-059 / AL-239, AL-240), run inside the backend process (no external API).

Florence-2-base (MIT licence) via transformers' native Florence2 support, CPU friendly.
The model is downloaded once into the Hugging Face cache, then loaded from disk.

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


class OCRService:
    """Florence-2 OCR, loaded once and lazily; thread-safe, one inference at a time.

    Images are recognised in one batch. If the batch fails, each image is retried on its
    own so a single bad image cannot sink the others; whatever still fails is returned as
    an error for that image (FR-063 then skips it). There is deliberately no second model:
    a system-wide fallback strategy is planned after the project is complete.
    """

    def __init__(self, engine_factory=Florence2Engine) -> None:
        self._factory = engine_factory
        self._engine: Optional[Engine] = None
        self._load_error: Optional[str] = None
        self._lock = threading.Lock()

    def engine(self) -> Optional[Engine]:
        if self._engine is None and self._load_error is None:
            try:
                self._engine = self._factory()
            except Exception as exc:  # model files missing, out of memory, ...
                self._load_error = f"{type(exc).__name__}: {exc}"[:200]
                logger.error("OCR model unavailable: %s", self._load_error)
        return self._engine

    def status(self) -> dict:
        return {"engine": getattr(self._engine, "name", None), "load_error": self._load_error}

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
            engine = self.engine()
            if engine is None:
                for i in images:
                    results[i].error = "OCR model unavailable"
                return results
            pending = list(images)
            if time.monotonic() < deadline:
                try:
                    for i, (text, conf) in zip(pending, engine.recognize([images[i] for i in pending])):
                        results[i] = Result(text, conf, engine.name)
                    pending = []
                except Exception as exc:
                    logger.warning("OCR batch failed, retrying images one by one: %s", exc)
            for i in pending:
                if time.monotonic() >= deadline:
                    results[i].error = "OCR timed out"
                    continue
                try:
                    text, conf = engine.recognize([images[i]])[0]
                    results[i] = Result(text, conf, engine.name)
                except Exception as exc:
                    results[i].error = f"OCR failed: {type(exc).__name__}"
        return results
