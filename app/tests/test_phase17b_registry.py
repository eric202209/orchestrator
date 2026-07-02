"""Phase 17B: Registry routing tests — reflection retry, machine profiles, audit events."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.services.orchestration.events.event_types import EventType, is_known_event_type
from app.services.orchestration.recovery.failure_event import make_failure_event
from app.services.orchestration.recovery.recovery_policy import (
    STRATEGY_EXISTING_RECOVERY,
    STRATEGY_RETRY_WITH_REFLECTION,
    STRATEGY_TERMINAL,
    PolicyTable,
)
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryDecision,
    RecoveryStrategyRegistry,
)
from app.services.orchestration.state.persistence import read_orchestration_events


def _event(failure_class: str = "unknown_failure", sig: str = "abc"):
    ev = make_failure_event(
        failure_class=failure_class,
        source="unknown",
        error_message="err",
        session_id=1,
        task_id=1,
    )
    ev.signature_hash = sig
    return ev


def _state():
    return SimpleNamespace()


def _llm(prompt: str) -> str:
    return "corrective action"


# ── policy table 17B changes ──────────────────────────────────────────────────


def test_unknown_failure_policy_is_retry_with_reflection():
    rule = PolicyTable.lookup("unknown_failure")
    assert rule.strategy == STRATEGY_RETRY_WITH_REFLECTION


def test_debug_parse_error_policy_is_retry_with_reflection():
    rule = PolicyTable.lookup("debug_parse_error")
    assert rule.strategy == STRATEGY_RETRY_WITH_REFLECTION


def test_retry_with_reflection_consumes_no_budget():
    rule = PolicyTable.lookup("unknown_failure")
    assert rule.consumes_budget is False


def test_17a_policy_rules_unchanged():
    from app.services.orchestration.recovery.execution_recovery_service import (
        ELIGIBLE_RECOVERY_FAILURE_CLASSES,
    )

    for fc in ELIGIBLE_RECOVERY_FAILURE_CLASSES:
        rule = PolicyTable.lookup(fc)
        assert (
            rule.strategy == STRATEGY_EXISTING_RECOVERY
        ), f"17A rule for {fc!r} changed to {rule.strategy!r}"


# ── new event types are canonical ─────────────────────────────────────────────


def test_reflection_event_types_are_known():
    assert is_known_event_type(EventType.RECOVERY_REFLECTION_STARTED)
    assert is_known_event_type(EventType.RECOVERY_REFLECTION_COMPLETED)
    assert is_known_event_type(EventType.RECOVERY_REFLECTION_SKIPPED)
    assert is_known_event_type(EventType.RECOVERY_REFLECTION_FAILED)


# ── registry routing ──────────────────────────────────────────────────────────


def test_registry_routes_unknown_failure_to_terminal_after_reflection(tmp_path):
    decision = RecoveryStrategyRegistry.route(
        _event("unknown_failure"),
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        orchestration_state=_state(),
        llm_callable=_llm,
    )
    # Effective strategy after reflection is always terminal
    assert decision.strategy == STRATEGY_TERMINAL


def test_registry_routes_debug_parse_error_to_terminal_after_reflection(tmp_path):
    decision = RecoveryStrategyRegistry.route(
        _event("debug_parse_error"),
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        orchestration_state=_state(),
        llm_callable=_llm,
    )
    assert decision.strategy == STRATEGY_TERMINAL


def test_registry_returns_recovery_decision(tmp_path):
    decision = RecoveryStrategyRegistry.route(
        _event("unknown_failure"),
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        orchestration_state=_state(),
        llm_callable=_llm,
    )
    assert isinstance(decision, RecoveryDecision)


# ── audit events ──────────────────────────────────────────────────────────────


def test_reflection_emits_started_event(tmp_path):
    RecoveryStrategyRegistry.route(
        _event("unknown_failure"),
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        orchestration_state=_state(),
        llm_callable=_llm,
    )
    events = read_orchestration_events(
        tmp_path,
        session_id=1,
        task_id=1,
        event_type_filter=EventType.RECOVERY_REFLECTION_STARTED,
    )
    assert len(events) == 1
    assert events[0]["details"]["failure_class"] == "unknown_failure"


def test_reflection_emits_completed_on_success(tmp_path):
    RecoveryStrategyRegistry.route(
        _event("unknown_failure"),
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        orchestration_state=_state(),
        llm_callable=_llm,
    )
    events = read_orchestration_events(
        tmp_path,
        session_id=1,
        task_id=1,
        event_type_filter=EventType.RECOVERY_REFLECTION_COMPLETED,
    )
    assert len(events) == 1
    assert events[0]["details"]["outcome"] == "success"


def test_reflection_emits_failed_on_llm_error(tmp_path):
    def _bad_llm(_: str) -> str:
        raise RuntimeError("LLM down")

    RecoveryStrategyRegistry.route(
        _event("unknown_failure"),
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        orchestration_state=_state(),
        llm_callable=_bad_llm,
    )
    events = read_orchestration_events(
        tmp_path,
        session_id=1,
        task_id=1,
        event_type_filter=EventType.RECOVERY_REFLECTION_FAILED,
    )
    assert len(events) == 1


def test_decision_event_routed_emitted(tmp_path):
    RecoveryStrategyRegistry.route(
        _event("unknown_failure"),
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        orchestration_state=_state(),
        llm_callable=_llm,
    )
    events = read_orchestration_events(
        tmp_path,
        session_id=1,
        task_id=1,
        event_type_filter=EventType.RECOVERY_DECISION_ROUTED,
    )
    assert len(events) == 1
    assert events[0]["details"]["strategy"] == STRATEGY_TERMINAL


# ── machine profile guard (Machine C) ────────────────────────────────────────


def test_low_resource_profile_skips_reflection(tmp_path):
    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry._reflection_allowed",
        return_value=False,
    ):
        RecoveryStrategyRegistry.route(
            _event("unknown_failure"),
            project_dir=tmp_path,
            session_id=1,
            task_id=1,
            orchestration_state=_state(),
            llm_callable=_llm,
        )

    skipped = read_orchestration_events(
        tmp_path,
        session_id=1,
        task_id=1,
        event_type_filter=EventType.RECOVERY_REFLECTION_SKIPPED,
    )
    assert len(skipped) == 1
    assert skipped[0]["details"]["skip_reason"] == "low_resource_profile"


def test_low_resource_profile_still_routes_to_terminal(tmp_path):
    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry._reflection_allowed",
        return_value=False,
    ):
        decision = RecoveryStrategyRegistry.route(
            _event("unknown_failure"),
            project_dir=tmp_path,
            session_id=1,
            task_id=1,
            orchestration_state=_state(),
            llm_callable=_llm,
        )
    assert decision.strategy == STRATEGY_TERMINAL


def test_low_resource_profile_does_not_start_reflection(tmp_path):
    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry._reflection_allowed",
        return_value=False,
    ):
        RecoveryStrategyRegistry.route(
            _event("unknown_failure"),
            project_dir=tmp_path,
            session_id=1,
            task_id=1,
            orchestration_state=_state(),
            llm_callable=_llm,
        )

    started = read_orchestration_events(
        tmp_path,
        session_id=1,
        task_id=1,
        event_type_filter=EventType.RECOVERY_REFLECTION_STARTED,
    )
    assert len(started) == 0


# ── safe fallback ─────────────────────────────────────────────────────────────


def test_reflection_strategy_failure_falls_back_to_terminal(tmp_path):
    """If the strategy itself crashes, routing is still terminal, no exception raised."""
    with patch(
        "app.services.orchestration.recovery.recovery_strategy_registry.ReflectionRetryStrategy.execute",
        side_effect=RuntimeError("strategy crashed"),
    ):
        decision = RecoveryStrategyRegistry.route(
            _event("unknown_failure"),
            project_dir=tmp_path,
            session_id=1,
            task_id=1,
            orchestration_state=_state(),
            llm_callable=_llm,
        )
    assert decision.strategy == STRATEGY_TERMINAL


# ── non-reflection classes unaffected ────────────────────────────────────────


def test_existing_recovery_classes_unaffected(tmp_path):
    ev = _event("pytest_failure")
    decision = RecoveryStrategyRegistry.route(
        ev,
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        orchestration_state=_state(),
        llm_callable=_llm,
    )
    assert decision.strategy == STRATEGY_EXISTING_RECOVERY


def test_wrapper_timeout_noise_unaffected(tmp_path):
    ev = _event("wrapper_timeout_noise")
    decision = RecoveryStrategyRegistry.route(
        ev,
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        orchestration_state=_state(),
        llm_callable=_llm,
    )
    assert decision.strategy == "annotate_and_continue"
