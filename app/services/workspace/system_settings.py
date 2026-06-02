"""Helpers for runtime-configurable system settings."""

import logging
import os
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db_session
from app.models import SystemSetting

logger = logging.getLogger(__name__)

WORKSPACE_ROOT_KEY = "workspace_root"
# Legacy DB key kept for rows written by older installs. Retire when: (1) a DB
# migration confirms no system_settings rows use this key; (2) all UI/API callers
# read WORKSPACE_ROOT_KEY only; (3) OPENCLAW_WORKSPACE fallback in
# _host_workspace_root_fallback() is also removed.
LEGACY_WORKSPACE_ROOT_KEY = "openclaw_workspace_root"
MOBILE_API_KEY_KEY = "mobile_gateway_api_key"
AGENT_BACKEND_KEY = "orchestrator_agent_backend"
AGENT_MODEL_FAMILY_KEY = "orchestrator_agent_model_family"
ADAPTATION_PROFILE_KEY = "orchestrator_adaptation_profile"
ORCHESTRATION_POLICY_PROFILE_KEY = "orchestration_policy_profile"
WORKSPACE_REVIEW_POLICY_KEY = "workspace_review_policy"
WORKSPACE_REVIEW_POLICIES = {"auto_publish_all", "hold_nontrivial", "hold_all"}
CONTAINER_WORKSPACE_ROOTS = {"/app/projects", "/app"}


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
        # Dual-write keeps the legacy key in sync for older readers.
        # Remove when LEGACY_WORKSPACE_ROOT_KEY retirement criteria are met.
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


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("ORCHESTRATOR_IN_DOCKER") in {
        "1",
        "true",
        "yes",
    }


def _host_workspace_root_fallback() -> str:
    return (
        os.environ.get("HOST_WORKSPACE_ROOT")
        or os.environ.get("WORKSPACE_ROOT")
        or os.environ.get("OPENCLAW_WORKSPACE")
        or "~/.openclaw/workspace/vault/projects/"
    )


def _coerce_workspace_root_for_runtime(value: str) -> str:
    normalized = str(Path(value).expanduser())
    if (
        normalized.rstrip("/") in CONTAINER_WORKSPACE_ROOTS
        and not _running_in_container()
    ):
        return _host_workspace_root_fallback()
    return value


def get_effective_workspace_root(db: Optional[Session] = None) -> Path:
    fallback = _host_workspace_root_fallback()
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
    return Path(_coerce_workspace_root_for_runtime(value)).expanduser().resolve()


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


def classify_model_lane(
    *, backend: str, model_family: str, adaptation_profile: str
) -> Dict[str, Any]:
    backend_normalized = str(backend or "").strip().lower()
    model_normalized = str(model_family or "").strip().lower()
    adaptation_normalized = str(adaptation_profile or "").strip().lower()
    reasons: List[str] = []

    if "openai" in backend_normalized:
        label = "hosted_openai"
        capability_tier = "hosted"
        reasons.append("Hosted OpenAI backend")
    elif "ollama" in backend_normalized:
        label = "local_ollama"
        capability_tier = "local_default"
        reasons.append("Local Ollama backend")
    elif "llama" in backend_normalized:
        label = "local_llama"
        capability_tier = "local_constrained"
        reasons.append("Local llama.cpp-style backend")
    elif "openclaw" in backend_normalized:
        label = "local_openclaw"
        capability_tier = "local_default"
        reasons.append("Local OpenClaw backend")
    else:
        safe_backend = backend_normalized.replace("-", "_") or "unknown"
        label = f"backend_{safe_backend}"
        capability_tier = "unknown"
        reasons.append("Unclassified backend")

    constrained_markers = ("7b", "8b", "13b", "14b", "q4", "q5", "low")
    if any(marker in model_normalized for marker in constrained_markers):
        capability_tier = "local_constrained"
        reasons.append("Model name suggests constrained local capacity")

    if "compact" in adaptation_normalized or "low" in adaptation_normalized:
        capability_tier = "local_constrained"
        reasons.append("Adaptation profile uses compact/low-resource mode")

    capability_traits: Dict[str, Any] = {
        "structured_output_reliability": "unknown",
        "repair_convergence": "unknown",
        "large_context_stability": "unknown",
        "tool_discipline": "unknown",
        "evidence_following": "unknown",
        "latency_cost_class": "unknown",
        "configured_available": False,
    }
    try:
        from app.services.agents.agent_backends import get_backend_descriptor

        descriptor = get_backend_descriptor(backend)
        capability_traits = descriptor.to_dict().get("lane_traits", capability_traits)
    except Exception:
        pass

    return {
        "label": label,
        "capability_tier": capability_tier,
        "backend": backend,
        "model_family": model_family,
        "adaptation_profile": adaptation_profile,
        "capability_traits": capability_traits,
        "reasons": reasons,
    }


def model_lane_snapshot(db: Optional[Session] = None) -> Dict[str, Any]:
    backend = get_effective_agent_backend(settings.AGENT_BACKEND, db=db)
    model_family = get_effective_agent_model_family(settings.AGENT_MODEL, db=db)
    adaptation_profile = get_effective_adaptation_profile(db=db)
    return classify_model_lane(
        backend=backend,
        model_family=model_family,
        adaptation_profile=adaptation_profile,
    )


def diagnose_runtime_lane(db: Optional[Session] = None) -> Dict[str, Any]:
    """Return a structured health verdict for the current runtime lane.

    Checks whether the process is a container or host worker, whether the
    effective workspace root is reachable and writable, and whether any
    DB-persisted project workspace roots conflict with the current runtime.
    """
    in_container = _running_in_container()
    runtime = "container" if in_container else "host"

    # Read raw stored value (before coercion) to detect container-path misconfiguration.
    fallback = os.environ.get(
        "WORKSPACE_ROOT",
        os.environ.get("OPENCLAW_WORKSPACE", "~/.openclaw/workspace/vault/projects/"),
    )
    try:
        _db = db
        _owned_inner = False
        if _db is None:
            _db = get_db_session()
            _owned_inner = True
        try:
            raw_value = get_workspace_root_setting_value(_db, fallback) or fallback
        finally:
            if _owned_inner:
                _db.close()
    except Exception:
        raw_value = fallback

    raw_normalized = str(Path(raw_value).expanduser()).rstrip("/")
    container_path_on_host = (
        not in_container and raw_normalized in CONTAINER_WORKSPACE_ROOTS
    )

    try:
        effective_root = get_effective_workspace_root(db)
        root_str = str(effective_root)
    except Exception as exc:
        return {
            "runtime": runtime,
            "effective_workspace_root": None,
            "workspace_writable": False,
            "container_path_on_host": container_path_on_host,
            "db_conflict_projects": [],
            "verdict": "misconfigured",
            "reasons": [f"Failed to resolve effective workspace root: {exc}"],
        }

    writable = False
    writable_reason: Optional[str] = None
    if effective_root.exists():
        probe: Optional[Path] = None
        try:
            probe = effective_root / f".lane_probe_{secrets.token_hex(8)}"
            with probe.open("x", encoding="utf-8") as handle:
                handle.write("ok\n")
            probe.unlink()
            writable = True
        except OSError as exc:
            writable_reason = str(exc)
            if probe is not None:
                try:
                    if probe.exists():
                        probe.unlink()
                except OSError:
                    pass
    else:
        writable_reason = f"Path does not exist: {root_str}"

    db_conflict_projects: List[Dict[str, Any]] = []
    try:
        from app.models import Project as ProjectModel

        _db = db
        _owned = False
        if _db is None:
            _db = get_db_session()
            _owned = True
        try:
            projects = (
                _db.query(ProjectModel)
                .filter(
                    ProjectModel.deleted_at.is_(None),
                    ProjectModel.workspace_path.isnot(None),
                )
                .all()
            )
            for project in projects:
                ws = str(project.workspace_path or "").strip()
                if not ws:
                    continue
                ws_norm = Path(ws).expanduser()
                # Conflict: project path is under a container root but runtime is host
                if not in_container and any(
                    str(ws_norm).startswith(cr) for cr in CONTAINER_WORKSPACE_ROOTS
                ):
                    db_conflict_projects.append(
                        {"project_id": project.id, "workspace_path": ws}
                    )
        finally:
            if _owned:
                _db.close()
    except Exception:
        pass

    reasons: List[str] = []
    if container_path_on_host:
        reasons.append(
            f"Configured workspace root '{raw_normalized}' is a container-only path "
            "but the process is not running in a container"
        )
    if not writable:
        reasons.append(
            f"Workspace root '{root_str}' is not writable: "
            + (writable_reason or "unknown error")
        )
    if db_conflict_projects:
        ids = [str(p["project_id"]) for p in db_conflict_projects[:5]]
        reasons.append(
            f"Projects with container-path workspace_root running on host "
            f"(project_ids: {', '.join(ids)})"
        )

    if reasons:
        verdict = (
            "misconfigured" if (container_path_on_host or not writable) else "warning"
        )
    else:
        verdict = "ok"

    return {
        "runtime": runtime,
        "effective_workspace_root": root_str,
        "workspace_writable": writable,
        "container_path_on_host": container_path_on_host,
        "db_conflict_projects": db_conflict_projects,
        "verdict": verdict,
        "reasons": reasons,
    }


def emit_runtime_lane_warning() -> None:
    """Log a WARNING if the runtime lane is not healthy. Called at startup."""
    try:
        result = diagnose_runtime_lane()
        if result.get("verdict") != "ok":
            logger.warning(
                "[RUNTIME LANE] verdict=%s runtime=%s workspace_root=%s reasons=%s",
                result.get("verdict"),
                result.get("runtime"),
                result.get("effective_workspace_root"),
                result.get("reasons"),
            )
    except Exception as exc:
        logger.warning("[RUNTIME LANE] Lane diagnosis failed at startup: %s", exc)
