# Backend API image (FastAPI + Alembic).
# Build context is the repository root:  docker compose build backend
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libmagic1: real MIME detection for FR-065 (python-magic).
# curl: container healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libmagic1 curl \
 && rm -rf /var/lib/apt/lists/*

# Docker CLI only (no daemon): the backend starts the parser sandbox (FR-067)
# through the host's Docker socket, mounted by docker-compose.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app

# CPU-only torch first keeps the image much smaller; requirements then reuse it.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r backend/requirements.txt

COPY backend backend

# Package markers so "backend.app..." imports resolve from /app.
ENV PYTHONPATH=/app

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8000/ || exit 1

# Apply migrations, then serve.
CMD ["sh", "-c", "alembic -c backend/alembic.ini upgrade head && uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]
