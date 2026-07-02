"""Phase 17A: Tests for FailureEvent dataclass and make_failure_event factory."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.orchestration.recovery.failure_event import (
    FailureEvent,
    make_failure_event,
)


def test_make_failure_event_required_fields():
    ev = make_failure_event(
        failure_class="pytest_failure",
        source="execution",
        error_message="AssertionError",
    )
    assert ev.failure_class == "pytest_failure"
    assert ev.source == "execution"
    assert ev.error_message == "AssertionError"


def test_event_id_is_non_empty_string():
    ev = make_failure_event(
        failure_class="unknown_failure",
        source="unknown",
        error_message="boom",
    )
    assert isinstance(ev.event_id, str)
    assert len(ev.event_id) > 0


def test_event_ids_are_unique():
    a = make_failure_event(
        failure_class="unknown_failure", source="unknown", error_message="x"
    )
    b = make_failure_event(
        failure_class="unknown_failure", source="unknown", error_message="x"
    )
    assert a.event_id != b.event_id


def test_created_at_is_parseable_iso8601():
    ev = make_failure_event(
        failure_class="import_error", source="execution", error_message="err"
    )
    dt = datetime.fromisoformat(ev.created_at)
    assert dt.tzinfo is not None


def test_error_message_truncated_to_400():
    long_msg = "x" * 600
    ev = make_failure_event(
        failure_class="unknown_failure", source="unknown", error_message=long_msg
    )
    assert len(ev.error_message) == 400


def test_optional_fields_default_to_none():
    ev = make_failure_event(
        failure_class="unknown_failure", source="unknown", error_message="e"
    )
    assert ev.session_id is None
    assert ev.task_id is None
    assert ev.step_index is None
    assert ev.terminal_reason is None
    assert ev.exception_type is None
    assert ev.orchestration_phase is None
    assert ev.orchestration_status is None
    assert ev.signature_hash is None


def test_optional_fields_populated():
    ev = make_failure_event(
        failure_class="syntax_error",
        source="execution",
        error_message="SyntaxError",
        session_id=5,
        task_id=42,
        step_index=3,
        orchestration_phase="executing",
        orchestration_status="executing",
        signature_hash="abc123def456abcd",
    )
    assert ev.session_id == 5
    assert ev.task_id == 42
    assert ev.step_index == 3
    assert ev.orchestration_phase == "executing"
    assert ev.signature_hash == "abc123def456abcd"
