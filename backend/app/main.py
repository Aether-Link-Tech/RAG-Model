import time
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
try:
    from backend.app.core.config import settings
except ImportError:
    from app.core.config import settings

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Sprint 0 Stack Setup - Enterprise RAG Architecture with FastAPI, PostgreSQL, Redis, ChromaDB, and Ollama",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all for development sprint 0
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        # parse url
        conn = await asyncpg.connect("postgresql://postgres:postgres@localhost:5432/rag_db", timeout=2.0)
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
