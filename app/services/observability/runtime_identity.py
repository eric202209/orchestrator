"""Provider-neutral runtime identity projections for dispatch evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.config import settings
from app.models import PlanningSession, TaskExecution
from app.services.agents.runtime_configuration import (
    BackendRole,
    RoleRuntimeConfiguration,
)
from app.services.tasks.execution import originating_planning_session_for_task
from app.services.workspace.system_settings import (
    get_effective_adaptation_profile,
    get_effective_agent_backend,
    get_effective_agent_model_family,
    get_effective_planning_model_family,
)


_PLANNING_FIELDS = (
    "planning_backend",
    "planner_model",
    "reasoning_profile",
    "configuration_fingerprint",
)


def _present(value: Any) -> bool:
    if value is None:
        return False
    return bool(str(value).strip())


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip() or None
    return value


@dataclass(frozen=True)
class RuntimeIdentityProjection:
    """Read-only identity evidence for one task execution boundary."""

    planning_backend: str | None = None
    planner_model: str | None = None
    reasoning_profile: str | None = None
    execution_backend: str | None = None
    executor_model: str | None = None
    configuration_fingerprint: str | None = None
    planning_session_id: int | None = None
    task_execution_id: int | None = None
    identity_source: str = "legacy_global_fallback"
    identity_sources: Mapping[str, str] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        """Return stable event/log names, including historical aliases."""

        return {
            # Existing dispatch metadata names remain compatible.
            "backend": self.execution_backend,
            "model_family": self.executor_model,
            "planner_backend": self.planning_backend,
            "planner_model": self.planner_model,
            "execution_backend": self.execution_backend,
            "execution_model": self.executor_model,
            # Canonical durable names are also exposed at the boundary.
            "planning_backend": self.planning_backend,
            "executor_model": self.executor_model,
            "reasoning_profile": self.reasoning_profile,
            "configuration_fingerprint": self.configuration_fingerprint,
            "planning_session_id": self.planning_session_id,
            "task_execution_id": self.task_execution_id,
            "identity_source": self.identity_source,
            "identity_sources": dict(self.identity_sources),
        }


def _role_configuration(
    db: Any,
    role: BackendRole,
    provided: RoleRuntimeConfiguration | None,
) -> RoleRuntimeConfiguration | None:
    if provided is not None:
        return provided
    try:
        from app.services.agents.agent_runtime import resolve_runtime_configuration

        return resolve_runtime_configuration(db, role)
    except Exception:
        # Historical rows must remain inspectable even when their original
        # provider is no longer registered. The final compatibility fallback
        # below is deliberately marked as global rather than historical.
        return None


def _global_fallbacks(db: Any) -> dict[str, Any]:
    try:
        effective_backend = get_effective_agent_backend(settings.AGENT_BACKEND, db=db)
        effective_model = get_effective_agent_model_family(settings.AGENT_MODEL, db=db)
        planning_profile = get_effective_adaptation_profile(db=db)
        planner_model = get_effective_planning_model_family(
            settings.PLANNER_MODEL,
            effective_model,
            db=db,
        )
    except Exception:
        effective_backend = settings.AGENT_BACKEND
        effective_model = settings.AGENT_MODEL
        planning_profile = None
        planner_model = settings.PLANNER_MODEL or effective_model
    return {
        "planning_backend": settings.PLANNING_BACKEND or effective_backend,
        "planner_model": planner_model,
        "reasoning_profile": planning_profile,
        "execution_backend": settings.EXECUTION_BACKEND or effective_backend,
        "executor_model": (
            settings.EXECUTION_MODEL or settings.OLLAMA_AGENT_MODEL or effective_model
        ),
    }


def _planning_session_for_missing_fields(
    db: Any,
    task_execution: TaskExecution | None,
) -> PlanningSession | None:
    if task_execution is None:
        return None
    missing_planning_fields = any(
        not _present(getattr(task_execution, field_name, None))
        for field_name in _PLANNING_FIELDS
    )
    if not missing_planning_fields:
        return None

    planning_session_id = getattr(task_execution, "planning_session_id", None)
    if planning_session_id:
        try:
            return (
                db.query(PlanningSession)
                .filter(PlanningSession.id == planning_session_id)
                .first()
            )
        except Exception:
            return None

    task_id = getattr(task_execution, "task_id", None)
    if not task_id:
        return None
    try:
        return originating_planning_session_for_task(db, task_id)
    except Exception:
        return None


def _choose(
    *,
    task_execution: TaskExecution | None,
    planning_session: PlanningSession | None,
    execution_configuration: RoleRuntimeConfiguration | None,
    planning_configuration: RoleRuntimeConfiguration | None,
    global_fallbacks: dict[str, Any],
    field_name: str,
    planning_session_field: str | None = None,
    configuration_field: str | None = None,
) -> tuple[Any, str]:
    stored_value = (
        getattr(task_execution, field_name, None)
        if task_execution is not None
        else None
    )
    if _present(stored_value):
        return _clean(stored_value), "stored_task_execution"

    if planning_session is not None and planning_session_field:
        session_value = getattr(planning_session, planning_session_field, None)
        if _present(session_value):
            return _clean(session_value), "originating_planning_session"

    configuration = (
        planning_configuration
        if field_name in _PLANNING_FIELDS
        else execution_configuration
    )
    if configuration is not None and configuration_field:
        configuration_value = getattr(configuration, configuration_field, None)
        if _present(configuration_value):
            return _clean(configuration_value), "current_role_fallback"

    fallback_value = global_fallbacks.get(field_name)
    return _clean(fallback_value), "legacy_global_fallback"


def build_runtime_identity_projection(
    db: Any,
    *,
    task_execution: TaskExecution | None = None,
    task_execution_id: int | None = None,
    planning_configuration: RoleRuntimeConfiguration | None = None,
    execution_configuration: RoleRuntimeConfiguration | None = None,
) -> RuntimeIdentityProjection:
    """Project one execution's identity without mutating durable state.

    Non-null TaskExecution values always win. Missing planning values may use
    the immutable originating PlanningSession, then resolved role configs,
    and finally the legacy global compatibility values. Missing execution
    values use the same role-config/global fallback order without borrowing
    planning fields.
    """

    planning_session = _planning_session_for_missing_fields(db, task_execution)
    missing_planning = any(
        not _present(getattr(task_execution, field_name, None))
        for field_name in _PLANNING_FIELDS
    )
    missing_execution = any(
        not _present(getattr(task_execution, field_name, None))
        for field_name in ("execution_backend", "executor_model")
    )
    if missing_planning:
        planning_configuration = _role_configuration(
            db, BackendRole.PLANNING, planning_configuration
        )
    if missing_execution:
        execution_configuration = _role_configuration(
            db, BackendRole.EXECUTION, execution_configuration
        )

    # Complete stored rows do not resolve current settings at all. This is the
    # key protection against configuration drift rewriting historical evidence.
    global_fallbacks = (
        _global_fallbacks(db) if (missing_planning or missing_execution) else {}
    )

    field_sources: dict[str, str] = {}
    planning_backend, field_sources["planning_backend"] = _choose(
        task_execution=task_execution,
        planning_session=planning_session,
        execution_configuration=execution_configuration,
        planning_configuration=planning_configuration,
        global_fallbacks=global_fallbacks,
        field_name="planning_backend",
        planning_session_field="planning_backend",
        configuration_field="backend_name",
    )
    planner_model, field_sources["planner_model"] = _choose(
        task_execution=task_execution,
        planning_session=planning_session,
        execution_configuration=execution_configuration,
        planning_configuration=planning_configuration,
        global_fallbacks=global_fallbacks,
        field_name="planner_model",
        planning_session_field="planner_model",
        configuration_field="model_family",
    )
    reasoning_profile, field_sources["reasoning_profile"] = _choose(
        task_execution=task_execution,
        planning_session=planning_session,
        execution_configuration=execution_configuration,
        planning_configuration=planning_configuration,
        global_fallbacks=global_fallbacks,
        field_name="reasoning_profile",
        planning_session_field="reasoning_profile",
        configuration_field="adaptation_profile",
    )
    configuration_fingerprint, field_sources["configuration_fingerprint"] = _choose(
        task_execution=task_execution,
        planning_session=planning_session,
        execution_configuration=execution_configuration,
        planning_configuration=planning_configuration,
        global_fallbacks={},
        field_name="configuration_fingerprint",
        planning_session_field="configuration_fingerprint",
    )
    execution_backend, field_sources["execution_backend"] = _choose(
        task_execution=task_execution,
        planning_session=planning_session,
        execution_configuration=execution_configuration,
        planning_configuration=planning_configuration,
        global_fallbacks=global_fallbacks,
        field_name="execution_backend",
        configuration_field="backend_name",
    )
    executor_model, field_sources["executor_model"] = _choose(
        task_execution=task_execution,
        planning_session=planning_session,
        execution_configuration=execution_configuration,
        planning_configuration=planning_configuration,
        global_fallbacks=global_fallbacks,
        field_name="executor_model",
        configuration_field="model_family",
    )

    planning_session_id = getattr(task_execution, "planning_session_id", None)
    planning_session_source = "stored_task_execution" if planning_session_id else None
    if not planning_session_id and planning_session is not None:
        planning_session_id = planning_session.id
        planning_session_source = "originating_planning_session"
    if planning_session_source:
        field_sources["planning_session_id"] = planning_session_source

    task_execution_id = (
        getattr(task_execution, "id", None)
        if task_execution is not None
        else task_execution_id
    )
    if task_execution_id is not None:
        field_sources["task_execution_id"] = "stored_task_execution"

    present_sources = {
        source
        for field_name, source in field_sources.items()
        if _present(
            {
                "planning_backend": planning_backend,
                "planner_model": planner_model,
                "reasoning_profile": reasoning_profile,
                "configuration_fingerprint": configuration_fingerprint,
                "execution_backend": execution_backend,
                "executor_model": executor_model,
                "planning_session_id": planning_session_id,
                "task_execution_id": task_execution_id,
            }.get(field_name)
        )
    }
    if len(present_sources) == 1:
        identity_source = next(iter(present_sources))
    elif "current_role_fallback" in present_sources:
        identity_source = "current_role_fallback"
    elif "originating_planning_session" in present_sources:
        identity_source = "originating_planning_session"
    elif "stored_task_execution" in present_sources:
        identity_source = "stored_task_execution"
    else:
        identity_source = "legacy_global_fallback"

    return RuntimeIdentityProjection(
        planning_backend=planning_backend,
        planner_model=planner_model,
        reasoning_profile=reasoning_profile,
        execution_backend=execution_backend,
        executor_model=executor_model,
        configuration_fingerprint=configuration_fingerprint,
        planning_session_id=planning_session_id,
        task_execution_id=task_execution_id,
        identity_source=identity_source,
        identity_sources=field_sources,
    )
