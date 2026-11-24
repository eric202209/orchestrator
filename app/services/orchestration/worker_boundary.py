"""Small, fail-closed contracts for the disposable restricted Worker.

This module intentionally contains no database, broker, provider, or process
imports. It is safe to use from Celery publication/consumption guards and
from offline contract tests. It does not create a new Task lifecycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping


DISPOSABLE_WORKER_ISOLATION_MODE = "disposable"
DISPOSABLE_WORKER_QUEUE = "orchestrator-c2-c1-test"
DISPOSABLE_WORKER_TASK_ALLOWLIST = frozenset(
    {
        "app.tasks.worker.answer_human_intervention_query",
        "app.tasks.worker.execute_orchestration_task",
        "app.tasks.worker.receive_execution_task_dispatch",
    }
)
STRUCTURED_HTTP_BACKENDS = frozenset({"direct_ollama", "openai_chat_completions"})
DISPOSABLE_WORKER_ENVIRONMENT = "c2-c1-test"
C2_BRIDGE_MODE = "c2b-sandbox"
C2_DISPOSABLE_EXECUTION_ADMISSION = "c2-c3-single-sandbox-slice"

# These values are authority inputs, not model/provider configuration. A
# future Sandbox adapter must build a provider environment without them.
WORKER_AUTHORITY_ENV_NAMES = frozenset(
    {
        "CELERY_BROKER_URL",
        "CELERY_BROKER_PASSWORD",
        "CELERY_RESULT_BACKEND",
        "CELERY_QUEUE_NAME",
        "WORKER_QUEUE_NAME",
        "WORKER_TASK_ALLOWLIST",
        "WORKER_BROKER_NAMESPACE",
        "WORKER_ENVIRONMENT",
        "WORKER_ISOLATION_MODE",
        "WORKER_BRIDGE_MODE",
        "C2_DISPOSABLE_EXECUTION_ADMISSION",
        "WORKER_ENV_FILE",
        "WORKER_IDENTITY_SOURCE",
        "WORKER_INSTANCE_ID",
        "WORKER_APP_ROOT",
        "WORKER_SOURCE_ROOT",
        "WORKER_DATABASE_DIR",
        "WORKER_RUNTIME_ROOT",
        "WORKER_CHECKPOINT_DIR",
        "WORKER_IDENTITY_ROOT",
        "WORKSPACE_ROOT",
        "WORKER_CANDIDATE_PIN_PATH",
        "WORKER_CANDIDATE_PIN_PROTECTED_ROOT",
        "DATABASE_URL",
        "RUNTIME_ROOT",
        "CHECKPOINT_DIR",
        "CONTROLLED_WORKER_IDENTITY_REQUIRED",
        "CONTROLLED_WORKER_NODE_IDENTITY",
        "HOST_BRIDGE_SOCKET",
        "HOST_BRIDGE_KEY_FILE",
        "SANDBOX_BRIDGE_SOCKET",
        "SANDBOX_BRIDGE_KEY_FILE",
    }
)


class WorkerBoundaryViolation(ValueError):
    """A disposable Worker input crosses its reviewed boundary."""


class WorkerTaskAdmissionError(WorkerBoundaryViolation):
    """A task or delivery queue is not admitted by the disposable Worker."""


def _tokens(value: str | Iterable[str] | None) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(item.strip() for item in value.split(",") if item.strip())
    return frozenset(str(item).strip() for item in value if str(item).strip())


def validate_disposable_task_admission(
    *,
    task_name: str | None,
    queue_name: str | None,
    isolation_mode: str | None,
    task_allowlist: str | Iterable[str] | None,
) -> None:
    """Reject non-admitted task names and queues in disposable mode."""

    if str(isolation_mode or "").strip() != DISPOSABLE_WORKER_ISOLATION_MODE:
        return
    if str(queue_name or "").strip() != DISPOSABLE_WORKER_QUEUE:
        raise WorkerTaskAdmissionError(
            "disposable Worker may consume only the disposable execution queue"
        )
    configured = _tokens(task_allowlist)
    if configured != DISPOSABLE_WORKER_TASK_ALLOWLIST:
        raise WorkerTaskAdmissionError(
            "disposable Worker task allowlist is missing, widened, or altered"
        )
    if str(task_name or "").strip() not in configured:
        raise WorkerTaskAdmissionError(
            f"task is not admitted to the disposable Worker: {task_name}"
        )


def disposable_c2_publication_admitted(
    environment: Mapping[str, str],
    *,
    queue_name: str | None,
    database_url: str | None,
    production_database_url: str,
) -> bool:
    """True only for the exact disposable C2 live-slice configuration.

    This admits Celery publication while ``EXECUTION_ENABLED=false``; it never
    enables the API, eager execution, or a production queue/database. Task
    names and queues are still checked by ``validate_disposable_task_admission``.
    """

    def value(name: str) -> str:
        return str(environment.get(name, "") or "").strip()

    database_dir = value("WORKER_DATABASE_DIR")
    return bool(
        value("WORKER_ISOLATION_MODE") == DISPOSABLE_WORKER_ISOLATION_MODE
        and value("WORKER_ENVIRONMENT") == DISPOSABLE_WORKER_ENVIRONMENT
        and value("WORKER_BRIDGE_MODE") == C2_BRIDGE_MODE
        and value("C2_DISPOSABLE_EXECUTION_ADMISSION")
        == C2_DISPOSABLE_EXECUTION_ADMISSION
        and str(queue_name or "").strip() == DISPOSABLE_WORKER_QUEUE
        and database_dir.startswith("/")
        and str(database_url or "") == f"sqlite:///{database_dir}/orchestrator.db"
        and str(database_url or "") != production_database_url
    )


def _resolved_absolute(path: str | Path | None, *, name: str) -> Path:
    value = str(path or "").strip()
    if not value:
        raise WorkerBoundaryViolation(f"{name} is required")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise WorkerBoundaryViolation(f"{name} must be absolute")
    return candidate.resolve(strict=False)


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def validate_worker_authority_paths(
    *,
    application_root: str | Path,
    authority_paths: Mapping[str, str | Path],
    model_workspace_root: str | Path | None = None,
) -> None:
    """Require Worker authority directories to be separate from code/model IO."""

    application = _resolved_absolute(application_root, name="application root")
    resolved = {
        name: _resolved_absolute(path, name=name)
        for name, path in authority_paths.items()
    }
    names = tuple(resolved)
    for index, left_name in enumerate(names):
        left = resolved[left_name]
        if _overlaps(application, left):
            raise WorkerBoundaryViolation(
                f"Worker authority path overlaps immutable application root: {left_name}"
            )
        for right_name in names[index + 1 :]:
            if _overlaps(left, resolved[right_name]):
                raise WorkerBoundaryViolation(
                    f"Worker authority paths overlap: {left_name} and {right_name}"
                )

    if model_workspace_root is not None and str(model_workspace_root).strip():
        model_workspace = _resolved_absolute(
            model_workspace_root, name="model workspace root"
        )
        for name, path in resolved.items():
            if _overlaps(model_workspace, path):
                raise WorkerBoundaryViolation(
                    f"model workspace overlaps Worker authority path: {name}"
                )
        if _overlaps(model_workspace, application):
            raise WorkerBoundaryViolation(
                "model workspace overlaps immutable application root"
            )


def validate_structured_http_provider_contract(
    *,
    provider_name: str | None,
    backend_values: Mapping[str, str | None],
) -> None:
    """Keep every disposable Worker role on the structured HTTP topology."""

    if str(provider_name or "").strip() != "direct_openai_compatible":
        raise WorkerBoundaryViolation(
            "disposable Worker requires PLANNING_PROVIDER=direct_openai_compatible"
        )
    for name, value in backend_values.items():
        normalized = str(value or "").strip()
        if normalized and normalized not in STRUCTURED_HTTP_BACKENDS:
            raise WorkerBoundaryViolation(
                f"{name} is not an approved structured HTTP backend"
            )


def model_environment_authority_violations(
    environment: Mapping[str, object],
) -> tuple[str, ...]:
    """List Worker-only inputs that must not cross into a model environment."""

    return tuple(
        sorted(
            name
            for name in WORKER_AUTHORITY_ENV_NAMES
            if str(environment.get(name, "") or "").strip()
        )
    )


__all__ = [
    "C2_DISPOSABLE_EXECUTION_ADMISSION",
    "disposable_c2_publication_admitted",
    "DISPOSABLE_WORKER_ISOLATION_MODE",
    "DISPOSABLE_WORKER_QUEUE",
    "DISPOSABLE_WORKER_TASK_ALLOWLIST",
    "STRUCTURED_HTTP_BACKENDS",
    "WORKER_AUTHORITY_ENV_NAMES",
    "WorkerBoundaryViolation",
    "WorkerTaskAdmissionError",
    "model_environment_authority_violations",
    "validate_disposable_task_admission",
    "validate_structured_http_provider_contract",
    "validate_worker_authority_paths",
]
