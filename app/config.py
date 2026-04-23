"""Configuration settings"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


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
    # Located in root directory (relative to where app is started)
    DATABASE_URL: str = "sqlite:///./orchestrator.db"

    # Auth
    SECRET_KEY: str = "your-secret-key-change-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    AUTH_RATE_LIMIT_WINDOW_SECONDS: int = 60
    AUTH_RATE_LIMIT_MAX_ATTEMPTS: int = 5

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
    CHECKPOINT_DIR: str = str(BASE_DIR / "checkpoints")

    # GitHub
    GITHUB_TOKEN: str = ""
    GITHUB_USERNAME: str = ""
    GITHUB_WEBHOOK_SECRET: str = ""

    # Data Retention (soft delete cleanup)
    SOFT_DELETE_RETENTION_DAYS: int = (
        30  # Automatically purge soft-deleted projects after 30 days
    )


settings = Settings()


def validate_runtime_secrets() -> None:
    """Fail fast if production-critical secrets are unset or still defaulted."""

    insecure_secret = not settings.SECRET_KEY or (
        settings.SECRET_KEY == "your-secret-key-change-in-production"
    )
    if insecure_secret:
        raise RuntimeError(
            "SECRET_KEY is unset or still using the default value; "
            "configure a unique SECRET_KEY before starting the API"
        )
