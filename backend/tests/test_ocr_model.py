"""FR-059 / AL-242: optional slow test with the real OCR models (skipped by default).

    RUN_OCR_MODEL_TESTS=1 python -m pytest tests/test_ocr_model.py

Needs Florence-2-base in the Hugging Face cache (first run downloads ~0.5 GB).
"""
import base64
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_OCR_MODEL_TESTS") != "1", reason="set RUN_OCR_MODEL_TESTS=1")

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"
REPO = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_florence_reads_a_clean_test_image():
    runner = _load("parser_runner_model_test", REPO / "docker" / "parser" / "runner.py")
    from backend.app.services import ocr_engines as engines
    truth = json.loads((FIXTURES / "ground_truth.json").read_text())["ocr_pdf_01_clean.pdf"][0]["text"]
    layout = runner.extract_layout(FIXTURES / "testset" / "ocr_pdf_01_clean.pdf")
    result = engines.OCRService().recognize([base64.b64decode(layout["images"][0]["data"])], timeout_seconds=120)[0]
    assert result.error is None and result.engine == "florence-2-base"
    assert result.text.lower().strip() == truth.lower() and result.confidence > 0.8
