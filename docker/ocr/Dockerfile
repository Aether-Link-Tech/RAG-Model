# OCR worker (FR-059 / AL-239 – AL-241): Florence-2-base with EasyOCR fallback, CPU only.
# Optional (OCR_BACKEND=worker); by default the backend runs OCR in-process.
# Build context is the repository root, so the engine code is shared with the backend.
# Runs on an internal Docker network (no internet) as a non-root user; models come from the
# ocr_models volume, filled once by `docker compose --profile ocr-setup run --rm ocr-model-download`.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/hf \
    EASYOCR_MODULE_PATH=/models/easyocr

# libgl/glib: required by OpenCV (EasyOCR dependency)
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

RUN pip install torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu \
 && pip install transformers==5.22.1 huggingface_hub==2.4.0 easyocr==1.7.2 pillow numpy \
        fastapi uvicorn

RUN groupadd -g 10002 ocr && useradd -u 10002 -g ocr -s /sbin/nologin -M ocr \
 && mkdir -p /models && chown ocr:ocr /models
WORKDIR /app
COPY docker/ocr/server.py docker/ocr/download.py backend/app/services/ocr_engines.py /app/
USER 10002:10002

EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=3)" || exit 1
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
