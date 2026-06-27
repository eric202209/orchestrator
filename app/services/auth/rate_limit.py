"""Best-effort in-memory auth rate limiting."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock

from fastapi import HTTPException, Request, status

from app.config import settings


@dataclass(frozen=True)
class RateLimitBucket:
    action: str
    client_id: str
    user_id: str | None = None


_attempts: dict[RateLimitBucket, deque[datetime]] = defaultdict(deque)
_attempts_lock = Lock()


def _get_client_id(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        first_hop = forwarded_for.split(",", 1)[0].strip()
        if first_hop:
            return first_hop

    if request.client and request.client.host:
        return request.client.host

    return "unknown"


def clear_auth_rate_limits() -> None:
    """Clear all in-memory counters. Intended for tests."""

    with _attempts_lock:
        _attempts.clear()


def enforce_auth_rate_limit(request: Request, action: str) -> None:
    """Reject excessive auth attempts for a given client/action pair."""

    if settings.AUTH_RATE_LIMIT_MAX_ATTEMPTS <= 0:
        return

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(seconds=settings.AUTH_RATE_LIMIT_WINDOW_SECONDS)
    bucket = RateLimitBucket(action=action, client_id=_get_client_id(request))

    with _attempts_lock:
        attempts = _attempts[bucket]
        while attempts and attempts[0] < window_start:
            attempts.popleft()

        if len(attempts) >= settings.AUTH_RATE_LIMIT_MAX_ATTEMPTS:
            retry_after_seconds = max(
                1,
                int(
                    (
                        attempts[0]
                        + timedelta(seconds=settings.AUTH_RATE_LIMIT_WINDOW_SECONDS)
                        - now
                    ).total_seconds()
                ),
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "Too many authentication attempts. " "Please wait and try again."
                ),
                headers={"Retry-After": str(retry_after_seconds)},
            )

        attempts.append(now)


def enforce_api_rate_limit(
    request: Request, action: str, *, current_user: object | None = None
) -> None:
    """Reject excessive API calls for write/mutation endpoints per client/action pair."""

    if settings.API_RATE_LIMIT_MAX_ATTEMPTS <= 0:
        return

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(seconds=settings.API_RATE_LIMIT_WINDOW_SECONDS)
    raw_user_id = getattr(current_user, "id", None)
    user_id = str(raw_user_id).strip() if raw_user_id is not None else ""
    bucket = RateLimitBucket(
        action=action,
        client_id=_get_client_id(request),
        user_id=user_id or None,
    )

    with _attempts_lock:
        attempts = _attempts[bucket]
        while attempts and attempts[0] < window_start:
            attempts.popleft()

        if len(attempts) >= settings.API_RATE_LIMIT_MAX_ATTEMPTS:
            retry_after_seconds = max(
                1,
                int(
                    (
                        attempts[0]
                        + timedelta(seconds=settings.API_RATE_LIMIT_WINDOW_SECONDS)
                        - now
                    ).total_seconds()
                ),
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please slow down and try again.",
                headers={"Retry-After": str(retry_after_seconds)},
            )

        attempts.append(now)
