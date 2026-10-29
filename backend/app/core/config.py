import os
from typing import List

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    class BaseConfig(BaseSettings):
        model_config = SettingsConfigDict(env_file=".env", extra="ignore")
except ImportError:
    from pydantic import BaseModel
    class BaseConfig(BaseModel):
        pass

class Settings(BaseConfig):
    PROJECT_NAME: str = os.getenv("PROJECT_NAME", "RAG-Model Enterprise AI")
    ENV: str = os.getenv("ENV", "development")
    DEBUG: bool = os.getenv("DEBUG", "True").lower() in ("true", "1")
    PORT: int = int(os.getenv("PORT", "8000"))
    HOST: str = os.getenv("HOST", "0.0.0.0")

    # CORS
    BACKEND_CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/rag_db")

    # Redis
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # ChromaDB
    CHROMA_HOST: str = os.getenv("CHROMA_HOST", "localhost")
    CHROMA_PORT: int = int(os.getenv("CHROMA_PORT", "8001"))
    CHROMA_PUBLIC_COLLECTION: str = os.getenv("CHROMA_PUBLIC_COLLECTION", "public_knowledge")
    CHROMA_PRIVATE_COLLECTION_PREFIX: str = os.getenv("CHROMA_PRIVATE_COLLECTION_PREFIX", "private_user_")
    CHROMA_EMBEDDING_DIM: int = int(os.getenv("CHROMA_EMBEDDING_DIM", "768"))

    # Ollama
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "qwen3:4b")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")

    # Security
    SECRET_KEY: str = os.getenv("SECRET_KEY", "rag-secret-development-key-2026-auth-token-super-secure")
    ALGORITHM: str = os.getenv("ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

    # Uploads
    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", "uploads")
    MAX_UPLOAD_BYTES: int = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "1000"))
    CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "200"))

settings = Settings()
