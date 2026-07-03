"""Phase 17G: sibling candidate generation contract tests."""

from __future__ import annotations

from types import SimpleNamespace

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


def test_candidate_recovery_generates_exactly_one_sibling(tmp_path):
    calls = {"generate": 0, "validate": 0}

    def generate_sibling():
        calls["generate"] += 1
        return ([{"step_number": 1, "description": "sibling"}], "sibling-json")

    def validate_candidate(_plan, _output_text):
        calls["validate"] += 1
        return _verdict("accepted")

    result = execute_single_sibling_candidate_recovery(
        CandidateRecoveryRequest(
            project_dir=tmp_path,
            session_id=1,
            task_id=2,
            original_plan=[{"step_number": 1, "description": "original"}],
            original_output_text="original-json",
            original_verdict=_verdict("repair_required", ("missing file",)),
            runtime_profile="standard",
            parent_event_id=None,
            generate_sibling=generate_sibling,
            validate_candidate=validate_candidate,
        )
    )

    assert calls == {"generate": 1, "validate": 1}
    assert result.selected is True
    assert result.outcome.candidate_count == 2
    assert result.outcome.operator_sequence == ("original", "sibling_generation")
    assert result.outcome.selected_candidate.candidate_id == "candidate-sibling-1"
