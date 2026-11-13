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
        "https://localhost",
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
    # Must contain "{user_id}"; rendered with the owner's UUID hex.
    CHROMA_PRIVATE_COLLECTION_TEMPLATE: str = os.getenv(
        "CHROMA_PRIVATE_COLLECTION_TEMPLATE", "user_{user_id}_private"
    )
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
    REFRESH_TOKEN_EXPIRE_DAYS: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))
    # bcrypt work factor (AL-116 / AL-135). Values below 12 are rejected at startup.
    BCRYPT_ROUNDS: int = int(os.getenv("BCRYPT_ROUNDS", "12"))

    # PII encryption at rest (FR-007): AES-256-GCM key and a separate HMAC key for
    # equality lookups (blind index). Base64-encoded 32-byte values. In local
    # development they are derived from SECRET_KEY when unset; elsewhere they are required.
    PII_ENCRYPTION_KEY: str = os.getenv("PII_ENCRYPTION_KEY", "")
    PII_INDEX_KEY: str = os.getenv("PII_INDEX_KEY", "")

    # Security audit log (FR-069)
    AUDIT_LOG_RETENTION_DAYS: int = int(os.getenv("AUDIT_LOG_RETENTION_DAYS", "90"))

    # Login lockout (FR-002 / AL-121)
    LOGIN_CAPTCHA_AFTER_FAILURES: int = int(os.getenv("LOGIN_CAPTCHA_AFTER_FAILURES", "3"))
    LOGIN_MAX_FAILED_ATTEMPTS: int = int(os.getenv("LOGIN_MAX_FAILED_ATTEMPTS", "5"))
    LOGIN_LOCKOUT_MINUTES: int = int(os.getenv("LOGIN_LOCKOUT_MINUTES", "15"))

    # Google reCAPTCHA v2 (FR-001 / AL-118)
    RECAPTCHA_ENABLED: bool = os.getenv("RECAPTCHA_ENABLED", "True").lower() in ("true", "1")
    RECAPTCHA_SECRET_KEY: str = os.getenv("RECAPTCHA_SECRET_KEY", "")
    RECAPTCHA_VERIFY_URL: str = os.getenv(
        "RECAPTCHA_VERIFY_URL", "https://www.google.com/recaptcha/api/siteverify"
    )

    # Email verification (FR-001 / AL-117). With no SMTP_HOST in a local/dev
    # environment, the verification link is written to the log instead.
    EMAIL_VERIFICATION_EXPIRE_HOURS: int = int(os.getenv("EMAIL_VERIFICATION_EXPIRE_HOURS", "24"))
    EMAIL_VERIFICATION_URL: str = os.getenv(
        "EMAIL_VERIFICATION_URL", "http://localhost:3000/verify-email"
    )
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM: str = os.getenv("SMTP_FROM", "no-reply@aether-link.local")
    SMTP_USE_TLS: bool = os.getenv("SMTP_USE_TLS", "True").lower() in ("true", "1")
    SMTP_TIMEOUT_SECONDS: int = int(os.getenv("SMTP_TIMEOUT_SECONDS", "8"))

    # Uploads
    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", "uploads")
    MAX_UPLOAD_BYTES: int = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    CLAMAV_HOST: str = os.getenv("CLAMAV_HOST", "localhost")
    CLAMAV_PORT: int = int(os.getenv("CLAMAV_PORT", "3310"))
    CLAMAV_TIMEOUT_SECONDS: int = int(os.getenv("CLAMAV_TIMEOUT_SECONDS", "25"))
    CLAMAV_QUARANTINE_DIR: str = os.getenv(
        "CLAMAV_QUARANTINE_DIR", "quarantine"
    )
    # Character-based chunker used by the current upload pipeline (services/chunking.py)
    CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "1000"))
    CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "200"))
    # Token-based, section-aware chunker (FR-016, services/text_chunking.py)
    CHUNK_SIZE_TOKENS: int = int(os.getenv("CHUNK_SIZE_TOKENS", "800"))
    CHUNK_OVERLAP_TOKENS: int = int(os.getenv("CHUNK_OVERLAP_TOKENS", "100"))

    # Parser Sandbox (FR-067 / AL-163 / AL-164)
    PARSER_SANDBOX_ENABLED: bool = os.getenv("PARSER_SANDBOX_ENABLED", "True").lower() in ("true", "1")
    PARSER_DOCKER_IMAGE: str = os.getenv("PARSER_DOCKER_IMAGE", "rag-parser-sandbox:latest")
    PARSER_MEMORY_LIMIT: str = os.getenv("PARSER_MEMORY_LIMIT", "512m")
    PARSER_CPU_LIMIT: str = os.getenv("PARSER_CPU_LIMIT", "1.0")
    PARSER_TIMEOUT_SECONDS: int = int(os.getenv("PARSER_TIMEOUT_SECONDS", "60"))
    PARSER_MAX_OUTPUT_BYTES: int = int(os.getenv("PARSER_MAX_OUTPUT_BYTES", str(10 * 1024 * 1024)))
    # "mount": bind-mount the file read-only (backend on the host).
    # "stdin": stream the file through stdin (backend inside a container).
    PARSER_INPUT_MODE: str = os.getenv("PARSER_INPUT_MODE", "mount")
    # Layout mode returns embedded images as base64 (FR-058), so it needs a larger cap.
    PARSER_LAYOUT_MAX_OUTPUT_BYTES: int = int(os.getenv("PARSER_LAYOUT_MAX_OUTPUT_BYTES", str(40 * 1024 * 1024)))

    # OCR of images inside PDF / DOCX (FR-058 – FR-063), run in-process with local models.
    OCR_ENABLED: bool = os.getenv("OCR_ENABLED", "False").lower() in ("true", "1")
    OCR_DOCUMENT_BUDGET_SECONDS: float = float(os.getenv("OCR_DOCUMENT_BUDGET_SECONDS", "30"))
    OCR_BATCH_SIZE: int = int(os.getenv("OCR_BATCH_SIZE", "4"))

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

        if self.BCRYPT_ROUNDS < 12:
            raise ValueError("BCRYPT_ROUNDS must be at least 12.")

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
            if self.RECAPTCHA_ENABLED and not self.RECAPTCHA_SECRET_KEY.strip():
                raise ValueError(
                    "RECAPTCHA_SECRET_KEY is required when RECAPTCHA_ENABLED is true "
                    "outside development."
                )
            if not self.SMTP_HOST.strip():
                raise ValueError("SMTP_HOST is required outside development.")
            if not self.PII_ENCRYPTION_KEY.strip() or not self.PII_INDEX_KEY.strip():
                raise ValueError(
                    "PII_ENCRYPTION_KEY and PII_INDEX_KEY must be set outside development "
                    "(base64-encoded 32 random bytes each)."
                )

    def model_post_init(self, __context: Any) -> None:
        self.validate_security_settings()


settings = Settings()
