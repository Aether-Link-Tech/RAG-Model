"""One-time model download into the shared models volume (FR-059 / AL-241).

    docker compose --profile ocr-setup run --rm ocr-model-download

Downloads Florence-2-base (~0.5 GB) and the English EasyOCR models (~0.1 GB). The OCR
worker then runs with HF_HUB_OFFLINE=1 on an internal network and never downloads.
"""
import os

from huggingface_hub import snapshot_download

repo = os.getenv("OCR_FLORENCE_REPO", "florence-community/Florence-2-base")
path = snapshot_download(repo)
print("Florence-2 cached at", path)

import easyocr  # noqa: E402

easyocr.Reader(["en"], gpu=False, model_storage_directory=os.environ["EASYOCR_MODULE_PATH"], download_enabled=True)
print("EasyOCR models cached at", os.environ["EASYOCR_MODULE_PATH"])
