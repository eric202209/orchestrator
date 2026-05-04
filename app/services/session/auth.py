"""Secure session management for cookie-backed authentication.

Provides server-side session store, JWT signing for httpOnly cookies,
short-lived WebSocket tickets, and session lifecycle helpers.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import jwt

from app.auth import ALGORITHM


def _get_secret_key() -> str:
    from app.config import settings

    return settings.SECRET_KEY


def generate_session_token(user_id: int, email: str) -> str:
    """Generate a server-side session token (stored in httpOnly cookie).

    The token is short-lived (configurable) and contains:
    - sub: user email
    - sid: random session ID for server-side tracking
    - uid: user ID
    """
    from app.config import settings

    session_id = secrets.token_urlsafe(16)
    # Use SESSION_COOKIE_MAX_AGE (seconds) from config, falling back to 7 days
    max_age_seconds = getattr(settings, "SESSION_COOKIE_MAX_AGE", 604800)
    expire = datetime.now(timezone.utc) + timedelta(seconds=max_age_seconds)

    payload = {
        "sub": email,
        "uid": user_id,
        "sid": session_id,
        "exp": expire,
        "type": "session",
    }
    token = jwt.encode(payload, _get_secret_key(), algorithm=ALGORITHM)
    return token


def verify_session_token(token: str, *, require_active: bool = True) -> Optional[dict]:
    """Verify and decode a session token.

    When ``require_active`` is true, the token's session id must still exist in the
    server-side registry. This makes logout/invalidation effective.
    """
    try:
        payload = jwt.decode(token, _get_secret_key(), algorithms=[ALGORITHM])
        if payload.get("type") != "session":
            return None
        session_id = payload.get("sid")
        if require_active:
            if not session_id or not is_session_active(session_id):
                return None
            session_info = _session_store.get(session_id)
            if session_info:
                session_info["last_accessed"] = datetime.now(timezone.utc)
        return payload
    except Exception:
        return None


def generate_ws_ticket() -> str:
    """Generate a short-lived, one-time WebSocket ticket.

    The ticket is a signed JWT valid for ~2 minutes. Used to authenticate
    WebSocket connections without exposing a long-lived bearer token in
    query params.
    """
    ticket_id = secrets.token_urlsafe(12)
    expire = datetime.now(timezone.utc) + timedelta(minutes=2)

    payload = {
        "tid": ticket_id,
        "exp": expire,
        "type": "ws_ticket",
    }
    token = jwt.encode(payload, _get_secret_key(), algorithm=ALGORITHM)
    return token


def verify_ws_ticket(token: str) -> Optional[dict]:
    """Verify and decode a WebSocket ticket. Returns payload or None."""
    try:
        payload = jwt.decode(token, _get_secret_key(), algorithms=[ALGORITHM])
        if payload.get("type") != "ws_ticket":
            return None
        return payload
    except Exception:
        return None


# ── Server-side session store (in-memory, backed by token sid) ──

_session_store: dict[str, dict] = {}


def store_session(session_id: str, user_id: int, email: str) -> None:
    """Store a session record in the in-memory registry."""
    _session_store[session_id] = {
        "user_id": user_id,
        "email": email,
        "created_at": datetime.now(timezone.utc),
        "last_accessed": datetime.now(timezone.utc),
    }


def invalidate_session(session_id: str) -> None:
    """Remove a session from the in-memory registry."""
    _session_store.pop(session_id, None)


def is_session_active(session_id: str) -> bool:
    """Check if a session ID is still in the registry."""
    return session_id in _session_store


def cleanup_expired_sessions() -> int:
    """Remove stale sessions not accessed in 7 days. Returns count removed."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    expired = [
        sid for sid, info in _session_store.items() if info["last_accessed"] < cutoff
    ]
    for sid in expired:
        _session_store.pop(sid, None)
    return len(expired)
