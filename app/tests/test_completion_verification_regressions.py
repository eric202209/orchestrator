from __future__ import annotations

import logging
from types import SimpleNamespace

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.persistence import read_orchestration_events
from app.services.orchestration.phases.completion_flow import (
    _augment_completion_verification_command,
    _classify_completion_verification_failure,
    _execute_completion_verification,
    finalize_successful_task,
)
from app.services.orchestration.types import OrchestrationRunContext, ValidationVerdict
from app.services.orchestration.validation.validator import ValidatorService
from app.services.prompt_templates import OrchestrationState, StepResult


class _FakeRuntime:
    async def execute_task(self, prompt, timeout_seconds=None):
        return {"output": "Task summary"}

    def get_backend_metadata(self):
        return {"backend": "fake", "model_family": "test"}


class _FakeTaskService:
    def analyze_workspace_consistency(self, project_dir):
        return {}


def test_missing_jest_binary_is_treated_as_repairable_completion_verification():
    completion_validation = SimpleNamespace(
        profile="implementation",
        details={"expected_core_files": ["src/index.ts", "src/utils/format.test.ts"]},
    )

    verdict = _classify_completion_verification_failure(
        command="pnpm test",
        source="package.json test script via pnpm",
        verification_output=(
            "> demo@1.0.0 test /workspace/demo\n" "> jest\n" "sh: 1: jest: not found\n"
        ),
        completion_validation=completion_validation,
    )

    assert verdict is not None
    assert verdict.repairable is True
    assert verdict.stage == "completion_verification"
    assert "dependencies are missing or not installed" in verdict.reasons[0]
    assert verdict.details["verification_command"] == "pnpm test"
    assert "src/utils/format.test.ts" in verdict.details["expected_core_files"]


def test_python_no_module_named_is_repairable_completion_verification():
    completion_validation = SimpleNamespace(
        profile="implementation",
        details={"expected_core_files": ["calc_smoke.py", "tests/test_calc.py"]},
    )

    verdict = _classify_completion_verification_failure(
        command="pytest",
        source="python test suite detected",
        verification_output=(
            "ModuleNotFoundError: No module named 'calc_smoke'\n"
            "ERROR tests/test_calc.py"
        ),
        completion_validation=completion_validation,
    )

    assert verdict is not None
    assert verdict.repairable is True
    assert verdict.stage == "completion_verification"
    assert "repairable test/module issue" in verdict.reasons[0]
    assert verdict.details["verification_command"] == "pytest"


def test_python_modulenotfounderror_prefix_is_repairable_completion_verification():
    completion_validation = SimpleNamespace(
        profile="implementation",
        details={"expected_core_files": ["calc_smoke.py"]},
    )

    verdict = _classify_completion_verification_failure(
        command="pytest",
        source="python test suite detected",
        verification_output="ModuleNotFoundError while importing test module",
        completion_validation=completion_validation,
    )

    assert verdict is not None
    assert verdict.repairable is True


def test_real_test_failure_is_not_reclassified_as_missing_dependency():
    completion_validation = SimpleNamespace(
        profile="implementation",
        details={"expected_core_files": ["src/index.ts"]},
    )

    verdict = _classify_completion_verification_failure(
        command="pnpm test",
        source="package.json test script via pnpm",
        verification_output=(
            "FAIL src/index.test.ts\n" "Expected: 2\n" "Received: 1\n"
        ),
        completion_validation=completion_validation,
    )

    assert verdict is None


def test_vitest_completion_verification_excludes_openclaw_snapshots():
    command = _augment_completion_verification_command(
        "pnpm test",
        "vitest run",
    )

    assert command == "pnpm test -- --exclude=.openclaw/**"


def test_jest_completion_verification_excludes_openclaw_snapshots():
    command = _augment_completion_verification_command(
        "pnpm test",
        "node --runInBand jest",
    )

    assert command == "pnpm test -- --testPathIgnorePatterns=.openclaw/"


def test_completion_verification_rejects_shell_metacharacters(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    result = _execute_completion_verification(
        project_dir=project_dir,
        command="pytest; echo pwned",
        timeout_seconds=1,
    )

    assert result["success"] is False
    assert "unsafe shell metacharacters" in result["output"]


def test_module_resolution_failure_is_treated_as_repairable_verification_issue():
    completion_validation = SimpleNamespace(
        profile="implementation",
        details={
            "expected_core_files": ["src/utils/format.ts", "src/utils/format.spec.ts"]
        },
    )

    verdict = _classify_completion_verification_failure(
        command="pnpm test -- --exclude=.openclaw/**",
        source="package.json test script via pnpm",
        verification_output=(
            "FAIL src/utils/format.spec.ts\n"
            "Error: Failed to load url ./format.js in src/utils/format.spec.ts. "
            "Does the file exist?\n"
        ),
        completion_validation=completion_validation,
    )

    assert verdict is not None
    assert verdict.repairable is True
    assert "repairable test/module issue" in verdict.reasons[0]


def test_verification_completion_does_not_require_execution_results(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "test").mkdir()
    (project_dir / "test" / "replay.spec.ts").write_text(
        "export const ok = true;\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Inspect replay coverage",
                "commands": ["ls test"],
                "verification": "test -f test/replay.spec.ts",
                "expected_files": ["test/replay.spec.ts"],
            }
        ],
        task_prompt="Review the project and verify replay stability.",
        execution_profile="review_only",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 0,
        },
    )

    assert verdict.accepted is True
    assert (
        "Completion contract requires at least one recorded execution result"
        not in verdict.reasons
    )


def test_completion_validation_rejects_reported_files_that_never_materialized(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "src").mkdir(parents=True)
    (project_dir / "src" / "index.ts").write_text(
        "export const ready = true;\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Create source implementation",
                "commands": ["echo ready"],
                "verification": "test -f src/index.ts",
                "expected_files": ["src/index.ts"],
            }
        ],
        task_prompt="Implement the source file.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["README.md"],
        },
    )

    assert verdict.accepted is False
    assert verdict.repairable is True
    assert "none materialized in the canonical workspace" in verdict.reasons[0]
    assert verdict.details["reported_changed_files"] == ["README.md"]


def test_completion_validation_placeholder_pass_remains_rejected(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "services").mkdir(parents=True)
    (project_dir / "services" / "health.py").write_text(
        "class ServiceStatus:\n    pass\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Create health service",
                "commands": [
                    "printf 'class ServiceStatus:\\n    pass\\n' > services/health.py"
                ],
                "verification": "python3 -m py_compile services/health.py",
                "expected_files": ["services/health.py"],
            }
        ],
        task_prompt="Build a distributed workflow health checker.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["services/health.py"],
        },
    )

    assert verdict.status == "rejected"
    assert verdict.rejected is True
    assert "placeholder_only_implementation" not in verdict.details
    assert verdict.details["placeholder_reasons"] == [
        "health.py still contains `pass` placeholders"
    ]


def test_detect_placeholder_content_flags_broken_python_main_guard(tmp_path):
    entrypoint = tmp_path / "app.py"
    entrypoint.write_text(
        'if __name__ == __main__:\n    print("broken")\n',
        encoding="utf-8",
    )

    reasons = ValidatorService._detect_placeholder_content(entrypoint)

    assert any(
        "broken Python __main__ entrypoint check" in reason for reason in reasons
    )


def _seed_finalize_ctx(db_session, tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = Project(name="Phase 7J", workspace_path=str(project_dir))
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="Phase 7J Session",
        status="running",
        is_active=True,
        execution_mode="manual",
    )
    task = Task(
        project_id=project.id,
        title="Phase 7J Task",
        status=TaskStatus.RUNNING,
        task_subfolder=None,
    )
    db_session.add_all([session, task])
    db_session.flush()
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add_all([link, execution])
    db_session.commit()

    state = OrchestrationState(
        session_id=str(session.id),
        task_description="Fix import verification",
        project_name="Phase 7J",
        task_id=task.id,
        plan=[
            {
                "step_number": 1,
                "description": "Create source",
                "commands": ["true"],
                "verification": "python -m py_compile calc_smoke.py",
                "rollback": None,
                "expected_files": ["calc_smoke.py"],
            }
        ],
    )
    state._project_dir_override = str(project_dir)
    state.execution_results = [
        StepResult(
            step_number=1,
            status="success",
            output="created",
            files_changed=["calc_smoke.py"],
        )
    ]
    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt="Fix import verification",
        timeout_seconds=120,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=True,
        orchestration_state=state,
        runtime_service=_FakeRuntime(),
        task_service=_FakeTaskService(),
        logger=logging.getLogger("phase7j-test"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=SimpleNamespace(),
        task_execution_id=execution.id,
        restore_workspace_snapshot_if_needed=lambda reason: None,
    )
    return ctx, execution


def test_final_verification_7f_gate_repairs_when_classifier_misses(
    db_session, tmp_path, monkeypatch
):
    ctx, execution = _seed_finalize_ctx(db_session, tmp_path)
    repair_calls = []
    verification_outputs = [
        {
            "success": False,
            "returncode": 1,
            "output": "ImportError: cannot import name 'add' from 'calc_smoke'",
        },
        {"success": True, "returncode": 0, "output": "1 passed"},
    ]

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={"expected_core_files": ["calc_smoke.py"]},
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._detect_completion_verification_command",
        lambda project_dir: ("pytest", "python test suite detected"),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._classify_completion_verification_failure",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._execute_completion_verification",
        lambda **kwargs: verification_outputs.pop(0),
    )

    def _fake_repair(ctx, completion_validation, save_orchestration_checkpoint_fn):
        repair_calls.append(completion_validation)
        return {"status": "success", "step": {"description": "repair"}}

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._attempt_completion_repair",
        _fake_repair,
    )

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed"
    assert repair_calls
    assert repair_calls[0].details["completion_repair_source"] == (
        "final_completion_verification"
    )
    assert repair_calls[0].details["failure_class"] == "import_error"
    assert ctx.orchestration_state.debug_repair_task_execution_ids == [execution.id]
    assert ctx.task.status == TaskStatus.DONE


def test_final_verification_7f_gate_blocks_second_attempt(
    db_session, tmp_path, monkeypatch
):
    ctx, execution = _seed_finalize_ctx(db_session, tmp_path)
    ctx.orchestration_state.debug_repair_task_execution_ids = [execution.id]
    repair_calls = []

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={"expected_core_files": ["calc_smoke.py"]},
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._detect_completion_verification_command",
        lambda project_dir: ("pytest", "python test suite detected"),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._classify_completion_verification_failure",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._execute_completion_verification",
        lambda **kwargs: {
            "success": False,
            "returncode": 1,
            "output": "ImportError: cannot import name 'add' from 'calc_smoke'",
        },
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._attempt_completion_repair",
        lambda *args, **kwargs: repair_calls.append(args) or {"status": "success"},
    )

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result == {"status": "failed", "reason": "completion_verification_failed"}
    assert repair_calls == []
    assert ctx.task.status == TaskStatus.FAILED
    events = read_orchestration_events(
        ctx.orchestration_state.project_dir, ctx.session_id, ctx.task_id
    )
    assert events[-1]["event_type"] == EventType.PHASE_FINISHED
    assert events[-1]["details"]["status"] == "verification_failed"
