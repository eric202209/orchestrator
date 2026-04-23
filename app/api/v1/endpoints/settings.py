"""User-facing application settings endpoints."""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth import get_password_hash, verify_password
from app.config import settings
from app.database import get_db
from app.dependencies import get_current_active_user, get_current_admin_user
from app.models import LogEntry, User
from app.schemas import (
    AppSettingsResponse,
    PasswordChangeRequest,
    ProfileUpdateRequest,
    SystemSettingsUpdateRequest,
)
from app.services.agents.agent_backends import (
    UnsupportedAgentBackendError,
    get_backend_descriptor,
    list_supported_backends,
)
from app.services.model_adaptation import (
    get_adaptation_profile,
    list_adaptation_profiles,
)
from app.services.orchestration.policy import (
    get_policy_profile,
    list_policy_profiles,
)
from app.services.workspace.system_settings import (
    ADAPTATION_PROFILE_KEY,
    AGENT_BACKEND_KEY,
    AGENT_MODEL_FAMILY_KEY,
    ORCHESTRATION_POLICY_PROFILE_KEY,
    WORKSPACE_ROOT_KEY,
    get_effective_adaptation_profile,
    get_effective_agent_backend,
    get_effective_agent_model_family,
    get_effective_mobile_gateway_key,
    get_effective_policy_profile,
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


def _log_system_setting_change(
    db: Session,
    *,
    current_user: User,
    changes: dict[str, dict[str, str | None]],
) -> None:
    if not changes:
        return

    db.add(
        LogEntry(
            level="INFO",
            message=f"System settings updated by {current_user.email}",
            log_metadata=json.dumps(
                {
                    "event_type": "system_settings_updated",
                    "actor_user_id": current_user.id,
                    "actor_email": current_user.email,
                    "changes": changes,
                }
            ),
        )
    )
    db.commit()


@router.get("", response_model=AppSettingsResponse)
def get_app_settings(
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    effective_backend_name = get_effective_agent_backend(
        settings.ORCHESTRATOR_AGENT_BACKEND, db=db
    )
    effective_model_family = get_effective_agent_model_family(
        settings.ORCHESTRATOR_AGENT_MODEL_FAMILY, db=db
    )
    effective_adaptation_profile = get_effective_adaptation_profile(db=db)
    effective_policy_profile = get_effective_policy_profile(db=db)
    backend = get_backend_descriptor(effective_backend_name)
    mobile_key, key_source = get_effective_mobile_gateway_key(
        settings.MOBILE_GATEWAY_API_KEY,
        settings.OPENCLAW_API_KEY,
        db=db,
    )
    return {
        "account": {
            "email": current_user.email,
            "name": current_user.name,
        },
        "system": {
            "workspace_root": str(get_effective_workspace_root(db=db)),
            "mobile_base_url": _derive_mobile_base_url(request),
            "mobile_api_key_configured": bool(mobile_key),
            "mobile_api_key_preview": _mask_secret(mobile_key),
            "mobile_api_key_source": key_source,
            "openclaw_gateway_url": settings.OPENCLAW_GATEWAY_URL,
            "agent_backend": backend.name,
            "agent_model_family": effective_model_family,
            "agent_adaptation_profile": get_adaptation_profile(
                effective_adaptation_profile
            ).name,
            "backend_capabilities": backend.capabilities.to_dict(),
            "backend_health": backend.health.to_dict(),
            "supported_backends": [
                descriptor.to_dict() for descriptor in list_supported_backends()
            ],
            "orchestration_policy_profile": get_policy_profile(
                effective_policy_profile
            ).name,
            "available_policy_profiles": [
                profile.to_dict() for profile in list_policy_profiles()
            ],
            "available_adaptation_profiles": [
                profile.to_dict() for profile in list_adaptation_profiles()
            ],
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
    return get_app_settings(request, current_user, db)


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
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    changes: dict[str, dict[str, str | None]] = {}

    if payload.rotate_mobile_api_key or payload.mobile_api_key is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Mobile gateway key rotation is not available from this endpoint "
                "until admin-only controls are implemented"
            ),
        )

    if payload.workspace_root is not None:
        previous_workspace_root = str(get_effective_workspace_root(db=db))
        workspace_root = str(Path(payload.workspace_root).expanduser().resolve())
        set_setting_value(
            db,
            WORKSPACE_ROOT_KEY,
            workspace_root,
            description="OpenClaw workspace root used for orchestration projects",
        )
        if previous_workspace_root != workspace_root:
            changes["workspace_root"] = {
                "from": previous_workspace_root,
                "to": workspace_root,
            }

    if payload.agent_backend is not None:
        previous_backend = get_effective_agent_backend(
            settings.ORCHESTRATOR_AGENT_BACKEND, db=db
        )
        try:
            backend = get_backend_descriptor(payload.agent_backend)
        except UnsupportedAgentBackendError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        if not backend.implemented:
            detail = (
                backend.health.errors[0]
                if backend.health.errors
                else (
                    f"Backend '{backend.name}' is visible for planning and UI work, "
                    "but it cannot be selected for execution yet."
                )
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=detail,
            )
        set_setting_value(
            db,
            AGENT_BACKEND_KEY,
            backend.name,
            description="Operator-selected orchestration backend",
        )
        if previous_backend != backend.name:
            changes["agent_backend"] = {
                "from": previous_backend,
                "to": backend.name,
            }

        if payload.agent_model_family is None:
            previous_model_family = get_effective_agent_model_family(
                settings.ORCHESTRATOR_AGENT_MODEL_FAMILY, db=db
            )
            set_setting_value(
                db,
                AGENT_MODEL_FAMILY_KEY,
                backend.default_model_family,
                description="Operator-selected orchestration model family",
            )
            if previous_model_family != backend.default_model_family:
                changes["agent_model_family"] = {
                    "from": previous_model_family,
                    "to": backend.default_model_family,
                }
        if payload.agent_adaptation_profile is None:
            previous_adaptation = get_effective_adaptation_profile(db=db)
            default_profile = (
                backend.config.adaptation_profiles[0]
                if backend.config.adaptation_profiles
                else "openclaw_default"
            )
            set_setting_value(
                db,
                ADAPTATION_PROFILE_KEY,
                default_profile,
                description="Operator-selected backend/model adaptation profile",
            )
            if previous_adaptation != default_profile:
                changes["agent_adaptation_profile"] = {
                    "from": previous_adaptation,
                    "to": default_profile,
                }

    if payload.agent_model_family is not None:
        previous_model_family = get_effective_agent_model_family(
            settings.ORCHESTRATOR_AGENT_MODEL_FAMILY, db=db
        )
        next_model_family = (
            payload.agent_model_family.strip()
            or settings.ORCHESTRATOR_AGENT_MODEL_FAMILY
        )
        set_setting_value(
            db,
            AGENT_MODEL_FAMILY_KEY,
            next_model_family,
            description="Operator-selected orchestration model family",
        )
        if previous_model_family != next_model_family:
            changes["agent_model_family"] = {
                "from": previous_model_family,
                "to": next_model_family,
            }

    if payload.agent_adaptation_profile is not None:
        previous_adaptation = get_effective_adaptation_profile(db=db)
        profile = get_adaptation_profile(payload.agent_adaptation_profile)
        effective_backend_name = (
            payload.agent_backend
            if payload.agent_backend is not None
            else get_effective_agent_backend(settings.ORCHESTRATOR_AGENT_BACKEND, db=db)
        )
        try:
            backend = get_backend_descriptor(effective_backend_name)
        except UnsupportedAgentBackendError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        if profile.name not in backend.config.adaptation_profiles:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Adaptation profile '{profile.name}' is not supported by backend "
                    f"'{backend.name}'"
                ),
            )
        set_setting_value(
            db,
            ADAPTATION_PROFILE_KEY,
            profile.name,
            description="Operator-selected backend/model adaptation profile",
        )
        if previous_adaptation != profile.name:
            changes["agent_adaptation_profile"] = {
                "from": previous_adaptation,
                "to": profile.name,
            }

    if payload.orchestration_policy_profile is not None:
        previous_policy = get_effective_policy_profile(db=db)
        profile = get_policy_profile(payload.orchestration_policy_profile)
        set_setting_value(
            db,
            ORCHESTRATION_POLICY_PROFILE_KEY,
            profile.name,
            description="Operator-selected orchestration policy profile",
        )
        if previous_policy != profile.name:
            changes["orchestration_policy_profile"] = {
                "from": previous_policy,
                "to": profile.name,
            }

    _log_system_setting_change(db, current_user=current_user, changes=changes)

    return get_app_settings(request, current_user, db)


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
        "api_key_preview": _mask_secret(mobile_key),
        "api_key_source": key_source,
        "detail": "Raw mobile gateway secrets are not returned by the API",
    }
