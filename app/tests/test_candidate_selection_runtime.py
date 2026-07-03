"""Phase 17G: runtime candidate selection tests."""

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


def _request(tmp_path, *, original_status: str, sibling_status: str):
    return CandidateRecoveryRequest(
        project_dir=tmp_path,
        session_id=1,
        task_id=2,
        original_plan=[{"step_number": 1, "description": "original"}],
        original_output_text="original-json",
        original_verdict=_verdict(original_status, ("original issue",)),
        runtime_profile="standard",
        parent_event_id=None,
        generate_sibling=lambda: (
            [{"step_number": 1, "description": "sibling"}],
            "sibling-json",
        ),
        validate_candidate=lambda _plan, _output_text: _verdict(
            sibling_status, ("sibling issue",)
        ),
    )


def test_runtime_selection_uses_phase17f_policy(tmp_path):
    result = execute_single_sibling_candidate_recovery(
        _request(tmp_path, original_status="repair_required", sibling_status="warning")
    )

    assert result.selected is True
    assert result.outcome.selected_candidate.validator_status == "warning"
    assert result.selected_output_text == "sibling-json"


def test_runtime_selection_exhausts_when_no_candidate_accepted(tmp_path):
    result = execute_single_sibling_candidate_recovery(
        _request(tmp_path, original_status="rejected", sibling_status="repair_required")
    )

    assert result.selected is False
    assert result.outcome.outcome == "exhausted"
    assert result.selected_plan is None
