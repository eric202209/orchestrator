from __future__ import annotations

from fastapi.routing import APIWebSocketRoute

from app.api.v1.router import api_router
from app.services.session.session_stream_service import _prepare_initial_log_batch


def test_prepare_initial_log_batch_orders_oldest_to_newest_and_tracks_max_id():
    recent_logs = [
        {"id": 12, "message": "latest"},
        {"id": 9, "message": "middle"},
        {"id": 4, "message": "oldest"},
    ]

    ordered, last_log_id = _prepare_initial_log_batch(recent_logs)

    assert [entry["id"] for entry in ordered] == [4, 9, 12]
    assert last_log_id == 12


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
