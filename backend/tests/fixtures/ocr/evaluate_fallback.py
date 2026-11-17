"""Accuracy of the fallback OCR engine (AL-283) on the same test set as Florence-2 (FR-062).

    python backend/tests/fixtures/ocr/evaluate_fallback.py

Same metric as evaluate.py: character accuracy = 1 - edit distance / ground-truth length,
case-insensitive with whitespace collapsed. Uses the production RapidOCREngine (reading order
included). Prints a markdown table for docs/OCR_ACCURACY.md.
"""
from __future__ import annotations

import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3]))

from backend.app.services.ocr_engines import RapidOCREngine, decode  # noqa: E402

spec = importlib.util.spec_from_file_location("ocr_eval", HERE / "evaluate.py")
ocr_eval = importlib.util.module_from_spec(spec)
sys.modules["ocr_eval"] = ocr_eval
spec.loader.exec_module(ocr_eval)


def images_of(path: Path) -> list[bytes]:
    import fitz

    if path.suffix == ".pdf":
        doc = fitz.open(path)
        return [doc.extract_image(img[0])["image"] for page in doc for img in page.get_images(full=True)]
    import zipfile

    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if n.startswith("word/media/"))
        return [z.read(n) for n in names]


def main() -> None:
    truth = json.loads((HERE / "ground_truth.json").read_text(encoding="utf-8"))
    engine = RapidOCREngine()
    scores: dict[str, list[float]] = {"clean": [], "low": []}
    seconds = 0.0
    for name, expected in truth.items():
        payloads = images_of(HERE / "testset" / name)
        for item, data in zip(sorted(expected, key=lambda e: e["image_index"]), payloads):
            start = time.perf_counter()
            text, _ = engine.recognize([decode(data)])[0]
            seconds += time.perf_counter() - start
            ref = ocr_eval.normalise(item["text"])
            acc = max(0.0, 1 - ocr_eval.edit_distance(ocr_eval.normalise(text), ref) / max(1, len(ref)))
            scores[item["quality"]].append(acc)
    total = sum(len(v) for v in scores.values())
    print("| Image quality | Images | Fallback (RapidOCR) mean character accuracy |")
    print("| --- | --- | --- |")
    for quality, values in scores.items():
        print(f"| {quality} | {len(values)} | {statistics.mean(values) * 100:.1f}% |")
    print(f"\nRapidOCR: {total} images, {seconds:.1f} s total ({seconds / max(1, total):.2f} s per image), CPU only.")


if __name__ == "__main__":
    main()
