"""Phase 13B-S1: Tests for ExecutionRecoveryService skeleton.

Verifies:
  - Eligibility gating (budget, failure class, empty evidence, repeated signature)
  - Audit event emission for all outcomes
  - Budget counter increments on eligible attempts
  - No file modifications in any code path
  - OrchestrationState field defaults (backward compat)
  - Existing abort behavior unchanged (recovery always returns non-success in S1)
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.events.event_types import EventType, is_known_event_type
from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
    build_completion_recovery_evidence,
    build_step_recovery_evidence,
)
from app.services.orchestration.recovery.execution_recovery_service import (
    ELIGIBLE_RECOVERY_FAILURE_CLASSES,
    RECOVERY_BUDGET,
    ExecutionRecoveryService,
    _LLM_PATCH_GENERATION_ENABLED,
    _STEP_SCOPE_RECOVERY_ENABLED,
    _COMPLETION_SCOPE_RECOVERY_ENABLED,
    _failure_signature_hash,
)
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.orchestration.prompt_templates import OrchestrationState


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_state(**kwargs) -> OrchestrationState:
    defaults = dict(session_id="s1", task_description="do a thing")
    defaults.update(kwargs)
    return OrchestrationState(**defaults)


def _make_evidence(**kwargs) -> ExecutionRecoveryEvidence:
    defaults = dict(
        task_title="My task",
        task_description="do a thing",
        failed_command="pytest tests/test_foo.py -x",
        exit_code=1,
        stdout_excerpt="collected 1 item",
        stderr_excerpt="FAILED tests/test_foo.py::test_bar - ImportError: cannot import name 'Foo'",
        traceback_excerpt="ImportError: cannot import name 'Foo' from 'src.foo'",
        changed_files=["src/foo.py"],
        failure_class="import_error",
    )
    defaults.update(kwargs)
    return ExecutionRecoveryEvidence(**defaults)


def _call_attempt(tmp_path, state, evidence, scope="step", step_index=1):
    return ExecutionRecoveryService.attempt_recovery(
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        evidence=evidence,
        orchestration_state=state,
        scope=scope,
        step_index=step_index,
    )


def _events(tmp_path, event_type=None):
    return read_orchestration_events(
        tmp_path, session_id=1, task_id=1, event_type_filter=event_type
    )


# ── event type registration ───────────────────────────────────────────────────


def test_recovery_event_types_are_registered():
    assert hasattr(EventType, "EXECUTION_RECOVERY_ATTEMPTED")
    assert hasattr(EventType, "EXECUTION_RECOVERY_SUCCEEDED")
    assert hasattr(EventType, "EXECUTION_RECOVERY_FAILED")
    assert hasattr(EventType, "EXECUTION_RECOVERY_SKIPPED")
    assert is_known_event_type(EventType.EXECUTION_RECOVERY_ATTEMPTED)
    assert is_known_event_type(EventType.EXECUTION_RECOVERY_SUCCEEDED)
    assert is_known_event_type(EventType.EXECUTION_RECOVERY_FAILED)
    assert is_known_event_type(EventType.EXECUTION_RECOVERY_SKIPPED)


# ── OrchestrationState field defaults ────────────────────────────────────────


def test_orchestration_state_recovery_fields_default_to_zero():
    state = _make_state()
    assert state.execution_recovery_attempts == 0
    assert state.execution_recovery_signature_hashes == []


def test_orchestration_state_recovery_fields_can_be_set():
    state = _make_state()
    state.execution_recovery_attempts = 1
    state.execution_recovery_signature_hashes = ["abc123"]
    assert state.execution_recovery_attempts == 1
    assert state.execution_recovery_signature_hashes == ["abc123"]


# ── LLM gate ─────────────────────────────────────────────────────────────────


def test_recovery_scope_flags_s2():
    # S2: step scope enabled, completion scope disabled.
    assert _LLM_PATCH_GENERATION_ENABLED is True
    assert _STEP_SCOPE_RECOVERY_ENABLED is True
    assert _COMPLETION_SCOPE_RECOVERY_ENABLED is False


# ── should_attempt: budget exhausted ─────────────────────────────────────────


def test_should_attempt_false_when_budget_exhausted():
    state = _make_state()
    state.execution_recovery_attempts = RECOVERY_BUDGET
    evidence = _make_evidence()
    ok, reason = ExecutionRecoveryService.should_attempt(evidence, state)
    assert ok is False
    assert reason == "budget_exhausted"


def test_attempt_recovery_emits_skipped_when_budget_exhausted(tmp_path):
    state = _make_state()
    state.execution_recovery_attempts = RECOVERY_BUDGET
    evidence = _make_evidence()
    result = _call_attempt(tmp_path, state, evidence)
    assert result["status"] == "skipped"
    assert result["reason"] == "budget_exhausted"
    # Budget must NOT increase
    assert state.execution_recovery_attempts == RECOVERY_BUDGET
    skipped = _events(tmp_path, EventType.EXECUTION_RECOVERY_SKIPPED)
    assert len(skipped) == 1
    assert skipped[0]["details"]["skip_reason"] == "budget_exhausted"


# ── should_attempt: empty evidence ───────────────────────────────────────────


def test_should_attempt_false_when_evidence_is_empty():
    state = _make_state()
    evidence = _make_evidence(
        stdout_excerpt="",
        stderr_excerpt="",
        traceback_excerpt="",
        git_diff_summary="",
    )
    ok, reason = ExecutionRecoveryService.should_attempt(evidence, state)
    assert ok is False
    assert reason == "evidence_empty"


def test_attempt_recovery_emits_skipped_on_empty_evidence(tmp_path):
    state = _make_state()
    evidence = _make_evidence(
        stdout_excerpt="",
        stderr_excerpt="",
        traceback_excerpt="",
        git_diff_summary="",
    )
    result = _call_attempt(tmp_path, state, evidence)
    assert result["status"] == "skipped"
    assert result["reason"] == "evidence_empty"
    assert state.execution_recovery_attempts == 0
    skipped = _events(tmp_path, EventType.EXECUTION_RECOVERY_SKIPPED)
    assert len(skipped) == 1
    assert skipped[0]["details"]["skip_reason"] == "evidence_empty"


# ── should_attempt: ineligible failure class ──────────────────────────────────


def test_should_attempt_false_for_ineligible_failure_class():
    state = _make_state()
    evidence = _make_evidence(failure_class="permission_denied")
    ok, reason = ExecutionRecoveryService.should_attempt(evidence, state)
    assert ok is False
    assert reason == "ineligible_failure_class"


def test_attempt_recovery_emits_skipped_for_ineligible_class(tmp_path):
    state = _make_state()
    evidence = _make_evidence(failure_class="permission_denied")
    result = _call_attempt(tmp_path, state, evidence)
    assert result["status"] == "skipped"
    assert result["reason"] == "ineligible_failure_class"
    assert state.execution_recovery_attempts == 0
    skipped = _events(tmp_path, EventType.EXECUTION_RECOVERY_SKIPPED)
    assert len(skipped) == 1
    assert skipped[0]["details"]["failure_class"] == "permission_denied"


@pytest.mark.parametrize(
    "failure_class",
    [
        "permission_denied",
        "safety_block",
        "workspace_lock_failure",
        "malformed_planning_json",
        "unknown_class_xyz",
    ],
)
def test_ineligible_classes_are_skipped(failure_class):
    state = _make_state()
    evidence = _make_evidence(failure_class=failure_class)
    ok, reason = ExecutionRecoveryService.should_attempt(evidence, state)
    assert ok is False
    assert reason == "ineligible_failure_class"


@pytest.mark.parametrize("failure_class", sorted(ELIGIBLE_RECOVERY_FAILURE_CLASSES))
def test_all_eligible_classes_pass_gate(failure_class):
    state = _make_state()
    evidence = _make_evidence(failure_class=failure_class)
    ok, reason = ExecutionRecoveryService.should_attempt(evidence, state)
    assert ok is True
    assert reason == ""


# ── should_attempt: repeated failure signature ───────────────────────────────


def test_should_attempt_false_on_repeated_failure_signature():
    state = _make_state()
    evidence = _make_evidence()
    sig = _failure_signature_hash(evidence)
    state.execution_recovery_signature_hashes = [sig]
    ok, reason = ExecutionRecoveryService.should_attempt(evidence, state)
    assert ok is False
    assert reason == "repeated_failure_signature"


def test_attempt_recovery_emits_skipped_on_repeated_signature(tmp_path):
    state = _make_state()
    evidence = _make_evidence()
    sig = _failure_signature_hash(evidence)
    state.execution_recovery_signature_hashes = [sig]
    result = _call_attempt(tmp_path, state, evidence)
    assert result["status"] == "skipped"
    assert result["reason"] == "repeated_failure_signature"
    assert state.execution_recovery_attempts == 0


# ── eligible attempt: ATTEMPTED + FAILED, budget consumed ────────────────────


def test_eligible_attempt_increments_budget(tmp_path):
    state = _make_state()
    evidence = _make_evidence(failure_class="pytest_failure")
    result = _call_attempt(tmp_path, state, evidence)
    assert result["status"] == "failed"
    assert result["reason"] == "llm_patch_generation_disabled"
    assert state.execution_recovery_attempts == 1


def test_eligible_attempt_emits_attempted_and_failed_events(tmp_path):
    state = _make_state()
    evidence = _make_evidence(failure_class="import_error")
    _call_attempt(tmp_path, state, evidence)
    attempted = _events(tmp_path, EventType.EXECUTION_RECOVERY_ATTEMPTED)
    failed = _events(tmp_path, EventType.EXECUTION_RECOVERY_FAILED)
    assert len(attempted) == 1
    assert len(failed) == 1
    assert attempted[0]["details"]["failure_class"] == "import_error"
    assert failed[0]["details"]["stop_reason"] == "llm_patch_generation_disabled"
    # S2: LLM patch generation is enabled globally; noop path still used when llm_callable absent.
    assert "llm_patch_generation_enabled" in failed[0]["details"]


def test_eligible_attempt_stores_signature_hash(tmp_path):
    state = _make_state()
    evidence = _make_evidence(failure_class="syntax_error")
    _call_attempt(tmp_path, state, evidence)
    assert len(state.execution_recovery_signature_hashes) == 1


def test_two_eligible_attempts_exhaust_budget(tmp_path):
    state = _make_state()
    evidence1 = _make_evidence(
        failure_class="pytest_failure", stderr_excerpt="first error"
    )
    evidence2 = _make_evidence(
        failure_class="import_error", stderr_excerpt="second error"
    )
    r1 = _call_attempt(tmp_path, state, evidence1)
    r2 = _call_attempt(tmp_path, state, evidence2)
    assert r1["status"] == "failed"
    assert r2["status"] == "failed"
    assert state.execution_recovery_attempts == 2
    # Third attempt must be skipped
    evidence3 = _make_evidence(
        failure_class="syntax_error", stderr_excerpt="third error"
    )
    r3 = _call_attempt(tmp_path, state, evidence3)
    assert r3["status"] == "skipped"
    assert r3["reason"] == "budget_exhausted"
    assert state.execution_recovery_attempts == 2  # unchanged


def test_second_attempt_budget_exhausted_flag(tmp_path):
    state = _make_state()
    evidence1 = _make_evidence(failure_class="pytest_failure", stderr_excerpt="err1")
    evidence2 = _make_evidence(failure_class="import_error", stderr_excerpt="err2")
    _call_attempt(tmp_path, state, evidence1)
    _call_attempt(tmp_path, state, evidence2)
    failed_events = _events(tmp_path, EventType.EXECUTION_RECOVERY_FAILED)
    assert len(failed_events) == 2
    assert failed_events[1]["details"]["budget_exhausted"] is True
    assert failed_events[0]["details"]["budget_exhausted"] is False


# ── no success path in S1 ────────────────────────────────────────────────────


def test_recovery_never_returns_success_in_s1(tmp_path):
    state = _make_state()
    for fc in ELIGIBLE_RECOVERY_FAILURE_CLASSES:
        s = _make_state()
        ev = _make_evidence(failure_class=fc, stderr_excerpt=f"err for {fc}")
        result = ExecutionRecoveryService.attempt_recovery(
            project_dir=tmp_path,
            session_id=99,
            task_id=99,
            evidence=ev,
            orchestration_state=s,
            scope="step",
        )
        assert (
            result["status"] != "success"
        ), f"Got success for {fc} — must not happen in S1"


# ── evidence builders ─────────────────────────────────────────────────────────


def test_build_step_recovery_evidence_from_nones():
    step_record = SimpleNamespace(
        error_message="pytest error",
        verification_output="",
        files_changed=["src/foo.py"],
    )
    failure_envelope = SimpleNamespace(root_cause="pytest_failure", input={})
    debug_feedback_envelope = SimpleNamespace(
        failure_class="pytest_failure",
        failed_command="pytest -x",
        return_code=1,
        validator_reasons=["test_bar failed"],
    )
    ev = build_step_recovery_evidence(
        failure_envelope=failure_envelope,
        debug_feedback_envelope=debug_feedback_envelope,
        step_record=step_record,
        step_output="collected 1 item",
        task_title="My task",
        task_prompt="Implement feature X",
    )
    assert ev.failure_class == "pytest_failure"
    assert ev.failed_command == "pytest -x"
    assert ev.exit_code == 1
    assert "pytest error" in ev.stderr_excerpt
    assert ev.changed_files == ["src/foo.py"]
    assert not ev.is_empty


def test_build_step_recovery_evidence_is_empty_when_no_output():
    step_record = SimpleNamespace(
        error_message="",
        verification_output="",
        files_changed=[],
    )
    failure_envelope = SimpleNamespace(root_cause="unknown", input={})
    debug_feedback_envelope = SimpleNamespace(
        failure_class="import_error",
        failed_command="",
        return_code=None,
        validator_reasons=[],
    )
    ev = build_step_recovery_evidence(
        failure_envelope=failure_envelope,
        debug_feedback_envelope=debug_feedback_envelope,
        step_record=step_record,
        step_output="",
        task_title="",
        task_prompt="",
    )
    assert ev.is_empty


def test_build_completion_recovery_evidence():
    completion_validation = SimpleNamespace(
        reasons=["missing symbol: Foo", "symbol not found"],
        details={
            "symbol_verification": {
                "missing": ["Foo", "Bar"],
                "required": ["Foo", "Bar"],
            },
            "verification_command": "pytest tests/ -x",
            "verification_output_preview": "ImportError: cannot import name 'Foo'",
        },
        failure_class="missing_requested_symbol",
    )
    debug_feedback_envelope = SimpleNamespace(failure_class="missing_requested_symbol")
    orchestration_state = SimpleNamespace(changed_files=["src/foo.py"])
    ev = build_completion_recovery_evidence(
        completion_validation=completion_validation,
        debug_feedback_envelope=debug_feedback_envelope,
        orchestration_state=orchestration_state,
        task_title="My task",
        task_prompt="Implement feature X",
    )
    assert ev.failure_class == "missing_requested_symbol"
    assert "Foo" in ev.requested_symbols
    assert "Bar" in ev.requested_symbols
    assert not ev.is_empty


# ── event fields correctness ──────────────────────────────────────────────────


def test_skipped_event_has_required_fields(tmp_path):
    state = _make_state()
    evidence = _make_evidence(failure_class="permission_denied")
    _call_attempt(tmp_path, state, evidence)
    skipped = _events(tmp_path, EventType.EXECUTION_RECOVERY_SKIPPED)
    assert len(skipped) == 1
    d = skipped[0]["details"]
    assert "scope" in d
    assert "skip_reason" in d
    assert "failure_class" in d
    assert "total_recovery_attempts_used" in d
    assert "llm_patch_generation_enabled" in d


def test_attempted_event_has_required_fields(tmp_path):
    state = _make_state()
    evidence = _make_evidence(failure_class="pytest_failure")
    _call_attempt(tmp_path, state, evidence)
    attempted = _events(tmp_path, EventType.EXECUTION_RECOVERY_ATTEMPTED)
    assert len(attempted) == 1
    d = attempted[0]["details"]
    for key in [
        "scope",
        "step_index",
        "attempt",
        "failure_class",
        "failed_command",
        "evidence_chars",
        "patch_type",
        "llm_patch_generation_enabled",
    ]:
        assert key in d, f"missing key: {key}"


def test_failed_event_has_required_fields(tmp_path):
    state = _make_state()
    evidence = _make_evidence(failure_class="import_error")
    _call_attempt(tmp_path, state, evidence)
    failed = _events(tmp_path, EventType.EXECUTION_RECOVERY_FAILED)
    assert len(failed) == 1
    d = failed[0]["details"]
    for key in [
        "scope",
        "step_index",
        "attempt",
        "failure_class",
        "stop_reason",
        "total_recovery_attempts_used",
        "budget_exhausted",
        "llm_patch_generation_enabled",
    ]:
        assert key in d, f"missing key: {key}"


# ── completion scope ──────────────────────────────────────────────────────────


def test_completion_scope_recovery_emits_events(tmp_path):
    state = _make_state()
    evidence = _make_evidence(failure_class="completion_validation_failed")
    result = ExecutionRecoveryService.attempt_recovery(
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        evidence=evidence,
        orchestration_state=state,
        scope="completion",
        step_index=None,
    )
    assert result["status"] == "failed"
    attempted = _events(tmp_path, EventType.EXECUTION_RECOVERY_ATTEMPTED)
    assert attempted[0]["details"]["scope"] == "completion"
    assert attempted[0]["details"]["step_index"] is None
