"""Authentication dependencies and middleware"""

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User
from app.auth import verify_token
from app.services.session.auth import verify_session_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/auth/login", auto_error=False)
optional_oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="api/v1/auth/login", auto_error=False
)


async def get_current_user(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Get authenticated user from httpOnly session cookie or Bearer token fallback."""
    # 1. Try session cookie first (preferred for browser clients)
    session_cookie = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if session_cookie:
        payload = verify_session_token(session_cookie)
        if payload:
            email = payload.get("sub")
            if email:
                user = db.query(User).filter(User.email == email).first()
                if user and user.is_active:
                    return user

    # 2. Fallback: Bearer token (API keys, mobile, legacy clients)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    payload = verify_token(token, credentials_exception)
    email: str = payload.get("sub")

    if email is None:
        raise credentials_exception

    user = db.query(User).filter(User.email == email).first()

    if user is None:
        raise credentials_exception

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Get the current active user.

    Raises HTTPException if user is deactivated.
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated",
        )

    return current_user


def _configured_admin_emails() -> set[str]:
    return {
        email.strip().lower()
        for email in (settings.ORCHESTRATOR_ADMIN_EMAILS or "").split(",")
        if email.strip()
    }


async def get_current_admin_user(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> User:
    """
    Get the current admin user.

    Secure default:
    - If explicit admin emails are configured, only those users are admins.
    - Otherwise, only single-user deployments are allowed to access admin-only
      endpoints to avoid silent multi-user authorization breaks.
    """
    admin_emails = _configured_admin_emails()
    if current_user.email and current_user.email.lower() in admin_emails:
        return current_user

    active_user_count = db.query(User).filter(User.is_active.is_(True)).count()
    if not admin_emails and active_user_count <= 1:
        return current_user

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin privileges are required for this action",
    )


async def get_current_optional_user(
    request: Request,
    token: str | None = Depends(optional_oauth2_scheme),
    db: Session = Depends(get_db),
) -> User | None:
    """Get current user if authenticated (cookie or bearer), otherwise None."""
    # Try session cookie first
    session_cookie = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if session_cookie:
        payload = verify_session_token(session_cookie)
        if payload:
            email = payload.get("sub")
            if email:
                user = db.query(User).filter(User.email == email).first()
                if user and user.is_active:
                    return user

    if not token:
        return None

    try:
        payload = verify_token(
            token,
            HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            ),
        )
        email: str = payload.get("sub")

        if email is None:
            return None

        user = db.query(User).filter(User.email == email).first()
        return user
    except HTTPException:
        return None
