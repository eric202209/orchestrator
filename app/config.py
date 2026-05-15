"""Configuration settings"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator, field_validator
from typing import Any, List
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_URL = f"sqlite:///{BASE_DIR}/orchestrator.db"

LEGACY_ENV_ALIASES = {
    "ORCHESTRATOR_AGENT_BACKEND": "AGENT_BACKEND",
    "ORCHESTRATOR_AGENT_MODEL_FAMILY": "AGENT_MODEL",
    "ORCHESTRATOR_PLANNING_REPAIR_DIRECT_ENABLED": "PLANNING_REPAIR_ENABLED",
    "ORCHESTRATOR_PLANNING_REPAIR_DIRECT_BASE_URL": "PLANNING_REPAIR_BASE_URL",
    "ORCHESTRATOR_PLANNING_REPAIR_DIRECT_MODEL": "PLANNING_REPAIR_MODEL",
    "ORCHESTRATOR_PLANNING_REPAIR_DIRECT_API_KEY": "PLANNING_REPAIR_API_KEY",
    "ORCHESTRATOR_PLANNING_REPAIR_DIRECT_DISABLE_THINKING": (
        "PLANNING_REPAIR_DISABLE_THINKING"
    ),
    "ORCHESTRATOR_PLANNING_REPAIR_DIRECT_TIMEOUT_SECONDS": (
        "PLANNING_REPAIR_TIMEOUT_SECONDS"
    ),
    "ORCHESTRATOR_ENABLE_JUDGE_AGENT": "JUDGE_AGENT_ENABLED",
    "ORCHESTRATOR_TRACE_EXPORTER_BACKEND": "TRACE_EXPORTER_BACKEND",
    "ORCHESTRATOR_LANGFUSE_ENABLED": "LANGFUSE_ENABLED",
    "ORCHESTRATOR_ADMIN_EMAILS": "ADMIN_EMAILS",
    "ORCHESTRATOR_MOBILE_BASE_URL": "MOBILE_BASE_URL",
    "ORCHESTRATOR_FORCE_INLINE_PLANNING": "INLINE_PLANNING",
    "ORCHESTRATOR_WORKSPACE_REVIEW_POLICY": "WORKSPACE_REVIEW_POLICY",
}


class Settings(BaseSettings):
    """Application settings"""

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        populate_by_name=True,
        extra="ignore",
    )

    @model_validator(mode="before")
    @classmethod
    def apply_legacy_env_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        for legacy_name, current_name in LEGACY_ENV_ALIASES.items():
            if current_name not in normalized and legacy_name in normalized:
                normalized[current_name] = normalized[legacy_name]
        return normalized

    # Project
    PROJECT_NAME: str = "AI Dev Agent Orchestrator"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str = "development"

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
    DATABASE_URL: str = DEFAULT_DATABASE_URL

    @field_validator("DATABASE_URL")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        """Keep local SQLite DBs anchored to this project, not caller CWD."""

        database_url = str(value or "").strip() or DEFAULT_DATABASE_URL
        sqlite_prefix = "sqlite:///"
        if not database_url.startswith(sqlite_prefix):
            return database_url

        raw_path = database_url[len(sqlite_prefix) :]
        sqlite_path = raw_path.split("?", 1)[0]
        if sqlite_path in {"", ":memory:"}:
            return database_url

        path = Path(sqlite_path)
        if path.name == "app.db":
            return DEFAULT_DATABASE_URL
        if not path.is_absolute():
            suffix = ""
            if "?" in raw_path:
                suffix = "?" + raw_path.split("?", 1)[1]
            return f"sqlite:///{(BASE_DIR / path).resolve()}{suffix}"
        return database_url

    # Auth
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15  # 15-minute access token (short-lived)
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7  # 7-day refresh token
    SESSION_COOKIE_NAME: str = "orchestrator_session"
    SESSION_COOKIE_MAX_AGE: int = 604800  # 7 days in seconds
    WEBSOCKET_TICKET_EXPIRY_SECONDS: int = 30  # 30-second WebSocket ticket
    AUTH_RATE_LIMIT_WINDOW_SECONDS: int = 60
    AUTH_RATE_LIMIT_MAX_ATTEMPTS: int = 5
    API_RATE_LIMIT_WINDOW_SECONDS: int = 60
    API_RATE_LIMIT_MAX_ATTEMPTS: int = 20
    ALLOW_TEST_ENDPOINTS: bool = False

    # OpenClaw integration
    # Default to the local OpenClaw gateway, not the LLM-only port.
    OPENCLAW_GATEWAY_URL: str = "http://127.0.0.1:8000"
    OPENCLAW_API_KEY: str = ""
    OPENCLAW_CLI_PATH: str = ""
    OPENCLAW_CLI_ARGS: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    MOBILE_GATEWAY_API_KEY: str = ""
    AGENT_BACKEND: str = "local_openclaw"
    AGENT_MODEL: str = "local"
    PLANNING_REPAIR_ENABLED: bool = True
    PLANNING_REPAIR_BASE_URL: str = "http://ai-gateway:8000/v1"
    PLANNING_REPAIR_MODEL: str = "qwen-local"
    PLANNING_REPAIR_API_KEY: str = ""
    PLANNING_REPAIR_DISABLE_THINKING: bool = True
    PLANNING_REPAIR_TIMEOUT_SECONDS: int = 90
    JUDGE_AGENT_ENABLED: bool = False
    TRACE_EXPORTER_BACKEND: str = "local_json"
    LANGFUSE_ENABLED: bool = False
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_BASE_URL: str = ""
    LANGFUSE_ENVIRONMENT: str = "development"
    ADMIN_EMAILS: str = ""

    # Mobile app configuration
    MOBILE_BASE_URL: str = "http://localhost:8080/api/v1"

    # Demo mode flag - set to True for testing, False for real execution
    DEMO_MODE: bool = False  # Disabled (real execution enabled)
    ALLOW_TEST_KEYPAIR_ENDPOINT: bool = False
    INLINE_PLANNING: bool = False
    WORKSPACE_REVIEW_POLICY: str = "hold_nontrivial"

    @field_validator("WORKSPACE_REVIEW_POLICY")
    @classmethod
    def validate_workspace_review_policy(cls, value: str) -> str:
        policy = str(value or "").strip() or "hold_nontrivial"
        allowed = {"auto_publish_all", "hold_nontrivial", "hold_all"}
        if policy not in allowed:
            raise ValueError(
                "WORKSPACE_REVIEW_POLICY must be one of: " + ", ".join(sorted(allowed))
            )
        return policy

    # Celery Task Queue
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"
    CHECKPOINT_DIR: str = str(BASE_DIR / "checkpoints")

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
    # Embedding provider: "auto" | "openai" | "ollama"
    # "auto" uses OpenAI when OPENAI_API_KEY is set, otherwise falls back to Ollama.
    EMBEDDING_PROVIDER: str = "auto"
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    # Ollama runs on the host; orchestrator reaches it via host.docker.internal.
    OLLAMA_BASE_URL: str = "http://host.docker.internal:11434"
    OLLAMA_EMBEDDING_MODEL: str = "nomic-embed-text"
    # 0 = auto (1536 for openai, 768 for ollama nomic-embed-text)
    EMBEDDING_DIM: int = 0
    KNOWLEDGE_CONTENT_MAX_CHARS: int = 800
    KNOWLEDGE_MAX_ITEMS: int = 3
    KNOWLEDGE_MAX_TOTAL_CHARS: int = 2000


settings = Settings()


def validate_runtime_secrets() -> None:
    """Fail fast if production-critical secrets are unset or still defaulted."""
    import logging

    logger = logging.getLogger(__name__)

    if not settings.SECRET_KEY:
        raise RuntimeError(
            "SECRET_KEY is unset; configure a unique SECRET_KEY before starting the API"
        )

    # Validate the configured backend is registered and its required env vars are set.
    try:
        from app.services.agents.agent_backends import require_backend_descriptor

        descriptor = require_backend_descriptor(settings.AGENT_BACKEND)
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
            settings.AGENT_MODEL,
        )
    except RuntimeError:
        raise
    except Exception as exc:  # UnsupportedAgentBackendError or import error
        raise RuntimeError(
            f"Invalid AGENT_BACKEND '{settings.AGENT_BACKEND}': {exc}"
        ) from exc
