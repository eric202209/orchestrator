"""Phase 17G: runtime candidate audit event tests."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.planning.candidate_recovery import (
    CandidateRecoveryRequest,
    execute_single_sibling_candidate_recovery,
)


def _verdict(status: str, reasons: tuple[str, ...] = ()):
    return SimpleNamespace(
        status=status,
        accepted=status in {"accepted", "warning"},
        warning=status == "warning",
        repairable=status == "repair_required",
        rejected=status == "rejected",
        reasons=list(reasons),
        details={},
    )


def test_candidate_runtime_emits_required_candidate_events(tmp_path):
    execute_single_sibling_candidate_recovery(
        CandidateRecoveryRequest(
            project_dir=tmp_path,
            session_id=3,
            task_id=4,
            original_plan=[{"step_number": 1, "description": "original"}],
            original_output_text="original-json",
            original_verdict=_verdict("repair_required", ("missing file",)),
            runtime_profile="standard",
            parent_event_id=None,
            generate_sibling=lambda: (
                [{"step_number": 1, "description": "sibling"}],
                "sibling-json",
            ),
            validate_candidate=lambda _plan, _output_text: _verdict("accepted"),
        )
    )

    event_types = [
        event["event_type"]
        for event in read_orchestration_events(tmp_path, session_id=3, task_id=4)
    ]

    assert event_types.count(EventType.PLAN_CANDIDATE_CREATED) == 2
    assert event_types.count(EventType.PLAN_CANDIDATE_VALIDATED) == 2
    assert event_types.count(EventType.PLAN_CANDIDATE_SELECTED) == 1
    assert event_types.count(EventType.PLAN_CANDIDATE_REJECTED) == 1


def test_candidate_runtime_emits_exhausted_event(tmp_path):
    execute_single_sibling_candidate_recovery(
        CandidateRecoveryRequest(
            project_dir=tmp_path,
            session_id=5,
            task_id=6,
            original_plan=[{"step_number": 1, "description": "original"}],
            original_output_text="original-json",
            original_verdict=_verdict("rejected", ("bad",)),
            runtime_profile="standard",
            parent_event_id=None,
            generate_sibling=lambda: (
                [{"step_number": 1, "description": "sibling"}],
                "sibling-json",
            ),
            validate_candidate=lambda _plan, _output_text: _verdict(
                "repair_required", ("still bad",)
            ),
        )
    )

    exhausted = read_orchestration_events(
        tmp_path,
        session_id=5,
        task_id=6,
        event_type_filter=EventType.PLAN_CANDIDATE_EXHAUSTED,
    )

    assert len(exhausted) == 1
    assert exhausted[0]["details"]["candidate_count"] == 2
