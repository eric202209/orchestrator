"""RR1 deterministic regressions: canonical endpoint and stabilization.

Covers RR1 sections 5 (canonical logical endpoint + equivalence proof),
6 (logical stabilization window), 7 (research observation timeout), and
22 (RER-02A reproduction).  Every case is provider-free.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.lifecycle.authority import (
    derive_lifecycle_authority,
)
from app.services.research.rr1.endpoint import (
    CANONICAL_ENDPOINT_EXPRESSION,
    legacy_cohort1_released,
    logical_endpoint_reached,
    observe_logical_endpoint,
)
from app.services.research.rr1.endpoint_equivalence import (
    prove_endpoint_equivalence,
)
from app.services.research.rr1.harness import (
    CENSOR_OBSERVATION_TIMEOUT,
    ObservationConfig,
    RunObserver,
)
from app.services.research.rr1.physical import RunPhysicalIdentity
from app.services.research.rr1.stabilization import (
    RESET_FINGERPRINT_CHANGED,
    RESET_NOT_SATISFIED,
    StabilizationWindow,
)
from app.services.research.rr1.state_machine import (
    CENSORED_OR_BLOCKED,
    LOGICAL_ENDPOINT_STABILIZING,
    OBSERVING_LOGICAL_EXECUTION,
    WAITING_FOR_PHYSICAL_RELEASE,
)

pytestmark = [pytest.mark.integration, pytest.mark.critical_regression]

T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


def _seed(db, tmp_path: Path, *, status: str = "running"):
    workspace = tmp_path / "rr1-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    project = Project(name="RR1 Project", workspace_path=str(workspace))
    session = SessionModel(
        project=project,
        name="RR1 Session",
        status=status,
        execution_mode="manual",
        is_active=True,
        instance_id="rr1-generation-1",
    )
    task = Task(
        project=project,
        title="RR1 Task",
        status=TaskStatus.RUNNING,
        task_subfolder="task-rr1",
        workspace_status="isolated",
    )
    link = SessionTask(session=session, task=task, status=TaskStatus.RUNNING)
    execution = TaskExecution(
        session=session, task=task, attempt_number=1, status=TaskStatus.RUNNING
    )
    db.add_all([project, session, task, link, execution])
    db.commit()
    for row in (project, session, task, link, execution):
        db.refresh(row)
    return project, session, task, link, execution


def _quiesce(db, session, task, link, execution, *, status="done"):
    """Drive the Session to a genuinely terminal, quiescent, marker-free state."""

    session.status = status
    session.continuation_task_id = None
    session.continuation_kind = None
    session.continuation_retry_count = 0
    session.continuation_retry_eta = None
    task.status = TaskStatus.DONE
    link.status = TaskStatus.DONE
    execution.status = TaskStatus.DONE
    db.commit()
    db.refresh(session)


# ---------------------------------------------------------------------------
# Section 5 -- canonical endpoint and equivalence
# ---------------------------------------------------------------------------


class TestCanonicalEndpointEquivalence:
    def test_predicate_is_proven_equivalent_across_full_state_space(
        self, db_session, tmp_path
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)

        def apply_active_work(active: bool) -> None:
            execution.status = TaskStatus.RUNNING if active else TaskStatus.DONE
            link.status = TaskStatus.RUNNING if active else TaskStatus.DONE
            task.status = TaskStatus.RUNNING if active else TaskStatus.DONE
            db_session.flush()

        result = prove_endpoint_equivalence(
            db_session, session, apply_active_work=apply_active_work
        )

        assert result.proven, result.as_evidence()
        assert result.cases_checked == 13 * 3 * 2
        assert result.disagreements == []
        assert result.raw_authority_violations == []
        assert result.structural_invariants == {
            "logical_terminal_implies_not_continuation_pending": True,
            "quiescent_implies_not_continuation_pending": True,
        }
        assert result.expression == CANONICAL_ENDPOINT_EXPRESSION

    def test_raw_values_never_independently_terminate_observation(
        self, db_session, tmp_path
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)

        # Session.status == failed, is_active False, TaskExecution FAILED and
        # Task FAILED: every raw marker the obsolete harness trusted.
        session.status = "failed"
        session.is_active = False
        task.status = TaskStatus.FAILED
        link.status = TaskStatus.FAILED
        execution.status = TaskStatus.FAILED
        # ...but a valid durable continuation is still pending.
        session.continuation_task_id = task.id
        session.continuation_kind = "celery_retry"
        session.continuation_retry_count = 1
        db_session.commit()
        db_session.refresh(session)

        authority = derive_lifecycle_authority(db_session, session)
        assert authority.continuation_pending is True
        assert logical_endpoint_reached(authority) is False

        # The obsolete Cohort-1 logic would have released here.
        assert legacy_cohort1_released(task_status="failed", session_status="failed")

    def test_paused_session_is_not_an_endpoint(self, db_session, tmp_path):
        _, session, task, link, execution = _seed(db_session, tmp_path)
        _quiesce(db_session, session, task, link, execution, status="paused")

        authority = derive_lifecycle_authority(db_session, session)
        assert authority.logical_terminal is False
        assert logical_endpoint_reached(authority) is False

    def test_endpoint_reached_only_when_all_three_facts_hold(
        self, db_session, tmp_path
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)
        _quiesce(db_session, session, task, link, execution, status="done")

        observation = observe_logical_endpoint(db_session, session, observed_at=T0)
        assert observation.reached is True
        assert observation.logical_terminal is True
        assert observation.continuation_pending is False
        assert observation.quiescent is True
        assert observation.as_evidence()["predicate"] == CANONICAL_ENDPOINT_EXPRESSION

    def test_non_boolean_authority_answer_is_not_terminality(self):
        assert logical_endpoint_reached({}) is False
        assert (
            logical_endpoint_reached(
                {
                    "logical_terminal": True,
                    "continuation_pending": False,
                    "quiescent": None,
                }
            )
            is False
        )


# ---------------------------------------------------------------------------
# Section 6 -- stabilization window
# ---------------------------------------------------------------------------


class TestStabilizationWindow:
    def test_single_observation_is_insufficient(self):
        window = StabilizationWindow(name="logical", window_seconds=30)
        assert window.observe(satisfied=True, at=T0, fingerprint=("g1",)) is False
        assert window.stable is False
        assert window.first_observed_at == T0

    def test_window_confirms_only_after_full_interval(self):
        window = StabilizationWindow(name="logical", window_seconds=30)
        window.observe(satisfied=True, at=T0, fingerprint=("g1",))
        assert (
            window.observe(
                satisfied=True, at=T0 + timedelta(seconds=29), fingerprint=("g1",)
            )
            is False
        )
        assert (
            window.observe(
                satisfied=True, at=T0 + timedelta(seconds=30), fingerprint=("g1",)
            )
            is True
        )
        assert window.stable_confirmed_at == T0 + timedelta(seconds=30)

    def test_break_resets_timer_and_discards_earlier_observation(self):
        window = StabilizationWindow(name="logical", window_seconds=30)
        window.observe(satisfied=True, at=T0, fingerprint=("g1",))
        window.observe(
            satisfied=False, at=T0 + timedelta(seconds=10), fingerprint=("g1",)
        )
        assert window.first_observed_at is None
        assert window.resets[-1].reason == RESET_NOT_SATISFIED

        window.observe(
            satisfied=True, at=T0 + timedelta(seconds=20), fingerprint=("g1",)
        )
        # The earlier observation must not back-date terminality.
        assert window.first_observed_at == T0 + timedelta(seconds=20)
        assert (
            window.observe(
                satisfied=True, at=T0 + timedelta(seconds=49), fingerprint=("g1",)
            )
            is False
        )
        assert (
            window.observe(
                satisfied=True, at=T0 + timedelta(seconds=50), fingerprint=("g1",)
            )
            is True
        )

    def test_generation_change_resets_even_while_satisfied(self):
        window = StabilizationWindow(name="logical", window_seconds=30)
        window.observe(satisfied=True, at=T0, fingerprint=("g1",))
        window.observe(
            satisfied=True, at=T0 + timedelta(seconds=20), fingerprint=("g2",)
        )
        assert window.resets[-1].reason == RESET_FINGERPRINT_CHANGED
        assert window.first_observed_at == T0 + timedelta(seconds=20)
        assert window.stable is False


# ---------------------------------------------------------------------------
# Section 22 -- RER-02A reproduction
# ---------------------------------------------------------------------------


class TestRer02aReproduction:
    """Provider-free reproduction of the original causal pattern."""

    def test_old_predicate_releases_where_new_predicate_keeps_observing(
        self, db_session, tmp_path
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)

        # The attempt fails; Session enters recovering with a live autonomous
        # retry.  This is exactly the RER-02A shape.
        execution.status = TaskStatus.FAILED
        execution.failure_category = "runtime_error"
        task.status = TaskStatus.FAILED
        link.status = TaskStatus.FAILED
        session.status = "recovering"
        session.continuation_task_id = task.id
        session.continuation_kind = "automatic_recovery"
        session.continuation_retry_count = 1
        db_session.commit()
        db_session.refresh(session)

        old_released = legacy_cohort1_released(
            task_status="failed", session_status="recovering"
        )
        authority = derive_lifecycle_authority(db_session, session)
        new_reached = logical_endpoint_reached(authority)

        assert old_released is True, "the obsolete harness released incorrectly"
        assert new_reached is False, "RR1 must continue observing"
        assert authority.continuation_pending is True

    def test_full_sequence_retry_pending_running_terminal_then_stabilization(
        self, db_session, tmp_path
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)
        observer = RunObserver(
            research_run_id="rr1-repro",
            session_id=session.id,
            identity=RunPhysicalIdentity(session_id=session.id, task_id=task.id),
            started_at=T0,
            task_id=task.id,
            config=ObservationConfig(
                logical_stabilization_seconds=30,
                physical_stabilization_seconds=30,
                run_observation_timeout_seconds=100_000,
            ),
        )

        # retry_pending -> still observing
        session.status = "retry_pending"
        session.continuation_task_id = task.id
        session.continuation_kind = "celery_retry"
        session.continuation_retry_count = 1
        execution.status = TaskStatus.FAILED
        task.status = TaskStatus.FAILED
        link.status = TaskStatus.FAILED
        db_session.commit()
        db_session.refresh(session)
        assert (
            observer.observe(db_session, session, at=T0 + timedelta(seconds=1))
            == OBSERVING_LOGICAL_EXECUTION
        )

        # running -> still observing
        session.status = "running"
        session.continuation_task_id = None
        session.continuation_kind = None
        session.continuation_retry_count = 0
        execution.status = TaskStatus.RUNNING
        task.status = TaskStatus.RUNNING
        link.status = TaskStatus.RUNNING
        db_session.commit()
        db_session.refresh(session)
        assert (
            observer.observe(db_session, session, at=T0 + timedelta(seconds=2))
            == OBSERVING_LOGICAL_EXECUTION
        )

        # stable logical terminal -> begin stabilization
        _quiesce(db_session, session, task, link, execution, status="done")
        assert (
            observer.observe(db_session, session, at=T0 + timedelta(seconds=3))
            == LOGICAL_ENDPOINT_STABILIZING
        )
        assert observer.logical_first_observed_at == T0 + timedelta(seconds=3)

        # stable window passes -> logical endpoint accepted
        assert (
            observer.observe(db_session, session, at=T0 + timedelta(seconds=32))
            == LOGICAL_ENDPOINT_STABILIZING
        )
        assert (
            observer.observe(db_session, session, at=T0 + timedelta(seconds=33))
            == WAITING_FOR_PHYSICAL_RELEASE
        )
        assert observer.logical_stable_confirmed_at == T0 + timedelta(seconds=33)

    def test_continuation_during_stabilization_returns_to_observation(
        self, db_session, tmp_path
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)
        observer = RunObserver(
            research_run_id="rr1-break",
            session_id=session.id,
            identity=RunPhysicalIdentity(session_id=session.id),
            started_at=T0,
            config=ObservationConfig(
                logical_stabilization_seconds=30,
                run_observation_timeout_seconds=100_000,
            ),
        )
        _quiesce(db_session, session, task, link, execution, status="failed")
        assert (
            observer.observe(db_session, session, at=T0 + timedelta(seconds=1))
            == LOGICAL_ENDPOINT_STABILIZING
        )

        # BR2 restores a continuation part-way through the window.
        session.continuation_task_id = task.id
        session.continuation_kind = "celery_retry"
        session.continuation_retry_count = 1
        db_session.commit()
        db_session.refresh(session)
        assert (
            observer.observe(db_session, session, at=T0 + timedelta(seconds=10))
            == OBSERVING_LOGICAL_EXECUTION
        )
        assert observer.logical_window.stable is False
        assert observer.logical_window.first_observed_at is None


# ---------------------------------------------------------------------------
# Section 7 -- research observation timeout
# ---------------------------------------------------------------------------


class TestRunObservationTimeout:
    def test_timeout_censors_without_mutating_product_lifecycle(
        self, db_session, tmp_path
    ):
        _, session, task, link, execution = _seed(db_session, tmp_path)
        session.status = "retry_pending"
        session.continuation_task_id = task.id
        session.continuation_kind = "celery_retry"
        session.continuation_retry_count = 1
        db_session.commit()
        db_session.refresh(session)

        before = {
            "status": session.status,
            "continuation_task_id": session.continuation_task_id,
            "continuation_kind": session.continuation_kind,
            "continuation_retry_count": session.continuation_retry_count,
            "task_status": task.status,
            "execution_status": execution.status,
        }

        observer = RunObserver(
            research_run_id="rr1-timeout",
            session_id=session.id,
            identity=RunPhysicalIdentity(session_id=session.id),
            started_at=T0,
            config=ObservationConfig(run_observation_timeout_seconds=60),
        )
        state = observer.observe(db_session, session, at=T0 + timedelta(seconds=61))

        assert state == CENSORED_OR_BLOCKED
        assert observer.censored_reason == CENSOR_OBSERVATION_TIMEOUT
        assert observer.next_run_eligible is False

        db_session.refresh(session)
        db_session.refresh(task)
        db_session.refresh(execution)
        assert {
            "status": session.status,
            "continuation_task_id": session.continuation_task_id,
            "continuation_kind": session.continuation_kind,
            "continuation_retry_count": session.continuation_retry_count,
            "task_status": task.status,
            "execution_status": execution.status,
        } == before, "the research timeout must not mutate Product lifecycle state"
