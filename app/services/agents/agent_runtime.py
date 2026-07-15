"""Factory helpers for orchestration runtime backends."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.services.agents.agent_backends import (
    UnsupportedAgentBackendError,
    require_backend_descriptor,
)
from app.services.agents.interfaces import AgentRuntime
from app.services.agents.providers import get_runtime_factory
from app.services.agents.runtime_configuration import (
    BackendRole,
    RoleRuntimeConfiguration,
)
from app.services.model_adaptation import (
    require_adaptation_profile,
    resolve_adaptation_profile,
)
from app.services.workspace.system_settings import get_effective_agent_backend
from app.services.workspace.system_settings import (
    PLANNING_ADAPTATION_PROFILE_KEY,
    COMPLETION_REPAIR_ADAPTATION_PROFILE_KEY,
    DEBUG_REPAIR_ADAPTATION_PROFILE_KEY,
    EXECUTION_ADAPTATION_PROFILE_KEY,
    REPAIR_ADAPTATION_PROFILE_KEY,
    get_effective_adaptation_profile,
    get_effective_agent_model_family,
    get_setting_value_runtime,
)

logger = logging.getLogger(__name__)


_TEST_RUNTIME_BACKENDS = {"stub_success", "stub_capacity"}
_PROFILE_OVERRIDE_UNSET = object()


class UnsupportedRuntimeProfileError(ValueError):
    """Raised when an explicit role profile is not supported by its backend."""


def _test_runtime_backends_enabled() -> bool:
    return bool(getattr(settings, "ENABLE_TEST_RUNTIME_BACKENDS", False))


def _is_test_runtime_backend(backend_name: str) -> bool:
    return backend_name in _TEST_RUNTIME_BACKENDS


def _coerce_backend_role(role: BackendRole | str) -> BackendRole:
    if isinstance(role, BackendRole):
        return role
    try:
        return BackendRole(str(role))
    except ValueError as exc:
        raise ValueError(f"Unknown runtime role: {role!r}") from exc


def resolve_backend_name_for_role(db: Session, role: BackendRole) -> str:
    """Return the configured backend name for role, falling back to AGENT_BACKEND."""
    role = _coerce_backend_role(role)
    role_setting = {
        BackendRole.PLANNING: getattr(settings, "PLANNING_BACKEND", None),
        BackendRole.EXECUTION: getattr(settings, "EXECUTION_BACKEND", None),
        BackendRole.DEBUG_REPAIR: getattr(settings, "DEBUG_REPAIR_BACKEND", None)
        or getattr(settings, "REPAIR_BACKEND", None),
        BackendRole.REPAIR: getattr(settings, "REPAIR_BACKEND", None),
        # Completion repair historically inherits the active execution lane
        # when no fast-route backend is configured.
        BackendRole.COMPLETION_REPAIR: getattr(
            settings, "COMPLETION_REPAIR_BACKEND", None
        )
        or getattr(settings, "EXECUTION_BACKEND", None),
    }.get(role)
    if role_setting:
        return str(role_setting).strip()
    return get_effective_agent_backend(settings.AGENT_BACKEND, db=db).strip()


def _effective_global_model_family(db: Session, backend_name: str) -> str:
    descriptor = require_backend_descriptor(backend_name)
    return (
        get_effective_agent_model_family(settings.AGENT_MODEL, db=db).strip()
        or descriptor.default_model_family
    )


def _role_model_family(db: Session, role: BackendRole, backend_name: str) -> str:
    """Resolve role model ownership using the current compatibility order."""

    role_models = {
        BackendRole.PLANNING: getattr(settings, "PLANNER_MODEL", ""),
        # OLLAMA_AGENT_MODEL is the existing local-provider fallback used by
        # canonical OpenClaw execution and worker selection details.
        BackendRole.EXECUTION: getattr(settings, "EXECUTION_MODEL", "")
        or getattr(settings, "OLLAMA_AGENT_MODEL", ""),
        BackendRole.REPAIR: getattr(settings, "PLANNING_REPAIR_MODEL", ""),
        BackendRole.DEBUG_REPAIR: getattr(settings, "DEBUG_REPAIR_MODEL", "")
        or getattr(settings, "PLANNING_REPAIR_MODEL", ""),
        BackendRole.COMPLETION_REPAIR: getattr(settings, "COMPLETION_REPAIR_MODEL", "")
        or getattr(settings, "PLANNING_REPAIR_MODEL", ""),
    }
    return str(role_models[role] or "").strip() or _effective_global_model_family(
        db, backend_name
    )


def _configured_profile_name(db: Session, key: str, setting_name: str) -> Optional[str]:
    value = get_setting_value_runtime(
        key,
        getattr(settings, setting_name, None),
        db=db,
    )
    normalized = str(value or "").strip()
    return normalized or None


def _profile_for_role(db: Session, role: BackendRole) -> Optional[str]:
    profile_settings = {
        BackendRole.PLANNING: (
            PLANNING_ADAPTATION_PROFILE_KEY,
            "PLANNING_ADAPTATION_PROFILE",
        ),
        BackendRole.EXECUTION: (
            EXECUTION_ADAPTATION_PROFILE_KEY,
            "EXECUTION_ADAPTATION_PROFILE",
        ),
        BackendRole.REPAIR: (
            REPAIR_ADAPTATION_PROFILE_KEY,
            "REPAIR_ADAPTATION_PROFILE",
        ),
        BackendRole.DEBUG_REPAIR: (
            DEBUG_REPAIR_ADAPTATION_PROFILE_KEY,
            "DEBUG_REPAIR_ADAPTATION_PROFILE",
        ),
        BackendRole.COMPLETION_REPAIR: (
            COMPLETION_REPAIR_ADAPTATION_PROFILE_KEY,
            "COMPLETION_REPAIR_ADAPTATION_PROFILE",
        ),
    }
    profile_key, setting_name = profile_settings[role]
    profile_name = _configured_profile_name(db, profile_key, setting_name)
    if profile_name:
        return profile_name

    # Debug and completion repair inherit the repair profile before falling
    # back to the compatible global profile. This mirrors their direct-path
    # model fallback without routing those consumers in Stage A.
    if role in {BackendRole.DEBUG_REPAIR, BackendRole.COMPLETION_REPAIR}:
        return _configured_profile_name(
            db,
            REPAIR_ADAPTATION_PROFILE_KEY,
            "REPAIR_ADAPTATION_PROFILE",
        )
    return None


def _profile_matches_backend(profile_name: str, descriptor: Any) -> bool:
    profile = require_adaptation_profile(profile_name)
    return (
        profile.backend == "*" or profile.name in descriptor.config.adaptation_profiles
    )


def _resolve_profile(
    db: Session,
    *,
    role: BackendRole,
    backend_name: str,
    model_family: str,
) -> str:
    descriptor = require_backend_descriptor(backend_name)
    explicit_profile = _profile_for_role(db, role)
    if explicit_profile:
        if not _profile_matches_backend(explicit_profile, descriptor):
            raise UnsupportedRuntimeProfileError(
                f"Adaptation profile '{explicit_profile}' is not supported by "
                f"{role.value} backend '{descriptor.name}'."
            )
        return require_adaptation_profile(explicit_profile).name

    # Planning preserves its Phase 26C-1 global-profile fallback exactly.
    if role is BackendRole.PLANNING:
        global_profile_name = get_effective_adaptation_profile(db=db)
        return require_adaptation_profile(global_profile_name).name

    # Non-planning adapters historically selected a registry-compatible
    # profile from backend/model, ignoring the global profile setting. Keep
    # that effective behavior while making the value explicit in the role
    # configuration.
    resolved_profile = resolve_adaptation_profile(
        backend=descriptor.name,
        model_family=model_family,
    )
    if _profile_matches_backend(resolved_profile.name, descriptor):
        return resolved_profile.name

    # The registry's first listed profile is the deterministic compatible
    # fallback when no profile advertises the resolved model family.
    for profile_name in descriptor.config.adaptation_profiles:
        if _profile_matches_backend(profile_name, descriptor):
            return require_adaptation_profile(profile_name).name
    raise UnsupportedRuntimeProfileError(
        f"Backend '{descriptor.name}' has no registered adaptation profile."
    )


def resolve_runtime_configuration(
    db: Session,
    role: BackendRole,
    *,
    backend_override: Optional[str] = None,
    adaptation_profile_override: object = _PROFILE_OVERRIDE_UNSET,
) -> RoleRuntimeConfiguration:
    """Resolve complete provider-neutral ownership for one explicit role."""

    role = _coerce_backend_role(role)

    backend_name = (
        str(backend_override).strip()
        if backend_override
        else resolve_backend_name_for_role(db, role)
    )

    if _is_test_runtime_backend(backend_name):
        if not _test_runtime_backends_enabled():
            raise UnsupportedAgentBackendError(
                f"Backend '{backend_name}' is test-only and ENABLE_TEST_RUNTIME_BACKENDS is false."
            )
        return RoleRuntimeConfiguration(
            role=role,
            backend_name=backend_name,
            model_family="stub",
            adaptation_profile="stub",
        )

    descriptor = require_backend_descriptor(backend_name)
    if (
        role is BackendRole.PLANNING
        and adaptation_profile_override is not _PROFILE_OVERRIDE_UNSET
    ):
        explicit_profile = str(adaptation_profile_override or "").strip() or None
        if explicit_profile:
            profile = require_adaptation_profile(explicit_profile)
            if not _profile_matches_backend(profile.name, descriptor):
                raise UnsupportedRuntimeProfileError(
                    f"Adaptation profile '{profile.name}' is not supported by "
                    f"planning backend '{descriptor.name}'."
                )
            adaptation_profile = profile.name
        else:
            adaptation_profile = _resolve_profile(
                db,
                role=role,
                backend_name=descriptor.name,
                model_family=_role_model_family(db, role, descriptor.name),
            )
    else:
        adaptation_profile = _resolve_profile(
            db,
            role=role,
            backend_name=descriptor.name,
            model_family=_role_model_family(db, role, descriptor.name),
        )

    return RoleRuntimeConfiguration(
        role=role,
        backend_name=descriptor.name,
        model_family=_role_model_family(db, role, descriptor.name),
        adaptation_profile=adaptation_profile,
    )


def resolve_planning_runtime_configuration(
    db: Session,
    *,
    adaptation_profile_override: object = _PROFILE_OVERRIDE_UNSET,
) -> RoleRuntimeConfiguration:
    """Resolve the single configuration owner used by Planning Sessions."""

    return resolve_runtime_configuration(
        db,
        BackendRole.PLANNING,
        adaptation_profile_override=adaptation_profile_override,
    )


def create_agent_runtime(
    db: Session,
    session_id: Optional[int],
    task_id: Optional[int] = None,
    *,
    use_demo_mode: Optional[bool] = None,
    role: Optional[BackendRole] = None,
    backend_override: Optional[str] = None,
) -> AgentRuntime:
    """Instantiate the configured backend runtime for a session/task pair.

    For an explicit role, ``backend_override`` replaces only the backend
    setting; model and adaptation profile remain role-owned resolver outputs
    and are validated against the overridden backend. Role-less calls retain
    the legacy backend-only path.
    """

    role = _coerce_backend_role(role) if role is not None else None
    runtime_configuration: RoleRuntimeConfiguration | None = None
    if role is not None:
        runtime_configuration = resolve_runtime_configuration(
            db,
            role,
            backend_override=backend_override,
        )
        backend_name = runtime_configuration.backend_name
    elif backend_override:
        backend_name = str(backend_override).strip()
    else:
        backend_name = get_effective_agent_backend(
            settings.AGENT_BACKEND, db=db
        ).strip()
    if _is_test_runtime_backend(backend_name):
        if not _test_runtime_backends_enabled():
            raise UnsupportedAgentBackendError(
                f"Backend '{backend_name}' is test-only and ENABLE_TEST_RUNTIME_BACKENDS is false."
            )
        from app.services.agents.stub_runtime import create_stub_runtime

        return create_stub_runtime(
            db,
            session_id,
            task_id,
            use_demo_mode=use_demo_mode,
            backend_id=backend_name,
            runtime_configuration=runtime_configuration,
        )
    descriptor = require_backend_descriptor(backend_name)
    runtime_factory = get_runtime_factory(descriptor.name)
    if runtime_factory is not None:
        runtime_kwargs: dict[str, Any] = {"use_demo_mode": use_demo_mode}
        if runtime_configuration is not None:
            runtime_kwargs["runtime_configuration"] = runtime_configuration
        runtime = runtime_factory(db, session_id, task_id, **runtime_kwargs)
        if role is not None and hasattr(runtime, "__dict__"):
            runtime.backend_role = role.value
        if runtime_configuration is not None and hasattr(runtime, "__dict__"):
            runtime.runtime_configuration = runtime_configuration
        return runtime

    raise UnsupportedAgentBackendError(
        f"Backend '{descriptor.name}' does not have a registered runtime adapter."
    )


def invoke_runtime_prompt(
    db: Session,
    prompt: str,
    *,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
    task_execution_id: Optional[int] = None,
    project_id: Optional[int] = None,
    source_brain: str = "local",
    timeout_seconds: int = 180,
    session_prefix: str = "planning",
    role: Optional[BackendRole] = None,
) -> dict[str, Any]:
    """Execute a one-shot runtime prompt across local or remote backends."""

    runtime = create_agent_runtime(db, session_id, task_id, role=role)
    if project_id is not None and hasattr(runtime, "project_id"):
        runtime.project_id = project_id
    if task_execution_id is not None and hasattr(runtime, "task_execution_id"):
        runtime.task_execution_id = task_execution_id
    try:
        return asyncio.run(
            runtime.invoke_prompt(
                prompt,
                timeout_seconds=timeout_seconds,
                source_brain=source_brain,
                session_prefix=session_prefix,
            )
        )
    except Exception:
        logger.exception(
            "Runtime prompt invocation failed for session_id=%s task_id=%s "
            "task_execution_id=%s",
            session_id,
            task_id,
            task_execution_id,
        )
        raise


def runtime_reports_context_overflow(
    db: Session,
    result: Optional[dict[str, Any]],
    *,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
    role: Optional[BackendRole] = None,
) -> bool:
    """Backend-neutral context overflow check for planning retries."""

    runtime = create_agent_runtime(
        db,
        session_id=session_id,
        task_id=task_id,
        role=role,
    )
    return runtime.reports_context_overflow(result)
