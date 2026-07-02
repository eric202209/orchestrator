"""Phase 17B: Tests for reflection retry signature deduplication."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.recovery.failure_event import make_failure_event
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
    _add_reflection_signature,
    _get_reflection_signatures,
)
from app.services.orchestration.state.persistence import read_orchestration_events


def _unknown_event(sig: str = "abc123"):
    ev = make_failure_event(
        failure_class="unknown_failure",
        source="unknown",
        error_message="boom",
        session_id=1,
        task_id=1,
    )
    ev.signature_hash = sig
    return ev


def _fresh_state():
    return SimpleNamespace()


def _llm(prompt: str) -> str:
    return "here is the fix"


# ── helpers ───────────────────────────────────────────────────────────────────


def test_get_reflection_signatures_empty_on_fresh_state():
    sigs = _get_reflection_signatures(_fresh_state())
    assert sigs == frozenset()


def test_add_reflection_signature_stores_key():
    state = _fresh_state()
    _add_reflection_signature(state, "some:key")
    assert "some:key" in _get_reflection_signatures(state)


def test_add_reflection_signature_is_idempotent():
    state = _fresh_state()
    _add_reflection_signature(state, "key")
    _add_reflection_signature(state, "key")
    assert len(_get_reflection_signatures(state)) == 1


# ── dedup via registry ────────────────────────────────────────────────────────


def test_same_signature_blocks_second_reflection(tmp_path):
    state = _fresh_state()
    ev = _unknown_event("sig1")

    # First call — should attempt reflection
    RecoveryStrategyRegistry.route(
        ev,
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        orchestration_state=state,
        llm_callable=_llm,
    )
    # Second call — same signature, same state → should skip
    RecoveryStrategyRegistry.route(
        ev,
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        orchestration_state=state,
        llm_callable=_llm,
    )

    skipped_events = read_orchestration_events(
        tmp_path,
        session_id=1,
        task_id=1,
        event_type_filter=EventType.RECOVERY_REFLECTION_SKIPPED,
    )
    # At least one skipped event from the second call
    assert any(
        e["details"].get("skip_reason") == "signature_already_attempted"
        for e in skipped_events
    )


def test_different_signature_allows_reflection(tmp_path):
    state = _fresh_state()

    ev1 = _unknown_event("sig_a")
    ev2 = _unknown_event("sig_b")

    RecoveryStrategyRegistry.route(
        ev1,
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        orchestration_state=state,
        llm_callable=_llm,
    )
    RecoveryStrategyRegistry.route(
        ev2,
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        orchestration_state=state,
        llm_callable=_llm,
    )

    started_events = read_orchestration_events(
        tmp_path,
        session_id=1,
        task_id=1,
        event_type_filter=EventType.RECOVERY_REFLECTION_STARTED,
    )
    # Both signatures allowed → two started events
    assert len(started_events) == 2


def test_fresh_state_allows_reflection(tmp_path):
    # Two separate state objects → each is a fresh task run → both allowed
    state1 = _fresh_state()
    state2 = _fresh_state()
    ev = _unknown_event("sig_x")

    RecoveryStrategyRegistry.route(
        ev,
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        orchestration_state=state1,
        llm_callable=_llm,
    )
    RecoveryStrategyRegistry.route(
        ev,
        project_dir=tmp_path,
        session_id=1,
        task_id=2,
        orchestration_state=state2,
        llm_callable=_llm,
    )

    started_events = read_orchestration_events(
        tmp_path,
        session_id=1,
        task_id=1,
        event_type_filter=EventType.RECOVERY_REFLECTION_STARTED,
    )
    assert len(started_events) == 1  # task_id=1 started once
