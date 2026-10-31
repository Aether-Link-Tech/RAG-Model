import os
from typing import Any, List
from sqlalchemy.engine import make_url

DEFAULT_DEV_SECRET_KEY = "rag-secret-development-key-2026-auth-token-super-secure"
DEFAULT_DEV_APP_DB_PASSWORD = "rag_app_dev_password"
INSECURE_SECRET_KEYS = {
    DEFAULT_DEV_SECRET_KEY,
    "changeme",
    "secret",
    "default",
    "password",
}
MIN_SECRET_KEY_LENGTH = 32
LOCAL_DEV_ENVIRONMENTS = {"development", "dev", "local", "test", "testing"}

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

    # Database:
    # - DATABASE_URL / MIGRATION_DATABASE_URL: privileged connection for Alembic DDL migrations
    # - APP_DATABASE_URL (or derived APP_DB_USER/APP_DB_PASSWORD): least-privilege NOBYPASSRLS login role for runtime queries
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/rag_db")
    MIGRATION_DATABASE_URL: str = os.getenv("MIGRATION_DATABASE_URL", "")
    APP_DB_USER: str = os.getenv("APP_DB_USER", "rag_app")
    APP_DB_PASSWORD: str = os.getenv("APP_DB_PASSWORD", DEFAULT_DEV_APP_DB_PASSWORD)
    APP_DATABASE_URL: str = os.getenv("APP_DATABASE_URL", "")

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
    SECRET_KEY: str = os.getenv("SECRET_KEY", DEFAULT_DEV_SECRET_KEY)
    ALGORITHM: str = os.getenv("ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
    JWT_ISSUER: str = os.getenv("JWT_ISSUER", "aether-link-rag")
    JWT_AUDIENCE: str = os.getenv("JWT_AUDIENCE", "aether-link-rag-api")

    # Uploads
    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", "uploads")
    MAX_UPLOAD_BYTES: int = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "1000"))
    CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "200"))

    @property
    def migration_database_url(self) -> str:
        """Privileged database URL used exclusively for Alembic schema migrations."""
        return self.MIGRATION_DATABASE_URL.strip() or self.DATABASE_URL

    @property
    def app_database_url(self) -> str:
        """Runtime application database URL using the non-superuser `rag_app` LOGIN role."""
        if self.APP_DATABASE_URL.strip():
            return self.APP_DATABASE_URL.strip()
        base_url = make_url(self.DATABASE_URL)
        app_url = base_url.set(
            username=self.APP_DB_USER,
            password=self.APP_DB_PASSWORD,
        )
        return app_url.render_as_string(hide_password=False)

    def validate_security_settings(self) -> None:
        """Fail closed if required secrets or database credentials are missing or insecure."""
        secret = (self.SECRET_KEY or "").strip()
        if not secret:
            raise ValueError("SECRET_KEY must not be empty.")

        env_normalized = (self.ENV or "").lower().strip()
        if env_normalized not in LOCAL_DEV_ENVIRONMENTS:
            if secret in INSECURE_SECRET_KEYS or len(secret) < MIN_SECRET_KEY_LENGTH:
                raise ValueError(
                    "Invalid SECRET_KEY for non-development environment: must be at least "
                    f"{MIN_SECRET_KEY_LENGTH} characters and must not use a known default key."
                )

            app_url = make_url(self.app_database_url)
            if (app_url.username or "").lower() in ("", "postgres"):
                raise ValueError(
                    "Invalid APP_DATABASE_URL for non-development environment: runtime "
                    "connections must use a non-superuser role (not 'postgres')."
                )
            if (app_url.password or "") in ("", "postgres", DEFAULT_DEV_APP_DB_PASSWORD):
                raise ValueError(
                    "Invalid APP_DATABASE_URL password for non-development environment: "
                    "must not use an empty or default development password."
                )

    def model_post_init(self, __context: Any) -> None:
        self.validate_security_settings()


settings = Settings()
