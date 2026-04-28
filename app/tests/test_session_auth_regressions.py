from types import SimpleNamespace

from app.services import session_auth
from app.services import session_auth_service
from app.services.session import session_stream_service


def test_session_token_requires_active_session() -> None:
    token = session_auth.generate_session_token(user_id=123, email="user@example.com")
    payload = session_auth.verify_session_token(token, require_active=False)

    assert payload is not None
    session_id = payload["sid"]

    assert session_auth.verify_session_token(token) is None

    session_auth.store_session(session_id, user_id=123, email="user@example.com")
    active_payload = session_auth.verify_session_token(token)
    assert active_payload is not None
    assert active_payload["sid"] == session_id

    session_auth.invalidate_session(session_id)
    assert session_auth.verify_session_token(token) is None


def test_websocket_ticket_is_single_use() -> None:
    session_auth_service._ticket_store.clear()

    ticket = session_auth_service.create_websocket_ticket(
        user_id=456, expiry_seconds=30
    )

    first_use = session_auth_service.verify_websocket_ticket(ticket.ticket)
    second_use = session_auth_service.verify_websocket_ticket(ticket.ticket)

    assert first_use is not None
    assert first_use.user_id == 456
    assert second_use is None


def test_websocket_query_param_token_is_rejected() -> None:
    websocket = SimpleNamespace(
        headers={},
        query_params={"token": "legacy-bearer-token"},
        cookies={},
    )

    assert session_stream_service._authenticate_websocket(websocket, db=None) is None
