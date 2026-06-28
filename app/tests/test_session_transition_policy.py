from __future__ import annotations

import pytest

from app.services.orchestration.state.session_state import resolve_session_transition


@pytest.mark.parametrize(
    ("current_status", "action", "result_status", "is_active", "timestamp_policy"),
    [
        ("pending", "start", "running", True, "started_at"),
        ("stopped", "start", "running", True, "started_at"),
        ("running", "pause", "paused", False, "paused_at"),
        ("paused", "resume", "running", True, "resumed_at"),
        ("awaiting_input", "resume", "running", True, "resumed_at"),
        ("running", "await_input", "awaiting_input", True, None),
        ("running", "stop", "stopped", False, "stopped_at"),
        ("paused", "stop", "stopped", False, "stopped_at"),
        ("awaiting_input", "stop", "stopped", False, "stopped_at"),
        ("running", "complete", "completed", False, "stopped_at"),
    ],
)
def test_resolve_session_transition_allows_known_transitions(
    current_status, action, result_status, is_active, timestamp_policy
):
    transition = resolve_session_transition(current_status, action)

    assert transition.allowed is True
    assert transition.result_status == result_status
    assert transition.is_active is is_active
    assert transition.timestamp_policy == timestamp_policy


@pytest.mark.parametrize("current_status", ["stopped", "completed"])
def test_resolve_session_transition_rejects_terminal_resume(current_status):
    transition = resolve_session_transition(current_status, "resume")

    assert transition.allowed is False
    assert transition.result_status == current_status
    assert transition.is_active is False
    assert transition.timestamp_policy is None
