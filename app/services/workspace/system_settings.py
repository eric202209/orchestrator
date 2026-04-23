"""Helpers for runtime-configurable system settings."""

import os
import secrets
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.database import get_db_session
from app.models import SystemSetting

WORKSPACE_ROOT_KEY = "openclaw_workspace_root"
MOBILE_API_KEY_KEY = "mobile_gateway_api_key"
AGENT_BACKEND_KEY = "orchestrator_agent_backend"
AGENT_MODEL_FAMILY_KEY = "orchestrator_agent_model_family"
ORCHESTRATION_POLICY_PROFILE_KEY = "orchestration_policy_profile"


def get_setting_value(
    db: Session, key: str, default: Optional[str] = None
) -> Optional[str]:
    record = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if not record or record.value in {None, ""}:
        return default
    return record.value


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
    return record


def get_setting_value_runtime(
    key: str, default: Optional[str] = None, db: Optional[Session] = None
) -> Optional[str]:
    if db is not None:
        return get_setting_value(db, key, default)

    runtime_db = get_db_session()
    try:
        return get_setting_value(runtime_db, key, default)
    finally:
        runtime_db.close()


def get_effective_workspace_root(db: Optional[Session] = None) -> Path:
    fallback = os.environ.get(
        "OPENCLAW_WORKSPACE", "~/.openclaw/workspace/vault/projects/"
    )
    value = get_setting_value_runtime(WORKSPACE_ROOT_KEY, fallback, db=db) or fallback
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


def get_effective_policy_profile(
    default_profile: str = "balanced", db: Optional[Session] = None
) -> str:
    return (
        get_setting_value_runtime(
            ORCHESTRATION_POLICY_PROFILE_KEY, default_profile, db=db
        )
        or default_profile
    )
