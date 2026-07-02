"""Phase 17A: Tests for RecoveryStrategyRegistry."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.recovery.failure_classifier import FailureClassifier
from app.services.orchestration.recovery.failure_event import make_failure_event
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryDecision,
    RecoveryStrategyRegistry,
)
from app.services.orchestration.state.persistence import read_orchestration_events


def _done_state():
    s = SimpleNamespace()
    s.status = SimpleNamespace(value="done")
    return s


def _exec_state():
    s = SimpleNamespace()
    s.status = SimpleNamespace(value="executing")
    return s


def _wrapper_noise_event():
    return make_failure_event(
        failure_class="wrapper_timeout_noise",
        source="execution",
        error_message="time limit exceeded",
        session_id=1,
        task_id=1,
    )


def _recovery_event():
    return make_failure_event(
        failure_class="pytest_failure",
        source="execution",
        error_message="test failed",
        session_id=1,
        task_id=1,
    )


# ── return type ───────────────────────────────────────────────────────────────


def test_route_returns_recovery_decision(tmp_path):
    ev = _wrapper_noise_event()
    decision = RecoveryStrategyRegistry.route(
        ev, project_dir=tmp_path, session_id=1, task_id=1
    )
    assert isinstance(decision, RecoveryDecision)


def test_wrapper_timeout_noise_strategy(tmp_path):
    ev = _wrapper_noise_event()
    decision = RecoveryStrategyRegistry.route(
        ev, project_dir=tmp_path, session_id=1, task_id=1
    )
    assert decision.strategy == "annotate_and_continue"


def test_existing_recovery_class_strategy(tmp_path):
    ev = _recovery_event()
    decision = RecoveryStrategyRegistry.route(
        ev, project_dir=tmp_path, session_id=1, task_id=1
    )
    assert decision.strategy == "existing_recovery"


def test_policy_version_propagated(tmp_path):
    ev = _recovery_event()
    decision = RecoveryStrategyRegistry.route(
        ev, project_dir=tmp_path, session_id=1, task_id=1
    )
    assert decision.policy_version == "v2"


# ── audit event emission ──────────────────────────────────────────────────────


def test_wrapper_noise_emits_recovery_noise_annotated(tmp_path):
    ev = _wrapper_noise_event()
    RecoveryStrategyRegistry.route(ev, project_dir=tmp_path, session_id=1, task_id=1)
    events = read_orchestration_events(
        tmp_path,
        session_id=1,
        task_id=1,
        event_type_filter=EventType.RECOVERY_NOISE_ANNOTATED,
    )
    assert len(events) == 1
    assert events[0]["details"]["failure_class"] == "wrapper_timeout_noise"
    assert events[0]["details"]["strategy"] == "annotate_and_continue"


def test_existing_recovery_emits_recovery_decision_routed(tmp_path):
    ev = _recovery_event()
    RecoveryStrategyRegistry.route(ev, project_dir=tmp_path, session_id=1, task_id=1)
    events = read_orchestration_events(
        tmp_path,
        session_id=1,
        task_id=1,
        event_type_filter=EventType.RECOVERY_DECISION_ROUTED,
    )
    assert len(events) == 1
    assert events[0]["details"]["failure_class"] == "pytest_failure"


def test_no_event_when_project_dir_is_none():
    ev = _wrapper_noise_event()
    # Should not raise even when project_dir is None
    decision = RecoveryStrategyRegistry.route(
        ev, project_dir=None, session_id=1, task_id=1
    )
    assert decision.strategy == "annotate_and_continue"


def test_no_event_when_session_id_is_none(tmp_path):
    ev = _wrapper_noise_event()
    decision = RecoveryStrategyRegistry.route(
        ev, project_dir=tmp_path, session_id=None, task_id=1
    )
    assert decision.strategy == "annotate_and_continue"


# ── event details ─────────────────────────────────────────────────────────────


def test_event_details_include_policy_version(tmp_path):
    ev = _wrapper_noise_event()
    RecoveryStrategyRegistry.route(ev, project_dir=tmp_path, session_id=1, task_id=1)
    events = read_orchestration_events(
        tmp_path,
        session_id=1,
        task_id=1,
        event_type_filter=EventType.RECOVERY_NOISE_ANNOTATED,
    )
    assert events[0]["details"]["policy_version"] == "v2"


def test_event_details_include_session_and_task(tmp_path):
    ev = make_failure_event(
        failure_class="wrapper_timeout_noise",
        source="execution",
        error_message="timeout",
        session_id=5,
        task_id=99,
    )
    RecoveryStrategyRegistry.route(ev, project_dir=tmp_path, session_id=5, task_id=99)
    events = read_orchestration_events(
        tmp_path,
        session_id=5,
        task_id=99,
        event_type_filter=EventType.RECOVERY_NOISE_ANNOTATED,
    )
    assert len(events) == 1
    assert events[0]["details"]["session_id"] == 5
    assert events[0]["details"]["task_id"] == 99
