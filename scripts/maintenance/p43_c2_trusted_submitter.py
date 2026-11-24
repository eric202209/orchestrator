"""P43-PRODUCT-22-C2-C3B trusted one-shot submitter for the disposable C2 slice.

Runs once, inside a disposable container built from the reviewed Worker image,
against the fresh Worker-owned SQLite database. It never starts a Worker, never
claims or completes a TaskExecution, never receives the C2 Bridge key/socket,
and never calls a model provider.

    python3 -m scripts.maintenance.p43_c2_trusted_submitter init
    python3 -m scripts.maintenance.p43_c2_trusted_submitter submit
    python3 -m scripts.maintenance.p43_c2_trusted_submitter observe

The plan is fixed in this module and is persisted through the existing
``TaskCreate.steps`` field. The Worker reuses a stored plan only after the
existing normalization and ``ValidatorService.validate_plan`` acceptance. No
plan is read from arguments, files, or the environment.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping

FIXED_COMMAND = "python3 -c 'import sys; sys.exit(0)'"
FIXED_PLAN: tuple[dict[str, Any], ...] = (
    {
        "step_number": 1,
        "description": "Run the bounded C2 sandbox exit check.",
        "commands": [FIXED_COMMAND],
        "verification": None,
        "rollback": None,
        "expected_files": [],
    },
)
PROJECT_NAME = "p43-c2-c3b-disposable"
TASK_TITLE = "C2 sandbox exit check"
TASK_DESCRIPTION = f"Run the bounded C2 sandbox exit check: {FIXED_COMMAND}"
# The existing validation profile that admits a command-only step.
EXECUTION_PROFILE = "test_only"
SUBMITTER_EMAIL = "c2-c3b-submitter@disposable.invalid"
NODE_IDENTITY_BASE = "orchestrator-worker-c2-c1"
ORCHESTRATION_TIMEOUT_SECONDS = 300

# Bridge, Docker, and C1B material the submitter must never be able to see.
FORBIDDEN_PATHS = (
    "/run/p43-sandbox-bridge-c2/launcher.sock",
    "/run/secrets/p43-sandbox-bridge-c2.key",
    "/run/p43-sandbox-bridge/launcher.sock",
    "/run/secrets/p43-sandbox-bridge.key",
    "/var/run/docker.sock",
)
FORBIDDEN_ENV = (
    "SANDBOX_BRIDGE_SOCKET",
    "SANDBOX_BRIDGE_KEY_FILE",
    "HOST_BRIDGE_SOCKET",
    "HOST_BRIDGE_KEY_FILE",
    "DOCKER_HOST",
)
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


class SubmitterRefused(RuntimeError):
    """The environment is not the exact disposable C2 submitter contract."""


def _redis_database(url: str) -> str | None:
    if not url.startswith(("redis://", "rediss://")) or "/" not in url[8:]:
        return None
    return url.rsplit("/", 1)[1].split("?", 1)[0]


def refusal_reasons(
    environ: Mapping[str, str],
    *,
    path_exists: Callable[[str], bool] = os.path.lexists,
) -> list[str]:
    """Every reason this process may not act as the disposable submitter."""

    from app.config import DEFAULT_DATABASE_URL
    from app.services.orchestration.worker_boundary import (
        DISPOSABLE_WORKER_QUEUE,
        disposable_c2_publication_admitted,
    )

    def value(name: str) -> str:
        return str(environ.get(name, "") or "").strip()

    reasons: list[str] = []
    if not disposable_c2_publication_admitted(
        environ,
        queue_name=value("CELERY_QUEUE_NAME"),
        database_url=value("DATABASE_URL"),
        production_database_url=DEFAULT_DATABASE_URL,
    ):
        reasons.append("disposable_c2_configuration_required")
    if value("WORKER_QUEUE_NAME") != DISPOSABLE_WORKER_QUEUE:
        reasons.append("disposable_worker_queue_required")
    if value("EXECUTION_ENABLED").lower() != "false":
        reasons.append("execution_enabled_must_be_false")
    if value("CONTROLLED_WORKER_IDENTITY_REQUIRED").lower() != "true":
        reasons.append("controlled_worker_identity_required")
    instance = value("WORKER_INSTANCE_ID")
    if not instance.startswith("c2-c1-") or value(
        "CONTROLLED_WORKER_NODE_IDENTITY"
    ) != (f"{NODE_IDENTITY_BASE}@{instance}"):
        reasons.append("controlled_worker_node_identity_mismatch")
    for name in ("CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"):
        if _redis_database(value(name)) in {None, "", "0", "1"}:
            reasons.append(f"{name.lower()}_must_use_disposable_redis_database")
        # The credential travels as CELERY_BROKER_PASSWORD; a credential-bearing
        # URL would also fail the Worker configuration fingerprint.
        if "@" in value(name):
            reasons.append(f"{name.lower()}_must_not_carry_credentials")
    for name in FORBIDDEN_ENV:
        if value(name):
            reasons.append(f"{name.lower()}_must_be_absent")
    for path in FORBIDDEN_PATHS:
        if path_exists(path):
            reasons.append(f"forbidden_material_visible:{path}")
    return reasons


def require_disposable_environment(environ: Mapping[str, str] | None = None) -> None:
    environ = os.environ if environ is None else environ
    reasons = refusal_reasons(environ)
    if not reasons:
        # Settings may also read a .env file; the effective values must agree.
        from app.config import settings

        if settings.EXECUTION_ENABLED:
            reasons.append("effective_execution_enabled_must_be_false")
        if settings.DATABASE_URL != environ.get("DATABASE_URL"):
            reasons.append("effective_database_url_mismatch")
    if reasons:
        raise SubmitterRefused(",".join(reasons))


def _database_file(environ: Mapping[str, str]) -> Path:
    return Path(str(environ["DATABASE_URL"]).removeprefix("sqlite:///"))


def init_database(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Create the fresh disposable schema with the existing ``init_db``."""

    environ = os.environ if environ is None else environ
    require_disposable_environment(environ)
    database = _database_file(environ)
    if os.path.lexists(database):
        raise SubmitterRefused("database_already_exists")
    from app.database import init_db

    init_db()
    return {"initialized": str(database)}


def _fixed_plan_json() -> str:
    return json.dumps([dict(step) for step in FIXED_PLAN])


def _plan_commands(steps: str | None) -> list[list[str]] | None:
    """Per-step commands of the persisted plan (the Worker adds status fields)."""

    try:
        plan = json.loads(steps or "null")
        return [list(step.get("commands") or []) for step in plan]
    except (TypeError, ValueError, AttributeError):
        return None


def submit(
    db_factory: Callable[[], Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create one Project/Task/Session and queue it via ``queue_task_for_session``."""

    environ = os.environ if environ is None else environ
    require_disposable_environment(environ)
    from app.api.v1.endpoints.projects import create_project
    from app.api.v1.endpoints.sessions import create_session
    from app.api.v1.endpoints.tasks import create_task
    from app.models import Project, Session as SessionModel, Task, User
    from app.schemas import ProjectCreate, SessionCreate, TaskCreate
    from app.services.session.session_runtime_service import queue_task_for_session

    if db_factory is None:
        from app.database import SessionLocal as db_factory
    db = db_factory()
    try:
        if any(
            db.query(model).count() for model in (User, Project, Task, SessionModel)
        ):
            raise SubmitterRefused("database_not_fresh")
        # Unusable password: this row only owns the disposable Project.
        user = User(email=SUBMITTER_EMAIL, hashed_password="!", is_active=True)
        db.add(user)
        db.commit()
        db.refresh(user)
        project = create_project(
            ProjectCreate(
                name=PROJECT_NAME,
                description="Disposable C2-C3B execution-path verification.",
                workspace_path=PROJECT_NAME,
            ),
            db=db,
            current_user=user,
        )
        task = create_task(
            TaskCreate(
                project_id=project.id,
                title=TASK_TITLE,
                description=TASK_DESCRIPTION,
                execution_profile=EXECUTION_PROFILE,
                steps=_fixed_plan_json(),
            ),
            db=db,
            current_user=user,
        )
        session = create_session(
            SessionCreate(
                project_id=project.id,
                name="C2-C3B disposable execution",
                description=TASK_DESCRIPTION,
                execution_mode="manual",
            ),
            db=db,
            current_user=user,
        )
        queued = queue_task_for_session(
            db, session, task.id, timeout_seconds=ORCHESTRATION_TIMEOUT_SECONDS
        )
        return {
            "project_id": project.id,
            "task_id": task.id,
            "session_id": session.id,
            "session_instance_id": session.instance_id,
            "task_execution_id": queued.get("task_execution_id"),
            "queue_status": queued.get("status"),
        }
    finally:
        db.close()


def observe(
    db_factory: Callable[[], Any] | None = None,
    *,
    deadline_seconds: float = 600.0,
    poll_seconds: float = 2.0,
) -> dict[str, Any]:
    """Wait for the single TaskExecution to reach a persisted terminal state."""

    from app.models import Session as SessionModel, Task, TaskExecution

    if db_factory is None:
        from app.database import SessionLocal as db_factory
    stop_at = time.monotonic() + deadline_seconds
    while True:
        db = db_factory()
        try:
            executions = db.query(TaskExecution).all()
            if len(executions) != 1:
                raise SubmitterRefused(f"expected_one_task_execution:{len(executions)}")
            execution = executions[0]
            status = getattr(execution.status, "value", execution.status)
            if str(status).lower() in TERMINAL_STATUSES or time.monotonic() >= stop_at:
                task = db.get(Task, execution.task_id)
                session = db.get(SessionModel, execution.session_id)
                return {
                    "terminal": str(status).lower() in TERMINAL_STATUSES,
                    "task_execution_id": execution.id,
                    "task_execution_status": str(status),
                    "attempt_number": execution.attempt_number,
                    "failure_category": execution.failure_category,
                    "task_id": execution.task_id,
                    "task_status": str(getattr(task.status, "value", task.status)),
                    "task_error_message": (task.error_message or "")[:500],
                    "task_plan_commands": _plan_commands(task.steps),
                    "session_id": execution.session_id,
                    "session_status": session.status,
                    "session_instance_id": session.instance_id,
                    "dispatch_session_instance_id": (
                        execution.dispatch_session_instance_id
                    ),
                    "dispatch_state": execution.dispatch_state,
                    "dispatch_celery_task_id": execution.dispatch_celery_task_id,
                    "dispatch_lease_fence": execution.dispatch_lease_fence,
                    "dispatch_refusal_reason": execution.dispatch_refusal_reason,
                    "worker_hostname": execution.worker_hostname,
                    "worker_pid": execution.worker_pid,
                    "worker_process_start_identity": (
                        execution.worker_process_start_identity
                    ),
                }
        finally:
            db.close()
        time.sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("init", "submit", "observe"))
    parser.add_argument("--deadline-seconds", type=float, default=600.0)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = init_database()
        elif args.command == "submit":
            result = submit()
        else:
            require_disposable_environment()
            result = observe(deadline_seconds=args.deadline_seconds)
    except SubmitterRefused as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr)
        return 78
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.command == "observe":
        return 0 if result["task_execution_status"].lower() == "done" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
