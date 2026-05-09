"""Configuration settings"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VAR_DIR = BASE_DIR / "var"
LEGACY_DATABASE_PATH = BASE_DIR / "orchestrator.db"
DEFAULT_DATABASE_PATH = (
    LEGACY_DATABASE_PATH
    if LEGACY_DATABASE_PATH.exists()
    else VAR_DIR / "orchestrator.db"
)
LEGACY_CHECKPOINT_DIR = BASE_DIR / "checkpoints"
DEFAULT_CHECKPOINT_DIR = (
    LEGACY_CHECKPOINT_DIR if LEGACY_CHECKPOINT_DIR.exists() else VAR_DIR / "checkpoints"
)


class Settings(BaseSettings):
    """Application settings"""

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
    )

    # Project
    PROJECT_NAME: str = "AI Dev Agent Orchestrator"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/api/v1"

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8080  # Changed from 8000 to avoid llama-proxy conflict
    LOCALHOST: str = "127.0.0.1"  # Container localhost for health checks

    # CORS
    @property
    def CORS_ORIGINS(self) -> List[str]:
        return [
            "http://localhost:3000",
            "http://localhost:8080",
            "http://localhost:8000",  # Keep for OpenClaw dashboard
            "http://127.0.0.1:3000",
            "http://127.0.0.1:8080",
            "http://127.0.0.1:8000",
            "http://172.17.0.2:3000",  # Container IP for frontend
            "http://172.17.0.2:8080",  # ✅ Allow mobile app to access API
        ]

    # Database
    # Absolute path derived from config.py location — CWD-independent.
    DATABASE_URL: str = f"sqlite:///{DEFAULT_DATABASE_PATH}"

    # Auth
    SECRET_KEY: str = "your-secret-key-change-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15  # 15-minute access token (short-lived)
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7  # 7-day refresh token
    SESSION_COOKIE_NAME: str = "orchestrator_session"
    SESSION_COOKIE_MAX_AGE: int = 604800  # 7 days in seconds
    WEBSOCKET_TICKET_EXPIRY_SECONDS: int = 30  # 30-second WebSocket ticket
    AUTH_RATE_LIMIT_WINDOW_SECONDS: int = 60
    AUTH_RATE_LIMIT_MAX_ATTEMPTS: int = 5
    API_RATE_LIMIT_WINDOW_SECONDS: int = 60
    API_RATE_LIMIT_MAX_ATTEMPTS: int = 20

    # OpenClaw integration
    # Default to the local OpenClaw gateway, not the LLM-only port.
    OPENCLAW_GATEWAY_URL: str = "http://127.0.0.1:8000"
    OPENCLAW_API_KEY: str = ""
    OPENCLAW_CLI_PATH: str = ""
    OPENCLAW_CLI_ARGS: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    MOBILE_GATEWAY_API_KEY: str = ""
    ORCHESTRATOR_AGENT_BACKEND: str = "local_openclaw"
    ORCHESTRATOR_AGENT_MODEL_FAMILY: str = "local"
    ORCHESTRATOR_ENABLE_JUDGE_AGENT: bool = False
    ORCHESTRATOR_TRACE_EXPORTER_BACKEND: str = "local_json"
    ORCHESTRATOR_LANGFUSE_ENABLED: bool = False
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_BASE_URL: str = ""
    LANGFUSE_ENVIRONMENT: str = "development"
    ORCHESTRATOR_ADMIN_EMAILS: str = ""

    # Mobile app configuration
    ORCHESTRATOR_MOBILE_BASE_URL: str = "http://localhost:8080/api/v1"

    # Demo mode flag - set to True for testing, False for real execution
    DEMO_MODE: bool = False  # Disabled (real execution enabled)
    ALLOW_TEST_KEYPAIR_ENDPOINT: bool = False
    ORCHESTRATOR_FORCE_INLINE_PLANNING: bool = False

    # Celery Task Queue
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"
    CHECKPOINT_DIR: str = str(DEFAULT_CHECKPOINT_DIR)

    # GitHub
    GITHUB_TOKEN: str = ""
    GITHUB_USERNAME: str = ""
    GITHUB_WEBHOOK_SECRET: str = ""

    # Data Retention (soft delete cleanup)
    SOFT_DELETE_RETENTION_DAYS: int = (
        30  # Automatically purge soft-deleted projects after 30 days
    )

    # Knowledge Layer
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_COLLECTION_NAME: str = "knowledge"
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    KNOWLEDGE_CONTENT_MAX_CHARS: int = 800
    KNOWLEDGE_MAX_ITEMS: int = 3
    KNOWLEDGE_MAX_TOTAL_CHARS: int = 2000


settings = Settings()


def validate_runtime_secrets() -> None:
    """Fail fast if production-critical secrets are unset or still defaulted."""
    import logging

    logger = logging.getLogger(__name__)

    insecure_secret = not settings.SECRET_KEY or (
        settings.SECRET_KEY == "your-secret-key-change-in-production"
    )
    if insecure_secret:
        raise RuntimeError(
            "SECRET_KEY is unset or still using the default value; "
            "configure a unique SECRET_KEY before starting the API"
        )

    # Validate the configured backend is registered and its required env vars are set.
    try:
        from app.services.agents.agent_backends import require_backend_descriptor

        descriptor = require_backend_descriptor(settings.ORCHESTRATOR_AGENT_BACKEND)
        missing = [
            var
            for var in descriptor.config.required_env_vars
            if not str(getattr(settings, var, "")).strip()
        ]
        if missing:
            raise RuntimeError(
                f"Backend '{descriptor.name}' requires environment variable(s) that are not set: "
                + ", ".join(missing)
            )
        if not descriptor.health.ready:
            raise RuntimeError(
                f"Backend '{descriptor.name}' is not ready: "
                + ", ".join(descriptor.health.errors)
            )
        logger.info(
            "Active backend: %s | model family: %s",
            descriptor.name,
            settings.ORCHESTRATOR_AGENT_MODEL_FAMILY,
        )
    except RuntimeError:
        raise
    except Exception as exc:  # UnsupportedAgentBackendError or import error
        raise RuntimeError(
            f"Invalid ORCHESTRATOR_AGENT_BACKEND '{settings.ORCHESTRATOR_AGENT_BACKEND}': {exc}"
        ) from exc
