"""User-facing application settings endpoints."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth import get_password_hash, verify_password
from app.config import settings
from app.database import get_db
from app.dependencies import get_current_active_user
from app.models import User
from app.schemas import (
    AppSettingsResponse,
    PasswordChangeRequest,
    ProfileUpdateRequest,
    SystemSettingsUpdateRequest,
)
from app.services.system_settings import (
    MOBILE_API_KEY_KEY,
    WORKSPACE_ROOT_KEY,
    generate_mobile_gateway_key,
    get_effective_mobile_gateway_key,
    get_effective_workspace_root,
    set_setting_value,
)

router = APIRouter(prefix="/settings", tags=["settings"])


def _mask_secret(secret: str | None) -> str | None:
    if not secret:
        return None
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:8]}...{secret[-4:]}"


def _derive_mobile_base_url(request: Request) -> str:
    configured = (settings.ORCHESTRATOR_MOBILE_BASE_URL or "").strip().rstrip("/")
    if configured:
        if configured.endswith("/api/v1/mobile"):
            return configured
        if configured.endswith("/api/v1"):
            return f"{configured}/mobile"
        return f"{configured}/api/v1/mobile"
    return f"{str(request.base_url).rstrip('/')}{settings.API_V1_STR}/mobile"


@router.get("", response_model=AppSettingsResponse)
def get_app_settings(
    request: Request,
    current_user: User = Depends(get_current_active_user),
):
    mobile_key, key_source = get_effective_mobile_gateway_key(
        settings.MOBILE_GATEWAY_API_KEY,
        settings.OPENCLAW_API_KEY,
    )
    return {
        "account": {
            "email": current_user.email,
            "name": current_user.name,
        },
        "system": {
            "workspace_root": str(get_effective_workspace_root()),
            "mobile_base_url": _derive_mobile_base_url(request),
            "mobile_api_key_configured": bool(mobile_key),
            "mobile_api_key_preview": _mask_secret(mobile_key),
            "mobile_api_key_source": key_source,
            "openclaw_gateway_url": settings.OPENCLAW_GATEWAY_URL,
        },
    }


@router.patch("/profile", response_model=AppSettingsResponse)
def update_profile(
    request: Request,
    profile: ProfileUpdateRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    current_user.name = profile.name
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return get_app_settings(request, current_user)


@router.post("/password")
def change_password(
    payload: PasswordChangeRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    if len(payload.new_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be at least 6 characters",
        )

    current_user.hashed_password = get_password_hash(payload.new_password)
    db.add(current_user)
    db.commit()
    return {"success": True, "message": "Password updated successfully"}


@router.patch("/system", response_model=AppSettingsResponse)
def update_system_settings(
    request: Request,
    payload: SystemSettingsUpdateRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    if payload.workspace_root is not None:
        workspace_root = str(Path(payload.workspace_root).expanduser().resolve())
        set_setting_value(
            db,
            WORKSPACE_ROOT_KEY,
            workspace_root,
            description="OpenClaw workspace root used for orchestration projects",
        )

    if payload.rotate_mobile_api_key:
        payload.mobile_api_key = generate_mobile_gateway_key()

    if payload.mobile_api_key is not None:
        set_setting_value(
            db,
            MOBILE_API_KEY_KEY,
            payload.mobile_api_key.strip(),
            description="Shared mobile API key used by ClawMobile/OpenClaw mobile endpoints",
        )

    return get_app_settings(request, current_user)


@router.get("/mobile-secret")
def reveal_mobile_secret(
    current_user: User = Depends(get_current_active_user),
):
    mobile_key, key_source = get_effective_mobile_gateway_key(
        settings.MOBILE_GATEWAY_API_KEY,
        settings.OPENCLAW_API_KEY,
    )
    if not mobile_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No mobile API key is configured",
        )

    return {
        "user_email": current_user.email,
        "header_name": "X-OpenClaw-API-Key",
        "api_key": mobile_key,
        "api_key_source": key_source,
    }
