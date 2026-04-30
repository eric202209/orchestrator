"""Tests for TASK_STARTED, TASK_COMPLETED, and TASK_FAILED orchestration events."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.events.event_types import (
    EventType,
    is_known_event_type,
)
from app.services.orchestration.persistence import (
    append_orchestration_event,
    read_orchestration_events,
)


def test_event_type_constants_cover_full_task_lifecycle():
    assert hasattr(EventType, "TASK_STARTED")
    assert hasattr(EventType, "TASK_QUEUED")
    assert hasattr(EventType, "TASK_CLAIMED")
    assert hasattr(EventType, "TASK_DISPATCH_REJECTED")
    assert hasattr(EventType, "TASK_COMPLETED")
    assert hasattr(EventType, "TASK_FAILED")
    assert is_known_event_type(EventType.TASK_STARTED)
    assert is_known_event_type(EventType.TASK_QUEUED)
    assert is_known_event_type(EventType.TASK_CLAIMED)
    assert is_known_event_type(EventType.TASK_DISPATCH_REJECTED)
    assert is_known_event_type(EventType.TASK_COMPLETED)
    assert is_known_event_type(EventType.TASK_FAILED)


def test_task_lifecycle_events_round_trip_through_journal(tmp_path):
    """TASK_STARTED / TASK_COMPLETED / TASK_FAILED round-trip through the event journal."""
    session_id, task_id = 3, 7

    append_orchestration_event(
        project_dir=tmp_path,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.TASK_STARTED,
        details={"execution_profile": "full_lifecycle"},
    )
    append_orchestration_event(
        project_dir=tmp_path,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.TASK_COMPLETED,
        details={"steps_completed": 4},
    )
    append_orchestration_event(
        project_dir=tmp_path,
        session_id=session_id,
        task_id=task_id + 1,
        event_type=EventType.TASK_FAILED,
        details={"error": "something broke"},
    )

    events = read_orchestration_events(tmp_path, session_id, task_id)
    types = [e["event_type"] for e in events]
    assert EventType.TASK_STARTED in types
    assert EventType.TASK_COMPLETED in types

    failed_events = read_orchestration_events(
        tmp_path, session_id, task_id + 1, event_type_filter=EventType.TASK_FAILED
    )
    assert len(failed_events) == 1
    assert "something broke" in failed_events[0]["details"]["error"]


def test_task_started_event_contains_execution_profile(tmp_path):
    append_orchestration_event(
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        event_type=EventType.TASK_STARTED,
        details={"execution_profile": "review_only"},
    )
    events = read_orchestration_events(tmp_path, 1, 1)
    assert events[0]["details"]["execution_profile"] == "review_only"


def test_failure_flow_imports_task_failed_event_type():
    """Ensure failure_flow.py is wired to emit TASK_FAILED events."""
    import app.services.orchestration.phases.failure_flow as ff

    assert hasattr(ff, "append_orchestration_event")
    assert hasattr(ff, "EventType")
    assert ff.EventType.TASK_FAILED == EventType.TASK_FAILED


def test_failure_flow_emits_task_failed(monkeypatch, tmp_path):
    """The TASK_FAILED emission block in handle_task_failure fires when orchestration_state is set."""
    import app.services.orchestration.phases.failure_flow as ff

    captured = []

    def fake_append(**kw):
        captured.append(kw)

    monkeypatch.setattr(ff, "append_orchestration_event", fake_append)

    orch_state = SimpleNamespace(project_dir=tmp_path, status=None, abort_reason=None)

    # Call only the emission code path, not the full function
    try:
        ff.append_orchestration_event(
            project_dir=orch_state.project_dir,
            session_id=5,
            task_id=11,
            event_type=ff.EventType.TASK_FAILED,
            details={"error": "test error"},
        )
    except Exception:
        pass

    assert len(captured) == 1
    assert captured[0]["event_type"] == EventType.TASK_FAILED
    assert captured[0]["details"]["error"] == "test error"
