"""Human Guidance activation controls — HG-P1e.

Per-project and per-session activation records for Human Guidance table mode,
WM persistence/render/injection, and conflict detection.

Global process flags remain hard upper bounds: even if activation says "on",
the feature is off if the corresponding flag is False.

No runtime behavior is changed by this module — activation reports readiness
and tracks operator intent. Actual write_working_memory and planner injection
remain gated by the same process flags as before HG-P1e.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session as DBSession

logger = logging.getLogger(__name__)

_WIRED_FLAGS = frozenset(
    {"table_enabled", "injection_enabled", "conflict_detection_enabled"}
)

_CRITICAL_BLOCKERS = frozenset(
    {
        "global_table_flag_off",
        "activation_disabled",
        "no_active_guidance",
        "migration_missing",
        "table_query_failed",
    }
)


def _row_to_flags(row: Any) -> Dict[str, Any]:
    return {
        "table_enabled": bool(row.table_enabled),
        "persistence_enabled": bool(row.persistence_enabled),
        "render_enabled": bool(row.render_enabled),
        "injection_enabled": bool(row.injection_enabled),
        "conflict_detection_enabled": bool(row.conflict_detection_enabled),
        "status": row.status,
    }


def _disabled_flags() -> Dict[str, Any]:
    return {
        "table_enabled": False,
        "persistence_enabled": False,
        "render_enabled": False,
        "injection_enabled": False,
        "conflict_detection_enabled": False,
        "status": "disabled",
    }


def _apply_global_bounds(requested: Dict[str, Any]) -> Dict[str, Any]:
    """AND requested flags with process-level flags."""
    from app.config import settings

    return {
        "table_enabled": requested["table_enabled"]
        and settings.HUMAN_GUIDANCE_TABLE_ENABLED,
        "persistence_enabled": (
            requested["persistence_enabled"]
            and settings.WORKING_MEMORY_PERSISTENCE_ENABLED
        ),
        "render_enabled": requested["render_enabled"]
        and settings.WORKING_MEMORY_RENDER_ENABLED,
        "injection_enabled": (
            requested["injection_enabled"] and settings.WORKING_MEMORY_INJECTION_ENABLED
        ),
        "conflict_detection_enabled": (
            requested["conflict_detection_enabled"]
            and settings.HUMAN_GUIDANCE_TABLE_ENABLED
            and settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED
        ),
        "status": requested["status"],
    }


def get_effective_activation(
    db: DBSession,
    *,
    project_id: Optional[int],
    session_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Return requested and effective activation for a project/session pair.

    Layer order:
    1. Default: all disabled.
    2. Project activation (if enabled row exists).
    3. Session activation overrides project (whether enabled or disabled).
    4. Global process flags AND-ed on top.
    """
    from app.models import HumanGuidanceActivation

    requested = _disabled_flags()

    if project_id is not None:
        try:
            project_row = (
                db.query(HumanGuidanceActivation)
                .filter(
                    HumanGuidanceActivation.project_id == project_id,
                    HumanGuidanceActivation.scope == "project",
                )
                .first()
            )
            if project_row and project_row.status == "enabled":
                requested = _row_to_flags(project_row)
        except Exception as exc:
            logger.warning("[HGA] project activation query failed: %s", exc)

    if session_id is not None:
        try:
            session_row = (
                db.query(HumanGuidanceActivation)
                .filter(
                    HumanGuidanceActivation.session_id == session_id,
                    HumanGuidanceActivation.scope == "session",
                )
                .first()
            )
            if session_row is not None:
                requested = (
                    _row_to_flags(session_row)
                    if session_row.status == "enabled"
                    else _disabled_flags()
                )
        except Exception as exc:
            logger.warning("[HGA] session activation query failed: %s", exc)

    return {"requested": requested, "effective": _apply_global_bounds(requested)}


def check_activation_flag(
    db: DBSession,
    *,
    project_id: Optional[int],
    session_id: Optional[int] = None,
    flag: str,
) -> bool:
    """Return True if the named flag is effectively enabled for this project/session.

    Backward-compat: if no activation row exists at all, returns True (runtime
    falls back to global flag alone, identical to pre-P1f behavior). Returns
    False only when an activation row explicitly disables the flag or activation
    is disabled entirely.

    Non-fatal: returns True on any DB error.
    """
    from app.models import HumanGuidanceActivation

    try:
        has_project_row = project_id is not None and (
            db.query(HumanGuidanceActivation)
            .filter(
                HumanGuidanceActivation.project_id == project_id,
                HumanGuidanceActivation.scope == "project",
            )
            .first()
            is not None
        )
        has_session_row = session_id is not None and (
            db.query(HumanGuidanceActivation)
            .filter(
                HumanGuidanceActivation.session_id == session_id,
                HumanGuidanceActivation.scope == "session",
            )
            .first()
            is not None
        )
        if not has_project_row and not has_session_row:
            # No activation row: global flag controls (backward compat)
            return True
        activation = get_effective_activation(
            db, project_id=project_id, session_id=session_id
        )
        return bool(activation["effective"].get(flag, False))
    except Exception as exc:
        logger.warning("[HGA] check_activation_flag error (non-fatal): %s", exc)
        return True


def set_project_activation(
    db: DBSession,
    project_id: int,
    flags: Dict[str, Any],
    *,
    enabled_by: Optional[str] = None,
) -> Any:
    """Upsert project-scope activation. Sets status=enabled."""
    from app.models import HumanGuidanceActivation

    row = (
        db.query(HumanGuidanceActivation)
        .filter(
            HumanGuidanceActivation.project_id == project_id,
            HumanGuidanceActivation.scope == "project",
        )
        .first()
    )
    if row is None:
        row = HumanGuidanceActivation(project_id=project_id, scope="project")
        db.add(row)

    row.table_enabled = bool(flags.get("table_enabled", False))
    row.persistence_enabled = bool(flags.get("persistence_enabled", False))
    row.render_enabled = bool(flags.get("render_enabled", False))
    row.injection_enabled = bool(flags.get("injection_enabled", False))
    row.conflict_detection_enabled = bool(
        flags.get("conflict_detection_enabled", False)
    )
    row.status = "enabled"
    row.enabled_by = enabled_by
    row.disabled_at = None
    row.disabled_by = None
    db.commit()
    db.refresh(row)
    return row


def set_session_activation(
    db: DBSession,
    session_id: int,
    project_id: Optional[int],
    flags: Dict[str, Any],
    *,
    enabled_by: Optional[str] = None,
) -> Any:
    """Upsert session-scope activation. Sets status=enabled."""
    from app.models import HumanGuidanceActivation

    row = (
        db.query(HumanGuidanceActivation)
        .filter(
            HumanGuidanceActivation.session_id == session_id,
            HumanGuidanceActivation.scope == "session",
        )
        .first()
    )
    if row is None:
        row = HumanGuidanceActivation(
            session_id=session_id, project_id=project_id, scope="session"
        )
        db.add(row)

    row.table_enabled = bool(flags.get("table_enabled", False))
    row.persistence_enabled = bool(flags.get("persistence_enabled", False))
    row.render_enabled = bool(flags.get("render_enabled", False))
    row.injection_enabled = bool(flags.get("injection_enabled", False))
    row.conflict_detection_enabled = bool(
        flags.get("conflict_detection_enabled", False)
    )
    row.status = "enabled"
    row.enabled_by = enabled_by
    row.disabled_at = None
    row.disabled_by = None
    db.commit()
    db.refresh(row)
    return row


def disable_activation(
    db: DBSession,
    scope: str,
    entity_id: int,
    *,
    disabled_by: Optional[str] = None,
) -> Any:
    """Set status=disabled for a project or session activation. Creates the row if missing."""
    from app.models import HumanGuidanceActivation

    if scope == "project":
        row = (
            db.query(HumanGuidanceActivation)
            .filter(
                HumanGuidanceActivation.project_id == entity_id,
                HumanGuidanceActivation.scope == "project",
            )
            .first()
        )
        if row is None:
            row = HumanGuidanceActivation(project_id=entity_id, scope="project")
            db.add(row)
    else:
        row = (
            db.query(HumanGuidanceActivation)
            .filter(
                HumanGuidanceActivation.session_id == entity_id,
                HumanGuidanceActivation.scope == "session",
            )
            .first()
        )
        if row is None:
            row = HumanGuidanceActivation(session_id=entity_id, scope="session")
            db.add(row)

    row.status = "disabled"
    row.disabled_at = datetime.now(UTC)
    row.disabled_by = disabled_by
    db.commit()
    db.refresh(row)
    return row


def readiness_status(
    db: DBSession,
    *,
    project_id: int,
    session_id: Optional[int] = None,
    backend: str = "all",
    model_family: str = "all",
) -> Dict[str, Any]:
    """Compute Human Guidance readiness for a project (optionally session-scoped).

    ready=True requires:
    - HUMAN_GUIDANCE_TABLE_ENABLED is True (global flag)
    - activation status is "enabled" at project or session scope
    - at least one active guidance entry exists for the project
    - no DB errors querying the tables

    WM pipeline flags (persistence/render/injection) are reported in
    blocking_reasons when requested but off, but do not block ready.
    """
    from app.config import settings
    from app.models import GuidanceStatus, HumanGuidance, Project

    global_flags = {
        "HUMAN_GUIDANCE_TABLE_ENABLED": settings.HUMAN_GUIDANCE_TABLE_ENABLED,
        "WORKING_MEMORY_PERSISTENCE_ENABLED": settings.WORKING_MEMORY_PERSISTENCE_ENABLED,
        "WORKING_MEMORY_RENDER_ENABLED": settings.WORKING_MEMORY_RENDER_ENABLED,
        "WORKING_MEMORY_INJECTION_ENABLED": settings.WORKING_MEMORY_INJECTION_ENABLED,
        "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED": settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED,
    }

    blocking_reasons = []
    activation = None
    guidance_statistics = {
        "active_guidance": 0,
        "selected_guidance": 0,
        "trimmed_guidance": 0,
    }
    purpose_statistics: Dict[str, int] = {
        "planning": 0,
        "execution": 0,
        "repair": 0,
        "validation": 0,
        "all": 0,
    }

    try:
        activation = get_effective_activation(
            db, project_id=project_id, session_id=session_id
        )
    except Exception as exc:
        logger.warning("[HGA] readiness query failed: %s", exc)
        blocking_reasons.append("table_query_failed")
        return {
            "project_id": project_id,
            "session_id": session_id,
            "requested": None,
            "effective": None,
            "global_flags": global_flags,
            "guidance_statistics": guidance_statistics,
            "purpose_statistics": purpose_statistics,
            "ready": False,
            "blocking_reasons": blocking_reasons,
        }

    if not settings.HUMAN_GUIDANCE_TABLE_ENABLED:
        blocking_reasons.append("global_table_flag_off")

    if activation["requested"]["status"] == "disabled":
        blocking_reasons.append("activation_disabled")

    # WM pipeline flags — informational, reported when requested but off
    if (
        activation["requested"]["persistence_enabled"]
        and not settings.WORKING_MEMORY_PERSISTENCE_ENABLED
    ):
        blocking_reasons.append("wm_persistence_flag_off")
    if (
        activation["requested"]["render_enabled"]
        and not settings.WORKING_MEMORY_RENDER_ENABLED
    ):
        blocking_reasons.append("wm_render_flag_off")
    if (
        activation["requested"]["injection_enabled"]
        and not settings.WORKING_MEMORY_INJECTION_ENABLED
    ):
        blocking_reasons.append("wm_injection_flag_off")

    # Active guidance check
    try:
        count = (
            db.query(HumanGuidance)
            .filter(
                HumanGuidance.project_id == project_id,
                HumanGuidance.status == GuidanceStatus.ACTIVE,
            )
            .count()
        )
        if count == 0:
            blocking_reasons.append("no_active_guidance")
    except Exception as exc:
        logger.warning("[HGA] guidance count failed: %s", exc)
        blocking_reasons.append("table_query_failed")

    backend_statistics: Dict[str, Any] = {
        "backend": backend,
        "model_family": model_family,
        "matching_guidance": 0,
        "filtered_guidance": 0,
    }
    purpose_statistics: Dict[str, int] = {
        "planning": 0,
        "execution": 0,
        "repair": 0,
        "validation": 0,
        "all": 0,
    }
    try:
        from app.services.human_guidance.service import collect_active_guidance
        from app.services.human_guidance.selection import (
            select_guidance_for_injection,
        )
        from app.services.orchestration.working_memory import _INJECTION_BUDGET

        project = db.query(Project).filter(Project.id == project_id).first()
        user_id = getattr(project, "user_id", None)
        all_entries = collect_active_guidance(
            db,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            task_id=None,
            backend="all",
            model_family="all",
        )
        if backend == "all" and model_family == "all":
            entries = all_entries
            backend_statistics["matching_guidance"] = len(all_entries)
            backend_statistics["filtered_guidance"] = 0
        else:
            entries = collect_active_guidance(
                db,
                user_id=user_id,
                project_id=project_id,
                session_id=session_id,
                task_id=None,
                backend=backend,
                model_family=model_family,
            )
            backend_statistics["matching_guidance"] = len(entries)
            backend_statistics["filtered_guidance"] = len(all_entries) - len(entries)
        selection = select_guidance_for_injection(entries, _INJECTION_BUDGET)
        guidance_statistics = {
            "active_guidance": selection["selection_metadata"]["active_count"],
            "selected_guidance": selection["selection_metadata"]["selected_count"],
            "trimmed_guidance": selection["selection_metadata"]["trimmed_count"],
        }

        # Count guidance per purpose using the full (unfiltered) entry set
        from app.services.human_guidance.service import _parse_purpose_targets

        for entry in all_entries:
            pts = _parse_purpose_targets(entry.get("purpose_targets"))
            for pt in pts:
                if pt in purpose_statistics:
                    purpose_statistics[pt] += 1
    except Exception as exc:
        logger.warning("[HGA] guidance statistics failed: %s", exc)

    ready = not any(r in _CRITICAL_BLOCKERS for r in blocking_reasons)

    # P1f: runtime_effective — what the runtime ACTUALLY uses (backward compat: no row = global flag)
    runtime_effective = _compute_runtime_effective(
        db,
        project_id=project_id,
        session_id=session_id,
        activation=activation,
        settings=settings,
    )

    return {
        "project_id": project_id,
        "session_id": session_id,
        "requested": activation["requested"],
        "effective": activation["effective"],
        "runtime_effective": runtime_effective,
        "global_flags": global_flags,
        "guidance_statistics": guidance_statistics,
        "backend_statistics": backend_statistics,
        "purpose_statistics": purpose_statistics,
        "ready": ready,
        "blocking_reasons": blocking_reasons,
    }


def _compute_runtime_effective(
    db: DBSession,
    *,
    project_id: Optional[int],
    session_id: Optional[int],
    activation: Dict[str, Any],
    settings: Any,
) -> Dict[str, Any]:
    """Compute flags that match the actual runtime gating decisions.

    When no activation row exists → runtime uses global flags only (backward compat).
    When a row exists → runtime uses effective activation (same as ``effective``).
    The ``mode`` field indicates which branch applies.
    """
    from app.models import HumanGuidanceActivation

    try:
        has_project_row = project_id is not None and (
            db.query(HumanGuidanceActivation)
            .filter(
                HumanGuidanceActivation.project_id == project_id,
                HumanGuidanceActivation.scope == "project",
            )
            .first()
            is not None
        )
        has_session_row = session_id is not None and (
            db.query(HumanGuidanceActivation)
            .filter(
                HumanGuidanceActivation.session_id == session_id,
                HumanGuidanceActivation.scope == "session",
            )
            .first()
            is not None
        )
    except Exception:
        has_project_row = False
        has_session_row = False

    if not has_project_row and not has_session_row:
        # No activation row: global flags control runtime (backward compat mode)
        return {
            "table_enabled": settings.HUMAN_GUIDANCE_TABLE_ENABLED,
            "persistence_enabled": settings.WORKING_MEMORY_PERSISTENCE_ENABLED,
            "render_enabled": settings.WORKING_MEMORY_RENDER_ENABLED,
            "injection_enabled": settings.WORKING_MEMORY_INJECTION_ENABLED,
            "conflict_detection_enabled": (
                settings.HUMAN_GUIDANCE_TABLE_ENABLED
                and settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED
            ),
            "mode": "global_flag_only",
        }

    # Activation row present: runtime uses effective activation
    eff = activation["effective"]
    return {
        "table_enabled": eff["table_enabled"],
        "persistence_enabled": eff["persistence_enabled"],
        "render_enabled": eff["render_enabled"],
        "injection_enabled": eff["injection_enabled"],
        "conflict_detection_enabled": eff["conflict_detection_enabled"],
        "mode": "activation_controlled",
    }
