"""
Durable session authentication service.

Provides server-managed session state backed by the database, with
httpOnly cookies, short-lived WebSocket tickets, and token refresh
support — the building blocks for replacing browser localStorage
bearer-token auth (Epic 1).

Public API:
- create_session / get_session_by_id / delete_session
- create_refresh_token_record / get_refresh_token_by_id / rotate_refresh_token
- create_websocket_ticket / verify_websocket_ticket
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.auth import get_password_hash


class SessionRecord:
    """Lightweight in-DB session record (maps to `sessions` table later)."""

    def __init__(
        self,
        id: str,
        user_id: int,
        session_token: str,
        refresh_token: Optional[str],
        expires_at: datetime,
        created_at: datetime,
        last_used_at: datetime,
    ):
        self.id = id
        self.user_id = user_id
        self.session_token = session_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.created_at = created_at
        self.last_used_at = last_used_at


class RefreshTokenRecord:
    """Represents a persisted refresh token."""

    def __init__(
        self,
        id: str,
        user_id: int,
        token_hash: str,
        expires_at: datetime,
        created_at: datetime,
    ):
        self.id = id
        self.user_id = user_id
        self.token_hash = token_hash
        self.expires_at = expires_at
        self.created_at = created_at


class WebSocketTicket:
    """Short-lived, single-use WebSocket connection ticket."""

    def __init__(
        self,
        id: str,
        user_id: int,
        ticket: str,
        expires_at: datetime,
        used: bool,
        used_at: Optional[datetime],
    ):
        self.id = id
        self.user_id = user_id
        self.ticket = ticket
        self.expires_at = expires_at
        self.used = used
        self.used_at = used_at


# In-memory stores — replace with DB tables once migrations are landed.
_session_store: dict[str, SessionRecord] = {}
_refresh_store: dict[str, RefreshTokenRecord] = {}
_ticket_store: dict[str, WebSocketTicket] = {}


# ── Session CRUD ──────────────────────────────────────────────────────────────


def create_session(user_id: int, max_age_seconds: int = 604800) -> SessionRecord:
    """Create a new server-side session for *user_id*."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=max_age_seconds)

    record = SessionRecord(
        id=str(uuid.uuid4()),
        user_id=user_id,
        session_token=secrets.token_urlsafe(32),
        refresh_token=secrets.token_urlsafe(48),
        expires_at=expires,
        created_at=now,
        last_used_at=now,
    )
    _session_store[record.id] = record
    return record


def get_session_by_id(session_id: str) -> Optional[SessionRecord]:
    session = _session_store.get(session_id)
    if session:
        session.last_used_at = datetime.now(timezone.utc)
    return session


def delete_session(session_id: str) -> bool:
    if session_id in _session_store:
        del _session_store[session_id]
        return True
    return False


def get_session_by_token(session_token: str) -> Optional[SessionRecord]:
    for s in _session_store.values():
        if secrets.compare_digest(s.session_token, session_token):
            s.last_used_at = datetime.now(timezone.utc)
            return s
    return None


# ── Refresh tokens ───────────────────────────────────────────────────────────


def create_refresh_token_record(
    user_id: int, expire_days: int = 7
) -> RefreshTokenRecord:
    raw_token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now = datetime.now(timezone.utc)

    record = RefreshTokenRecord(
        id=str(uuid.uuid4()),
        user_id=user_id,
        token_hash=token_hash,
        expires_at=now + timedelta(days=expire_days),
        created_at=now,
    )
    _refresh_store[record.id] = record
    return record


def get_refresh_token_by_hash(token_value: str) -> Optional[RefreshTokenRecord]:
    token_hash = hashlib.sha256(token_value.encode()).hexdigest()
    for r in _refresh_store.values():
        if secrets.compare_digest(r.token_hash, token_hash):
            return r
    return None


def rotate_refresh_token(old_token: str) -> Optional[RefreshTokenRecord]:
    """Invalidate *old_token* and return a new refresh token record."""
    old = get_refresh_token_by_hash(old_token)
    if not old:
        return None
    del _refresh_store[old.id]
    return create_refresh_token_record(old.user_id)


# ── WebSocket tickets ─────────────────────────────────────────────────────────


def create_websocket_ticket(user_id: int, expiry_seconds: int = 30) -> WebSocketTicket:
    now = datetime.now(timezone.utc)
    ticket = WebSocketTicket(
        id=str(uuid.uuid4()),
        user_id=user_id,
        ticket=secrets.token_urlsafe(32),
        expires_at=now + timedelta(seconds=expiry_seconds),
        used=False,
        used_at=None,
    )
    _ticket_store[ticket.id] = ticket
    return ticket


def verify_websocket_ticket(ticket_value: str) -> Optional[WebSocketTicket]:
    """Verify and mark a ticket as used."""
    now = datetime.now(timezone.utc)
    for t in _ticket_store.values():
        if secrets.compare_digest(t.ticket, ticket_value):
            if t.used:
                return None
            if t.expires_at < now:
                return None
            t.used = True
            t.used_at = now
            return t
    return None
