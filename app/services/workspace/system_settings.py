"""Helpers for runtime-configurable system settings."""

import os
import secrets
from pathlib import Path
from typing import Optional

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.database import get_db_session
from app.models import SystemSetting

WORKSPACE_ROOT_KEY = "workspace_root"
LEGACY_WORKSPACE_ROOT_KEY = "openclaw_workspace_root"
MOBILE_API_KEY_KEY = "mobile_gateway_api_key"
AGENT_BACKEND_KEY = "orchestrator_agent_backend"
AGENT_MODEL_FAMILY_KEY = "orchestrator_agent_model_family"
ADAPTATION_PROFILE_KEY = "orchestrator_adaptation_profile"
ORCHESTRATION_POLICY_PROFILE_KEY = "orchestration_policy_profile"
WORKSPACE_REVIEW_POLICY_KEY = "workspace_review_policy"
WORKSPACE_REVIEW_POLICIES = {"auto_publish_all", "hold_nontrivial", "hold_all"}


def normalize_workspace_review_policy(value: Optional[str]) -> str:
    policy = str(value or "").strip() or "hold_nontrivial"
    if policy not in WORKSPACE_REVIEW_POLICIES:
        raise ValueError(
            "workspace_review_policy must be one of: "
            + ", ".join(sorted(WORKSPACE_REVIEW_POLICIES))
        )
    return policy


def _is_missing_system_settings_table(exc: OperationalError) -> bool:
    return "system_settings" in str(exc).lower() and "no such table" in str(exc).lower()


def get_setting_value(
    db: Session, key: str, default: Optional[str] = None
) -> Optional[str]:
    try:
        record = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    except OperationalError as exc:
        if not _is_missing_system_settings_table(exc):
            raise
        return default
    if not record or record.value in {None, ""}:
        return default
    return record.value


def get_workspace_root_setting_value(
    db: Session, default: Optional[str] = None
) -> Optional[str]:
    value = get_setting_value(db, WORKSPACE_ROOT_KEY)
    if value not in {None, ""}:
        return value
    return get_setting_value(db, LEGACY_WORKSPACE_ROOT_KEY, default)


def set_setting_value(
    db: Session, key: str, value: Optional[str], description: Optional[str] = None
) -> SystemSetting:
    record = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if record is None:
        record = SystemSetting(key=key)
        db.add(record)

    record.value = value
    if description is not None:
        record.description = description
    db.commit()
    db.refresh(record)
    if key == WORKSPACE_ROOT_KEY:
        legacy_record = (
            db.query(SystemSetting)
            .filter(SystemSetting.key == LEGACY_WORKSPACE_ROOT_KEY)
            .first()
        )
        if legacy_record is None:
            legacy_record = SystemSetting(key=LEGACY_WORKSPACE_ROOT_KEY)
            db.add(legacy_record)
        legacy_record.value = value
        if description is not None:
            legacy_record.description = description
        db.commit()
    return record


def get_setting_value_runtime(
    key: str, default: Optional[str] = None, db: Optional[Session] = None
) -> Optional[str]:
    if db is not None:
        return get_setting_value(db, key, default)

    runtime_db = get_db_session()
    try:
        return get_setting_value(runtime_db, key, default)
    except OperationalError as exc:
        if not _is_missing_system_settings_table(exc):
            return default
        return default
    finally:
        runtime_db.close()


def get_effective_workspace_root(db: Optional[Session] = None) -> Path:
    fallback = os.environ.get(
        "WORKSPACE_ROOT",
        os.environ.get("OPENCLAW_WORKSPACE", "~/.openclaw/workspace/vault/projects/"),
    )
    if db is not None:
        value = get_workspace_root_setting_value(db, fallback) or fallback
    else:
        runtime_db = get_db_session()
        try:
            value = get_workspace_root_setting_value(runtime_db, fallback) or fallback
        except OperationalError:
            value = fallback
        finally:
            runtime_db.close()
    return Path(value).expanduser().resolve()


def get_effective_mobile_gateway_key(
    env_mobile_key: str, env_openclaw_key: str, db: Optional[Session] = None
) -> tuple[Optional[str], Optional[str]]:
    override_key = get_setting_value_runtime(MOBILE_API_KEY_KEY, db=db)
    if override_key:
        return override_key, MOBILE_API_KEY_KEY
    if env_mobile_key:
        return env_mobile_key, "MOBILE_GATEWAY_API_KEY"
    if env_openclaw_key:
        return env_openclaw_key, "OPENCLAW_API_KEY"
    return None, None


def generate_mobile_gateway_key() -> str:
    return secrets.token_hex(32)


def get_effective_agent_backend(env_backend: str, db: Optional[Session] = None) -> str:
    return (
        get_setting_value_runtime(AGENT_BACKEND_KEY, env_backend, db=db) or env_backend
    )


def get_effective_agent_model_family(
    env_model_family: str, db: Optional[Session] = None
) -> str:
    return (
        get_setting_value_runtime(AGENT_MODEL_FAMILY_KEY, env_model_family, db=db)
        or env_model_family
    )


def get_effective_adaptation_profile(
    default_profile: str = "openclaw_default", db: Optional[Session] = None
) -> str:
    return (
        get_setting_value_runtime(ADAPTATION_PROFILE_KEY, default_profile, db=db)
        or default_profile
    )


def get_effective_policy_profile(
    default_profile: str = "balanced", db: Optional[Session] = None
) -> str:
    return (
        get_setting_value_runtime(
            ORCHESTRATION_POLICY_PROFILE_KEY, default_profile, db=db
        )
        or default_profile
    )


def get_effective_workspace_review_policy(
    default_policy: str = "hold_nontrivial", db: Optional[Session] = None
) -> str:
    policy = get_setting_value_runtime(
        WORKSPACE_REVIEW_POLICY_KEY, default_policy, db=db
    )
    return normalize_workspace_review_policy(policy)
