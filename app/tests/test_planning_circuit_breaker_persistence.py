"""Tests for B2: persisted planning circuit breaker (10H-A/10H-B F8/F12).

_PlanningRetryState.persisted_failures seeds the circuit breaker from prior
failed TaskExecution rows so a worker restart cannot reset the counter to zero.

10H-B: planning_circuit_breaker_opened* maps to failed_but_actionable and
carries last-output snippet + repair reason for operator correction.
"""

from __future__ import annotations

from datetime import timezone, datetime

import pytest

from app.models import (
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.phases.planning_support import (
    MAX_PLANNING_RETRIES,
    _PlanningRetryState,
)
from app.services.orchestration.phases.planning_flow import (
    _count_prior_failed_planning_executions,
    _last_plan_output_snippet,
)
from scripts.session_and_replay.failure_taxonomy import outcome_class


# ── _PlanningRetryState unit tests ────────────────────────────────────────────


def test_circuit_open_pure_in_memory_unchanged():
    """Existing behaviour: consecutive_failures alone still opens circuit."""
    state = _PlanningRetryState()
    for _ in range(MAX_PLANNING_RETRIES):
        state.consecutive_failures += 1
    assert state.circuit_open is True


def test_circuit_open_false_below_threshold():
    state = _PlanningRetryState()
    state.consecutive_failures = MAX_PLANNING_RETRIES - 1
    assert state.circuit_open is False


def test_circuit_open_combined_count():
    """persisted + consecutive opens circuit even when in-memory count alone would not."""
    state = _PlanningRetryState(persisted_failures=MAX_PLANNING_RETRIES - 1)
    state.consecutive_failures = 1
    assert state.circuit_open is True


def test_circuit_open_persisted_alone_at_threshold():
    """If persisted_failures already equals MAX, circuit opens on first iteration."""
    state = _PlanningRetryState(persisted_failures=MAX_PLANNING_RETRIES)
    assert state.circuit_open is True


def test_circuit_not_open_persisted_below_threshold():
    state = _PlanningRetryState(persisted_failures=MAX_PLANNING_RETRIES - 1)
    state.consecutive_failures = 0
    assert state.circuit_open is False


def test_default_persisted_failures_zero():
    """No regressions: default constructor leaves persisted_failures at zero."""
    state = _PlanningRetryState()
    assert state.persisted_failures == 0
    assert state.consecutive_failures == 0
    assert state.circuit_open is False


# ── _count_prior_failed_planning_executions ───────────────────────────────────


class _FakeCtx:
    """Minimal ctx substitute for DB-backed count tests."""

    def __init__(self, db, task_id, session_id, task_execution_id=None):
        self.db = db
        self.task_id = task_id
        self.session_id = session_id
        self.task_execution_id = task_execution_id


def _setup(db):
    project = Project(name="CB Persist Test", workspace_path="/tmp/cb_test")
    db.add(project)
    db.flush()
    session = SessionModel(
        project_id=project.id,
        name="cb-session",
        status="running",
        is_active=True,
    )
    db.add(session)
    db.flush()
    task = Task(project_id=project.id, title="cb-task", status=TaskStatus.PENDING)
    db.add(task)
    db.flush()
    return session, task


def _failed_execution(db, session, task, attempt):
    te = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=attempt,
        status=TaskStatus.FAILED,
    )
    db.add(te)
    db.flush()
    return te


def test_count_zero_when_no_prior_executions(db_session):
    session, task = _setup(db_session)
    db_session.commit()
    ctx = _FakeCtx(db_session, task.id, session.id)
    assert _count_prior_failed_planning_executions(ctx) == 0


def test_count_returns_prior_failed_executions(db_session):
    session, task = _setup(db_session)
    te1 = _failed_execution(db_session, session, task, attempt=1)
    te2 = _failed_execution(db_session, session, task, attempt=2)
    # current execution is attempt 3 (not yet written)
    db_session.commit()
    ctx = _FakeCtx(db_session, task.id, session.id, task_execution_id=te2.id + 1)
    assert _count_prior_failed_planning_executions(ctx) == 2


def test_count_excludes_current_execution(db_session):
    """Executions with id >= task_execution_id are not counted."""
    session, task = _setup(db_session)
    te1 = _failed_execution(db_session, session, task, attempt=1)
    te2 = _failed_execution(db_session, session, task, attempt=2)
    db_session.commit()
    # Current execution is te2 itself — only te1 should count.
    ctx = _FakeCtx(db_session, task.id, session.id, task_execution_id=te2.id)
    assert _count_prior_failed_planning_executions(ctx) == 1


def test_count_excludes_non_failed_executions(db_session):
    session, task = _setup(db_session)
    te_ok = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(te_ok)
    db_session.flush()
    db_session.commit()
    ctx = _FakeCtx(db_session, task.id, session.id, task_execution_id=te_ok.id + 1)
    assert _count_prior_failed_planning_executions(ctx) == 0


def test_count_excludes_different_session(db_session):
    """Failures from a different session do not bleed into the current count."""
    project = Project(name="CB Cross Test", workspace_path="/tmp/cb_cross")
    db_session.add(project)
    db_session.flush()

    session_a = SessionModel(
        project_id=project.id, name="sess-a", status="running", is_active=True
    )
    session_b = SessionModel(
        project_id=project.id, name="sess-b", status="running", is_active=True
    )
    db_session.add_all([session_a, session_b])
    db_session.flush()

    task = Task(project_id=project.id, title="cross-task", status=TaskStatus.PENDING)
    db_session.add(task)
    db_session.flush()

    # session_a has 2 failures
    _failed_execution(db_session, session_a, task, attempt=1)
    _failed_execution(db_session, session_a, task, attempt=2)
    db_session.commit()

    # counting for session_b — should see 0
    ctx = _FakeCtx(db_session, task.id, session_b.id)
    assert _count_prior_failed_planning_executions(ctx) == 0


def test_count_returns_zero_when_db_is_none():
    class _NullCtx:
        db = None
        task_id = 1
        session_id = 1
        task_execution_id = None

    assert _count_prior_failed_planning_executions(_NullCtx()) == 0


# ── 10H-B: failed_but_actionable taxonomy mapping ────────────────────────────


def test_planning_circuit_breaker_opened_maps_to_failed_but_actionable():
    """planning_circuit_breaker_opened is already a known terminal reason."""
    result = outcome_class(
        {"status": "stopped", "started_at": "2026-01-01T00:00:00+00:00"},
        [{"status": "failed", "attempt_number": 3}],
        [{"log_metadata": '{"reason": "planning_circuit_breaker_opened"}'}],
    )
    assert result == "failed_but_actionable"


def test_planning_circuit_breaker_opened_persisted_maps_to_failed_but_actionable():
    """planning_circuit_breaker_opened_persisted_attempts maps to failed_but_actionable."""
    result = outcome_class(
        {"status": "stopped", "started_at": "2026-01-01T00:00:00+00:00"},
        [{"status": "failed", "attempt_number": 2}],
        [
            {
                "log_metadata": '{"reason": "planning_circuit_breaker_opened_persisted_attempts"}'
            }
        ],
    )
    assert result == "failed_but_actionable"


# ── 10H-B: last-output snippet helper ────────────────────────────────────────


def test_last_plan_output_snippet_string_output():
    result = _last_plan_output_snippet({"output": "some plan text"})
    assert result == "some plan text"


def test_last_plan_output_snippet_dict_output_text_key():
    result = _last_plan_output_snippet({"output": {"text": "plan from dict"}})
    assert result == "plan from dict"


def test_last_plan_output_snippet_truncates_long_output():
    long_text = "x" * 600
    result = _last_plan_output_snippet({"output": long_text}, max_chars=400)
    assert len(result) <= 401  # 400 chars + ellipsis character
    assert result.endswith("…")


def test_last_plan_output_snippet_empty_output():
    result = _last_plan_output_snippet({})
    assert result == ""


def test_last_plan_output_snippet_none_output():
    result = _last_plan_output_snippet({"output": None})
    assert result == ""
