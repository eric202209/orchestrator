"""Configuration settings"""

from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    """Application settings"""

    # Project
    PROJECT_NAME: str = "AI Dev Agent Orchestrator"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/api/v1"

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8080  # Changed from 8000 to avoid llama-proxy conflict

    # CORS
    @property
    def CORS_ORIGINS(self) -> List[str]:
        return [
            "http://localhost:3000",
            "http://localhost:8080",
            "http://localhost:8000",  # Keep for OpenClaw dashboard
            self.OPENCLAW_GATEWAY_URL,
            "http://172.17.0.1:3000",  # Gateway IP for external browser access
            "http://172.17.0.2:3000",  # Container IP for frontend
            "http://172.17.0.2:8080",  # ✅ Allow mobile app to access API
        ]

    # Database
    # Located in root directory (relative to where app is started)
    DATABASE_URL: str = "sqlite:///./orchestrator.db"

    # Auth
    SECRET_KEY: str = "your-secret-key-change-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # OpenClaw integration
    # Use host.docker.internal for Docker container communication
    OPENCLAW_GATEWAY_URL: str = "http://host.docker.internal:8001"
    OPENCLAW_API_KEY: str = ""
    MOBILE_GATEWAY_API_KEY: str = ""

    # Mobile app configuration
    ORCHESTRATOR_MOBILE_BASE_URL: str = "http://localhost:8080/api/v1"

    # Demo mode flag - set to True for testing, False for real execution
    DEMO_MODE: bool = False  # Disabled (real execution enabled)

    # Celery Task Queue
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"

    # GitHub
    GITHUB_TOKEN: str = ""
    GITHUB_USERNAME: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
