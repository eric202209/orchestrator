"""PR03 regressions for Structured Runtime Plan verification authorization."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.services.agents.agent_backends import ExecutionTopology
from app.services.orchestration.execution.execution_flow import (
    assess_step_execution,
    execute_verification_command,
)
from app.services.orchestration.phases.planning_task1_bootstrap import (
    normalize_task1_brittle_inline_python_verification,
)
from app.services.orchestration.validation.validator import ValidatorService


UNSUPPORTED_VERIFICATIONS = [
    "python arbitrary_script.py",
    "bash -c 'echo unsafe'",
    'python -c "import dataflow; print(dataflow)"',
]


def _step(verification: str | None) -> dict[str, Any]:
    return {
        "step_number": 1,
        "description": "Create a recovery marker",
        "commands": [],
        "verification": verification,
        "rollback": None,
        "expected_files": ["recovery_marker.py"],
        "ops": [
            {
                "op": "write_file",
                "path": "recovery_marker.py",
                "content": "def marker():\n    return 1\n",
            }
        ],
    }


def _validate(step: dict[str, Any], project_dir: Path, topology: ExecutionTopology):
    plan = [step]
    return ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=(
            "Create recovery_marker.py with a marker function that returns 1."
        ),
        execution_profile="full_lifecycle",
        project_dir=project_dir,
        execution_topology=topology,
    )


@pytest.mark.parametrize("verification", UNSUPPORTED_VERIFICATIONS)
def test_structured_validation_rejects_unsupported_verification_without_commands(
    tmp_path: Path, verification: str
):
    outcome = _validate(
        _step(verification),
        tmp_path,
        ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )

    assert outcome.accepted is False
    assert outcome.status == "repair_required"
    assert outcome.details["steps_with_unsupported_verification"] == {"1": verification}
    assert any(
        "unsupported_verification_command" in reason for reason in outcome.reasons
    )


@pytest.mark.parametrize(
    "verification",
    [
        "python -m pytest -q tests",
        "python -m unittest discover -s tests",
    ],
)
def test_structured_validation_accepts_supported_verification(
    tmp_path: Path, verification: str
):
    outcome = _validate(
        _step(verification),
        tmp_path,
        ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )

    assert outcome.accepted is True
    assert "steps_with_unsupported_verification" not in outcome.details


def test_nullable_verification_remains_optional_for_review_steps(tmp_path: Path):
    plan = [
        {
            "step_number": 1,
            "description": "Inspect the repository layout",
            "commands": ["ls"],
            "verification": None,
            "rollback": None,
            "expected_files": [],
            "ops": [],
        }
    ]
    outcome = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Review the repository layout and summarize it.",
        execution_profile="review_only",
        workflow_stage="review",
        project_dir=tmp_path,
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )

    assert outcome.accepted is True
    assert "steps_with_unsupported_verification" not in outcome.details


@pytest.mark.parametrize("verification", UNSUPPORTED_VERIFICATIONS)
def test_agent_runtime_plan_validation_keeps_existing_verification_contract(
    tmp_path: Path, verification: str
):
    outcome = _validate(_step(verification), tmp_path, ExecutionTopology.AGENT_RUNTIME)

    assert outcome.accepted is True
    assert "steps_with_unsupported_verification" not in outcome.details


def test_task1_normalization_cannot_authorize_unsupported_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    inline = (
        'python -c "from dataflow.aggregate import customer_count as c; '
        "from tests.test_aggregate import FEED; "
        "assert c(FEED)==3, f'got {c(FEED)}'\""
    )
    step = _step(inline)
    initial = _validate(step, tmp_path, ExecutionTopology.STRUCTURED_ORCHESTRATOR)
    normalized = normalize_task1_brittle_inline_python_verification(
        [step],
        project_dir=tmp_path,
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )[0]
    post_normalization = _validate(
        normalized, tmp_path, ExecutionTopology.STRUCTURED_ORCHESTRATOR
    )
    observed = _mock_subprocess(monkeypatch)

    result = execute_verification_command(
        project_dir=tmp_path,
        command=normalized["verification"],
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
        include_evidence=True,
    )

    assert initial.accepted is False
    assert "steps_with_unsupported_verification" in initial.details
    assert normalized["verification"] == "python verify_task1_step1.py"
    assert post_normalization.accepted is False
    assert post_normalization.details["steps_with_unsupported_verification"] == {
        "1": "python verify_task1_step1.py"
    }
    assert result["success"] is False
    assert result["verification_evidence"]["command_execution_status"] == "rejected"
    assert observed == []


def _mock_subprocess(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0
) -> list[str]:
    observed: list[str] = []

    def fake_run(command: str, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.append(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=returncode,
            stdout="mocked stdout",
            stderr="mocked stderr" if returncode else "",
        )

    monkeypatch.setattr(
        "app.services.orchestration.execution.execution_flow.resolve_project_python",
        lambda _project_dir: sys.executable,
    )
    monkeypatch.setattr(
        "app.services.orchestration.execution.execution_flow.subprocess.run",
        fake_run,
    )
    return observed


@pytest.mark.parametrize("verification", UNSUPPORTED_VERIFICATIONS)
def test_direct_structured_executor_refuses_unsupported_persisted_plan_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verification: str
):
    observed = _mock_subprocess(monkeypatch)

    result = execute_verification_command(
        project_dir=tmp_path,
        command=verification,
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
        include_evidence=True,
    )

    assert result["success"] is False
    assert result["verification_evidence"]["command_execution_status"] == "rejected"
    assert result["verification_evidence"]["execution_kind"] == "not_started"
    assert observed == []


def test_executor_without_topology_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    observed = _mock_subprocess(monkeypatch)

    result = execute_verification_command(
        project_dir=tmp_path,
        command="python arbitrary_script.py",
        include_evidence=True,
    )

    assert result["success"] is False
    assert result["verification_evidence"]["command_execution_status"] == "rejected"
    assert observed == []


def test_executor_with_unresolved_topology_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    observed = _mock_subprocess(monkeypatch)

    result = execute_verification_command(
        project_dir=tmp_path,
        command="bash -c 'echo unsafe'",
        execution_topology="unresolved",  # type: ignore[arg-type]
        include_evidence=True,
    )

    assert result["success"] is False
    assert result["verification_evidence"]["command_execution_status"] == "rejected"
    assert observed == []


@pytest.mark.parametrize(
    "verification",
    [
        "python -m pytest -q tests",
        "python -m unittest discover -s tests",
    ],
)
def test_direct_structured_executor_still_runs_supported_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verification: str
):
    observed = _mock_subprocess(monkeypatch)

    result = execute_verification_command(
        project_dir=tmp_path,
        command=verification,
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
        include_evidence=True,
    )

    assert result["success"] is True
    assert observed
    assert result["verification_evidence"]["command_execution_status"] == "completed"


def test_agent_runtime_executor_keeps_existing_script_execution_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    observed = _mock_subprocess(monkeypatch)

    result = execute_verification_command(
        project_dir=tmp_path,
        command="python arbitrary_script.py",
        execution_topology=ExecutionTopology.AGENT_RUNTIME,
        include_evidence=True,
    )

    assert result["success"] is True
    assert observed


def test_workspace_escape_guard_still_precedes_agent_runtime_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    observed = _mock_subprocess(monkeypatch)

    result = execute_verification_command(
        project_dir=tmp_path,
        command=f"cd {tmp_path}/backend && python -m pytest -q tests",
        execution_topology=ExecutionTopology.AGENT_RUNTIME,
        include_evidence=True,
    )

    assert result["success"] is False
    assert result["verification_evidence"]["capture_error"] == (
        "workspace_boundary_rejected"
    )
    assert observed == []


def test_execution_assessment_refuses_bypassed_structured_plan_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    observed = _mock_subprocess(monkeypatch)
    step_result = {"status": "completed", "output": "ops applied", "files_changed": []}
    (tmp_path / "recovery_marker.py").write_text(
        "def marker():\n    return 1\n",
        encoding="utf-8",
    )

    assessment = assess_step_execution(
        db=None,
        session_id=1,
        task_id=1,
        project_dir=tmp_path,
        step=_step("bash -c 'echo unsafe'"),
        step_result=step_result,
        step_started_at=datetime.now(UTC),
        validation_profile="implementation",
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )

    assert assessment.step_status == "failed"
    assert assessment.error_message.startswith("Step verification command failed")
    assert observed == []


def test_normal_verification_failure_remains_distinct_from_policy_rejection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _mock_subprocess(monkeypatch, returncode=1)

    result = execute_verification_command(
        project_dir=tmp_path,
        command="python -m pytest -q tests",
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
        include_evidence=True,
    )

    evidence = result["verification_evidence"]
    assert result["success"] is False
    assert evidence["command_execution_status"] == "completed"
    assert evidence["process_exit_code"] == 1
