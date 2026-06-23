"""Phase 13B-E59 completion-repair signature guard tests."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
    User,
)
from app.services.orchestration.diagnostics.signature_guard import (
    COMPLETION_REPAIR_SIGNATURE_VIOLATION_REASON,
    check_completion_repair_signature_contract,
    completion_repair_signature_violation_event_details,
)
from app.services.orchestration.phases.completion_flow import _attempt_completion_repair
from app.services.orchestration.types import OrchestrationRunContext
from app.services.prompt_templates import OrchestrationState, StepResult


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _op(path: str, content: str) -> list[dict]:
    return [{"op": "write_file", "path": path, "content": content}]


def _guard(tmp_path: Path, before: str, after: str):
    _write(tmp_path / "src/api.py", before)
    return check_completion_repair_signature_contract(
        project_dir=tmp_path, ops=_op("src/api.py", after)
    )


def test_strict_guard_rejects_keyword_only_parameter_removal(tmp_path):
    result = _guard(
        tmp_path,
        "def format_task_line(task: Task, *, include_status: bool = False) -> str:\n    return ''\n",
        "def format_task_line(task):\n    return ''\n",
    )
    assert result.violations[0].violation_type == "signature_changed"


def test_strict_guard_rejects_default_value_change(tmp_path):
    result = _guard(
        tmp_path,
        "def format_task_line(task, *, include_status=False):\n    return ''\n",
        "def format_task_line(task, *, include_status=True):\n    return ''\n",
    )
    assert result.violations[0].violation_type == "signature_changed"


def test_strict_guard_rejects_annotation_change(tmp_path):
    result = _guard(
        tmp_path,
        "def format_task_line(task: Task) -> str:\n    return ''\n",
        "def format_task_line(task: dict) -> str:\n    return ''\n",
    )
    assert result.violations[0].violation_type == "signature_changed"


def test_strict_guard_rejects_added_optional_parameter(tmp_path):
    result = _guard(
        tmp_path,
        "def format_summary(total, completed):\n    return ''\n",
        "def format_summary(total=None, completed=None, store=None):\n    return ''\n",
    )
    assert result.violations[0].violation_type == "signature_changed"


def test_strict_guard_allows_body_only_implementation(tmp_path):
    result = _guard(
        tmp_path,
        "def format_summary(total: int, completed: int) -> str:\n    raise NotImplementedError\n",
        "def format_summary(total: int, completed: int) -> str:\n    return f'{total}/{completed}'\n",
    )
    assert result.violations == []


def test_strict_guard_allows_new_helper(tmp_path):
    result = _guard(
        tmp_path,
        "def format_summary(total, completed):\n    return ''\n",
        "def format_summary(total, completed):\n    return _format(total, completed)\n\ndef _format(total, completed):\n    return ''\n",
    )
    assert result.violations == []


def test_strict_guard_rejects_duplicate_existing_definition(tmp_path):
    result = _guard(
        tmp_path,
        "def format_summary(total, completed):\n    return ''\n",
        "def format_summary(total, completed):\n    return ''\n\ndef format_summary(total, completed):\n    return 'new'\n",
    )
    assert result.violations[0].violation_type == "duplicate_definition"


def test_strict_guard_rejects_missing_existing_definition(tmp_path):
    result = _guard(
        tmp_path,
        "def format_summary(total, completed):\n    return ''\n",
        "def other():\n    return ''\n",
    )
    assert result.violations[0].violation_type == "missing_existing_definition"


def test_strict_guard_rejects_unparsable_post_repair_file(tmp_path):
    result = _guard(
        tmp_path,
        "def format_summary(total, completed):\n    return ''\n",
        "def format_summary(:\n",
    )
    assert result.violations[0].violation_type == "post_parse_error"


def test_strict_guard_noops_for_non_python_files(tmp_path):
    result = check_completion_repair_signature_contract(
        project_dir=tmp_path,
        ops=[{"op": "write_file", "path": "README.md", "content": "ok"}],
    )
    assert result.checked is True
    assert result.candidate_unavailable is False
    assert result.violations == []


def test_command_only_repair_is_unavailable_and_does_not_block(tmp_path):
    result = check_completion_repair_signature_contract(project_dir=tmp_path, ops=None)
    details = completion_repair_signature_violation_event_details(result)
    assert result.violations == []
    assert details["completion_repair_signature_guard_checked"] is False
    assert details["completion_repair_signature_guard_candidate_unavailable"] is True


def test_completion_step_preserves_structured_ops_for_safe_preview():
    from app.services.orchestration.phases.completion_repair import (
        _extract_completion_repair_step,
    )

    step = _extract_completion_repair_step(
        {
            "description": "repair",
            "commands": ["true"],
            "verification": "true",
            "expected_files": ["src/api.py"],
            "ops": _op("src/api.py", "def api(task):\n    return ''\n"),
        },
        2,
    )
    assert step and step["ops"][0]["path"] == "src/api.py"


class _Runtime:
    def __init__(self, output: str):
        self.output = output
        self.prompts: list[str] = []

    async def execute_task(self, prompt, timeout_seconds=None):
        self.prompts.append(str(prompt))
        return {"output": self.output}


def _completion_validation():
    return SimpleNamespace(
        stage="task_completion",
        status="repair_required",
        repairable=True,
        profile="implementation",
        reasons=["pytest failure"],
        details={
            "expected_core_files": ["src/api.py"],
            "failure_class": "completion_verification:pytest_failure",
        },
    )


def test_completion_guard_rejects_before_append_or_execution(db_session, tmp_path):
    project_dir = tmp_path / "project"
    _write(
        project_dir / "src/api.py",
        "def api(task: Task, *, verbose: bool = False):\n    return ''\n",
    )
    eval_user = User(email="eval@local.dev", hashed_password="not-used", is_active=True)
    db_session.add(eval_user)
    db_session.flush()
    project = Project(name="E59", workspace_path=str(project_dir), user_id=eval_user.id)
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="E59",
        status="running",
        is_active=True,
        execution_mode="manual",
    )
    task = Task(
        project_id=project.id,
        title="E59",
        status=TaskStatus.RUNNING,
        task_subfolder="e59",
    )
    db_session.add_all([session, task])
    db_session.flush()
    link = SessionTask(
        session_id=session.id, task_id=task.id, status=TaskStatus.RUNNING
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
        task_description="repair",
        project_name="E59",
        task_id=task.id,
        plan=[
            {
                "step_number": 1,
                "description": "initial",
                "expected_files": ["src/api.py"],
            }
        ],
    )
    state._project_dir_override = str(project_dir)
    state.execution_results = [
        StepResult(
            step_number=1,
            status="failed",
            output="pytest failed",
            files_changed=["src/api.py"],
        )
    ]
    output = json.dumps(
        {
            "description": "bad signature",
            "commands": ["true"],
            "verification": "true",
            "expected_files": ["src/api.py"],
            "ops": _op("src/api.py", "def api(task):\n    return ''\n"),
        }
    )
    runtime = _Runtime(output)
    emitted = []
    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt="repair",
        timeout_seconds=120,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=state,
        runtime_service=runtime,
        task_service=SimpleNamespace(),
        logger=logging.getLogger("e59"),
        emit_live=lambda *args, **kwargs: emitted.append(kwargs.get("metadata", {})),
        error_handler=SimpleNamespace(),
        task_execution_id=execution.id,
        restore_workspace_snapshot_if_needed=lambda reason: None,
    )

    with patch(
        "app.services.orchestration.phases.completion_flow._completion_repair_invalid_paths",
        return_value=[],
    ), patch(
        "app.config.settings.COMPLETION_REPAIR_BACKEND",
        None,
    ):
        result = _attempt_completion_repair(
            ctx=ctx,
            completion_validation=_completion_validation(),
            save_orchestration_checkpoint_fn=lambda *args: None,
        )

    assert any(
        "completion_repair_signature_guard_checked" in event for event in emitted
    ), emitted
    signature_events = [
        event
        for event in emitted
        if "completion_repair_signature_guard_checked" in event
    ]
    assert result == {
        "status": "failed",
        "reason": COMPLETION_REPAIR_SIGNATURE_VIOLATION_REASON,
    }
    assert len(state.plan) == 1
    assert len(runtime.prompts) == 1
    assert any(
        event.get("completion_repair_signature_violation_count") == 1
        for event in emitted
    )
