"""Configuration settings"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator, field_validator
from typing import Any, List, Optional
from pathlib import Path

from app.runtime_naming import DEBUG_REPAIR_LEGACY_ENV_ALIASES

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_URL = f"sqlite:///{BASE_DIR}/orchestrator.db"

# Legacy env var aliases kept for deployments using old ORCHESTRATOR_* names.
# Retire individual entries only when: (1) all active deployments confirmed on
# current names; (2) /api/v1/ops/build-identity config_source shows no legacy
# vars in use; (3) a regression test verifies settings load without the alias.
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
    **DEBUG_REPAIR_LEGACY_ENV_ALIASES,
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

    # Connection pool sizing for this process's SQLAlchemy engine (uvicorn
    # API and each Celery worker process each own a separate pool onto the
    # same database). Defaults sized for a few concurrent daily-driver
    # sessions plus normal dashboard polling; see
    # docs/roadmap/done/phase19/phase19b-db-concurrency-soak-test-report.md
    # for the soak test that found size=5/overflow=10 (15 total) exhausted
    # under that load and required these to be raised.
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

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
    OPENAI_CHAT_COMPLETIONS_BASE_URL: str = ""
    OPENAI_CHAT_COMPLETIONS_API_KEY: str = ""
    OPENAI_CHAT_COMPLETIONS_MODEL: str = ""
    OPENAI_CHAT_COMPLETIONS_TEMPERATURE: float = 0.1
    OPENAI_CHAT_COMPLETIONS_TOP_P: Optional[float] = None
    OPENAI_CHAT_COMPLETIONS_REPEAT_PENALTY: Optional[float] = None
    MOBILE_GATEWAY_API_KEY: str = ""
    AGENT_BACKEND: str = (
        "local_openclaw"  # BACKEND_COUPLING: default names OpenClaw directly; future backends register here
    )
    AGENT_SECONDARY_BACKEND: Optional[str] = None
    PLANNING_BACKEND: Optional[str] = None
    EXECUTION_BACKEND: Optional[str] = None
    DEBUG_REPAIR_BACKEND: Optional[str] = None
    REPAIR_BACKEND: Optional[str] = None
    COMPLETION_REPAIR_BACKEND: Optional[str] = None
    LOCAL_OPENCLAW_MAX_PARALLEL_SESSIONS: int = 1
    ENABLE_TEST_RUNTIME_BACKENDS: bool = False
    AGENT_MODEL: str = "local"
    PLANNER_MODEL: str = ""
    EXECUTION_MODEL: str = ""
    PLANNING_REPAIR_ENABLED: bool = True
    CANDIDATE_RECOVERY_ENABLED: bool = False
    CANDIDATE_SLOT_MERGE_ENABLED: bool = False
    PLANNING_REPAIR_BASE_URL: str = "http://ai-gateway:8000/v1"
    PLANNING_REPAIR_MODEL: str = "qwen-local"
    PLANNING_REPAIR_API_KEY: str = ""
    PLANNING_REPAIR_DISABLE_THINKING: bool = True
    DEBUG_REPAIR_DIRECT_ENABLED: bool = True
    DEBUG_REPAIR_BASE_URL: str = ""
    DEBUG_REPAIR_MODEL: str = ""
    DEBUG_REPAIR_API_KEY: str = ""
    DEBUG_REPAIR_DISABLE_THINKING: bool = True

    PLANNING_DIRECT_NO_THINKING_FOR_DIRECT_OLLAMA: bool = False
    PLANNING_DIRECT_SKIP_PROMPT_CHAR_THRESHOLD: int = 0
    # Validated value: 240s. 0 = disabled (falls back to PLANNING_REPAIR_TIMEOUT_SECONDS).
    # Sessions 597-603 timed out with the prior default of 0; 240s resolved all failures.
    PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS: int = 240
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
    # Phase 23B: Orchestrator-owned runtime root for Task Execution Sandboxes.
    # Deliberately outside every project repo and outside the OpenClaw
    # workspace tree (see phase23a-workspace-runtime-separation-plan.md §8
    # Stage 1). Not yet read by any execution path.
    RUNTIME_ROOT: str = "~/.orchestrator/runtime"
    # Phase 23D-6: canonical-project-root dispatch executes OpenClaw inside an
    # allocated Task Execution Sandbox by default. Operators can still set this
    # False as an emergency compatibility override.
    RUNTIME_WORKSPACE_ENABLED: bool = True
    # Experimental: inject ProjectStateSummary into Task 2+ planning context.
    # Off by default. Characterization only — no effect on any runtime path when False.
    PSS_CONTINUATION_INJECTION_ENABLED: bool = False
    # Slice H: write WorkingMemory to .agent/working_memory.json after task success.
    # Off by default. No injection; persistence only.
    WORKING_MEMORY_PERSISTENCE_ENABLED: bool = False
    # Slice I: render WorkingMemory to a text block (for testing/debugging only).
    # Off by default. Does not inject into planner.
    WORKING_MEMORY_RENDER_ENABLED: bool = False
    # Slice J: inject rendered WorkingMemory into Task 2+ planning context.
    # Off by default. Requires WORKING_MEMORY_PERSISTENCE_ENABLED and injection gate.
    WORKING_MEMORY_INJECTION_ENABLED: bool = False
    # RepoMemory injection: prepend single-line structural facts to project_context.
    # Off by default. Read-only; load only, never rebuilds during prompt assembly.
    # No effect on planning behavior when False.
    REPO_MEMORY_INJECTION_ENABLED: bool = False
    # Slice J: incremental execution prototype — bypass full planning for
    # creation-only tasks where the target path, content, and verify command are
    # explicit in the description. Off by default. Enablement requires ≥20 live
    # observations, ≥70% success rate, 0 destructive false positives.
    INCREMENTAL_EXECUTION_ENABLED: bool = False
    # Priority 7: inject requirements_excerpt and implementation_plan_excerpt from
    # PlanningArtifact into Task 2+ planning prompt via a dedicated post-shaping
    # block. Off by default. When True, PSS block suppresses artifact lines to
    # avoid duplication. Requires PSS_CONTINUATION_INJECTION_ENABLED=True for full
    # task-history + constraint-language coverage.
    ARTIFACT_CONTINUATION_ENABLED: bool = False
    # Priority 8: Arm B reduced planning prompt experiment.
    # When True, build_planning_prompt uses the Arm B template (69% static frame
    # reduction). Off by default. No evaluation — implementation only.
    # Arm A (current production) is unchanged when False.
    REDUCED_PLANNING_PROMPT_ENABLED: bool = False

    # HG-P1a: dedicated HumanGuidance table.
    # When False (default), the existing LogEntry path for operator-guidance is authoritative.
    # When True, the new table is the authority (enabled in HG-P1b+).
    HUMAN_GUIDANCE_TABLE_ENABLED: bool = False

    # HG-P1c-2: heuristic conflict detection between active guidance and task descriptions.
    # Warning-only — no task rejection, no planner mutation, no WM mutation.
    # Only runs when HUMAN_GUIDANCE_TABLE_ENABLED=True.
    HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED: bool = False

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
    # Capability-based: low_resource for constrained deployments where local
    # models and memory budgets are minimal; compact_local for governed
    # low-end local-lane validation; medium for moderate-capacity deployments.
    # NOTE: this profile covers execution/runtime capacity only. Planning/repair
    # lane capability is a separate axis (conceptual "planning_lane_profile",
    # It has no runtime effect unless the explicit lane env vars are set
    # (PLANNING_BACKEND, PLANNING_REPAIR_BASE_URL/MODEL/API_KEY — which must move together).
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
            "DEBUG_REPAIR_BACKEND": settings.DEBUG_REPAIR_BACKEND,
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


# Minimum safe timeout for local_openclaw direct planning calls.
# Below this value, cold-start Qwen models consistently time out.
LOCAL_OPENCLAW_SAFE_TIMEOUT_SECONDS = 120
LOCAL_OPENCLAW_VALIDATED_TIMEOUT_SECONDS = 240


def warn_local_openclaw_timeout() -> None:
    """Warn at startup if the local_openclaw direct-planning timeout may be too short."""
    import logging

    logger = logging.getLogger(__name__)

    effective_planning_backend = (
        settings.PLANNING_BACKEND or settings.AGENT_BACKEND or ""
    ).strip()
    if effective_planning_backend != "local_openclaw":
        return

    timeout = settings.PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS
    if timeout >= LOCAL_OPENCLAW_SAFE_TIMEOUT_SECONDS:
        return

    logger.warning(
        "local_openclaw planning timeout is below the safe threshold: "
        "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS=%d "
        "(0 = disabled, falls back to PLANNING_REPAIR_TIMEOUT_SECONDS=%d). "
        "Validated value: %d. "
        "Short timeouts caused repeated backend_timeout failures on cold Qwen starts.",
        timeout,
        settings.PLANNING_REPAIR_TIMEOUT_SECONDS,
        LOCAL_OPENCLAW_VALIDATED_TIMEOUT_SECONDS,
    )


def warn_incremental_execution() -> None:
    """Log startup notice and safety warning when incremental execution is enabled."""
    import logging

    logger = logging.getLogger(__name__)

    if not settings.INCREMENTAL_EXECUTION_ENABLED:
        return

    logger.info("[ORCHESTRATION] Incremental execution ENABLED — limited opt-in only")

    effective_backend = (settings.AGENT_BACKEND or "").strip()
    if effective_backend == "local_openclaw":
        logger.warning(
            "[ORCHESTRATION] Incremental execution on local_openclaw may still "
            "fallback on OpenClawSessionError/timeouts"
        )
