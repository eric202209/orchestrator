"""Characterize HIGH RISK exception-swallow behavior per the audit.

Each test documents one or more HIGH RISK sites, confirming:
  1. The swallowing handler does not propagate the exception to the caller.
  2. The specific evidence or state affected by the swallowed exception.

No production code is changed here. These are read-only characterizations.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.phases.planning_repair_arbitration_control import (
    _emit_planning_repair_arbitration,
    _reject_repair_candidate_by_bootstrap_contract,
    arbitrate_planning_repair_candidate,
)
from app.services.orchestration.phases.planning_support import _PlanningRetryState
from app.services.orchestration.state.persistence import (
    append_orchestration_event,
    read_orchestration_state_snapshots,
    record_validation_verdict,
    save_orchestration_checkpoint,
)
from app.services.orchestration.types import ValidationVerdict
from app.services.orchestration.prompt_templates import (
    OrchestrationState,
    OrchestrationStatus,
)


# ─── Shared helpers ───────────────────────────────────────────────────────────


def _journal(project_dir: Path, session_id: int, task_id: int) -> list[dict]:
    path = (
        project_dir / ".agent" / "events" / f"session_{session_id}_task_{task_id}.jsonl"
    )
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _orch_state(
    project_dir: Path, *, session_id: int = 1, task_id: int = 1
) -> OrchestrationState:
    state = OrchestrationState(
        session_id=str(session_id),
        task_description="Characterization task",
        project_name="CharacterizationTest",
        task_id=task_id,
    )
    state._project_dir_override = str(project_dir)
    return state


def _make_ctx(
    project_dir: Path,
    *,
    plan: list | None = None,
    plan_position: int = 1,
) -> Any:
    task = SimpleNamespace(
        title="Char task",
        description="Characterize exception swallow",
        plan_position=plan_position,
        status=None,
        error_message=None,
    )
    orchestration_state = SimpleNamespace(
        plan=plan or [],
        project_dir=project_dir,
        project_context="",
        status=None,
        abort_reason=None,
        reasoning_artifact=None,
    )
    return SimpleNamespace(
        task=task,
        orchestration_state=orchestration_state,
        prompt="Characterization prompt",
        execution_profile="full_lifecycle",
        validation_severity="standard",
        workflow_profile=None,
        workflow_stage=None,
        session_id=1,
        task_id=1,
        task_execution_id=None,
        session_instance_id=None,
        logger=logging.getLogger("test.exception_swallow"),
        emit_live=MagicMock(),
        db=MagicMock(),
        restore_workspace_snapshot_if_needed=None,
    )


def _raising(*_args: Any, **_kw: Any) -> Any:
    raise RuntimeError("injected failure")


# ─── state/persistence.py ─────────────────────────────────────────────────────


def test_health_score_update_failure_does_not_drop_event(tmp_path, monkeypatch):
    """HIGH RISK #21: state/persistence.py:811 (append_orchestration_event).

    _append_health_score_update failure is swallowed. Characterization:
    the main event IS written to the journal before the health update attempt,
    so event-journal evidence is not lost when health updates fail.
    """
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    monkeypatch.setattr(
        "app.services.orchestration.state.persistence._append_health_score_update",
        _raising,
    )

    event = append_orchestration_event(
        project_dir=project_dir,
        session_id=1,
        task_id=1,
        event_type=EventType.PHASE_STARTED,
        details={"phase": "planning"},
    )

    # Caller receives the event payload — no exception propagated.
    assert event["event_type"] == EventType.PHASE_STARTED

    events = _journal(project_dir, 1, 1)
    # Characterization: phase_started event IS present despite health update failure.
    assert any(e["event_type"] == EventType.PHASE_STARTED for e in events)
    # Characterization: health_score_updated absent because the update was suppressed.
    assert not any(e["event_type"] == EventType.HEALTH_SCORE_UPDATED for e in events)


def test_save_checkpoint_event_write_failure_logs_warning_and_leaves_no_journal_entry(
    db_session, tmp_path, monkeypatch, caplog
):
    """HIGH RISK #22: state/persistence.py:932 (save_orchestration_checkpoint).

    append_orchestration_event inside save_orchestration_checkpoint is wrapped in
    a broad try/except. After the fix: failure is logged at WARNING so it is
    visible in logs. CHECKPOINT_SAVED event and state snapshot remain absent from
    replay (the non-fatal behavior is unchanged).
    """
    import logging

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    state = _orch_state(project_dir)

    monkeypatch.setattr(
        "app.services.orchestration.state.persistence.append_orchestration_event",
        _raising,
    )

    with caplog.at_level(
        logging.WARNING, logger="app.services.orchestration.state.persistence"
    ):
        # Does not raise despite the event write failure.
        save_orchestration_checkpoint(
            db_session,
            session_id=1,
            task_id=1,
            prompt="Characterization prompt",
            orchestration_state=state,
            checkpoint_name="autosave_latest",
        )

    # Fix verified: failure is now visible in WARNING logs.
    assert any("CHECKPOINT_SAVED" in r.message for r in caplog.records)

    events = _journal(project_dir, 1, 1)
    # Evidence gap: CHECKPOINT_SAVED event still absent from journal.
    assert not any(e.get("event_type") == EventType.CHECKPOINT_SAVED for e in events)
    snapshots = read_orchestration_state_snapshots(project_dir, 1, 1)
    assert snapshots == []


def test_record_validation_verdict_event_write_failure_logs_warning_and_preserves_memory(
    db_session, tmp_path, monkeypatch, caplog
):
    """HIGH RISK #23: state/persistence.py:982 (record_validation_verdict).

    append_orchestration_event inside record_validation_verdict is wrapped in a
    broad try/except. After the fix: failure is logged at WARNING. The verdict IS
    in orchestration_state.validation_history (in-memory update precedes the
    try/except), while the VALIDATION_RESULT event and state snapshot are absent.
    """
    import logging

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    state = _orch_state(project_dir)

    monkeypatch.setattr(
        "app.services.orchestration.state.persistence.append_orchestration_event",
        _raising,
    )

    verdict = ValidationVerdict(
        stage="plan",
        status="failed",
        profile="implementation",
        reasons=["Missing test file"],
    )

    with caplog.at_level(
        logging.WARNING, logger="app.services.orchestration.state.persistence"
    ):
        # Does not raise.
        record_validation_verdict(
            db_session,
            session_id=1,
            task_id=1,
            orchestration_state=state,
            verdict=verdict,
        )

    # Fix verified: failure is now visible in WARNING logs.
    assert any("VALIDATION_RESULT" in r.message for r in caplog.records)

    # Verdict IS in in-memory state.
    assert len(state.validation_history) == 1
    assert state.validation_history[0]["status"] == "failed"
    assert state.last_plan_validation["status"] == "failed"

    # Evidence gap: VALIDATION_RESULT event still absent from journal.
    events = _journal(project_dir, 1, 1)
    assert not any(e.get("event_type") == EventType.VALIDATION_RESULT for e in events)
    snapshots = read_orchestration_state_snapshots(project_dir, 1, 1)
    assert snapshots == []


# ─── planning_repair_arbitration_control.py ───────────────────────────────────

_ARBI_MODULE = "app.services.orchestration.phases.planning_repair_arbitration_control"


def test_arbitrate_bootstrap_contract_validator_exception_falls_through_to_accept(
    tmp_path, monkeypatch
):
    """HIGH RISK #17: planning_repair_arbitration_control.py:167.

    When ValidatorService.validate_plan raises inside the Bootstrap Contract
    pre-check, the exception is swallowed and bootstrap_verdict is set to None.
    Characterization: the candidate falls through to acceptance without a contract
    verdict; arbitrate_planning_repair_candidate returns {"action": "none"}.
    """
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    ctx = _make_ctx(project_dir, plan=[], plan_position=1)

    # Return a no-regression arbitration so we reach the bootstrap check.
    monkeypatch.setattr(
        f"{_ARBI_MODULE}.classify_planning_repair_candidate",
        lambda **kw: {
            "regression_labels": [],
            "python_syntax": {"status": "ok"},
            "source_materialization": None,
            "arbitration_action": "none",
            "repair_reason": None,
            "repair_attempts": 0,
            "invalid_output": False,
        },
    )
    monkeypatch.setattr(
        f"{_ARBI_MODULE}.build_source_api_contract_capsule",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        f"{_ARBI_MODULE}._emit_planning_repair_arbitration",
        lambda *a, **kw: None,
    )
    # Validator raises — the exception is swallowed at line 167.
    monkeypatch.setattr(
        f"{_ARBI_MODULE}.ValidatorService.validate_plan",
        _raising,
    )

    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True

    result = arbitrate_planning_repair_candidate(
        ctx=ctx,
        retry_state=retry_state,
        previous_plan=[],
        immediate_repair_issues={},
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=MagicMock(),
    )

    # Characterization: function returns without raising.
    # Characterization: candidate is accepted with no contract verdict.
    assert result.get("action") == "none"


def test_reject_repair_candidate_event_write_failure_continues(tmp_path, monkeypatch):
    """HIGH RISK #18/#19: planning_repair_arbitration_control.py:422 and :493.

    append_orchestration_event inside _reject_repair_candidate_by_bootstrap_contract
    is wrapped in two broad try/except blocks. Characterization: when the event
    write fails the function continues and returns a result dict, leaving no
    PLANNING_REPAIR_ARBITRATION event in the journal for the rejection decision.

    This exercises the no-budget path (line 493) where _get_targeted_second_repair_reason
    returns None.
    """
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    ctx = _make_ctx(project_dir, plan_position=1)

    # Make append_orchestration_event raise inside the module.
    monkeypatch.setattr(
        f"{_ARBI_MODULE}.append_orchestration_event",
        _raising,
    )
    # Suppress emit_phase_event — it runs before the try/except.
    monkeypatch.setattr(
        f"{_ARBI_MODULE}.emit_phase_event",
        MagicMock(),
    )
    # No repair budget → falls through to no-budget path (line 484 try/except).
    monkeypatch.setattr(
        f"{_ARBI_MODULE}._get_targeted_second_repair_reason",
        lambda **kw: None,
    )
    monkeypatch.setattr(
        f"{_ARBI_MODULE}._finalize_planning_terminal_failure",
        lambda **kw: None,
    )

    bootstrap_verdict = SimpleNamespace(
        details={
            "task1_bootstrap_contract": {
                "passed": False,
                "bootstrap_task_type": "source_code",
                "violation_codes": ["missing_test_file"],
                "violations": ["missing_test_file"],
                "expected_test_reason": "tests required",
                "required_artifacts": [],
                "required_source_files": [],
                "required_test_files": [],
                "required_verification": [],
            }
        }
    )
    arbitration: dict[str, Any] = {
        "arbitration_action": "none",
        "reason": "characterization",
        "repair_reason": "bootstrap_contract_failed",
        "repair_attempts": 1,
        "planning_root_cause": None,
        "regression_labels": [],
    }
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True

    # Does not raise despite event write failure.
    result = _reject_repair_candidate_by_bootstrap_contract(
        ctx=ctx,
        retry_state=retry_state,
        arbitration=arbitration,
        bootstrap_verdict=bootstrap_verdict,
        planning_phase_event=None,
        output_text="[]",
        planning_timeout_seconds=60,
        prompt_profile=None,
        repair_planning_output=MagicMock(return_value={"output": "[]", "error": None}),
    )

    # Characterization: function returns a result dict.
    assert result is not None
    # Characterization: no PLANNING_REPAIR_ARBITRATION event in journal.
    events = _journal(project_dir, 1, 1)
    assert not any(
        e.get("event_type") == EventType.PLANNING_REPAIR_ARBITRATION for e in events
    )


def test_emit_planning_repair_arbitration_event_write_failure_continues(
    tmp_path, monkeypatch
):
    """HIGH RISK #20: planning_repair_arbitration_control.py:553.

    append_orchestration_event inside _emit_planning_repair_arbitration is wrapped
    in a broad try/except. Characterization: when the event write fails the function
    returns without raising, leaving no PLANNING_REPAIR_ARBITRATION event in the
    journal for the arbitration decision.
    """
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    ctx = _make_ctx(project_dir)

    # Make append_orchestration_event raise inside the module.
    monkeypatch.setattr(
        f"{_ARBI_MODULE}.append_orchestration_event",
        _raising,
    )
    # emit_phase_event runs before the try/except; mock it to avoid side-effects.
    monkeypatch.setattr(
        f"{_ARBI_MODULE}.emit_phase_event",
        MagicMock(),
    )

    arbitration = {"arbitration_action": "none", "reason": "characterization"}

    # Does not raise.
    _emit_planning_repair_arbitration(
        ctx=ctx,
        arbitration=arbitration,
        planning_phase_event=None,
    )

    # Characterization: no journal entry for the arbitration decision.
    events = _journal(project_dir, 1, 1)
    assert not any(
        e.get("event_type") == EventType.PLANNING_REPAIR_ARBITRATION for e in events
    )
