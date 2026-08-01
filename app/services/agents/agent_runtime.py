"""Factory helpers for orchestration runtime backends."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.config import settings
from app.services.agents.agent_backends import (
    BackendDescriptor,
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
    get_effective_planning_model_family,
    get_setting_value_runtime,
)

logger = logging.getLogger(__name__)


_TEST_RUNTIME_BACKENDS = {"stub_success", "stub_capacity"}
_PROFILE_OVERRIDE_UNSET = object()


class UnsupportedRuntimeProfileError(ValueError):
    """Raised when an explicit role profile is not supported by its backend."""


MIN_REPAIR_CONTEXT_TOKENS = 16_000
MIN_EXECUTION_CONTEXT_TOKENS = 16_000


class RuntimeCapabilityError(ValueError):
    """Raised when a role cannot satisfy its dispatch-time requirements."""

    def __init__(self, message: str, *, code: str = "provider_configuration_invalid"):
        super().__init__(message)
        self.code = code


def _configured_context_tokens(role: BackendRole) -> Optional[int]:
    if role is BackendRole.EXECUTION:
        value = getattr(settings, "EXECUTION_CONTEXT_TOKENS", None)
    elif role is BackendRole.DEBUG_REPAIR:
        value = getattr(settings, "DEBUG_REPAIR_CONTEXT_TOKENS", None)
        if value is None:
            value = getattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", None)
    elif role in {BackendRole.REPAIR, BackendRole.COMPLETION_REPAIR}:
        value = getattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", None)
    else:
        value = None
    if value is None or not str(value).strip():
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeCapabilityError(
            f"Runtime role '{role.value}' has an invalid effective context value: {value!r}."
        ) from exc


def _role_provider_configuration_errors(role: BackendRole) -> list[str]:
    if role is BackendRole.EXECUTION:
        errors: list[str] = []
        if not str(getattr(settings, "EXECUTION_MODEL", "") or "").strip():
            errors.append("execution model is not configured")
        return errors
    if role not in {
        BackendRole.REPAIR,
        BackendRole.DEBUG_REPAIR,
        BackendRole.COMPLETION_REPAIR,
    }:
        return []
    base_url = getattr(settings, "PLANNING_REPAIR_BASE_URL", "")
    model = getattr(settings, "PLANNING_REPAIR_MODEL", "")
    if role is BackendRole.DEBUG_REPAIR:
        base_url = getattr(settings, "DEBUG_REPAIR_BASE_URL", "") or base_url
        model = getattr(settings, "DEBUG_REPAIR_MODEL", "") or model
    if role is BackendRole.COMPLETION_REPAIR:
        model = getattr(settings, "COMPLETION_REPAIR_MODEL", "") or model
    errors: list[str] = []
    if not str(base_url or "").strip():
        errors.append("repair endpoint is not configured")
    if not str(model or "").strip():
        errors.append("repair model is not configured")
    return errors


def validate_runtime_capabilities(
    descriptor: BackendDescriptor,
    role: BackendRole | str,
    *,
    effective_context_tokens: Optional[int] = None,
    required_context_tokens: int = MIN_REPAIR_CONTEXT_TOKENS,
    dispatch: bool = True,
) -> dict[str, Any]:
    """Validate provider-neutral capabilities before explicit-role dispatch.

    Repair context is intentionally an explicit deployment fact.  A provider
    that does not report a limit cannot be assumed to satisfy the planning
    contract, even when its native model metadata is larger.
    """

    role = _coerce_backend_role(role)
    if not descriptor.implemented:
        raise RuntimeCapabilityError(
            f"Runtime backend '{descriptor.name}' is not implemented.",
            code="provider_model_unavailable",
        )
    if dispatch and (not descriptor.health.available or not descriptor.health.ready):
        errors = "; ".join(descriptor.health.errors) or descriptor.health.status
        raise RuntimeCapabilityError(
            f"Runtime backend '{descriptor.name}' is not ready for {role.value}: {errors}.",
            code="provider_unavailable",
        )
    provider_errors = _role_provider_configuration_errors(role)
    if dispatch and provider_errors:
        raise RuntimeCapabilityError(
            f"Runtime role '{role.value}' is not provider-ready: "
            + "; ".join(provider_errors)
            + ".",
            code=(
                "provider_endpoint_incompatible"
                if "endpoint" in " ".join(provider_errors)
                else "provider_model_unavailable"
            ),
        )

    role_supported = {
        BackendRole.PLANNING: descriptor.capabilities.supports_planning,
        BackendRole.EXECUTION: descriptor.capabilities.supports_step_execution,
        BackendRole.REPAIR: descriptor.capabilities.supports_planning,
        BackendRole.DEBUG_REPAIR: descriptor.capabilities.supports_debug_repair,
        BackendRole.COMPLETION_REPAIR: descriptor.capabilities.supports_planning,
    }[role]
    if dispatch and not role_supported:
        raise RuntimeCapabilityError(
            f"Runtime backend '{descriptor.name}' does not support the {role.value} role.",
            code="provider_endpoint_incompatible",
        )

    resolved_context = effective_context_tokens
    if resolved_context is None:
        resolved_context = _configured_context_tokens(role)
    role_requires_context = role in {
        BackendRole.EXECUTION,
        BackendRole.REPAIR,
        BackendRole.DEBUG_REPAIR,
        BackendRole.COMPLETION_REPAIR,
    }
    if resolved_context is None and dispatch and role_requires_context:
        required = (
            MIN_EXECUTION_CONTEXT_TOKENS
            if role is BackendRole.EXECUTION
            else required_context_tokens
        )
        if role is not BackendRole.EXECUTION:
            resolved_context = descriptor.capabilities.max_context_tokens
        if resolved_context is None:
            raise RuntimeCapabilityError(
                f"Runtime role '{role.value}' has no verified effective context capacity; "
                f"at least {required} tokens are required.",
                code="provider_context_insufficient",
            )
    if role_requires_context:
        required = (
            MIN_EXECUTION_CONTEXT_TOKENS
            if role is BackendRole.EXECUTION
            else required_context_tokens
        )
        if resolved_context is not None and resolved_context < required:
            raise RuntimeCapabilityError(
                f"Runtime role '{role.value}' effective context {resolved_context} is below "
                f"the required {required} tokens.",
                code="provider_context_insufficient",
            )

    return {
        "backend": descriptor.name,
        "role": role.value,
        "provider_ready": True,
        "effective_context_tokens": resolved_context,
        "required_context_tokens": (
            MIN_EXECUTION_CONTEXT_TOKENS
            if role is BackendRole.EXECUTION
            else required_context_tokens if role_requires_context else None
        ),
    }


def _read_openclaw_model_catalog() -> list[dict[str, Any]]:
    """Read the effective OpenClaw model registry without invoking a task."""

    configured_path = os.environ.get("OPENCLAW_CONFIG_PATH", "").strip()
    state_dir = os.environ.get("OPENCLAW_STATE_DIR", "").strip()
    config_path = (
        Path(configured_path).expanduser()
        if configured_path
        else (
            Path(state_dir).expanduser() / "openclaw.json"
            if state_dir
            else Path.home() / ".openclaw" / "openclaw.json"
        )
    )
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeCapabilityError(
            f"OpenClaw model registry could not be read from {config_path}: {exc}",
            code="provider_model_unavailable",
        ) from exc

    providers = (config.get("models") or {}).get("providers") or {}
    if not isinstance(providers, dict):
        raise RuntimeCapabilityError(
            "OpenClaw model registry omitted its providers map.",
            code="provider_model_unavailable",
        )

    catalog: list[dict[str, Any]] = []
    for provider_name, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        models = provider.get("models")
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict):
                continue
            model_copy = dict(model)
            model_id = str(model_copy.get("id") or "").strip()
            if not model_id:
                continue
            model_copy.setdefault("key", f"{provider_name}/{model_id}")
            model_copy.setdefault("name", model_id)
            model_copy.setdefault("missing", False)
            catalog.append(model_copy)
    if not catalog:
        raise RuntimeCapabilityError(
            "OpenClaw model registry contains no configured provider models.",
            code="provider_model_unavailable",
        )
    return catalog


def _model_catalog_match(
    model_family: str, catalog: list[dict[str, Any]]
) -> dict[str, Any] | None:
    requested = str(model_family or "").strip().lower()
    for model in catalog:
        candidates = {
            str(model.get("key") or "").strip().lower(),
            str(model.get("name") or "").strip().lower(),
        }
        candidates.update(
            candidate.split("/", 1)[-1] for candidate in tuple(candidates) if candidate
        )
        if requested and requested in candidates:
            return model
    return None


def validate_runtime_provider_contract(
    db: Session,
    role: BackendRole | str,
    *,
    runtime_configuration: RoleRuntimeConfiguration | None = None,
) -> dict[str, Any]:
    """Validate the configured provider/model contract before role dispatch."""

    role = _coerce_backend_role(role)
    configuration = runtime_configuration or resolve_runtime_configuration(db, role)
    descriptor = require_backend_descriptor(configuration.backend_name)
    readiness = validate_runtime_capabilities(
        descriptor,
        role,
        effective_context_tokens=_configured_context_tokens(role),
        dispatch=True,
    )
    result: dict[str, Any] = {
        **readiness,
        "backend": configuration.backend_name,
        "model": configuration.model_family,
        "role": role.value,
        "adaptation_profile": configuration.adaptation_profile,
    }

    if role is BackendRole.EXECUTION and configuration.backend_name == "local_openclaw":
        model = _model_catalog_match(
            configuration.model_family,
            _read_openclaw_model_catalog(),
        )
        if model is None or model.get("missing") is True:
            raise RuntimeCapabilityError(
                f"Execution model '{configuration.model_family}' is not available in the OpenClaw catalog.",
                code="provider_model_unavailable",
            )
        observed_context = model.get("contextWindow")
        try:
            observed_context = int(observed_context)
        except (TypeError, ValueError) as exc:
            raise RuntimeCapabilityError(
                f"Execution model '{configuration.model_family}' has no verified context window.",
                code="provider_context_insufficient",
            ) from exc
        if observed_context < MIN_EXECUTION_CONTEXT_TOKENS:
            raise RuntimeCapabilityError(
                f"Execution model '{configuration.model_family}' exposes {observed_context} tokens; "
                f"at least {MIN_EXECUTION_CONTEXT_TOKENS} are required.",
                code="provider_context_insufficient",
            )
        configured_context = readiness.get("effective_context_tokens")
        if configured_context is not None and configured_context > observed_context:
            raise RuntimeCapabilityError(
                f"Configured execution context {configured_context} exceeds the provider's "
                f"usable {observed_context} tokens.",
                code="provider_context_insufficient",
            )
        result.update(
            {
                "provider_model": model.get("key") or model.get("name"),
                "provider_context_tokens": observed_context,
                "effective_context_tokens": min(
                    configured_context or observed_context,
                    observed_context,
                ),
            }
        )

    if role is BackendRole.DEBUG_REPAIR:
        if configuration.backend_name == "openai_chat_completions":
            base_url = (
                (
                    getattr(settings, "DEBUG_REPAIR_BASE_URL", "")
                    or getattr(settings, "PLANNING_REPAIR_BASE_URL", "")
                )
                .strip()
                .rstrip("/")
            )
            parsed = urlparse(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise RuntimeCapabilityError(
                    "Debug repair endpoint must be an absolute HTTP(S) base URL.",
                    code="provider_endpoint_incompatible",
                )
            if parsed.path.rstrip("/").endswith("/chat/completions"):
                raise RuntimeCapabilityError(
                    "Debug repair endpoint must be a provider base URL, not a chat path.",
                    code="provider_endpoint_incompatible",
                )
            if (
                parsed.hostname == "api.openai.com"
                and not (
                    getattr(settings, "DEBUG_REPAIR_API_KEY", "")
                    or getattr(settings, "PLANNING_REPAIR_API_KEY", "")
                    or getattr(settings, "OPENAI_API_KEY", "")
                ).strip()
            ):
                raise RuntimeCapabilityError(
                    "Debug repair endpoint requires an API key.",
                    code="provider_authentication_missing",
                )
            result.update(
                {
                    "base_url": base_url,
                    "endpoint": f"{base_url}/chat/completions",
                }
            )
    return result


def runtime_provider_role_matrix(db: Session) -> dict[str, dict[str, Any]]:
    """Return fail-closed readiness for the configured runtime role matrix."""

    matrix: dict[str, dict[str, Any]] = {}
    for role in (
        BackendRole.PLANNING,
        BackendRole.EXECUTION,
        BackendRole.REPAIR,
        BackendRole.DEBUG_REPAIR,
    ):
        try:
            matrix[role.value] = validate_runtime_provider_contract(db, role)
        except Exception as exc:
            matrix[role.value] = {
                "role": role.value,
                "ready": False,
                "error_code": getattr(exc, "code", "provider_configuration_invalid"),
                "error": str(exc),
            }
    return matrix


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

    global_model_family = _effective_global_model_family(db, backend_name)
    execution_model = str(getattr(settings, "EXECUTION_MODEL", "") or "").strip()
    if not execution_model and backend_name == "direct_ollama":
        execution_model = str(getattr(settings, "OLLAMA_AGENT_MODEL", "") or "").strip()
    role_models = {
        BackendRole.PLANNING: get_effective_planning_model_family(
            getattr(settings, "PLANNER_MODEL", ""),
            global_model_family,
            db=db,
        ),
        BackendRole.EXECUTION: execution_model,
        BackendRole.REPAIR: getattr(settings, "PLANNING_REPAIR_MODEL", ""),
        BackendRole.DEBUG_REPAIR: getattr(settings, "DEBUG_REPAIR_MODEL", "")
        or getattr(settings, "PLANNING_REPAIR_MODEL", ""),
        BackendRole.COMPLETION_REPAIR: getattr(settings, "COMPLETION_REPAIR_MODEL", "")
        or getattr(settings, "PLANNING_REPAIR_MODEL", ""),
    }
    return str(role_models[role] or "").strip() or global_model_family


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

    if role is BackendRole.EXECUTION and descriptor.name == "local_openclaw":
        global_profile_name = get_effective_adaptation_profile(db=db)
        if _profile_matches_backend(global_profile_name, descriptor):
            return require_adaptation_profile(global_profile_name).name
        for profile_name in descriptor.config.adaptation_profiles:
            if _profile_matches_backend(profile_name, descriptor):
                return require_adaptation_profile(profile_name).name

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
    if runtime_configuration is not None:
        validate_runtime_capabilities(
            descriptor,
            role,
            effective_context_tokens=_configured_context_tokens(role),
            dispatch=False,
        )
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
    no_output_timeout_seconds: Optional[int] = None,
    session_prefix: str = "planning",
    role: Optional[BackendRole] = None,
    backend_override: Optional[str] = None,
) -> dict[str, Any]:
    """Execute a one-shot runtime prompt across local or remote backends."""

    started_at = time.monotonic()
    role_name = role.value if isinstance(role, BackendRole) else str(role or "legacy")
    try:
        if role is not None:
            validate_runtime_provider_contract(db, role)
        runtime = create_agent_runtime(
            db,
            session_id,
            task_id,
            role=role,
            backend_override=backend_override,
        )
    except Exception as exc:
        diagnostics = {
            "diagnostic_category": "configuration_failure",
            "role": role_name,
            "session_prefix": session_prefix,
            "timeout_seconds": timeout_seconds,
            "no_output_timeout_seconds": no_output_timeout_seconds,
            "error_type": type(exc).__name__,
            "error": _safe_runtime_error(str(exc)),
        }
        logger.error(
            "[RUNTIME][%s_DIAGNOSTICS] %s",
            role_name.upper(),
            json.dumps(diagnostics, sort_keys=True),
        )
        setattr(exc, "runtime_diagnostics", diagnostics)
        raise

    if role is not None:
        descriptor = getattr(runtime, "backend_descriptor", None)
        if isinstance(descriptor, BackendDescriptor):
            validate_runtime_capabilities(
                descriptor,
                role,
                effective_context_tokens=_configured_context_tokens(
                    _coerce_backend_role(role)
                ),
            )

    if project_id is not None and hasattr(runtime, "project_id"):
        runtime.project_id = project_id
    if task_execution_id is not None and hasattr(runtime, "task_execution_id"):
        runtime.task_execution_id = task_execution_id
    configuration = getattr(runtime, "runtime_configuration", None)
    descriptor = getattr(runtime, "backend_descriptor", None)
    diagnostics_context = {
        "role": role_name,
        "backend": getattr(configuration, "backend_name", None)
        or getattr(descriptor, "name", None),
        "model_family": getattr(configuration, "model_family", None),
        "adaptation_profile": getattr(configuration, "adaptation_profile", None),
        "session_prefix": session_prefix,
        "timeout_seconds": timeout_seconds,
        "no_output_timeout_seconds": no_output_timeout_seconds,
    }
    logger.info(
        "[RUNTIME][%s_DIAGNOSTICS] %s",
        role_name.upper(),
        json.dumps(
            {"diagnostic_category": "inference_started", **diagnostics_context},
            sort_keys=True,
        ),
    )
    invoke_kwargs = {
        "timeout_seconds": timeout_seconds,
        "source_brain": source_brain,
        "session_prefix": session_prefix,
    }
    if no_output_timeout_seconds is not None:
        invoke_kwargs["no_output_timeout_seconds"] = no_output_timeout_seconds
    try:
        result = asyncio.run(runtime.invoke_prompt(prompt, **invoke_kwargs))
        result = dict(result or {})
        runtime_diagnostics = dict(result.get("runtime_diagnostics") or {})
        runtime_diagnostics.update(diagnostics_context)
        runtime_diagnostics["duration_seconds"] = round(
            time.monotonic() - started_at, 3
        )
        runtime_diagnostics["diagnostic_category"] = _diagnostic_category(
            runtime_diagnostics,
            result=result,
        )
        if role_name == BackendRole.PLANNING.value:
            result["runtime_diagnostics"] = runtime_diagnostics
        logger.info(
            "[RUNTIME][%s_DIAGNOSTICS] %s",
            role_name.upper(),
            json.dumps(runtime_diagnostics, sort_keys=True),
        )
        return result
    except Exception as exc:
        runtime_diagnostics = dict(getattr(exc, "runtime_diagnostics", {}) or {})
        runtime_diagnostics.update(diagnostics_context)
        runtime_diagnostics["duration_seconds"] = round(
            time.monotonic() - started_at, 3
        )
        runtime_diagnostics["diagnostic_category"] = _diagnostic_category(
            runtime_diagnostics,
            error=exc,
        )
        runtime_diagnostics.setdefault("error_type", type(exc).__name__)
        runtime_diagnostics.setdefault("error", _safe_runtime_error(str(exc)))
        setattr(exc, "runtime_diagnostics", runtime_diagnostics)
        logger.exception(
            "[RUNTIME][%s_DIAGNOSTICS] %s",
            role_name.upper(),
            json.dumps(runtime_diagnostics, sort_keys=True),
            extra={
                "session_id": session_id,
                "task_id": task_id,
                "task_execution_id": task_execution_id,
            },
        )
        raise


def _safe_runtime_error(message: str) -> str:
    """Bound and redact provider errors before putting them in diagnostics."""

    value = str(message or "")[:500]
    value = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|secret|password|bearer)\s*[:=]?\s*"
        r"[^\s,;}]+",
        r"\1=<redacted>",
        value,
    )
    return value


def _diagnostic_category(
    diagnostics: dict[str, Any],
    *,
    result: Optional[dict[str, Any]] = None,
    error: Optional[Exception] = None,
) -> str:
    """Classify a planning/runtime observation without changing control flow."""

    if diagnostics.get("no_output_timeout") is True:
        return "silent_inference"
    if diagnostics.get("timed_out") is True:
        return "timeout"
    if error is not None:
        if type(error).__name__ in {
            "UnsupportedAgentBackendError",
            "UnsupportedRuntimeProfileError",
            "OpenClawAgentSelectionError",
        }:
            return "configuration_failure"
        if "not configured" in str(error).lower():
            return "configuration_failure"
        return "provider_failure"
    if (result or {}).get("status") == "failed":
        return "provider_failure"
    first_output = diagnostics.get("first_output_after_seconds")
    if isinstance(first_output, (int, float)) and first_output >= 5:
        return "slow_inference"
    return "provider_success"


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
