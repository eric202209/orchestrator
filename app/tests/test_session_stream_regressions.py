from __future__ import annotations

import json
from pathlib import Path

from fastapi.routing import APIWebSocketRoute

from app.api.v1.router import api_router
from app.services.session.session_stream_service import (
    _prepare_initial_orchestration_events,
    _prepare_initial_log_batch,
    _poll_new_orchestration_events,
)


def test_prepare_initial_log_batch_orders_oldest_to_newest_and_tracks_max_id():
    recent_logs = [
        {"id": 12, "message": "latest"},
        {"id": 9, "message": "middle"},
        {"id": 4, "message": "oldest"},
    ]

    ordered, last_log_id = _prepare_initial_log_batch(recent_logs)

    assert [entry["id"] for entry in ordered] == [4, 9, 12]
    assert last_log_id == 12


def test_poll_new_orchestration_events_returns_empty_when_no_journal(tmp_path):
    events, cursors = _poll_new_orchestration_events(str(tmp_path), 1, {})
    assert events == []
    assert cursors == {}


def test_poll_new_orchestration_events_reads_new_events(tmp_path):
    events_dir = tmp_path / ".agent" / "events"
    events_dir.mkdir(parents=True)
    log_path = events_dir / "session_1_task_5.jsonl"
    log_path.write_text(
        json.dumps(
            {
                "event_type": "phase_started",
                "session_id": 1,
                "task_id": 5,
                "details": {},
            }
        )
        + "\n"
        + json.dumps(
            {
                "event_type": "step_finished",
                "session_id": 1,
                "task_id": 5,
                "details": {},
            }
        )
        + "\n"
    )

    events, cursors = _poll_new_orchestration_events(str(tmp_path), 1, {})
    assert len(events) == 2
    assert events[0]["event_type"] == "phase_started"
    assert cursors[5] == 2


def test_poll_new_orchestration_events_respects_cursor(tmp_path):
    events_dir = tmp_path / ".agent" / "events"
    events_dir.mkdir(parents=True)
    log_path = events_dir / "session_2_task_3.jsonl"
    log_path.write_text(
        json.dumps(
            {
                "event_type": "phase_started",
                "session_id": 2,
                "task_id": 3,
                "details": {},
            }
        )
        + "\n"
        + json.dumps(
            {
                "event_type": "step_finished",
                "session_id": 2,
                "task_id": 3,
                "details": {},
            }
        )
        + "\n"
        + json.dumps(
            {
                "event_type": "task_completed",
                "session_id": 2,
                "task_id": 3,
                "details": {},
            }
        )
        + "\n"
    )

    # First poll reads all 3
    events, cursors = _poll_new_orchestration_events(str(tmp_path), 2, {})
    assert len(events) == 3
    assert cursors[3] == 3

    # Second poll with cursor at 3 should return nothing new
    events2, cursors2 = _poll_new_orchestration_events(str(tmp_path), 2, cursors)
    assert events2 == []
    assert cursors2[3] == 3


def test_prepare_initial_orchestration_events_replays_recent_backlog_only(tmp_path):
    events_dir = tmp_path / ".agent" / "events"
    events_dir.mkdir(parents=True)
    log_path = events_dir / "session_3_task_9.jsonl"
    log_path.write_text(
        "".join(
            json.dumps(
                {
                    "event_type": "step_finished",
                    "session_id": 3,
                    "task_id": 9,
                    "details": {"index": index},
                }
            )
            + "\n"
            for index in range(5)
        )
    )

    events, cursors = _prepare_initial_orchestration_events(
        str(tmp_path), 3, replay_limit=2
    )

    assert [event["details"]["index"] for event in events] == [3, 4]
    assert cursors[9] == 5


def test_session_logs_websocket_route_has_no_http_auth_dependency():
    websocket_route = next(
        route
        for route in api_router.routes
        if isinstance(route, APIWebSocketRoute)
        and route.path == "/sessions/{session_id}/logs/stream"
    )

    dependency_calls = {
        dependant.call
        for dependant in websocket_route.dependant.dependencies
        if dependant.call is not None
    }

    dependency_names = {
        getattr(call, "__name__", repr(call)) for call in dependency_calls
    }

    assert "get_current_active_user" not in dependency_names
