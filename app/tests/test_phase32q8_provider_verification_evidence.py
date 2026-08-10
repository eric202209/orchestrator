"""Phase 32Q-8 provider-verification ordering and command-safety regressions."""

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
)
from app.services.orchestration.phases.completion_flow import (
    _attempt_completion_repair,
)
from app.services.orchestration.phases.execution_local_steps import (
    _is_simple_verification_command,
)
from app.services.orchestration.coordinators.completion_coordinator import (
    _annotate_completion_repair_progress,
    _retain_completion_repair_verification_evidence,
)
from app.services.orchestration.phases.completion_repair_capsule import (
    CompletionRepairProgress,
    classify_completion_repair_progress,
)
from app.services.orchestration.prompt_templates import OrchestrationState, StepResult
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.validator import ValidatorService


_REPO_ROOT = Path(__file__).resolve().parents[2]
_Q6V_REPLAY_FIXTURE = (
    _REPO_ROOT / "app/tests/fixtures/phase32q8_provider_verification_replay.json"
)
_TASK_PROMPT = (
    "Create a utc_now helper in app/time_utils.py, use it in "
    "app/services/workspace/context_service.py, and add "
    "app/tests/test_utc_now_helper.py"
)
_CANDIDATE_PATHS = [
    "app/services/workspace/context_service.py",
    "app/tests/test_utc_now_helper.py",
    "app/time_utils.py",
]


class _RetainedResponseRuntime:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls = 0

    async def execute_task(self, _prompt, timeout_seconds=None):
        self.calls += 1
        return {"output": self.output}

    def get_backend_metadata(self):
        return {"backend": "fake", "model_family": "provider-free-replay"}


def _write_q6v_candidate(project_dir: Path) -> None:
    test_path = project_dir / "app/tests/test_utc_now_helper.py"
    source_path = project_dir / "app/time_utils.py"
    context_path = project_dir / "app/services/workspace/context_service.py"
    test_path.parent.mkdir(parents=True)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.parent.mkdir(parents=True)
    (project_dir / "pytest.ini").write_text(
        "[pytest]\ntestpaths = app/tests\npythonpath = .\n",
        encoding="utf-8",
    )
    (project_dir / ".flake8").write_bytes((_REPO_ROOT / ".flake8").read_bytes())
    test_path.write_text(
        "import pytest\n"
        "from datetime import datetime\n"
        "from app.time_utils import utc_now\n"
        "\n"
        "def test_utc_now_is_timezone_aware():\n"
        '    """Test that utc_now returns a timezone-aware datetime"""\n'
        "    result = utc_now()\n"
        "    assert result.tzinfo is not None\n"
        "    assert result.tzinfo.utcoffset(result) == datetime.timedelta(0)\n"
        "\n"
        "def test_utc_now_has_utc_offset():\n"
        '    """Test that utc_now returns UTC offset"""\n'
        "    result = utc_now()\n"
        "    assert result.utcoffset() == datetime.timedelta(0)\n"
        "\n"
        "def test_utc_now_is_reasonably_close_to_current_time():\n"
        '    """Test that utc_now result is reasonably close to current UTC time"""\n'
        "    result = utc_now()\n"
        "    now = datetime.now(datetime.timezone.utc)\n"
        "    # Allow for a reasonable time difference (e.g., 5 seconds)\n"
        "    time_diff = abs((now - result).total_seconds())\n"
        "    assert time_diff < 5\n",
        encoding="utf-8",
    )
    source_path.write_text(
        "from datetime import datetime, timezone\n"
        "\n"
        "def utc_now() -> datetime:\n"
        "    return datetime.now(timezone.utc)\n",
        encoding="utf-8",
    )
    context_source = (
        _REPO_ROOT / "app/services/workspace/context_service.py"
    ).read_text(encoding="utf-8")
    context_source = context_source.replace(
        "import json\n"
        "import logging\n"
        "from datetime import datetime\n"
        "from typing import Optional, Dict, Any, List\n\n"
        "from sqlalchemy.orm import Session as DBSession\n"
        "from sqlalchemy import func\n\n"
        "from app.models import (\n"
        "    SessionState,\n"
        "    ConversationHistory,\n"
        "    TaskCheckpoint,\n"
        ")",
        "import json\n"
        "import logging\n"
        "from datetime import datetime\n"
        "from typing import Optional, Dict, Any, List\n\n"
        "from sqlalchemy.orm import Session as DBSession\n"
        "from sqlalchemy import func\n\n"
        "from app.models import (\n"
        "    SessionState,\n"
        "    ConversationHistory,\n"
        "    TaskCheckpoint,\n"
        ")\n"
        "from app.time_utils import utc_now",
        1,
    )
    context_source = context_source.replace(
        '"exported_at": datetime.utcnow().isoformat(),',
        '"exported_at": utc_now().isoformat(),',
        1,
    )
    context_source = context_source.replace("from datetime import datetime", "", 1)
    context_path.write_text(context_source, encoding="utf-8")


def _completion_evidence(project_dir: Path) -> dict:
    return {
        "candidate_delta_required": True,
        "run_candidate_checks": True,
        "include_static_checks": True,
        "summary_generated": True,
        "execution_results_count": 1,
        "reported_changed_files": list(_CANDIDATE_PATHS),
        "change_set": {
            "snapshot_path": str(project_dir.parent / "snapshot"),
            "target_path": str(project_dir),
            "added_files": [
                "app/time_utils.py",
                "app/tests/test_utc_now_helper.py",
            ],
            "modified_files": ["app/services/workspace/context_service.py"],
            "deleted_files": [],
        },
    }


def _validate_candidate(project_dir: Path, plan: list[dict]):
    return ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=plan,
        task_prompt=_TASK_PROMPT,
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title="Phase 32O-1R1 Attempt 10 utc_now",
        description=_TASK_PROMPT,
        completion_evidence=_completion_evidence(project_dir),
        validation_severity="standard",
        workflow_stage="implementation",
        is_first_ordered_task=True,
    )


def _seed_context(db_session, tmp_path: Path, response: str):
    project_dir = tmp_path / "candidate"
    snapshot_context = tmp_path / "snapshot/app/services/workspace/context_service.py"
    snapshot_context.parent.mkdir(parents=True)
    snapshot_context.write_bytes(
        (_REPO_ROOT / "app/services/workspace/context_service.py").read_bytes()
    )
    _write_q6v_candidate(project_dir)
    fixture = json.loads(_Q6V_REPLAY_FIXTURE.read_text(encoding="utf-8"))
    plan = fixture["plan"]
    state = OrchestrationState(
        session_id="1",
        task_description=_TASK_PROMPT,
        project_name="phase32q8",
        task_id=1,
        plan=plan,
    )
    state._project_dir_override = str(project_dir)
    state.execution_results = [
        StepResult(step_number=1, status="success", files_changed=_CANDIDATE_PATHS)
    ]

    identity = str(tmp_path.name)
    project = Project(name=f"Q8 Project {identity}", workspace_path=str(project_dir))
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name=f"Q8 Session {identity}",
        status="running",
        is_active=True,
        execution_mode="manual",
    )
    task = Task(
        project_id=project.id,
        title="Q8 Task",
        description=_TASK_PROMPT,
        status=TaskStatus.RUNNING,
        task_subfolder="task-q8",
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

    runtime = _RetainedResponseRuntime(response)
    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt=_TASK_PROMPT,
        timeout_seconds=120,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=state,
        runtime_service=runtime,
        task_service=SimpleNamespace(),
        logger=logging.getLogger("phase32q8-test"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=SimpleNamespace(),
        task_execution_id=execution.id,
        restore_workspace_snapshot_if_needed=lambda reason: None,
        completion_repair_budget=2,
    )
    return ctx, runtime


def _retained_q6v_content() -> str:
    fixture = json.loads(_Q6V_REPLAY_FIXTURE.read_text(encoding="utf-8"))
    return json.dumps(fixture["response"])


def _corrected_test_content() -> str:
    return (
        "import pytest\n"
        "from datetime import datetime, timedelta, timezone\n"
        "from app.time_utils import utc_now\n"
        "\n\n"
        "def test_utc_now_is_timezone_aware():\n"
        "    result = utc_now()\n"
        "    assert result.tzinfo is not None\n"
        "    assert result.tzinfo.utcoffset(result) == timedelta(0)\n"
        "\n\n"
        "def test_utc_now_has_utc_offset():\n"
        "    result = utc_now()\n"
        "    assert result.utcoffset() == timedelta(0)\n"
        "\n\n"
        "def test_utc_now_is_reasonably_close_to_current_time():\n"
        "    result = utc_now()\n"
        "    now = datetime.now(timezone.utc)\n"
        "    assert abs((now - result).total_seconds()) < 5\n"
    )


def _response_variant(*, verification: str, corrected: bool, regression: bool = False):
    response = json.loads(_retained_q6v_content())
    response["repair_step"]["verification"] = verification
    if corrected:
        response["repair_step"]["ops"][0]["content"] = _corrected_test_content()
    if regression:
        response["repair_step"]["ops"][0]["content"] = (
            "import pytest\n"
            "from app.time_utils import utc_now\n\n\n"
            "def test_utc_now_is_timezone_aware():\n"
            "    with pytest.raises(NotImplementedError):\n"
            "        utc_now()\n\n\n"
            "def test_utc_now_has_utc_offset():\n"
            "    with pytest.raises(NotImplementedError):\n"
            "        utc_now()\n\n\n"
            "def test_utc_now_is_reasonably_close_to_current_time():\n"
            "    with pytest.raises(NotImplementedError):\n"
            "        utc_now()\n"
        )
        response["repair_step"]["ops"][1]["content"] = (
            "from datetime import datetime\n\n\n"
            "def utc_now() -> datetime:\n"
            "    raise NotImplementedError\n"
        )
    return json.dumps(response)


def test_valid_failed_provider_verification_reaches_real_candidate_validator(
    db_session, tmp_path
):
    ctx, runtime = _seed_context(db_session, tmp_path, _retained_q6v_content())
    before = _validate_candidate(
        Path(ctx.orchestration_state.project_dir), ctx.orchestration_state.plan
    )
    assert [finding.rule_id for finding in before.repairable_findings] == [
        "focused_pytest_failed",
        "candidate_black_failed",
        "candidate_flake8_failed",
    ]
    assert (
        before.candidate_identity
        == "sha256:179808237677f608f1941f0d6b84fe90e805855c6976a92b472b7beebdef3169"
    )

    with patch("app.config.settings.COMPLETION_REPAIR_BACKEND", None):
        repair = _attempt_completion_repair(
            ctx=ctx,
            completion_validation=before,
            save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
        )

    assert runtime.calls == 1
    assert repair["status"] == "success"
    assert repair["provider_verification"]["verification_passed"] is False
    assert repair["provider_verification"]["verification_exit_code"] == 1
    after = _validate_candidate(
        Path(ctx.orchestration_state.project_dir), ctx.orchestration_state.plan
    )
    assert [finding.rule_id for finding in after.repairable_findings] == [
        "focused_pytest_failed"
    ]
    assert before.candidate_identity != after.candidate_identity
    assert (
        after.candidate_identity
        == "sha256:07608b6d7b903cb830470a04653152d1e09761e00b8a0f7d33e0b3633d56da63"
    )
    assert (
        classify_completion_repair_progress(before, after)
        == CompletionRepairProgress.PARTIAL_PROGRESS
    )


def test_unsafe_verification_command_rejects_before_candidate_application(
    db_session, tmp_path
):
    response = json.loads(_retained_q6v_content())
    response["repair_step"]["verification"] = (
        'python -c "import sys; sys.exit(0)"; ' 'python -c "import sys; sys.exit(0)"'
    )
    ctx, runtime = _seed_context(db_session, tmp_path, json.dumps(response))
    project_dir = Path(ctx.orchestration_state.project_dir)
    before = _validate_candidate(project_dir, ctx.orchestration_state.plan)
    bytes_before = {
        path: (project_dir / path).read_bytes() for path in _CANDIDATE_PATHS
    }

    with patch("app.config.settings.COMPLETION_REPAIR_BACKEND", None):
        repair = _attempt_completion_repair(
            ctx=ctx,
            completion_validation=before,
            save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
        )

    assert runtime.calls == 1
    assert repair == {
        "status": "failed",
        "reason": "completion_repair_verification_command_unsafe",
    }
    assert {
        path: (project_dir / path).read_bytes() for path in _CANDIDATE_PATHS
    } == bytes_before
    assert ctx.orchestration_state.execution_results == [
        StepResult(step_number=1, status="success", files_changed=_CANDIDATE_PATHS)
    ]


def test_provider_verification_conflicts_defer_to_real_candidate_validator(
    db_session, tmp_path
):
    cases = [
        (
            "A",
            "python -m pytest app/tests/test_utc_now_helper.py",
            True,
            False,
            True,
            CompletionRepairProgress.RESOLVED,
        ),
        (
            "B",
            "python -m py_compile app/time_utils.py",
            False,
            False,
            True,
            CompletionRepairProgress.PARTIAL_PROGRESS,
        ),
        (
            "D",
            "python -m py_compile app/time_utils.py",
            True,
            True,
            True,
            CompletionRepairProgress.NO_PROGRESS_OR_REGRESSION,
        ),
        (
            "E",
            'python -c "import sys; sys.exit(1)"',
            True,
            False,
            False,
            CompletionRepairProgress.RESOLVED,
        ),
    ]

    for case, command, corrected, regression, provider_passed, expected in cases:
        case_dir = tmp_path / case
        case_dir.mkdir()
        ctx, _runtime = _seed_context(
            db_session,
            case_dir,
            _response_variant(
                verification=command,
                corrected=corrected,
                regression=regression,
            ),
        )
        project_dir = Path(ctx.orchestration_state.project_dir)
        before = _validate_candidate(project_dir, ctx.orchestration_state.plan)
        with patch("app.config.settings.COMPLETION_REPAIR_BACKEND", None):
            repair = _attempt_completion_repair(
                ctx=ctx,
                completion_validation=before,
                save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
            )
        assert repair["status"] == "success", case
        assert (
            repair["provider_verification"]["verification_passed"] is provider_passed
        ), case
        after = _validate_candidate(project_dir, ctx.orchestration_state.plan)
        assert classify_completion_repair_progress(before, after) == expected, (
            case,
            after.status,
            after.reasons,
            [finding.rule_id for finding in after.findings],
        )
        if case == "D":
            assert any(
                finding.rule_id not in {item.rule_id for item in before.findings}
                for finding in after.findings
            )
        if case in {"A", "E"}:
            assert after.accepted is True


def test_q6v_partial_progress_makes_provider_free_second_iteration_eligible_and_resolves(
    db_session, tmp_path
):
    ctx, runtime = _seed_context(db_session, tmp_path, _retained_q6v_content())
    project_dir = Path(ctx.orchestration_state.project_dir)
    initial = _validate_candidate(project_dir, ctx.orchestration_state.plan)

    with patch("app.config.settings.COMPLETION_REPAIR_BACKEND", None):
        first_repair = _attempt_completion_repair(
            ctx=ctx,
            completion_validation=initial,
            save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
        )
    partial = _validate_candidate(project_dir, ctx.orchestration_state.plan)
    _retain_completion_repair_verification_evidence(partial, first_repair)
    first_progress = _annotate_completion_repair_progress(
        before_validation=initial,
        after_validation=partial,
        orchestration_state=ctx.orchestration_state,
        repair_budget=ctx.completion_repair_budget,
        accepted_plan_identity=json.dumps(
            ctx.orchestration_state.plan, sort_keys=True, separators=(",", ":")
        ),
        accepted_candidate_scope=tuple(
            sorted(partial.details["candidate_authorized_paths"])
        ),
    )

    assert first_repair["provider_verification"]["verification_passed"] is False
    assert first_progress == CompletionRepairProgress.PARTIAL_PROGRESS
    assert partial.details["completion_repair_budget_used"] == 1
    assert partial.details["completion_repair_budget_remaining"] == 1
    assert partial.details["verification_command_valid"] is True
    assert partial.details["verification_exit_code"] == 1
    assert partial.details["verification_passed"] is False
    assert partial.details["canonical_progress"] == "PARTIAL_PROGRESS"
    assert partial.details["completion_repair_after_finding_signature"]

    runtime.output = _response_variant(
        verification="python -m pytest app/tests/test_utc_now_helper.py",
        corrected=True,
    )
    with patch("app.config.settings.COMPLETION_REPAIR_BACKEND", None):
        second_repair = _attempt_completion_repair(
            ctx=ctx,
            completion_validation=partial,
            save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
        )
    resolved = _validate_candidate(project_dir, ctx.orchestration_state.plan)

    assert second_repair["status"] == "success"
    assert second_repair["provider_verification"]["verification_passed"] is True
    assert ctx.orchestration_state.completion_repair_attempts == 2
    assert resolved.accepted is True, (
        resolved.status,
        resolved.reasons,
        [finding.rule_id for finding in resolved.findings],
    )
    assert (
        classify_completion_repair_progress(partial, resolved)
        == CompletionRepairProgress.RESOLVED
    )


def test_existing_verification_command_safety_authority_accepts_focused_pytest_only(
    tmp_path,
):
    valid = [
        "pytest -q app/tests/test_example.py",
        "python -m pytest app/tests/test_example.py",
        f"{Path(__import__('sys').executable)} -m pytest app/tests/test_example.py",
        ".venv/bin/python3 -m pytest app/tests/test_example.py",
    ]
    invalid = [
        "",
        "echo unsupported",
        "pytestevil -q",
        "python -m pytestevil app/tests/test_example.py",
        "npm run buildanything",
        "pytest -q; echo chained",
        "pytest -q | tee output.txt",
        "pytest -q > output.txt",
        "pytest -q $(echo app/tests)",
        "pytest ../../outside.py",
        "/bin/sh -c true",
    ]

    assert all(
        _is_simple_verification_command(command, project_dir=tmp_path)
        for command in valid
    )
    assert not any(
        _is_simple_verification_command(command, project_dir=tmp_path)
        for command in invalid
    )
