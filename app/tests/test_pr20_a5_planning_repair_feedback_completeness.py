"""PR20-A5 — Planning Repair feedback names every unsupported-command step.

PR20-A4 (Task 51) failed Planning: the initial Plan had an unsupported
``python -c`` probe in step 1 and two pytest verification commands in step 3.
The verdict reasons describe only the first offending step, and the repair
feedback added nothing for step 3, so the single Planning Repair fixed step 1
and kept step 3.  The verdict is unchanged; the repair feedback now carries
each step's reason.  No provider is called.
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

import pytest

from app.config import settings
from app.services.agents.agent_backends import ExecutionTopology
from app.services.orchestration.phases.planning_support import (
    _build_repair_rejection_reasons,
)
from app.services.orchestration.planning.repair_prompts import (
    PLANNING_REPAIR_MAX_VALIDATION_ERROR_CHARS,
    build_planning_repair_prompt,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.validation.workspace_guard import normalize_plan
from app.tests.test_pr20_a1_task_acceptance_verification import (
    TASK31_PROMPT,
    _task31_project,
)

# Retained A4 provider captures; digests are the captures' retained_sha256,
# which hashes json.dumps(value).
A4_PLANS = Path(__file__).resolve().parent / "fixtures/pr20_a5_task31_a4_plans.json"
A4_PLAN_SHA256 = {
    "initial": "212479a89a8df3008e9e4b0ae8d7c66c0a722b337a01db9f03cadfc4146b8cae",
    "repaired": "d44eab9eb15542f6de8bac2c5ae780c5bf580474cb78a064da7560315bf9fa21",
}
# Persisted A4 verdict reasons (task_checkpoints 110 and 111).
A4_INITIAL_REASONS = [
    "unsupported_step_command: rejected 'python -c \"from dataflow.normalize "
    "import normalize_r...; structured runtime has no command tools. Plan ops "
    "for edits; keep goal, scope, checks.",
    "Plan contains brittle heredoc-heavy or malformed commands",
]
STEP3_REASON = (
    "unsupported_step_command: step 3 has 2 verification commands; use exactly "
    "one per step (one pytest call may list several node IDs)."
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("PR20-A5 tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def project(tmp_path):
    return _task31_project(tmp_path)


@pytest.fixture
def a4_repair_context(monkeypatch):
    # The A4 runtime declared 32768, so the full repair prompt (not the
    # 8000-character compact one) carried the validation-error block.
    monkeypatch.setattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", 32768)


def _plan_text(label: str) -> str:
    text = json.loads(A4_PLANS.read_text(encoding="utf-8"))[label]
    digest = hashlib.sha256(json.dumps(text).encode("utf-8")).hexdigest()
    assert digest == A4_PLAN_SHA256[label]
    return text


def _validate(output_text: str, project_dir: Path):
    import logging

    plan = normalize_plan(
        json.loads(output_text), project_dir, logging.getLogger(__name__)
    )
    return ValidatorService.validate_plan(
        plan,
        output_text=output_text,
        task_prompt=TASK31_PROMPT,
        execution_profile="full_lifecycle",
        project_dir=project_dir,
        title="Fix duplicate-customer regression in dataflow 2.0",
        description=TASK31_PROMPT,
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )


def _validation_error_block(output_text, project_dir, reasons) -> str:
    prompt = build_planning_repair_prompt(
        task_description=TASK31_PROMPT,
        malformed_output=output_text,
        project_dir=project_dir,
        rejection_reasons=reasons,
    )
    lines = prompt[prompt.index("Validation error:\n") :].split("\n")
    block = [lines[0]]
    for line in lines[1:]:
        if not line.startswith("- "):
            break
        block.append(line)
    return "\n".join(block)


def test_initial_validation_identifies_both_offending_steps(project):
    outcome = _validate(_plan_text("initial"), project)

    assert outcome.status == "repair_required"
    assert outcome.reasons == A4_INITIAL_REASONS
    assert sorted(outcome.details["steps_with_unsupported_commands"]) == ["1", "3"]
    step_reasons = outcome.details["unsupported_step_command_reasons"]
    assert list(step_reasons) == ["1", "3"]
    assert step_reasons["1"] == A4_INITIAL_REASONS[0]
    assert step_reasons["3"] == STEP3_REASON


def test_repair_feedback_includes_both_violations(project, a4_repair_context):
    text = _plan_text("initial")
    outcome = _validate(text, project)
    feedback = _build_repair_rejection_reasons(outcome.reasons, outcome.details)

    assert STEP3_REASON in feedback
    assert A4_INITIAL_REASONS[0] in feedback
    # No step-level reason the validator established is dropped.
    for reason in outcome.details["unsupported_step_command_reasons"].values():
        assert reason in feedback
    assert len(feedback) == len(set(feedback))

    block = _validation_error_block(text, project, feedback)
    assert "Step [1]: brittle inline Python" in block
    assert f"- {STEP3_REASON}\n" in block + "\n"
    assert "one pytest call may list several node IDs" in block


def test_feedback_is_deterministic(project):
    text = _plan_text("initial")
    first = _validate(text, project)
    second = _validate(text, project)
    assert first.details["unsupported_step_command_reasons"] == (
        second.details["unsupported_step_command_reasons"]
    )
    assert _build_repair_rejection_reasons(
        first.reasons, first.details
    ) == _build_repair_rejection_reasons(second.reasons, second.details)


def test_feedback_stays_within_existing_bounds(project, a4_repair_context):
    text = _plan_text("initial")
    outcome = _validate(text, project)
    feedback = _build_repair_rejection_reasons(outcome.reasons, outcome.details)
    block = _validation_error_block(text, project, feedback)

    assert PLANNING_REPAIR_MAX_VALIDATION_ERROR_CHARS == 450
    assert len(block) <= PLANNING_REPAIR_MAX_VALIDATION_ERROR_CHARS
    assert all(len(line) <= len("- ") + 180 for line in block.split("\n")[1:])
    assert len(block.split("\n")) - 1 <= 5


def test_repaired_a4_plan_still_fails_on_step_3(project):
    outcome = _validate(_plan_text("repaired"), project)

    assert outcome.status == "repair_required"
    assert outcome.reasons == [STEP3_REASON]
    assert outcome.details["unsupported_step_command_reasons"] == {"3": STEP3_REASON}
    assert _build_repair_rejection_reasons(outcome.reasons, outcome.details) == [
        STEP3_REASON
    ]


def test_single_pytest_invocation_with_both_node_ids_is_not_rejected(project):
    plan = json.loads(_plan_text("repaired"))
    step3 = next(step for step in plan if step["step_number"] == 3)
    step3["commands"] = [
        "python -m pytest -q "
        "tests/test_aggregate.py::test_case_and_whitespace_variants_count_once "
        "tests/test_aggregate.py::test_region_totals_use_latest_version_of_each"
        "_customer"
    ]
    outcome = _validate(json.dumps(plan, indent=2), project)

    assert "steps_with_unsupported_commands" not in outcome.details
    assert "unsupported_step_command_reasons" not in outcome.details
    assert "step_command_capability_unsupported" not in (
        outcome.details.get("validator_rule_ids") or []
    )


def test_single_offending_step_feedback_is_unchanged():
    details = {
        "steps_with_unsupported_commands": {"3": ["a", "b"]},
        "unsupported_step_command_reasons": {"3": STEP3_REASON},
    }
    assert _build_repair_rejection_reasons([STEP3_REASON], details) == [STEP3_REASON]
    # Verdicts persisted before PR20-A5 carry no per-step reasons.
    assert (
        _build_repair_rejection_reasons(
            A4_INITIAL_REASONS, {"steps_with_unsupported_commands": {"1": ["x"]}}
        )
        == A4_INITIAL_REASONS
    )
