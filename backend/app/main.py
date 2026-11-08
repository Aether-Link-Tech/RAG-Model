import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.core.config import settings
from backend.app.api.v1 import api_router
from backend.app.services.audit_log import purge_expired_entries

logger = logging.getLogger(__name__)

AUDIT_PURGE_INTERVAL_SECONDS = 24 * 60 * 60


async def _purge_audit_log_periodically() -> None:
    """Enforce the 90-day audit log retention (FR-069) once a day."""
    while True:
        try:
            removed = await purge_expired_entries()
            if removed:
                logger.info("Purged %s expired security audit entries", removed)
        except Exception:
            logger.exception("Security audit retention purge failed")
        await asyncio.sleep(AUDIT_PURGE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_purge_audit_log_periodically())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Aether Link: secure document intelligence and context-aware RAG (FastAPI, PostgreSQL, Redis, ChromaDB, Ollama)",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# The browser reaches the API through the same origin (nginx / Vite proxy); CORS is only
# needed for the configured development origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Captcha-Required", "Retry-After", "Content-Disposition"],
)


class NoStoreApiResponses:
    """API responses carry user data: never cache them, so the browser back button
    cannot show protected content after logout (FR-003).

    Plain ASGI (not BaseHTTPMiddleware) so request cancellation propagates unchanged.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                for name, value in (
                    (b"cache-control", b"no-store"),
                    (b"pragma", b"no-cache"),
                    (b"x-content-type-options", b"nosniff"),
                ):
                    if name not in present:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(NoStoreApiResponses)


app.include_router(api_router, prefix="/api/v1")

@app.get("/", tags=["Root"])
async def root():
    return {
        "status": "online",
        "service": settings.PROJECT_NAME,
        "environment": settings.ENV,
        "docs_url": "/docs",
        "health_check": "/api/health",
        "sprint": "Sprint 0 - Local Stack Initialization"
    }

@app.get("/api/health", tags=["Health"])
async def health_check():
    """
    Sprint 0 local infrastructure verification:
    Checks FastAPI, PostgreSQL 17, Redis 7, ChromaDB, and Ollama.
    """
    status_report = {
        "timestamp": time.time(),
        "backend": {"status": "healthy", "version": "FastAPI 0.142+"},
        "services": {}
    }

    # Check Ollama
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
            if resp.status_code == 200:
                models = [m.get("name") for m in resp.json().get("models", [])]
                status_report["services"]["ollama"] = {
                    "status": "connected",
                    "available_models": models,
                    "target_llm": settings.LLM_MODEL,
                    "target_embedding": settings.EMBEDDING_MODEL,
                }
            else:
                status_report["services"]["ollama"] = {
                    "status": "unhealthy",
                    "code": resp.status_code
                }
    except Exception as e:
        status_report["services"]["ollama"] = {
            "status": "not_reachable",
            "message": f"Ollama offline or starting: {str(e)}"
        }

    # Check ChromaDB
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"http://{settings.CHROMA_HOST}:{settings.CHROMA_PORT}/api/v2/heartbeat")
            if resp.status_code == 200:
                status_report["services"]["chromadb"] = {
                    "status": "connected",
                    "host": f"{settings.CHROMA_HOST}:{settings.CHROMA_PORT}",
                    "api": "v2"
                }
            else:
                status_report["services"]["chromadb"] = {"status": "unhealthy", "code": resp.status_code}
    except Exception as e:
        status_report["services"]["chromadb"] = {
            "status": "not_reachable",
            "message": str(e)
        }

    # Check Redis
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await r.ping()
        await r.aclose()
        status_report["services"]["redis"] = {"status": "connected", "url": settings.REDIS_URL}
    except Exception as e:
        status_report["services"]["redis"] = {
            "status": "not_reachable",
            "message": str(e)
        }

    # Check PostgreSQL
    try:
        import asyncpg
        # Use the configured runtime URL (rag_app role) so this also works in Docker.
        dsn = settings.app_database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        conn = await asyncpg.connect(dsn, timeout=2.0)
        version = await conn.fetchval("SELECT version();")
        await conn.close()
        status_report["services"]["postgres"] = {
            "status": "connected",
            "version": version
        }
    except Exception as e:
        status_report["services"]["postgres"] = {
            "status": "not_reachable",
            "message": str(e)
        }

    return status_report
