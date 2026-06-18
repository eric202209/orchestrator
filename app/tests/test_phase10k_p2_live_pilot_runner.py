from __future__ import annotations

from scripts.maintenance.phase10k_p2_live_pilot_runner import (
    TERMINAL_TASK_STATUSES,
    _should_continue_after_task,
)


def test_terminal_task_statuses_include_expected_outcomes():
    assert {"done", "failed", "blocked_prior_task_failed"}.issubset(
        TERMINAL_TASK_STATUSES
    )


def test_should_continue_after_task_only_for_terminal_outcomes():
    assert _should_continue_after_task("done") is True
    assert _should_continue_after_task("failed") is True
    assert _should_continue_after_task("blocked_prior_task_failed") is True
    assert _should_continue_after_task("running") is False
    assert _should_continue_after_task("") is False
