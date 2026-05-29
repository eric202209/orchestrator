"""Configuration settings"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator, field_validator
from typing import Any, List, Optional
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_URL = f"sqlite:///{BASE_DIR}/orchestrator.db"

LEGACY_ENV_ALIASES = {
    "ORCHESTRATOR_AGENT_BACKEND": "AGENT_BACKEND",
    "ORCHESTRATOR_AGENT_SECONDARY_BACKEND": "AGENT_SECONDARY_BACKEND",
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
            "http://localhost:5173",
            "http://localhost:8080",
            "http://localhost:8000",  # Keep for OpenClaw dashboard
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
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
    SECRET_KEY: str = ""
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
    AGENT_BACKEND: str = (
        "local_openclaw"  # BACKEND_COUPLING: default names OpenClaw directly; future backends register here
    )
    AGENT_SECONDARY_BACKEND: Optional[str] = None
    PLANNING_BACKEND: Optional[str] = None
    EXECUTION_BACKEND: Optional[str] = None
    REPAIR_BACKEND: Optional[str] = None
    LOCAL_OPENCLAW_MAX_PARALLEL_SESSIONS: int = 1
    ENABLE_TEST_RUNTIME_BACKENDS: bool = False
    AGENT_MODEL: str = "local"
    PLANNING_REPAIR_ENABLED: bool = True
    PLANNING_REPAIR_BASE_URL: str = "http://ai-gateway:8000/v1"
    PLANNING_REPAIR_MODEL: str = "qwen-local"
    PLANNING_REPAIR_API_KEY: str = ""
    PLANNING_REPAIR_DISABLE_THINKING: bool = True
    PLANNING_DIRECT_NO_THINKING_FOR_DIRECT_OLLAMA: bool = False
    PLANNING_DIRECT_SKIP_PROMPT_CHAR_THRESHOLD: int = 0
    PLANNING_REPAIR_TIMEOUT_SECONDS: int = 90
    PLANNING_SYNTHESIS_TIMEOUT_SECONDS: int = 180
    REPLAN_SYNTHESIS_TIMEOUT_SECONDS: int = 45
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

    @field_validator("AGENT_SECONDARY_BACKEND")
    @classmethod
    def validate_agent_secondary_backend(cls, value: Optional[str]) -> Optional[str]:
        backend = str(value or "").strip()
        if not backend:
            return None
        allowed_chars = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
        normalized = backend.lower()
        if normalized != backend or any(ch not in allowed_chars for ch in backend):
            raise ValueError(
                "AGENT_SECONDARY_BACKEND must be a registered backend id using "
                "lowercase letters, numbers, underscores, or hyphens"
            )
        return backend

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

    # Local Ollama model. Set this per machine to a model actually pulled on
    # the host Ollama server when direct_ollama is used.
    OLLAMA_AGENT_MODEL: str = ""
    # Tokens passed as num_ctx to Ollama. Override per deployment when the model
    # and hardware can support a larger context.
    OLLAMA_NUM_CTX: int = 4096
    # Optional planning-only request timeout for direct_ollama, in seconds.
    # 0 means use the orchestration profile timeout. Set this in .env for
    # slower local planning models without changing code between machines.
    OLLAMA_PLANNING_TIMEOUT_SECONDS: int = 0

    # Execution profile: "standard", "medium", "low_resource", or "compact_local".
    # Set low_resource for Windows / 16GB RAM / 6GB VRAM / Qwen3:8B deployments.
    # Set compact_local for governed low-end local-lane validation.
    # Set medium for mid-tier machines.
    RUNTIME_PROFILE: str = "standard"
    MAX_PLAN_STEPS: int = 10

    @field_validator("RUNTIME_PROFILE")
    @classmethod
    def validate_runtime_profile(cls, value: str) -> str:
        profile = str(value or "standard").strip()
        if profile not in {"standard", "medium", "low_resource", "compact_local"}:
            raise ValueError(
                "RUNTIME_PROFILE must be 'standard', 'medium', 'low_resource', "
                "or 'compact_local'"
            )
        return profile

    @model_validator(mode="after")
    def apply_runtime_profile(self) -> "Settings":
        if self.RUNTIME_PROFILE in {"low_resource", "compact_local"}:
            if self.PLANNING_REPAIR_TIMEOUT_SECONDS > 45:
                self.PLANNING_REPAIR_TIMEOUT_SECONDS = 45
            if self.PLANNING_SYNTHESIS_TIMEOUT_SECONDS > 90:
                self.PLANNING_SYNTHESIS_TIMEOUT_SECONDS = 90
            if self.REPLAN_SYNTHESIS_TIMEOUT_SECONDS > 30:
                self.REPLAN_SYNTHESIS_TIMEOUT_SECONDS = 30
            if self.KNOWLEDGE_MAX_ITEMS > 1:
                self.KNOWLEDGE_MAX_ITEMS = 1
            if self.KNOWLEDGE_MAX_TOTAL_CHARS > 800:
                self.KNOWLEDGE_MAX_TOTAL_CHARS = 800
            self.MAX_PLAN_STEPS = 3
        elif self.RUNTIME_PROFILE == "medium":
            if self.PLANNING_REPAIR_TIMEOUT_SECONDS > 60:
                self.PLANNING_REPAIR_TIMEOUT_SECONDS = 60
            if self.PLANNING_SYNTHESIS_TIMEOUT_SECONDS > 120:
                self.PLANNING_SYNTHESIS_TIMEOUT_SECONDS = 120
            if self.REPLAN_SYNTHESIS_TIMEOUT_SECONDS > 38:
                self.REPLAN_SYNTHESIS_TIMEOUT_SECONDS = 38
            if self.KNOWLEDGE_MAX_ITEMS > 2:
                self.KNOWLEDGE_MAX_ITEMS = 2
            if self.KNOWLEDGE_MAX_TOTAL_CHARS > 1400:
                self.KNOWLEDGE_MAX_TOTAL_CHARS = 1400
            self.MAX_PLAN_STEPS = 6
        return self


settings = Settings()


def validate_runtime_secrets() -> None:
    """Fail fast if production-critical secrets are unset or still defaulted."""
    import logging

    logger = logging.getLogger(__name__)

    test_runtime_backend_names = {"stub_success", "stub_capacity"}

    def _is_enabled_test_runtime_backend(backend_name: str | None) -> bool:
        return (
            settings.ENABLE_TEST_RUNTIME_BACKENDS
            and (backend_name or "").strip() in test_runtime_backend_names
        )

    if not settings.SECRET_KEY:
        raise RuntimeError(
            "SECRET_KEY is unset; configure a unique SECRET_KEY before starting the API"
        )

    # Validate the configured backend is registered and its required env vars are set.
    try:
        from app.services.agents.agent_backends import (
            UnsupportedAgentBackendError,
            require_backend_descriptor,
        )

        if _is_enabled_test_runtime_backend(settings.AGENT_BACKEND):
            logger.warning(
                "Using test-only runtime backend: %s", settings.AGENT_BACKEND
            )
            descriptor = None
        else:
            descriptor = require_backend_descriptor(settings.AGENT_BACKEND)
        if descriptor is None:
            missing = []
        else:
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
        if descriptor is not None and not descriptor.health.ready:
            raise RuntimeError(
                f"Backend '{descriptor.name}' is not ready: "
                + ", ".join(descriptor.health.errors)
            )
        if descriptor is not None:
            logger.info(
                "Active backend: %s | model family: %s",
                descriptor.name,
                settings.AGENT_MODEL,
            )

        role_backends = {
            "PLANNING_BACKEND": settings.PLANNING_BACKEND,
            "EXECUTION_BACKEND": settings.EXECUTION_BACKEND,
            "REPAIR_BACKEND": settings.REPAIR_BACKEND,
        }
        for env_name, role_backend in role_backends.items():
            if not role_backend:
                continue
            if _is_enabled_test_runtime_backend(role_backend):
                logger.warning(
                    "Role backend %s uses test-only runtime backend: %s",
                    env_name,
                    role_backend,
                )
                continue
            try:
                role_descriptor = require_backend_descriptor(role_backend)
            except UnsupportedAgentBackendError as exc:
                raise RuntimeError(
                    f"{env_name}='{role_backend}' is invalid: {exc}"
                ) from exc
            role_missing = [
                var
                for var in role_descriptor.config.required_env_vars
                if not str(getattr(settings, var, "")).strip()
            ]
            if role_missing:
                raise RuntimeError(
                    f"{env_name}='{role_backend}' requires env var(s) not set: "
                    + ", ".join(role_missing)
                )
            logger.info("Role backend %s: %s", env_name, role_descriptor.name)
    except RuntimeError:
        raise
    except Exception as exc:  # UnsupportedAgentBackendError or import error
        raise RuntimeError(
            f"Invalid AGENT_BACKEND '{settings.AGENT_BACKEND}': {exc}"
        ) from exc
