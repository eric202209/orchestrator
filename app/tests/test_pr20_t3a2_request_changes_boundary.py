"""PR20 T3-A2a request-changes feedback authority boundary."""

from pathlib import Path
import logging
import json
import hashlib
import traceback
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.v1.endpoints.tasks import _build_request_changes_repair_prompt
from app.models import (
    Project,
    RequestChangesIssuanceWitness,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
)
from app.services.orchestration.validation import candidate_checks
from app.services.orchestration.validation.candidate_checks import (
    declared_acceptance_forms,
    task_acceptance_pytest_commands,
)
from app.services.execution.workflow_lifecycle import dispatch_value_sha256

BOUNDARY = "Operator request-changes feedback (not acceptance criteria):"
OPERATOR_DESCRIPTION = (
    "Implement the widget.\n\n"
    "Acceptance criteria:\n"
    "- `python -m pytest -q tests/test_operator.py` passes."
)
OPERATOR_COMMAND = "python -m pytest -q tests/test_operator.py"


def _create_task(db_session, tmp_path: Path, description: str) -> Task:
    project_root = tmp_path / "project"
    project_root.mkdir()
    task_root = project_root / "task-review"
    task_root.mkdir()
    project = Project(name="request-changes-boundary", workspace_path=str(project_root))
    db_session.add(project)
    db_session.flush()
    task = Task(
        project_id=project.id,
        title="Request changes boundary",
        description=description,
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder=task_root.name,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def _add_prior_change_set(db_session, task: Task) -> TaskExecutionChangeSet:
    session = SessionModel(
        project_id=task.project_id,
        name=f"prior-change-set-{uuid4().hex}",
        status="completed",
        instance_id=str(uuid4()),
    )
    db_session.add(session)
    db_session.flush()
    prior_execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(prior_execution)
    db_session.flush()
    record = TaskExecutionChangeSet(
        project_id=task.project_id,
        task_id=task.id,
        session_id=session.id,
        task_execution_id=prior_execution.id,
        base_snapshot_key=f"prior:{prior_execution.id}",
        added_files=[f"added-{index}.py" for index in range(14)],
        modified_files=["existing.py"],
        deleted_files=["old.py"],
        warning_flags=[f"warning-{index}" for index in range(10)],
        status="completed",
        captured_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)
    return record


def _request_changes(authenticated_client, db_session, task, note: str) -> tuple:
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes",
        json={"note": note},
    )
    assert response.status_code == 200, response.text
    db_session.refresh(task)
    prompt = _build_request_changes_repair_prompt(
        task=task,
        base_prompt=task.description,
        change_set=None,
    )
    return response, prompt


def _stub_retry_publication(monkeypatch, *, on_publish=None) -> dict:
    from app.tasks import worker as worker_module

    captured = {}

    class _OfflineResult:
        id = "offline-request-changes-review"

    def _capture_apply_async(*args, kwargs=None, **_options):
        if kwargs is None:
            kwargs = args[1] if len(args) > 1 else {}
        captured.update(kwargs)
        captured["published_task_id"] = _options.get("task_id")
        if on_publish is not None:
            on_publish(kwargs, _options)
        return _OfflineResult()

    monkeypatch.setattr(
        worker_module.execute_orchestration_task,
        "apply_async",
        _capture_apply_async,
    )
    return captured


def _queue_active_request_changes_retry(
    authenticated_client, task, monkeypatch
) -> tuple[dict, int]:
    published = _stub_retry_publication(monkeypatch)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"retry_intent": "request_changes"},
    )
    assert response.status_code == 200, response.text
    execution_id = response.json()["task_execution_id"]
    assert published["published_task_id"]
    return published, execution_id


def _run_real_acceptance_consumer(
    monkeypatch,
    *,
    project_dir: Path,
    retry_prompt: str,
    trusted_task_description: str,
    acceptance_authority_status: str | None = None,
    task_acceptance_enabled: bool = True,
):
    executed = []
    monkeypatch.setattr(
        candidate_checks,
        "select_candidate_verification",
        lambda **_kwargs: candidate_checks.CandidateVerificationSelection(
            command="", source="test_no_focused_command", paths=()
        ),
    )
    monkeypatch.setattr(
        candidate_checks, "_candidate_python", lambda _project_dir: "/offline/python"
    )

    def _record_without_execution(*, project_dir, command, timeout_seconds):
        del project_dir, timeout_seconds
        executed.append(command)
        return 0, "offline command boundary stub"

    monkeypatch.setattr(candidate_checks, "_execute_command", _record_without_execution)
    result = candidate_checks.validate_candidate_delta(
        project_dir=project_dir,
        change_set={},
        plan=(),
        task_prompt=retry_prompt,
        acceptance_task_prompt=trusted_task_description,
        acceptance_authority_status=acceptance_authority_status,
        include_static_checks=False,
        task_acceptance_enabled=task_acceptance_enabled,
    )
    return result, executed


class _StopAtRealValidator(RuntimeError):
    """Stop the Worker after real validation and before completion side effects."""


def _run_worker_completion_seam(
    monkeypatch,
    tmp_path: Path,
    *,
    db_session_factory,
    task,
    prompt: str,
    context=None,
    continuation_kind=None,
    dispatch_kind=None,
    persisted_task_execution_id=None,
    expected_attempt_number=None,
    celery_task_id=None,
    managed_legacy_v1=False,
    omit_task_execution_identity=False,
    worker_task_execution_id=None,
    complete: bool = True,
):
    """Run the real Worker constructors and Coordinator→Validator path offline."""

    from contextlib import nullcontext

    import app.services.orchestration.coordinators.completion_coordinator as completion
    import app.services.orchestration.validation.validator as validator_module
    import app.tasks.worker as worker_module
    from app.services.agents.agent_runtime import BackendRole
    from app.services.agents.runtime_configuration import RoleRuntimeConfiguration
    from app.services.orchestration.prompt_templates import OrchestrationState
    from app.services.orchestration.types import OrchestrationRunContext

    persisted_execution = persisted_task_execution_id is not None
    persisted_task_execution_state = None
    prior_change_set_reference = None
    project_root = tmp_path / "worker-project"
    project_root.mkdir(parents=True, exist_ok=True)
    db = db_session_factory()
    isolated_db_url = db.get_bind().url.render_as_string(hide_password=True)
    assert db.get_bind().url.drivername == "sqlite"
    # The Worker seam normally uses the in-memory conftest database, but I2's
    # authoritative security cases use the migrated file-backed SQLite DB.
    task_row = db.query(Task).filter(Task.id == task.id).first()
    if persisted_execution:
        assert task_row is not None
        requested_project_id = task_row.project_id
    else:
        requested_project_id = getattr(task, "project_id", None)
    project = (
        db.query(Project).filter(Project.id == requested_project_id).first()
        if requested_project_id is not None
        else None
    )
    if persisted_execution:
        assert project is not None
    if project is None:
        project = Project(
            id=requested_project_id,
            name="worker-seam-project",
            workspace_path=str(project_root),
        )
        db.add(project)
        db.flush()
    project.workspace_path = str(project_root)

    persisted_task_review_state = None
    if task_row is not None and persisted_execution:
        persisted_task_review_state = {
            "id": task_row.id,
            "project_id": task_row.project_id,
            "description": task_row.description,
            "status": task_row.status,
            "workspace_status": task_row.workspace_status,
            "promotion_note": task_row.promotion_note,
        }
    if task_row is None:
        assert not persisted_execution
        task_row = Task(
            id=getattr(task, "id", None),
            project_id=project.id,
            title=getattr(task, "title", "Worker seam task"),
            description=getattr(task, "description", prompt),
            status=TaskStatus.PENDING,
        )
        db.add(task_row)
    if persisted_execution:
        assert task_row.id == task.id
        assert task_row.status == TaskStatus.PENDING
    else:
        task_row.project_id = project.id
        task_row.title = getattr(task, "title", task_row.title)
        task_row.description = getattr(task, "description", prompt)
        task_row.status = TaskStatus.PENDING
        task_row.execution_profile = getattr(
            task, "execution_profile", "full_lifecycle"
        )
        task_row.intent_mode = getattr(task, "intent_mode", "default") or "default"
        task_row.workflow_stage = getattr(task, "workflow_stage", None)
        task_row.plan_position = getattr(task, "plan_position", None)
        task_row.steps = getattr(task, "steps", None)
        task_row.current_step = getattr(task, "current_step", 0) or 0
        task_row.error_message = getattr(task, "error_message", None)
        task_row.workspace_status = getattr(task, "workspace_status", "isolated")
        task_row.promotion_note = getattr(task, "promotion_note", None)
        task_row.started_at = None
        task_row.completed_at = None
    task_subfolder = task_row.task_subfolder or f"task-{task_row.id}"
    task_root = project_root / task_subfolder
    task_root.mkdir(parents=True, exist_ok=True)

    if persisted_task_execution_id is not None:
        execution = (
            db.query(TaskExecution)
            .filter(TaskExecution.id == persisted_task_execution_id)
            .first()
        )
        assert execution is not None
        session = (
            db.query(SessionModel)
            .filter(SessionModel.id == execution.session_id)
            .first()
        )
        session_task_link = (
            db.query(SessionTask)
            .filter(
                SessionTask.session_id == execution.session_id,
                SessionTask.task_id == execution.task_id,
            )
            .order_by(SessionTask.id.desc())
            .first()
        )
        assert session is not None and session_task_link is not None
        assert execution.task_id == task_row.id
        assert execution.status == TaskStatus.PENDING
        assert execution.dispatch_invocation is not None
        assert execution.dispatch_invocation_sha256
        assert execution.dispatch_state == "READY"
        persisted_task_execution_state = {
            "id": execution.id,
            "session_id": execution.session_id,
            "task_id": execution.task_id,
            "attempt_number": execution.attempt_number,
            "status": execution.status,
            "dispatch_state": execution.dispatch_state,
            "dispatch_kind": execution.dispatch_kind,
            "dispatch_parent_task_execution_id": (
                execution.dispatch_parent_task_execution_id
            ),
            "dispatch_session_instance_id": execution.dispatch_session_instance_id,
            "dispatch_celery_task_id": execution.dispatch_celery_task_id,
            "dispatch_invocation": json.loads(
                json.dumps(execution.dispatch_invocation)
            ),
            "dispatch_invocation_sha256": execution.dispatch_invocation_sha256,
        }
        authority = execution.dispatch_invocation.get("acceptance_authority")
        if isinstance(authority, dict):
            prior_change_set_reference = json.loads(
                json.dumps(authority.get("prior_change_set"))
            )
    else:
        session = SessionModel(
            project_id=project.id,
            name=f"worker-seam-{uuid4().hex}",
            status="pending",
            instance_id=str(uuid4()),
            default_execution_profile=None,
            escalation_backend_id=None,
        )
        db.add(session)
        db.flush()
        session_task_link = SessionTask(
            session_id=session.id,
            task_id=task_row.id,
            status=TaskStatus.PENDING,
        )
        # The ordinary and legacy recovery cases use unmanaged TaskExecutions.
        # Recovery is identified only by its persisted RECOVERY classification.
        execution = TaskExecution(
            session_id=session.id,
            task_id=task_row.id,
            attempt_number=1,
            status=TaskStatus.PENDING,
            dispatch_kind=dispatch_kind,
            planning_backend="direct_ollama",
            planner_model="qwen3-coder:30b",
            execution_backend="local_openclaw",
            executor_model="qwen3.6:27B",
        )
        db.add_all([session_task_link, execution])
        if managed_legacy_v1:
            from app.services.execution.workflow_lifecycle import (
                configure_task_execution_dispatch,
            )

            configured = configure_task_execution_dispatch(
                db,
                task_execution=execution,
                session=session,
                task=task_row,
                dispatch_kind="ROOT",
                prompt=prompt,
                timeout_seconds=1,
                context=context,
            )
            assert configured
            execution.dispatch_state = "READY"
            celery_task_id = execution.dispatch_celery_task_id
    db.commit()
    db.refresh(project)
    db.refresh(task_row)
    db.refresh(session)
    db.refresh(session_task_link)
    db.refresh(execution)
    if persisted_execution:
        refreshed_task_review_state = {
            "id": task_row.id,
            "project_id": task_row.project_id,
            "description": task_row.description,
            "status": task_row.status,
            "workspace_status": task_row.workspace_status,
            "promotion_note": task_row.promotion_note,
        }
        assert refreshed_task_review_state == persisted_task_review_state
        refreshed_task_execution_state = {
            "id": execution.id,
            "session_id": execution.session_id,
            "task_id": execution.task_id,
            "attempt_number": execution.attempt_number,
            "status": execution.status,
            "dispatch_state": execution.dispatch_state,
            "dispatch_kind": execution.dispatch_kind,
            "dispatch_parent_task_execution_id": (
                execution.dispatch_parent_task_execution_id
            ),
            "dispatch_session_instance_id": execution.dispatch_session_instance_id,
            "dispatch_celery_task_id": execution.dispatch_celery_task_id,
            "dispatch_invocation": json.loads(
                json.dumps(execution.dispatch_invocation)
            ),
            "dispatch_invocation_sha256": execution.dispatch_invocation_sha256,
        }
        assert refreshed_task_execution_state == persisted_task_execution_state
    assert task_row.project_id == project.id
    assert session.project_id == project.id
    assert session_task_link.session_id == session.id
    assert session_task_link.task_id == task_row.id
    assert execution.session_id == session.id
    assert execution.task_id == task_row.id
    if expected_attempt_number is not None:
        assert execution.attempt_number == expected_attempt_number
    elif not persisted_execution:
        assert execution.attempt_number == 1
    assert execution.dispatch_state == (
        "READY"
        if persisted_task_execution_id is not None or managed_legacy_v1
        else None
    )
    if dispatch_kind is not None or not persisted_execution:
        assert execution.dispatch_kind == dispatch_kind
    assert session.instance_id
    # Keep the caller's task identity aligned with the real persisted row.
    task.id = task_row.id
    task.project_id = project.id

    initial_states = {
        "task": task_row.status,
        "session": session.status,
        "session_task": session_task_link.status,
        "task_execution": execution.status,
    }
    initial_identity = {
        "project_id": project.id,
        "task_id": task_row.id,
        "session_id": session.id,
        "session_instance_id": session.instance_id,
        "session_task_id": session_task_link.id,
        "task_execution_id": execution.id,
        "attempt_number": execution.attempt_number,
        "dispatch_state": execution.dispatch_state,
        "dispatch_kind": execution.dispatch_kind,
        "dispatch_parent_task_execution_id": execution.dispatch_parent_task_execution_id,
        "database_url": isolated_db_url,
    }
    db_close_calls = []
    actual_close = db.close

    def _record_db_close():
        db_close_calls.append("close")
        actual_close()

    monkeypatch.setattr(db, "close", _record_db_close)
    provider_calls = []
    worker_contexts = []
    main_run_contexts = []
    step_loop_calls = []
    worker_db_sessions = []
    worker_db_engines = []
    planning_phase_calls = []
    state_observations = []
    validator_calls = []
    validation_results = []
    executed_commands = []
    failure_calls = []
    primary_exceptions = []
    primary_tracebacks = []
    cleanup_exceptions = []
    planning = RoleRuntimeConfiguration(
        role=BackendRole.PLANNING,
        backend_name="direct_ollama",
        model_family="qwen3-coder:30b",
        adaptation_profile="planning_default",
    )
    execution_config = RoleRuntimeConfiguration(
        role=BackendRole.EXECUTION,
        backend_name="local_openclaw",
        model_family="qwen3.6:27B",
        adaptation_profile="openclaw_default",
    )

    class _Runtime:
        def __init__(self, config):
            self.config = config

        def get_backend_metadata(self):
            return {
                "backend": self.config.backend_name,
                "model": self.config.model_family,
                "model_family": self.config.model_family,
                "adaptation_profile": self.config.adaptation_profile,
            }

        async def get_session_context(self):
            return {}

        async def invoke_prompt(self, *args, **kwargs):
            provider_calls.append((args, kwargs))
            raise AssertionError("provider invocation is forbidden in this seam")

    from app.services.tasks.service import TaskService as RealTaskService

    class _TaskService(RealTaskService):
        """Use isolated workspace paths while retaining real DB persistence."""

        def get_project_root(self, project: Project) -> Path:
            assert project.id == task_row.project_id
            project_root.mkdir(parents=True, exist_ok=True)
            return project_root

        def ensure_project_gitignore_guard(self, project: Project) -> dict[str, Any]:
            root = self.get_project_root(project)
            guard = root / ".gitignore"
            content = "# deterministic Worker harness guard\n.agent/\n"
            changed = not guard.exists() or guard.read_text(encoding="utf-8") != content
            if changed:
                guard.write_text(content, encoding="utf-8")
            return {"status": "ok", "changed": changed}

        def get_project_baseline_dir(self, project: Project) -> Path:
            root = self.get_project_root(project)
            root.mkdir(parents=True, exist_ok=True)
            return root

        def hydrate_task_workspace(
            self,
            project: Optional[Project],
            current_task: Optional[Task],
            target_dir: Path,
        ) -> dict:
            assert project is None or project.id == task_row.project_id
            assert current_task is None or current_task.id == task_row.id
            target_dir.mkdir(parents=True, exist_ok=True)
            return {"hydrated": False, "source_tasks": [], "files_copied": 0}

        def build_project_execution_context(
            self,
            project: Optional[Project],
            current_task: Optional[Task],
            max_chars: int = 4000,
        ) -> str:
            assert project is None or project.id == task_row.project_id
            assert current_task is None or current_task.id == task_row.id
            return "Offline Worker harness project context."[:max_chars]

        def review_existing_workspace(
            self,
            project: Optional[Project],
            current_task: Optional[Task],
            target_dir: Path,
            max_chars: int = 2500,
        ) -> dict:
            assert project is None or project.id == task_row.project_id
            assert current_task is None or current_task.id == task_row.id
            return {
                "has_existing_files": False,
                "file_count": 0,
                "summary": "Existing workspace review: isolated seam is empty.",
            }

        def record_task_execution_change_set_unavailable(
            self,
            project: Project,
            task: Task,
            *,
            session_id: Optional[int],
            task_execution_id: int,
            snapshot_key: str,
            reason: str,
            commit: bool = True,
        ):
            return super().record_task_execution_change_set_unavailable(
                project,
                task,
                session_id=session_id,
                task_execution_id=task_execution_id,
                snapshot_key=snapshot_key,
                reason=reason,
                commit=commit,
            )

        def persist_task_execution_change_set(
            self,
            project: Project,
            task: Task,
            *,
            session_id: Optional[int],
            task_execution_id: int,
            snapshot_key: str,
            target_dir: Optional[Path] = None,
            preserve_project_root_rules: bool = True,
            status: Optional[str] = None,
            workspace_review_policy: Optional[str] = None,
            review_decision: Optional[dict[str, Any]] = None,
            workflow_profile: Optional[str] = None,
            evaluator_evidence: Optional[dict[str, Any]] = None,
            planner_contract: Optional[dict[str, Any]] = None,
            commit: bool = True,
            preserve_review_decision: bool = False,
        ) -> dict[str, Any]:
            return super().persist_task_execution_change_set(
                project,
                task,
                session_id=session_id,
                task_execution_id=task_execution_id,
                snapshot_key=snapshot_key,
                target_dir=target_dir,
                preserve_project_root_rules=preserve_project_root_rules,
                status=status,
                workspace_review_policy=workspace_review_policy,
                review_decision=review_decision,
                workflow_profile=workflow_profile,
                evaluator_evidence=evaluator_evidence,
                planner_contract=planner_contract,
                commit=commit,
                preserve_review_decision=preserve_review_decision,
            )

    class _FailureCoordinator:
        def handle_failure(self, **kwargs):
            failure_calls.append(kwargs)
            exc = kwargs.get("exc")
            primary_exceptions.append(exc)
            primary_tracebacks.append(
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                if isinstance(exc, BaseException)
                else ""
            )
            return {"status": "test_stopped_at_validator"}

    def _create_runtime(_db, _session_id, _task_id, *, role, backend_override=None):
        del backend_override
        config = planning if role is BackendRole.PLANNING else execution_config
        return _Runtime(config)

    def _get_isolated_db_session():
        worker_db_sessions.append(db)
        worker_db_engines.append(db.get_bind())
        return db

    monkeypatch.setattr(worker_module, "get_db_session", _get_isolated_db_session)
    original_run_context_factory = worker_module.OrchestrationRunContext

    def _record_main_run_context(*args, **kwargs):
        run_context = original_run_context_factory(*args, **kwargs)
        main_run_contexts.append(run_context)
        return run_context

    monkeypatch.setattr(
        worker_module, "OrchestrationRunContext", _record_main_run_context
    )
    # The harness exercises canonical workspace setup without allocating a
    # Runtime Workspace, which would launch local git subprocesses.
    monkeypatch.setattr(worker_module.settings, "RUNTIME_WORKSPACE_ENABLED", False)
    monkeypatch.setattr(
        worker_module,
        "resolve_runtime_configuration",
        lambda _db, role: {
            BackendRole.PLANNING: planning,
            BackendRole.EXECUTION: execution_config,
        }[role],
    )
    monkeypatch.setattr(
        worker_module, "validate_runtime_provider_contract", lambda *a, **k: {}
    )
    monkeypatch.setattr(worker_module, "create_agent_runtime", _create_runtime)
    monkeypatch.setattr(worker_module, "TaskService", _TaskService)
    monkeypatch.setattr(
        worker_module,
        "resolve_project_workspace_path",
        lambda *_args, **_kwargs: project_root,
    )
    monkeypatch.setattr(worker_module, "_runtime_selection_details", lambda *a, **k: {})
    monkeypatch.setattr(worker_module, "_build_claimed_details", lambda **kwargs: {})
    monkeypatch.setattr(
        worker_module, "_run_start_runtime_identity", lambda *args: {"config": {}}
    )
    monkeypatch.setattr(worker_module, "_record_live_log", lambda *a, **k: None)
    monkeypatch.setattr(
        worker_module, "_append_orchestration_event", lambda *a, **k: None
    )
    monkeypatch.setattr(
        worker_module, "_write_project_state_snapshot", lambda *a, **k: None
    )
    monkeypatch.setattr(
        worker_module, "_snapshot_workspace_before_run", lambda *a, **k: None
    )
    monkeypatch.setattr(
        worker_module, "_save_orchestration_checkpoint", lambda *a, **k: None
    )
    monkeypatch.setattr(
        worker_module, "_should_force_review_execution_profile", lambda *args: False
    )
    monkeypatch.setattr(
        worker_module, "get_effective_policy_profile", lambda **k: "balanced"
    )
    monkeypatch.setattr(
        worker_module,
        "get_policy_profile",
        lambda _name: SimpleNamespace(
            name="balanced", validation_severity="standard", completion_repair_budget=1
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "get_backend_descriptor",
        lambda backend: SimpleNamespace(
            name=backend, capabilities=SimpleNamespace(max_parallel_sessions=None)
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "register_forced_termination_cleanup",
        lambda _callback: lambda: None,
    )
    monkeypatch.setattr(
        worker_module, "start_langfuse_observation", lambda **k: nullcontext(None)
    )
    monkeypatch.setattr(
        worker_module, "update_langfuse_observation", lambda *a, **k: None
    )
    monkeypatch.setattr(worker_module, "flush_langfuse", lambda: None)
    monkeypatch.setattr(worker_module, "langfuse_tracing_enabled", lambda: False)
    monkeypatch.setattr(
        worker_module, "CheckpointService", lambda _db: SimpleNamespace()
    )
    monkeypatch.setattr(worker_module, "_run_virtual_merge_gate", lambda **kwargs: None)
    monkeypatch.setattr(worker_module, "_FailureCoordinator", _FailureCoordinator)
    monkeypatch.setattr(
        worker_module,
        "resolve_guidance_runtime_target",
        lambda *, backend, runtime_metadata, planning_backend, execution_backend: {
            "backend": backend,
            "model_name": runtime_metadata["model"],
            "model_family": runtime_metadata["model_family"],
        },
    )

    def _offline_planning_phase(*, ctx, **_kwargs):
        planning_phase_calls.append(
            (ctx.session_id, ctx.task_id, ctx.task_execution_id)
        )
        ctx.orchestration_state.plan = [{"step": "offline seam"}]
        return {"status": "completed", "reason": "offline planning boundary"}

    monkeypatch.setattr(
        worker_module, "_execute_planning_phase", _offline_planning_phase
    )

    def _execute_step_loop(**kwargs):
        step_loop_calls.append("reached")
        ctx = kwargs["ctx"]
        worker_contexts.append(ctx)
        task_state = ctx.db.execute(
            select(Task.id, Task.project_id, Task.status).where(Task.id == ctx.task_id)
        ).one()
        session_state = ctx.db.execute(
            select(
                SessionModel.id,
                SessionModel.project_id,
                SessionModel.status,
                SessionModel.instance_id,
            ).where(SessionModel.id == ctx.session_id)
        ).one()
        session_task_state = ctx.db.execute(
            select(
                SessionTask.id,
                SessionTask.session_id,
                SessionTask.task_id,
                SessionTask.status,
            ).where(
                SessionTask.session_id == ctx.session_id,
                SessionTask.task_id == ctx.task_id,
            )
        ).one()
        task_execution_state = ctx.db.execute(
            select(
                TaskExecution.id,
                TaskExecution.session_id,
                TaskExecution.task_id,
                TaskExecution.attempt_number,
                TaskExecution.status,
                TaskExecution.dispatch_state,
                TaskExecution.dispatch_kind,
                TaskExecution.dispatch_parent_task_execution_id,
            ).where(TaskExecution.id == ctx.task_execution_id)
        ).one()
        state_observations.append(
            {
                "state_type": type(ctx.orchestration_state).__name__,
                "context_type": type(ctx).__name__,
                "context_session_id": ctx.session_id,
                "context_task_id": ctx.task_id,
                "context_task_execution_id": ctx.task_execution_id,
                "context_session_task_id": ctx.session_task_link.id,
                "context_acceptance_authority_status": (
                    ctx.acceptance_authority_status
                ),
                "task_status": task_state.status,
                "session_status": session_state.status,
                "session_task_status": session_task_state.status,
                "task_execution_status": task_execution_state.status,
                "persisted_identity": {
                    "task_id": task_state.id,
                    "task_project_id": task_state.project_id,
                    "session_id": session_state.id,
                    "session_project_id": session_state.project_id,
                    "session_instance_id": session_state.instance_id,
                    "session_task_id": session_task_state.id,
                    "session_task_session_id": session_task_state.session_id,
                    "session_task_task_id": session_task_state.task_id,
                    "task_execution_id": task_execution_state.id,
                    "task_execution_session_id": task_execution_state.session_id,
                    "task_execution_task_id": task_execution_state.task_id,
                    "task_execution_attempt_number": task_execution_state.attempt_number,
                    "task_execution_dispatch_state": task_execution_state.dispatch_state,
                    "task_execution_dispatch_kind": task_execution_state.dispatch_kind,
                    "task_execution_dispatch_parent_id": task_execution_state.dispatch_parent_task_execution_id,
                },
            }
        )
        ctx.orchestration_state.plan = [{"step": "offline seam"}]
        return {"status": "completed" if complete else "stopped", "reason": "seam"}

    monkeypatch.setattr(worker_module, "_execute_step_loop", _execute_step_loop)

    monkeypatch.setattr(
        completion, "load_accepted_path_authority", lambda *a, **k: None
    )
    monkeypatch.setattr(completion, "control_state_of", lambda state: state.project_dir)
    monkeypatch.setattr(completion, "record_outcome_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr(completion, "append_orchestration_event", lambda *a, **k: None)
    monkeypatch.setattr(completion, "emit_phase_event", lambda *a, **k: None)
    monkeypatch.setattr(
        completion,
        "_generate_task_summary_with_fallback",
        lambda **kwargs: {"output": "offline summary", "pn_summary": "offline summary"},
    )
    monkeypatch.setattr(
        completion,
        "_scope_workspace_consistency_to_task_changes",
        lambda value, **k: value,
    )
    monkeypatch.setattr(
        completion, "_build_gating_change_set", lambda **kwargs: {"changed_files": []}
    )

    original_validate = validator_module.ValidatorService.validate_task_completion

    def _validate_through_real_service(**kwargs):
        validator_calls.append(dict(kwargs))
        evidence = dict(kwargs.get("completion_evidence") or {})
        evidence.update(
            candidate_delta_required=False,
            run_candidate_checks=True,
            include_static_checks=False,
            run_task_acceptance_checks=True,
            change_set={"changed_files": []},
        )
        kwargs["completion_evidence"] = evidence
        verdict = original_validate(**kwargs)
        validation_results.append(verdict)
        raise _StopAtRealValidator("offline seam complete")

    monkeypatch.setattr(
        validator_module.ValidatorService,
        "validate_task_completion",
        staticmethod(_validate_through_real_service),
    )
    monkeypatch.setattr(
        candidate_checks,
        "select_candidate_verification",
        lambda **kwargs: candidate_checks.CandidateVerificationSelection(
            command="", source="worker_seam_no_focused_command", paths=()
        ),
    )
    monkeypatch.setattr(
        candidate_checks, "_candidate_python", lambda _path: "/offline/python"
    )

    def _record_command(*, project_dir, command, timeout_seconds):
        del project_dir, timeout_seconds
        executed_commands.append(command)
        return 0, "recorded only; command not executed"

    monkeypatch.setattr(candidate_checks, "_execute_command", _record_command)

    class _CleanupExceptionRecorder(logging.Handler):
        def emit(self, record):
            if (
                isinstance(record.msg, str)
                and record.msg.startswith(
                    "[ORCHESTRATION] Failed to sync task execution"
                )
                and record.args
            ):
                cleanup_exceptions.append(record.args[-1])

    worker_logger = logging.getLogger(worker_module.__name__)
    cleanup_handler = _CleanupExceptionRecorder()
    worker_logger.addHandler(cleanup_handler)
    try:
        if celery_task_id is not None:
            worker_module.execute_orchestration_task.push_request(id=celery_task_id)
        try:
            result = worker_module.execute_orchestration_task.run(
                session_id=session.id,
                task_id=task.id,
                prompt=prompt,
                timeout_seconds=1,
                expected_session_instance_id=session.instance_id,
                task_execution_id=(
                    None
                    if omit_task_execution_identity
                    else worker_task_execution_id or execution.id
                ),
                context=context,
                continuation_kind=continuation_kind,
            )
        finally:
            if celery_task_id is not None:
                worker_module.execute_orchestration_task.pop_request()
    finally:
        worker_logger.removeHandler(cleanup_handler)
    database_states_after_worker = {
        "task": db.execute(
            select(Task.id, Task.project_id, Task.status).where(Task.id == task.id)
        ).one_or_none(),
        "session": db.execute(
            select(
                SessionModel.id,
                SessionModel.project_id,
                SessionModel.status,
                SessionModel.instance_id,
            ).where(SessionModel.id == session.id)
        ).one_or_none(),
        "session_task": db.execute(
            select(
                SessionTask.id,
                SessionTask.session_id,
                SessionTask.task_id,
                SessionTask.status,
            ).where(
                SessionTask.session_id == session.id,
                SessionTask.task_id == task.id,
            )
        ).one_or_none(),
        "task_execution": db.execute(
            select(
                TaskExecution.id,
                TaskExecution.session_id,
                TaskExecution.task_id,
                TaskExecution.attempt_number,
                TaskExecution.status,
                TaskExecution.dispatch_state,
                TaskExecution.dispatch_kind,
            ).where(TaskExecution.id == execution.id)
        ).one_or_none(),
    }
    return SimpleNamespace(
        result=result,
        task_execution=execution,
        worker_contexts=worker_contexts,
        main_run_contexts=main_run_contexts,
        step_loop_reached=bool(step_loop_calls),
        step_loop_calls=step_loop_calls,
        planning_phase_calls=planning_phase_calls,
        provider_calls=provider_calls,
        validator_calls=validator_calls,
        validation_results=validation_results,
        executed_commands=executed_commands,
        failure_calls=failure_calls,
        primary_exceptions=primary_exceptions,
        primary_tracebacks=primary_tracebacks,
        cleanup_exceptions=cleanup_exceptions,
        state_observations=state_observations,
        initial_states=initial_states,
        initial_identity=initial_identity,
        persisted_task_review_state=persisted_task_review_state,
        persisted_task_execution_state=persisted_task_execution_state,
        prior_change_set_reference=prior_change_set_reference,
        isolated_db_url=isolated_db_url,
        worker_db_sessions=worker_db_sessions,
        worker_db_engines=worker_db_engines,
        database_states_after_worker=database_states_after_worker,
        db_close_calls=db_close_calls,
        db=db,
    )


def _worker_task(
    description: str,
    *,
    workspace_status: str = "changes_requested",
    promotion_note: str | None = "Operator-requested repair",
    error_message: str | None = None,
):
    return SimpleNamespace(
        id=704,
        project_id=701,
        title="Worker seam task",
        description=description,
        status=TaskStatus.PENDING,
        execution_profile="full_lifecycle",
        intent_mode="default",
        workflow_stage=None,
        plan_position=1,
        task_subfolder="task-704",
        steps=None,
        started_at=None,
        current_step=0,
        workspace_status=workspace_status,
        promotion_note=promotion_note,
        error_message=error_message,
    )


def _worker_seam_failure_diagnostics(result) -> str:
    primary = []
    for index, exc in enumerate(result.primary_exceptions):
        primary.append(
            {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": (
                    result.primary_tracebacks[index]
                    if index < len(result.primary_tracebacks)
                    else ""
                ),
            }
        )
    cleanup = [
        {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
        }
        for exc in result.cleanup_exceptions
    ]
    return json.dumps(
        {
            "main_run_context_created": bool(result.main_run_contexts),
            "main_run_context_count": len(result.main_run_contexts),
            "step_loop_stub_reached": result.step_loop_reached,
            "step_loop_stub_call_count": len(result.step_loop_calls),
            "primary_exceptions": primary,
            "cleanup_exceptions": cleanup,
        },
        indent=2,
        default=str,
    )


@pytest.mark.parametrize(
    ("note", "feedback_command"),
    (
        (
            "Please repair the widget. `pytest tests/test_feedback.py`",
            "pytest tests/test_feedback.py",
        ),
        (
            "Please repair the widget. `pytest -k smoke tests/`",
            "pytest -k smoke tests/",
        ),
        ("Please repair the widget without changing its public API.", None),
    ),
)
def test_request_changes_api_feedback_is_outside_operator_acceptance(
    authenticated_client, db_session, tmp_path: Path, note, feedback_command
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)

    response, prompt = _request_changes(authenticated_client, db_session, task, note)

    assert response.json()["workspace_status"] == "changes_requested"
    assert response.json()["promotion_note"] == note
    assert response.json()["description"] == OPERATOR_DESCRIPTION
    assert task.status == TaskStatus.FAILED
    assert task.task_subfolder == "task-review"
    assert prompt.startswith(OPERATOR_DESCRIPTION)
    assert prompt.count(BOUNDARY) == 1
    assert prompt.index(BOUNDARY) < prompt.index(
        "Operator requested changes before this task can be accepted."
    )
    assert "Request changes note: " + note in prompt

    extraction = task_acceptance_pytest_commands(
        prompt, project_dir=tmp_path / "project"
    )
    assert extraction.section_found is True
    assert extraction.extracted == (OPERATOR_COMMAND,)
    assert tuple(command.text for command in extraction.admitted) == (OPERATOR_COMMAND,)
    assert extraction.refused == ()
    if feedback_command is not None:
        assert feedback_command not in extraction.extracted


def test_request_changes_cycles_use_latest_feedback_without_changing_operator_command(
    authenticated_client, db_session, tmp_path: Path
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)

    _, first_prompt = _request_changes(
        authenticated_client,
        db_session,
        task,
        "First review: `pytest tests/test_first_feedback.py`",
    )
    _, second_prompt = _request_changes(
        authenticated_client,
        db_session,
        task,
        "Second review: `pytest tests/test_second_feedback.py`",
    )

    assert "First review:" in first_prompt
    assert "First review:" not in second_prompt
    assert "Second review:" in second_prompt
    extraction = task_acceptance_pytest_commands(
        second_prompt, project_dir=tmp_path / "project"
    )
    assert extraction.extracted == (OPERATOR_COMMAND,)
    assert tuple(command.text for command in extraction.admitted) == (OPERATOR_COMMAND,)
    assert extraction.refused == ()


def test_request_changes_markdown_and_whitespace_variations_keep_boundary(
    authenticated_client, db_session, tmp_path: Path
):
    description = (
        "Implement the widget.\n\n"
        "  acceptance criteria:\n"
        "  - `pytest -q tests/test_operator.py` passes."
    )
    note = (
        "  Please repair the spacing:\n"
        "    - Keep the layout responsive.\n"
        "    - `pytest tests/test_feedback.py` should pass.  "
    )
    task = _create_task(db_session, tmp_path, description)

    response, prompt = _request_changes(authenticated_client, db_session, task, note)

    assert response.json()["promotion_note"] == note.strip()
    assert prompt.count(BOUNDARY) == 1
    extraction = task_acceptance_pytest_commands(
        prompt, project_dir=tmp_path / "project"
    )
    assert extraction.extracted == ("pytest -q tests/test_operator.py",)
    assert tuple(command.text for command in extraction.admitted) == (
        "pytest -q tests/test_operator.py",
    )
    assert extraction.refused == ()


def test_empty_request_changes_feedback_keeps_existing_api_refusal(
    authenticated_client, db_session, tmp_path: Path
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes",
        json={"note": "  \n  "},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "A review note is required when requesting changes"
    )
    db_session.refresh(task)
    assert task.workspace_status == "blocked"
    assert task.promotion_note is None
    prompt = _build_request_changes_repair_prompt(
        task=Task(
            project_id=task.project_id,
            title=task.title,
            description=task.description,
            promotion_note="  ",
        ),
        base_prompt=task.description,
        change_set=None,
    )
    assert prompt == OPERATOR_DESCRIPTION
    extraction = task_acceptance_pytest_commands(prompt)
    assert tuple(command.text for command in extraction.admitted) == (OPERATOR_COMMAND,)


@pytest.mark.parametrize(
    "description",
    (
        "Implement the feature.\n\n"
        "Planner acceptance criteria:\n"
        "- `pytest tests/test_planner.py` passes.",
        "Implement the feature.\n\n"
        "Planner acceptance criteria:\n"
        "- `pytest tests/test_planner.py` passes.\n\n"
        "Acceptance criteria:\n"
        "- `pytest tests/test_operator.py` passes.",
        "Implement the feature.\n\n"
        "Acceptance criteria:\n"
        "- `pytest tests/test_operator.py` passes.\n\n"
        "Planner acceptance criteria:\n"
        "- `pytest tests/test_planner.py` passes.",
    ),
)
def test_planner_only_and_mixed_descriptions_keep_planner_commands_non_authoritative(
    authenticated_client, db_session, tmp_path: Path, description
):
    task = _create_task(db_session, tmp_path, description)

    _, prompt = _request_changes(
        authenticated_client,
        db_session,
        task,
        "Feedback includes `pytest tests/test_feedback.py`.",
    )

    extraction = task_acceptance_pytest_commands(
        prompt, project_dir=tmp_path / "project"
    )
    if "Acceptance criteria:\n" in description:
        assert extraction.section_found is True
        assert extraction.extracted == ("pytest tests/test_operator.py",)
        assert tuple(command.text for command in extraction.admitted) == (
            "pytest tests/test_operator.py",
        )
    else:
        assert extraction.section_found is False
        assert extraction.extracted == ()
        assert extraction.admitted == ()
    assert extraction.refused == ()
    assert "pytest tests/test_planner.py" not in extraction.extracted
    assert "pytest tests/test_feedback.py" not in extraction.extracted


def test_t3a1_declared_forms_remain_observational_after_request_changes_boundary(
    authenticated_client, db_session, tmp_path: Path
):
    description = (
        "Implement the feature.\n\n"
        "Planner acceptance criteria:\n"
        "- `pytest tests/test_planner.py` passes."
    )
    task = _create_task(db_session, tmp_path, description)

    _, prompt = _request_changes(
        authenticated_client,
        db_session,
        task,
        "Feedback includes `pytest tests/test_feedback.py`.",
    )
    findings = []
    commands_run = []
    raw = candidate_checks._run_task_acceptance(
        project_dir=tmp_path / "project",
        task_prompt=prompt,
        focused_command="",
        focused_returncode=None,
        timeout_seconds=1,
        findings=findings,
        commands_run=commands_run,
    )

    assert raw["section_found"] is False
    assert raw["extracted"] == []
    assert raw["admitted"] == []
    assert raw["refused"] == []
    assert raw["declared_forms"] == [{"form": "planner", "pytest_span_count": 1}]
    assert declared_acceptance_forms(prompt) == tuple(raw["declared_forms"])
    assert findings == []
    assert commands_run == []


def test_r1_f1_real_retry_prompt_handoff_uses_only_trusted_task_description(
    authenticated_client, db_session, tmp_path: Path, monkeypatch
):
    note = (
        "Please review the implementation first.\n\n"
        "Acceptance criteria:\n"
        "- `pytest tests/test_feedback.py` passes."
    )
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    # C1-I1 (D-5): request-changes issuance requires a legitimate source.
    prior_record = _add_prior_change_set(db_session, task)

    request_changes = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": note}
    )
    assert request_changes.status_code == 200, request_changes.text
    assert request_changes.json()["description"] == OPERATOR_DESCRIPTION
    assert request_changes.json()["promotion_note"] == note
    published, task_execution_id = _queue_active_request_changes_retry(
        authenticated_client, task, monkeypatch
    )
    retry_execution = (
        db_session.query(TaskExecution)
        .filter(TaskExecution.id == task_execution_id)
        .one()
    )

    prompt = published["prompt"]
    assert prompt == OPERATOR_DESCRIPTION
    assert BOUNDARY not in prompt
    assert note not in prompt
    authority = retry_execution.dispatch_invocation["acceptance_authority"]
    assert authority == {
        "version": 1,
        "acceptance_mode": "request_changes",
        "trusted_source_kind": "operator_task_description",
        "task_description_sha256": dispatch_value_sha256(OPERATOR_DESCRIPTION),
        "promotion_note_sha256": dispatch_value_sha256(note),
        "prior_change_set": {
            "change_set_id": prior_record.id,
            "task_execution_id": prior_record.task_execution_id,
            "bounded_fields_sha256": authority["prior_change_set"][
                "bounded_fields_sha256"
            ],
        },
    }
    assert retry_execution.dispatch_invocation_sha256 == dispatch_value_sha256(
        retry_execution.dispatch_invocation
    )
    assert published["published_task_id"] == retry_execution.dispatch_celery_task_id
    assert "request_changes_retry" not in (published.get("context") or {})
    result, intercepted = _run_real_acceptance_consumer(
        monkeypatch,
        project_dir=tmp_path / "project",
        retry_prompt=prompt,
        trusted_task_description=OPERATOR_DESCRIPTION,
    )
    extraction = result.task_acceptance
    assert extraction["section_found"] is True
    assert extraction["extracted"] == [OPERATOR_COMMAND]
    assert extraction["admitted"] == [OPERATOR_COMMAND]
    assert extraction["refused"] == []
    assert result.findings == ()
    assert len(intercepted) == 1
    assert "test_operator.py" in intercepted[0]
    assert "first_feedback.py" not in " ".join(intercepted)
    assert "second_feedback.py" not in " ".join(intercepted)
    assert "test_feedback.py" not in " ".join(intercepted)


@pytest.mark.parametrize(
    "note",
    (
        "Review this first.\nAcceptance criteria:\n"
        "- `pytest tests/test_feedback.py` passes.",
        "Ordinary feedback prose.\n\n  aCcEpTaNcE cRiTeRiA:\n"
        "    - `pytest -k smoke tests/` passes.",
        "Acceptance criteria:\n- `pytest tests/test_feedback.py` passes.\n"
        "Further feedback.\nACCEPTANCE CRITERIA:\n"
        "- `pytest --invalid-option tests/test_bad.py` passes.",
        "Please inspect this note.\n  Acceptance criteria:\n"
        "  - `pytest tests/test_feedback.py` passes.\n"
        "  Acceptance criteria:\n"
        "  - `pytest --invalid-option tests/test_bad.py` passes.",
        "Implementation notes:\n- Keep the API stable and preserve spacing.",
        "Please review the layout; this note contains no commands.",
    ),
)
def test_feedback_text_never_enters_the_real_acceptance_consumer(
    authenticated_client, db_session, tmp_path: Path, monkeypatch, note
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    _, retry_prompt = _request_changes(authenticated_client, db_session, task, note)

    result, intercepted = _run_real_acceptance_consumer(
        monkeypatch,
        project_dir=tmp_path / "project",
        retry_prompt=retry_prompt,
        trusted_task_description=task.description,
    )

    extraction = result.task_acceptance
    assert extraction["extracted"] == [OPERATOR_COMMAND]
    assert extraction["admitted"] == [OPERATOR_COMMAND]
    assert extraction["refused"] == []
    assert result.findings == ()
    assert len(intercepted) == 1
    assert "test_operator.py" in intercepted[0]
    assert "test_feedback.py" not in " ".join(intercepted)


def test_unverified_legacy_source_is_blocked_and_configuration_disable_is_distinct(
    monkeypatch, tmp_path: Path
):
    legacy_prompt = OPERATOR_DESCRIPTION + "\n\nFeedback: pytest tests/test_feedback.py"
    blocked, blocked_commands = _run_real_acceptance_consumer(
        monkeypatch,
        project_dir=tmp_path,
        retry_prompt=legacy_prompt,
        trusted_task_description=OPERATOR_DESCRIPTION,
        acceptance_authority_status="unverified_legacy",
    )
    assert blocked.task_acceptance["status"] == "blocked_unverified_provenance"
    assert blocked.task_acceptance["admitted"] == []
    assert blocked_commands == []
    assert any(
        finding.rule_id == "task_acceptance_provenance_unverified"
        for finding in blocked.findings
    )

    disabled, disabled_commands = _run_real_acceptance_consumer(
        monkeypatch,
        project_dir=tmp_path,
        retry_prompt=legacy_prompt,
        trusted_task_description=OPERATOR_DESCRIPTION,
        acceptance_authority_status="unverified_legacy",
        task_acceptance_enabled=False,
    )
    assert disabled.task_acceptance["status"] == "disabled_by_configuration"
    assert disabled.task_acceptance["authority_status"] == "unverified_legacy"
    assert disabled_commands == []

    missing, missing_commands = _run_real_acceptance_consumer(
        monkeypatch,
        project_dir=tmp_path,
        retry_prompt=legacy_prompt,
        trusted_task_description="",
        acceptance_authority_status="verified_request_changes",
    )
    assert missing.task_acceptance["status"] == "blocked_missing_trusted_source"
    assert missing.task_acceptance["admitted"] == []
    assert missing_commands == []


@pytest.mark.parametrize(
    ("description", "expected_section", "expected_command"),
    (
        (
            "Implement the feature.\n\nPlanner acceptance criteria:\n"
            "- `pytest tests/test_planner.py` passes.",
            False,
            None,
        ),
        (
            "Implement the feature.\n\nPlanner acceptance criteria:\n"
            "- `pytest tests/test_planner.py` passes.\n\n"
            "Acceptance criteria:\n- `pytest tests/test_operator.py` passes.",
            True,
            "pytest tests/test_operator.py",
        ),
    ),
)
def test_planner_only_and_mixed_tasks_keep_feedback_non_authoritative(
    authenticated_client,
    db_session,
    tmp_path: Path,
    monkeypatch,
    description,
    expected_section,
    expected_command,
):
    task = _create_task(db_session, tmp_path, description)
    _, retry_prompt = _request_changes(
        authenticated_client,
        db_session,
        task,
        "Feedback prose.\nAcceptance criteria:\n"
        "- `pytest tests/test_feedback.py` passes.",
    )

    result, intercepted = _run_real_acceptance_consumer(
        monkeypatch,
        project_dir=tmp_path / "project",
        retry_prompt=retry_prompt,
        trusted_task_description=task.description,
    )

    extraction = result.task_acceptance
    assert extraction["section_found"] is expected_section
    assert extraction["extracted"] == ([expected_command] if expected_command else [])
    assert extraction["admitted"] == ([expected_command] if expected_command else [])
    assert extraction["refused"] == []
    assert result.findings == ()
    assert len(intercepted) == int(expected_command is not None)
    assert "test_feedback.py" not in " ".join(intercepted)


def test_repeated_request_changes_cycles_use_latest_note_but_original_authority(
    authenticated_client, db_session, tmp_path: Path, monkeypatch
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    # C1-I1 (D-5): request-changes issuance requires a legitimate source.
    _add_prior_change_set(db_session, task)
    first_note = (
        "First cycle.\nAcceptance criteria:\n"
        "- `pytest tests/test_first_feedback.py` passes."
    )
    first_review = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": first_note}
    )
    assert first_review.status_code == 200, first_review.text
    first_published, first_execution_id = _queue_active_request_changes_retry(
        authenticated_client, task, monkeypatch
    )
    first_execution = db_session.get(TaskExecution, first_execution_id)
    assert first_execution is not None
    first_authority = first_execution.dispatch_invocation["acceptance_authority"]

    # Model completion of that repair before the next review cycle.
    task.status = TaskStatus.DONE
    task.workspace_status = "ready"
    db_session.commit()
    latest_note = (
        "Second cycle.\nacceptance criteria:\n"
        "- `pytest tests/test_second_feedback.py` passes."
    )
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": latest_note}
    )
    assert response.status_code == 200, response.text
    second_published, second_execution_id = _queue_active_request_changes_retry(
        authenticated_client, task, monkeypatch
    )
    second_execution = db_session.get(TaskExecution, second_execution_id)
    assert second_execution is not None
    second_authority = second_execution.dispatch_invocation["acceptance_authority"]
    assert (
        first_published["prompt"] == second_published["prompt"] == OPERATOR_DESCRIPTION
    )
    assert (
        first_authority["task_description_sha256"]
        == second_authority["task_description_sha256"]
        == dispatch_value_sha256(OPERATOR_DESCRIPTION)
    )
    assert first_authority["promotion_note_sha256"] == dispatch_value_sha256(first_note)
    assert second_authority["promotion_note_sha256"] == dispatch_value_sha256(
        latest_note
    )
    assert first_execution.id != second_execution.id
    assert (
        second_authority["promotion_note_sha256"]
        != first_authority["promotion_note_sha256"]
    )


def test_active_review_requires_explicit_request_changes_retry_intent(
    authenticated_client, db_session, tmp_path: Path, monkeypatch
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    # C1-I1 (D-5): request-changes issuance requires a legitimate source.
    _add_prior_change_set(db_session, task)
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": "Repair this."}
    )
    assert changed.status_code == 200, changed.text
    before = db_session.query(TaskExecution).filter_by(task_id=task.id).count()

    missing = authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")
    ordinary = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json={"retry_intent": "ordinary"}
    )
    invalid = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json={"retry_intent": "surprise"}
    )
    assert missing.status_code == ordinary.status_code == 409
    assert invalid.status_code == 422
    assert db_session.query(TaskExecution).filter_by(task_id=task.id).count() == before

    published, execution_id = _queue_active_request_changes_retry(
        authenticated_client, task, monkeypatch
    )
    execution = db_session.get(TaskExecution, execution_id)
    assert (
        execution.dispatch_invocation["acceptance_authority"]["acceptance_mode"]
        == "request_changes"
    )
    assert published["prompt"] == OPERATOR_DESCRIPTION


def test_ordinary_retry_intent_is_persisted_for_non_review_task(
    authenticated_client, db_session, tmp_path: Path, monkeypatch
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    published = _stub_retry_publication(monkeypatch)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json={"retry_intent": "ordinary"}
    )
    assert response.status_code == 200, response.text
    execution = db_session.get(TaskExecution, response.json()["task_execution_id"])
    authority = execution.dispatch_invocation["acceptance_authority"]
    assert authority["acceptance_mode"] == "ordinary"
    assert authority["trusted_source_kind"] == "managed_task_prompt"
    assert authority["promotion_note_sha256"] is None
    assert authority["prior_change_set"] is None
    assert execution.dispatch_invocation_sha256 == dispatch_value_sha256(
        execution.dispatch_invocation
    )
    assert published["prompt"] == OPERATOR_DESCRIPTION


def test_request_changes_intent_is_rejected_without_active_review(
    authenticated_client, db_session, tmp_path: Path
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"retry_intent": "request_changes"},
    )
    assert response.status_code == 409
    assert db_session.query(TaskExecution).filter_by(task_id=task.id).count() == 0


def test_request_changes_replay_reference_remains_strictly_description_bound(
    authenticated_client, db_session, tmp_path: Path, monkeypatch
):
    from app.services.execution.workflow_lifecycle import (
        _dispatch_replay_arguments,
        configure_task_execution_dispatch,
    )

    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    # C1-I1 (D-5): request-changes issuance requires a legitimate source.
    _add_prior_change_set(db_session, task)
    note = (
        "Change this.\nAcceptance criteria:\n- `pytest tests/test_feedback.py` passes."
    )
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": note}
    )
    assert changed.status_code == 200, changed.text
    published, execution_id = _queue_active_request_changes_retry(
        authenticated_client, task, monkeypatch
    )
    db_session.refresh(task)
    execution = db_session.get(TaskExecution, execution_id)
    session = db_session.get(SessionModel, execution.session_id)
    invocation = execution.dispatch_invocation
    authority = invocation["acceptance_authority"]
    replay_arguments, replay_error = _dispatch_replay_arguments(db_session, execution)
    assert replay_error is None
    assert replay_arguments["prompt"] == task.description == OPERATOR_DESCRIPTION
    assert published["prompt"] == task.description
    assert invocation["prompt_source"] == "description"
    assert (
        invocation["prompt_sha256"]
        == hashlib.sha256(task.description.encode("utf-8")).hexdigest()
    )
    assert authority["trusted_source_kind"] == "operator_task_description"
    assert authority["task_description_sha256"] == dispatch_value_sha256(
        task.description
    )
    assert authority["promotion_note_sha256"] == dispatch_value_sha256(
        task.promotion_note
    )
    assert note not in replay_arguments["prompt"]
    assert note not in json.dumps(invocation, sort_keys=True)
    assert "acceptance_authority_status" not in invocation
    assert execution.dispatch_invocation_sha256 == dispatch_value_sha256(invocation)

    combined_feedback_prompt = (
        f"{task.description}\n\n{BOUNDARY}\n{task.promotion_note}"
    )
    assert combined_feedback_prompt != task.description
    original_invocation = json.loads(json.dumps(invocation))
    original_digest = execution.dispatch_invocation_sha256
    assert not configure_task_execution_dispatch(
        db_session,
        task_execution=execution,
        session=session,
        task=task,
        dispatch_kind="ROOT",
        prompt=combined_feedback_prompt,
        timeout_seconds=300,
        acceptance_authority=authority,
    )
    assert execution.dispatch_invocation == original_invocation
    assert execution.dispatch_invocation_sha256 == original_digest


def test_request_changes_binds_exact_prior_change_set_and_bounded_repair_fields(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
):
    from app.services.tasks.service import TaskService

    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    prior_record = _add_prior_change_set(db_session, task)
    note = "Fix the first added file."
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": note}
    )
    assert changed.status_code == 200, changed.text
    published, execution_id = _queue_active_request_changes_retry(
        authenticated_client, task, monkeypatch
    )
    execution = db_session.get(TaskExecution, execution_id)
    authority = execution.dispatch_invocation["acceptance_authority"]
    original_execution = db_session.get(TaskExecution, prior_record.task_execution_id)
    assert original_execution is not None
    assert original_execution.id == prior_record.task_execution_id
    assert original_execution.id != execution.id
    assert original_execution.status == TaskStatus.DONE
    assert execution.status == TaskStatus.PENDING
    assert original_execution.task_id == execution.task_id == task.id
    assert original_execution.session_id == execution.session_id
    assert original_execution.attempt_number == 1
    assert execution.attempt_number == 2
    assert prior_record.task_id == task.id
    assert prior_record.project_id == task.project_id
    assert prior_record.session_id == original_execution.session_id
    assert authority["prior_change_set"]["task_execution_id"] != execution.id
    latest = TaskService(db_session).get_latest_task_change_set_for_task(task.id)
    fields = TaskService.request_changes_change_set_fields(latest)
    fields_again = TaskService.request_changes_change_set_fields(latest)
    assert fields_again == fields
    assert dispatch_value_sha256(fields_again) == dispatch_value_sha256(fields)
    unrelated_authority = dict(latest)
    unrelated_authority["operator_acceptance_authority"] = "caller supplied"
    assert TaskService.request_changes_change_set_fields(unrelated_authority) == fields
    assert authority["prior_change_set"] == {
        "change_set_id": prior_record.id,
        "task_execution_id": prior_record.task_execution_id,
        "bounded_fields_sha256": dispatch_value_sha256(fields),
    }
    assert authority["task_description_sha256"] == dispatch_value_sha256(
        OPERATOR_DESCRIPTION
    )
    assert published["prompt"] == OPERATOR_DESCRIPTION

    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / "worker-change-set",
        db_session_factory=db_session_factory,
        task=task,
        prompt=published["prompt"],
        context=None,
        dispatch_kind="ROOT",
        persisted_task_execution_id=execution_id,
        expected_attempt_number=2,
        celery_task_id=published["published_task_id"],
    )
    diagnostics = _worker_seam_failure_diagnostics(result)
    assert len(result.worker_contexts) == 1, diagnostics
    assert result.persisted_task_review_state == {
        "id": task.id,
        "project_id": task.project_id,
        "description": OPERATOR_DESCRIPTION,
        "status": TaskStatus.PENDING,
        "workspace_status": "changes_requested",
        "promotion_note": note,
    }, diagnostics
    assert result.persisted_task_execution_state["id"] == execution_id
    assert result.persisted_task_execution_state["attempt_number"] == 2
    assert (
        result.persisted_task_execution_state["dispatch_invocation_sha256"]
        == execution.dispatch_invocation_sha256
    )
    assert (
        result.persisted_task_execution_state["dispatch_invocation"]
        == execution.dispatch_invocation
    )
    assert result.prior_change_set_reference == authority["prior_change_set"]
    assert result.main_run_contexts == result.worker_contexts
    assert result.step_loop_reached is True
    assert result.cleanup_exceptions == [], diagnostics
    assert (
        result.worker_contexts[0].acceptance_authority_status
        == "verified_request_changes"
    )
    assert "added=14" in result.worker_contexts[0].prompt
    assert "added-11.py" in result.worker_contexts[0].prompt
    assert "added-12.py" not in result.worker_contexts[0].prompt
    assert "warning-7" in result.worker_contexts[0].prompt
    assert "warning-8" not in result.worker_contexts[0].prompt
    assert result.worker_contexts[0].acceptance_task_prompt == OPERATOR_DESCRIPTION


def test_request_changes_requires_authenticated_completed_review_state(
    authenticated_client, db_session, tmp_path: Path
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    task.status = TaskStatus.FAILED
    task.workspace_status = "blocked"
    db_session.commit()
    incomplete_review = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": "Repair this."}
    )
    assert incomplete_review.status_code == 409


def test_unauthenticated_request_changes_is_rejected(
    api_client, db_session, tmp_path: Path
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    response = api_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": "Repair this."}
    )
    assert response.status_code == 401


def test_authority_commit_precedes_recording_only_publication(
    authenticated_client, db_session_factory, db_session, tmp_path: Path, monkeypatch
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    # C1-I1 (D-5): request-changes issuance requires a legitimate source.
    _add_prior_change_set(db_session, task)
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": "Review note."}
    )
    assert changed.status_code == 200, changed.text
    observed = []

    def _observe_committed_invocation(kwargs, _options):
        observer = db_session_factory()
        try:
            execution = observer.get(TaskExecution, kwargs["task_execution_id"])
            assert execution is not None
            assert execution.dispatch_state == "READY"
            assert (
                execution.dispatch_invocation["acceptance_authority"]["acceptance_mode"]
                == "request_changes"
            )
            assert execution.dispatch_invocation_sha256 == dispatch_value_sha256(
                execution.dispatch_invocation
            )
            observed.append(execution.id)
        finally:
            observer.close()

    published = _stub_retry_publication(
        monkeypatch, on_publish=_observe_committed_invocation
    )
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"retry_intent": "request_changes"},
    )
    assert response.status_code == 200, response.text
    assert observed == [response.json()["task_execution_id"]]
    assert published["published_task_id"]


def test_publication_failure_keeps_same_authority_recoverable(
    authenticated_client, db_session, tmp_path: Path, monkeypatch
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    # C1-I1 (D-5): request-changes issuance requires a legitimate source.
    _add_prior_change_set(db_session, task)
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": "Review note."}
    )
    assert changed.status_code == 200, changed.text

    def _record_then_fail(_kwargs, _options):
        raise RuntimeError("offline publisher failure")

    published = _stub_retry_publication(monkeypatch, on_publish=_record_then_fail)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"retry_intent": "request_changes"},
    )
    assert response.status_code == 200, response.text
    execution = db_session.get(TaskExecution, response.json()["task_execution_id"])
    assert execution.dispatch_state == "READY"
    assert execution.status == TaskStatus.PENDING
    assert execution.dispatch_last_error == "publisher_error:RuntimeError"
    assert execution.dispatch_publish_attempts == 1
    assert (
        execution.dispatch_invocation["acceptance_authority"]["acceptance_mode"]
        == "request_changes"
    )
    assert execution.dispatch_invocation_sha256 == dispatch_value_sha256(
        execution.dispatch_invocation
    )
    assert published["published_task_id"] == execution.dispatch_celery_task_id


def test_invocation_commit_failure_prevents_publication(
    authenticated_client,
    db_session_factory,
    db_session,
    tmp_path: Path,
    monkeypatch,
):
    import app.api.v1.endpoints.tasks as tasks_endpoint

    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    # C1-I1 (D-5): request-changes issuance requires a legitimate source.
    prior_record = _add_prior_change_set(db_session, task)
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": "Review note."}
    )
    assert changed.status_code == 200, changed.text
    published = _stub_retry_publication(monkeypatch)
    configure = tasks_endpoint.configure_task_execution_dispatch

    def _configure_then_break_commit(db, **kwargs):
        result = configure(db, **kwargs)

        def _fail_commit():
            raise RuntimeError("offline commit failure")

        monkeypatch.setattr(db, "commit", _fail_commit)
        return result

    monkeypatch.setattr(
        tasks_endpoint,
        "configure_task_execution_dispatch",
        _configure_then_break_commit,
    )
    with pytest.raises(RuntimeError, match="offline commit failure"):
        authenticated_client.post(
            f"/api/v1/tasks/{task.id}/retry",
            json={"retry_intent": "request_changes"},
        )
    observer = db_session_factory()
    try:
        # Only the seeded source execution exists; no retry row or witness.
        assert [
            row.id for row in observer.query(TaskExecution).filter_by(task_id=task.id)
        ] == [prior_record.task_execution_id]
        assert observer.query(RequestChangesIssuanceWitness).count() == 0
    finally:
        observer.close()
    assert published == {}


def test_duplicate_publication_reuses_same_invocation_and_task_identity(
    authenticated_client, db_session, tmp_path: Path, monkeypatch
):
    from app.services.execution.workflow_lifecycle import (
        publish_task_execution_dispatch,
    )

    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    # C1-I1 (D-5): request-changes issuance requires a legitimate source.
    _add_prior_change_set(db_session, task)
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": "Review note."}
    )
    assert changed.status_code == 200, changed.text
    published, execution_id = _queue_active_request_changes_retry(
        authenticated_client, task, monkeypatch
    )
    execution = db_session.get(TaskExecution, execution_id)
    original_invocation = json.loads(json.dumps(execution.dispatch_invocation))
    original_digest = execution.dispatch_invocation_sha256
    original_task_id = execution.dispatch_celery_task_id
    execution.dispatch_next_attempt_at = datetime.now(timezone.utc).replace(
        tzinfo=None
    ) - timedelta(seconds=1)
    db_session.commit()

    duplicate_calls = []

    def _record_duplicate(*, kwargs, task_id, retries=0):
        duplicate_calls.append((kwargs, task_id, retries))

    result = publish_task_execution_dispatch(
        db_session, execution_id, publisher=_record_duplicate
    )
    db_session.refresh(execution)
    assert result["state"] == "published_unclaimed"
    assert len(duplicate_calls) == 1
    assert duplicate_calls[0][1] == original_task_id == published["published_task_id"]
    assert execution.dispatch_invocation == original_invocation
    assert execution.dispatch_invocation_sha256 == original_digest
    assert (
        execution.dispatch_invocation["acceptance_authority"]["acceptance_mode"]
        == "request_changes"
    )


@pytest.mark.parametrize(
    (
        "workspace_status",
        "promotion_note",
        "error_message",
        "dispatch_kind",
        "continuation_kind",
    ),
    (
        ("isolated", None, None, None, None),
        (
            "changes_requested",
            None,
            "Automatic recovery requested: inspect and repair.",
            "RECOVERY",
            None,
        ),
    ),
    ids=("ordinary", "recovery"),
)
def test_real_worker_state_constructor_accepts_ordinary_and_recovery_entry(
    monkeypatch,
    tmp_path: Path,
    db_session_factory,
    workspace_status,
    promotion_note,
    error_message,
    dispatch_kind,
    continuation_kind,
):
    from app.services.orchestration.prompt_templates import OrchestrationState
    from app.services.orchestration.types import OrchestrationRunContext

    task = _worker_task(
        OPERATOR_DESCRIPTION,
        workspace_status=workspace_status,
        promotion_note=promotion_note,
        error_message=error_message,
    )
    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path,
        db_session_factory=db_session_factory,
        task=task,
        prompt=OPERATOR_DESCRIPTION,
        context=None,
        continuation_kind=continuation_kind,
        dispatch_kind=dispatch_kind,
        complete=False,
    )

    diagnostics = json.dumps(
        {
            "primary_exceptions": [
                {"type": type(exc).__name__, "repr": repr(exc), "traceback": trace}
                for exc, trace in zip(
                    result.primary_exceptions, result.primary_tracebacks
                )
            ],
            "cleanup_exceptions": [repr(exc) for exc in result.cleanup_exceptions],
            "initial_states": result.initial_states,
            "initial_identity": result.initial_identity,
            "after_claim": result.state_observations,
            "after_worker": result.database_states_after_worker,
            "planning_phase_calls": result.planning_phase_calls,
            "provider_calls": result.provider_calls,
            "worker_db_getter_calls": len(result.worker_db_sessions),
            "worker_db_session_identity": [
                session is result.db for session in result.worker_db_sessions
            ],
            "worker_db_engine_identity": [
                engine is result.db.get_bind() for engine in result.worker_db_engines
            ],
            "database_url": result.isolated_db_url,
            "db_close_calls": result.db_close_calls,
        },
        indent=2,
        default=str,
    )
    assert len(result.worker_contexts) == 1, diagnostics
    assert result.primary_exceptions == [], diagnostics
    assert result.cleanup_exceptions == [], diagnostics
    assert result.planning_phase_calls == [
        (
            result.initial_identity["session_id"],
            result.initial_identity["task_id"],
            result.initial_identity["task_execution_id"],
        )
    ], diagnostics
    assert result.provider_calls == [], diagnostics
    assert result.initial_states == {
        "task": TaskStatus.PENDING,
        "session": "pending",
        "session_task": TaskStatus.PENDING,
        "task_execution": TaskStatus.PENDING,
    }
    expected_identity = result.initial_identity
    assert result.state_observations == [
        {
            "state_type": "OrchestrationState",
            "context_type": "OrchestrationRunContext",
            "context_session_id": expected_identity["session_id"],
            "context_task_id": expected_identity["task_id"],
            "context_task_execution_id": expected_identity["task_execution_id"],
            "context_session_task_id": expected_identity["session_task_id"],
            "context_acceptance_authority_status": (
                "verified_recovery_legacy"
                if dispatch_kind == "RECOVERY"
                else "unverified_legacy"
            ),
            "session_status": "running",
            "task_status": TaskStatus.RUNNING,
            "session_task_status": TaskStatus.RUNNING,
            "task_execution_status": TaskStatus.RUNNING,
            "persisted_identity": {
                "task_id": expected_identity["task_id"],
                "task_project_id": expected_identity["project_id"],
                "session_id": expected_identity["session_id"],
                "session_project_id": expected_identity["project_id"],
                "session_instance_id": expected_identity["session_instance_id"],
                "session_task_id": expected_identity["session_task_id"],
                "session_task_session_id": expected_identity["session_id"],
                "session_task_task_id": expected_identity["task_id"],
                "task_execution_id": expected_identity["task_execution_id"],
                "task_execution_session_id": expected_identity["session_id"],
                "task_execution_task_id": expected_identity["task_id"],
                "task_execution_attempt_number": expected_identity["attempt_number"],
                "task_execution_dispatch_state": expected_identity["dispatch_state"],
                "task_execution_dispatch_kind": expected_identity["dispatch_kind"],
                "task_execution_dispatch_parent_id": expected_identity[
                    "dispatch_parent_task_execution_id"
                ],
            },
        }
    ], diagnostics
    assert result.worker_db_sessions, diagnostics
    assert all(
        session is result.db for session in result.worker_db_sessions
    ), diagnostics
    assert len(result.worker_db_engines) == len(result.worker_db_sessions), diagnostics
    assert all(
        engine is result.db.get_bind() for engine in result.worker_db_engines
    ), diagnostics
    assert result.isolated_db_url == "sqlite://", diagnostics
    assert len(result.db_close_calls) == len(result.worker_db_sessions), diagnostics
    assert isinstance(result.worker_contexts[0], OrchestrationRunContext)
    assert isinstance(result.worker_contexts[0].orchestration_state, OrchestrationState)
    assert result.worker_contexts[0].acceptance_task_prompt is None
    assert result.worker_contexts[0].acceptance_authority_status == (
        "verified_recovery_legacy"
        if dispatch_kind == "RECOVERY"
        else "unverified_legacy"
    )
    assert (
        result.worker_contexts[0].orchestration_state.task_description
        == OPERATOR_DESCRIPTION
    )
    assert result.validator_calls == [], diagnostics
    assert result.executed_commands == [], diagnostics


@pytest.mark.parametrize(
    "source_context",
    (
        None,
        {},
        {"request_changes_retry": False},
        {
            "acceptance_mode": "ordinary",
            "trusted_source_kind": "forged",
            "acceptance_task_prompt": "forged",
        },
        {
            "request_changes_retry": True,
            "request_changes_acceptance_task_prompt": ["malformed", "source"],
        },
        {
            "request_changes_retry": True,
            "request_changes_acceptance_task_prompt": (
                "Forged request.\nAcceptance criteria:\n"
                "- `pytest tests/test_forged_context.py` passes."
            ),
        },
    ),
    ids=(
        "missing-context",
        "empty-context",
        "false-broker-mode",
        "forged-mode-and-source",
        "malformed-source",
        "forged-source",
    ),
)
def test_real_worker_coordinator_validator_use_only_persisted_operator_source(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
    source_context,
):
    feedback = (
        "Please repair the widget.\n\n"
        "Acceptance criteria:\n"
        "- `pytest tests/test_feedback.py` passes."
    )
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    # C1-I1 (D-5): request-changes issuance requires a legitimate source.
    _add_prior_change_set(db_session, task)
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": feedback}
    )
    assert changed.status_code == 200, changed.text
    published, task_execution_id = _queue_active_request_changes_retry(
        authenticated_client, task, monkeypatch
    )
    assert published["prompt"] == OPERATOR_DESCRIPTION
    assert BOUNDARY not in published["prompt"]
    assert "test_feedback.py" not in published["prompt"]

    # The real API no longer sends a caller-authored acceptance source.
    # Variants below inject legacy, malformed or forged values directly at the
    # Worker seam to verify they cannot replace the persisted Task.description.
    context = published.get("context") if source_context is None else source_context
    # Keep this fixture ORM object deliberately stale after the API committed
    # the review note. The Worker seam must use the persisted Task row.
    task.promotion_note = f"stale caller-side note: {feedback}"
    task.error_message = None
    task.execution_profile = "full_lifecycle"
    task.task_subfolder = task.task_subfolder or f"task-{task.id}"

    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path
        / (
            source_context.__class__.__name__
            if source_context is not None
            else "missing-source"
        ),
        db_session_factory=db_session_factory,
        task=task,
        prompt=published["prompt"],
        context=context,
        persisted_task_execution_id=task_execution_id,
        celery_task_id=published["published_task_id"],
    )

    diagnostics = _worker_seam_failure_diagnostics(result)
    assert len(result.worker_contexts) == 1, diagnostics
    assert result.persisted_task_review_state["workspace_status"] == (
        "changes_requested"
    ), diagnostics
    assert result.persisted_task_review_state["promotion_note"] == feedback
    assert result.persisted_task_review_state["description"] == OPERATOR_DESCRIPTION
    assert result.main_run_contexts == result.worker_contexts
    assert result.step_loop_reached is True
    run_context = result.worker_contexts[0]
    identity = result.initial_identity
    observation = result.state_observations[0]
    assert result.initial_states == {
        "task": TaskStatus.PENDING,
        "session": "running",
        "session_task": TaskStatus.PENDING,
        "task_execution": TaskStatus.PENDING,
    }
    assert observation["context_session_id"] == identity["session_id"]
    assert observation["context_task_id"] == identity["task_id"]
    assert observation["context_task_execution_id"] == identity["task_execution_id"]
    assert observation["session_status"] == "running"
    assert observation["task_status"] == TaskStatus.RUNNING
    assert observation["session_task_status"] == TaskStatus.RUNNING
    assert observation["task_execution_status"] == TaskStatus.RUNNING
    assert (
        observation["persisted_identity"]["session_instance_id"]
        == identity["session_instance_id"]
    )
    assert (
        observation["persisted_identity"]["task_execution_dispatch_state"] == "CLAIMED"
    )
    assert observation["persisted_identity"]["task_execution_dispatch_kind"] == "ROOT"
    assert result.isolated_db_url == "sqlite://"
    assert all(session is result.db for session in result.worker_db_sessions)
    assert all(engine is result.db.get_bind() for engine in result.worker_db_engines)
    assert len(result.db_close_calls) == len(result.worker_db_sessions)
    assert result.cleanup_exceptions == []
    assert len(result.primary_exceptions) == 1
    assert isinstance(result.primary_exceptions[0], _StopAtRealValidator)
    assert run_context.acceptance_task_prompt == OPERATOR_DESCRIPTION
    assert run_context.acceptance_authority_status == "verified_request_changes"
    assert BOUNDARY in run_context.prompt
    assert feedback in run_context.prompt
    assert len(result.validator_calls) == 1
    assert result.validator_calls[0]["acceptance_task_prompt"] == OPERATOR_DESCRIPTION
    assert (
        result.validator_calls[0]["acceptance_authority_status"]
        == "verified_request_changes"
    )
    assert result.validation_results
    acceptance = result.validation_results[0].details["task_acceptance_verification"]
    assert acceptance["section_found"] is True
    assert acceptance["extracted"] == [OPERATOR_COMMAND]
    assert acceptance["admitted"] == [OPERATOR_COMMAND]
    assert acceptance["refused"] == []
    assert len(result.executed_commands) == 1
    assert "test_operator.py" in result.executed_commands[0]
    assert "test_feedback.py" not in " ".join(result.executed_commands)
    assert "test_forged_context.py" not in " ".join(result.executed_commands)
    assert len(result.db_close_calls) == len(result.worker_db_sessions)


@pytest.mark.parametrize(
    ("description", "expected_commands"),
    (
        (
            "Implement the widget.\n\n"
            "Planner acceptance criteria:\n"
            "- `pytest tests/test_planner_only.py` passes.",
            [],
        ),
        (
            OPERATOR_DESCRIPTION + "\n\nPlanner acceptance criteria:\n"
            "- `pytest tests/test_planner_only.py` passes.",
            [OPERATOR_COMMAND],
        ),
    ),
    ids=("planner-only", "mixed-operator-and-planner"),
)
def test_real_worker_preserves_planner_non_authority(
    authenticated_client,
    db_session,
    tmp_path: Path,
    monkeypatch,
    db_session_factory,
    description,
    expected_commands,
):
    feedback = "Acceptance criteria:\n- `pytest tests/test_feedback.py` passes."
    task = _create_task(db_session, tmp_path, description)
    # C1-I1 (D-5): request-changes issuance requires a legitimate source.
    _add_prior_change_set(db_session, task)
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": feedback}
    )
    assert changed.status_code == 200, changed.text
    published, task_execution_id = _queue_active_request_changes_retry(
        authenticated_client, task, monkeypatch
    )
    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path,
        db_session_factory=db_session_factory,
        task=task,
        prompt=published["prompt"],
        context={"request_changes_retry": True, "acceptance_source": "forged"},
        persisted_task_execution_id=task_execution_id,
        celery_task_id=published["published_task_id"],
    )

    assert result.worker_contexts[0].acceptance_task_prompt == description
    assert (
        result.worker_contexts[0].acceptance_authority_status
        == "verified_request_changes"
    )
    acceptance = result.validation_results[0].details["task_acceptance_verification"]
    assert acceptance["admitted"] == expected_commands
    assert len(result.executed_commands) == len(expected_commands)
    assert "test_planner_only.py" not in " ".join(result.executed_commands)
    assert "test_feedback.py" not in " ".join(result.executed_commands)


def test_real_worker_ordinary_retry_keeps_verified_legacy_acceptance_source(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    published = _stub_retry_publication(monkeypatch)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json={"retry_intent": "ordinary"}
    )
    assert response.status_code == 200, response.text
    execution_id = response.json()["task_execution_id"]
    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / "ordinary-worker",
        db_session_factory=db_session_factory,
        task=task,
        prompt=published["prompt"],
        context={
            "acceptance_mode": "request_changes",
            "acceptance_task_prompt": "forged feedback source",
        },
        persisted_task_execution_id=execution_id,
        celery_task_id=published["published_task_id"],
    )
    assert len(result.worker_contexts) == 1
    run_context = result.worker_contexts[0]
    assert run_context.acceptance_authority_status == "verified_ordinary"
    assert run_context.acceptance_task_prompt is None
    acceptance = result.validation_results[0].details["task_acceptance_verification"]
    assert acceptance["admitted"] == [OPERATOR_COMMAND]
    assert acceptance["authority_status"] == "verified_ordinary"
    assert len(result.executed_commands) == 1
    assert "test_operator.py" in result.executed_commands[0]
    assert "forged" not in " ".join(result.executed_commands)


def test_real_worker_request_changes_without_persisted_provenance_is_refused(
    tmp_path: Path, monkeypatch, db_session_factory
):
    task = _worker_task(OPERATOR_DESCRIPTION)
    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path,
        db_session_factory=db_session_factory,
        task=task,
        prompt=OPERATOR_DESCRIPTION,
        context={"request_changes_retry": True},
    )
    assert result.worker_contexts == []
    assert len(result.primary_exceptions) == 1
    assert "acceptance_provenance_refused:request_changes_metadata_missing" in str(
        result.primary_exceptions[0]
    )
    assert result.validator_calls == []
    assert result.executed_commands == []


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    (
        ("authority_mode", "authority_metadata_invalid"),
        ("trusted_source_kind", "authority_metadata_invalid"),
        ("task_description_digest", "task_description_changed"),
        ("promotion_note_digest", "request_changes_source_changed"),
        (
            "prior_change_set_identity",
            "request_changes_witness_invocation_change_set_mismatch",
        ),
        (
            "bounded_change_set_digest",
            "request_changes_witness_invocation_change_set_mismatch",
        ),
        ("recomputed_unauthorized_metadata", "authority_metadata_invalid"),
        ("missing", "request_changes_metadata_missing"),
        ("malformed", "authority_metadata_invalid"),
        ("unsupported_version", "authority_metadata_invalid"),
        ("invocation_digest", "invocation_digest_mismatch"),
        ("stale_description", "broker_prompt_differs_from_persisted_reference"),
        ("stale_note", "request_changes_source_changed"),
    ),
)
def test_real_worker_fails_closed_on_provenance_mutation(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
    mutation,
    expected_reason,
):
    note = (
        "Repair this.\nAcceptance criteria:\n- `pytest tests/test_feedback.py` passes."
    )
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    prior_record = _add_prior_change_set(db_session, task)
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": note}
    )
    assert changed.status_code == 200, changed.text
    published, execution_id = _queue_active_request_changes_retry(
        authenticated_client, task, monkeypatch
    )
    execution = db_session.get(TaskExecution, execution_id)
    invocation = json.loads(json.dumps(execution.dispatch_invocation))
    authority = invocation["acceptance_authority"]
    if mutation == "missing":
        invocation.pop("acceptance_authority")
    elif mutation == "malformed":
        invocation["acceptance_authority"] = {"version": 1}
    elif mutation == "unsupported_version":
        authority["version"] = 2
    elif mutation == "invocation_digest":
        invocation["timeout_seconds"] = 2
    elif mutation == "authority_mode":
        authority["acceptance_mode"] = "ordinary"
    elif mutation == "trusted_source_kind":
        authority["trusted_source_kind"] = "managed_task_prompt"
    elif mutation == "task_description_digest":
        authority["task_description_sha256"] = dispatch_value_sha256(
            "unauthorized description"
        )
    elif mutation == "promotion_note_digest":
        authority["promotion_note_sha256"] = dispatch_value_sha256(
            "unauthorized promotion note"
        )
    elif mutation == "prior_change_set_identity":
        authority["prior_change_set"]["change_set_id"] = prior_record.id + 1000
    elif mutation == "bounded_change_set_digest":
        authority["prior_change_set"]["bounded_fields_sha256"] = dispatch_value_sha256(
            "unauthorized bounded fields"
        )
    elif mutation == "recomputed_unauthorized_metadata":
        authority["untrusted_metadata"] = {"source": "caller"}
    elif mutation == "stale_description":
        task.description = OPERATOR_DESCRIPTION + " edited after publication"
    elif mutation == "stale_note":
        task.promotion_note = note + " edited after publication"
    execution.dispatch_invocation = invocation
    if mutation != "invocation_digest":
        execution.dispatch_invocation_sha256 = dispatch_value_sha256(invocation)
    db_session.commit()

    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / mutation,
        db_session_factory=db_session_factory,
        task=task,
        prompt=published["prompt"],
        context=None,
        persisted_task_execution_id=execution_id,
        expected_attempt_number=2,
        celery_task_id=published["published_task_id"],
    )
    diagnostics = _worker_seam_failure_diagnostics(result)
    assert result.worker_contexts == [], diagnostics
    assert len(result.primary_exceptions) == 1, diagnostics
    assert expected_reason in str(result.primary_exceptions[0])
    assert result.cleanup_exceptions == [], diagnostics
    assert result.provider_calls == [], diagnostics
    assert result.executed_commands == [], diagnostics
    assert result.validator_calls == [], diagnostics


def test_request_changes_retry_rejects_caller_supplied_authority(
    authenticated_client, db_session, tmp_path: Path, monkeypatch
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": "Review note."}
    )
    assert changed.status_code == 200, changed.text
    published = _stub_retry_publication(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={
            "retry_intent": "request_changes",
            "acceptance_authority": {
                "version": 1,
                "acceptance_mode": "request_changes",
                "trusted_source_kind": "operator_task_description",
            },
        },
    )

    assert response.status_code == 422
    assert published == {}
    assert db_session.query(TaskExecution).filter_by(task_id=task.id).count() == 0


def test_real_worker_rejects_cross_task_change_set_substitution(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
):
    from app.services.tasks.service import TaskService

    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    _add_prior_change_set(db_session, task)
    other_root = tmp_path / "other-task"
    other_root.mkdir()
    other_task = _create_task(db_session, other_root, OPERATOR_DESCRIPTION)
    other_change_set = _add_prior_change_set(db_session, other_task)
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": "Review note."}
    )
    assert changed.status_code == 200, changed.text
    published, execution_id = _queue_active_request_changes_retry(
        authenticated_client, task, monkeypatch
    )
    execution = db_session.get(TaskExecution, execution_id)
    invocation = json.loads(json.dumps(execution.dispatch_invocation))
    other_record = TaskService(db_session).get_task_execution_change_set(
        task_execution_id=other_change_set.task_execution_id
    )
    fields = TaskService.request_changes_change_set_fields(other_record)
    invocation["acceptance_authority"]["prior_change_set"] = {
        "change_set_id": other_change_set.id,
        "task_execution_id": other_change_set.task_execution_id,
        "bounded_fields_sha256": dispatch_value_sha256(fields),
    }
    execution.dispatch_invocation = invocation
    execution.dispatch_invocation_sha256 = dispatch_value_sha256(invocation)
    db_session.commit()

    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / "cross-task-change-set",
        db_session_factory=db_session_factory,
        task=task,
        prompt=published["prompt"],
        context=None,
        persisted_task_execution_id=execution_id,
        expected_attempt_number=2,
        celery_task_id=published["published_task_id"],
    )

    diagnostics = _worker_seam_failure_diagnostics(result)
    assert result.worker_contexts == [], diagnostics
    assert len(result.primary_exceptions) == 1, diagnostics
    assert "request_changes_witness_invocation_change_set_mismatch" in str(
        result.primary_exceptions[0]
    )
    assert result.provider_calls == [], diagnostics
    assert result.executed_commands == [], diagnostics


def test_real_worker_rejects_cross_attempt_change_set_substitution(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
):
    from app.services.tasks.service import TaskService

    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    _add_prior_change_set(db_session, task)
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": "Review note."}
    )
    assert changed.status_code == 200, changed.text
    published, execution_id = _queue_active_request_changes_retry(
        authenticated_client, task, monkeypatch
    )
    execution = db_session.get(TaskExecution, execution_id)
    invocation = json.loads(json.dumps(execution.dispatch_invocation))
    substituted_change_set = TaskExecutionChangeSet(
        project_id=task.project_id,
        task_id=task.id,
        session_id=execution.session_id,
        task_execution_id=execution.id,
        base_snapshot_key=f"same-attempt:{execution.id}",
        added_files=["same-attempt-repair.py"],
        modified_files=[],
        deleted_files=[],
        warning_flags=[],
        status="completed",
        captured_at=datetime.now(timezone.utc) + timedelta(seconds=5),
        created_at=datetime.now(timezone.utc) + timedelta(seconds=5),
    )
    db_session.add(substituted_change_set)
    db_session.flush()
    substituted_record = TaskService(db_session).get_task_execution_change_set(
        task_execution_id=execution.id
    )
    fields = TaskService.request_changes_change_set_fields(substituted_record)
    invocation["acceptance_authority"]["prior_change_set"] = {
        "change_set_id": substituted_change_set.id,
        "task_execution_id": execution.id,
        "bounded_fields_sha256": dispatch_value_sha256(fields),
    }
    execution.dispatch_invocation = invocation
    execution.dispatch_invocation_sha256 = dispatch_value_sha256(invocation)
    db_session.commit()

    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / "cross-attempt-change-set",
        db_session_factory=db_session_factory,
        task=task,
        prompt=published["prompt"],
        context=None,
        persisted_task_execution_id=execution_id,
        expected_attempt_number=2,
        celery_task_id=published["published_task_id"],
    )

    diagnostics = _worker_seam_failure_diagnostics(result)
    assert result.worker_contexts == [], diagnostics
    assert len(result.primary_exceptions) == 1, diagnostics
    assert "request_changes_witness_invocation_change_set_mismatch" in str(
        result.primary_exceptions[0]
    )
    assert result.provider_calls == [], diagnostics
    assert result.executed_commands == [], diagnostics


def test_worker_rejects_missing_task_execution_identity_for_review_task(
    tmp_path: Path, monkeypatch, db_session_factory
):
    task = _worker_task(OPERATOR_DESCRIPTION)
    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path,
        db_session_factory=db_session_factory,
        task=task,
        prompt=OPERATOR_DESCRIPTION,
        context=None,
        omit_task_execution_identity=True,
    )
    assert result.worker_contexts == []
    assert len(result.primary_exceptions) == 1
    assert "task_execution_identity_missing" in str(result.primary_exceptions[0])
    assert result.provider_calls == []
    assert result.executed_commands == []


def test_worker_rejects_mismatched_task_execution_binding(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    # C1-I1 (D-5): request-changes issuance requires a legitimate source.
    _add_prior_change_set(db_session, task)
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": "Repair."}
    )
    assert changed.status_code == 200, changed.text
    published, execution_id = _queue_active_request_changes_retry(
        authenticated_client, task, monkeypatch
    )
    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / "mismatched-execution",
        db_session_factory=db_session_factory,
        task=task,
        prompt=published["prompt"],
        context=None,
        persisted_task_execution_id=execution_id,
        celery_task_id=published["published_task_id"],
        worker_task_execution_id=execution_id + 1000,
    )
    assert result.worker_contexts == []
    assert result.provider_calls == []
    assert result.executed_commands == []


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    (
        ("changed", "request_changes_witness_change_set_fields_changed"),
        ("missing", "request_changes_witness_change_set_provenance_invalid"),
        ("appeared", "requires a prior ChangeSet"),
    ),
)
def test_real_worker_fails_closed_on_change_set_version_change(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
    mutation,
    expected_reason,
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    prior_record = (
        None if mutation == "appeared" else _add_prior_change_set(db_session, task)
    )
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": "Review the diff."}
    )
    assert changed.status_code == 200, changed.text
    if mutation == "appeared":
        # C1-I1: a no-source request-changes retry can no longer be issued, so
        # the fail-closed point moves from the Worker to issuance: nothing is
        # created or published.
        published = _stub_retry_publication(monkeypatch)
        refused = authenticated_client.post(
            f"/api/v1/tasks/{task.id}/retry",
            json={"retry_intent": "request_changes"},
        )
        assert refused.status_code == 409
        assert expected_reason in refused.json()["detail"]
        assert db_session.query(TaskExecution).filter_by(task_id=task.id).count() == 0
        assert published == {}
        return
    published, execution_id = _queue_active_request_changes_retry(
        authenticated_client, task, monkeypatch
    )
    if mutation == "changed":
        prior_record.added_files = ["different-after-publication.py"]
        db_session.commit()
    elif mutation == "missing":
        db_session.delete(prior_record)
        db_session.commit()
    else:
        _add_prior_change_set(db_session, task)

    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / mutation,
        db_session_factory=db_session_factory,
        task=task,
        prompt=published["prompt"],
        context=None,
        persisted_task_execution_id=execution_id,
        expected_attempt_number=1 if mutation == "appeared" else 2,
        celery_task_id=published["published_task_id"],
    )
    assert result.worker_contexts == []
    assert len(result.primary_exceptions) == 1
    assert expected_reason in str(result.primary_exceptions[0])
    assert result.provider_calls == []
    assert result.executed_commands == []
