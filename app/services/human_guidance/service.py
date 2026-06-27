"""Human Guidance service — CRUD (HG-P1a) + active collection and usage telemetry (HG-P1b)."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session as DBSession

from app.models import (
    GuidanceScope,
    GuidanceStatus,
    HumanGuidance,
    HumanGuidanceRevision,
    HumanGuidanceUsage,
)

logger = logging.getLogger(__name__)

_UNSET = object()

# Metadata examples for API/docs consumers. These are not validation allowlists.
VALID_BACKENDS: frozenset[str] = frozenset(
    {
        "all",
        "direct_ollama",
        "llama_cpp",
        "local_openclaw",
        "openai_api",
        "azure_openai",
        "anthropic_api",
        "claude_code",
        "gemini_api",
        "openrouter",
    }
)
VALID_MODELS: frozenset[str] = frozenset(
    {
        "all",
        "qwen",
        "llama",
        "deepseek",
        "mistral",
        "gpt",
        "claude",
        "gemini",
        "codex",
        "unknown",
    }
)


def _parse_targets(raw: Any) -> list[str]:
    """Return a target list from a DB/API value. Defaults to ["all"]."""
    if not raw:
        return ["all"]
    if isinstance(raw, list):
        targets = [str(item).strip().lower() for item in raw if str(item).strip()]
        return targets or ["all"]
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return ["all"]
        targets = [str(item).strip().lower() for item in parsed if str(item).strip()]
        return targets or ["all"]
    except Exception:
        return ["all"]


def _parse_backend_targets(raw: Any) -> list[str]:
    """Return backend_targets list from a DB column value. Defaults to ["all"]."""
    return _parse_targets(raw)


def _parse_model_targets(raw: Any) -> list[str]:
    """Return model_targets list from a DB column value. Defaults to ["all"]."""
    return _parse_targets(raw)


def _parse_purpose_targets(raw: Any) -> list[str]:
    """Return purpose_targets list from a DB column value. Defaults to ["all"]."""
    return _parse_targets(raw)


VALID_PURPOSES: frozenset[str] = frozenset(
    {"all", "planning", "execution", "repair", "validation"}
)


def _model_family_from_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    for family in (
        "qwen",
        "llama",
        "deepseek",
        "mistral",
        "claude",
        "gemini",
        "codex",
        "gpt",
    ):
        if family in text:
            return family
    return text if text in VALID_MODELS else "unknown"


def resolve_guidance_runtime_target(
    *,
    backend: Any = None,
    model_name: Any = None,
    model_family: Any = None,
    runtime_metadata: Optional[Dict[str, Any]] = None,
    planning_backend: Any = None,
    execution_backend: Any = None,
) -> Dict[str, str]:
    """Resolve runtime metadata used by Human Guidance target filters.

    This is metadata-only: it does not verify that a backend is installed or a
    model exists.
    """
    metadata = runtime_metadata or {}
    resolved_backend = (
        str(
            backend
            or metadata.get("backend")
            or planning_backend
            or execution_backend
            or "all"
        )
        .strip()
        .lower()
        or "all"
    )
    resolved_model_name = (
        str(
            model_name
            or metadata.get("model")
            or metadata.get("model_name")
            or metadata.get("model_family")
            or ""
        )
        .strip()
        .lower()
    )
    resolved_model_family = str(model_family or "").strip().lower()
    if not resolved_model_family:
        resolved_model_family = _model_family_from_name(resolved_model_name)
    if not resolved_model_name:
        resolved_model_name = resolved_model_family
    return {
        "backend": resolved_backend,
        "model_name": resolved_model_name or "unknown",
        "model_family": resolved_model_family or "unknown",
    }


def _backend_matches(row: Any, backend: str) -> bool:
    """Return True if a HumanGuidance row targets the given backend."""
    backend = str(backend or "all").strip().lower()
    if backend == "all":
        return True
    targets = _parse_backend_targets(getattr(row, "backend_targets", None))
    return "all" in targets or backend in targets


def _model_matches(row: Any, model_family: str) -> bool:
    """Return True if a HumanGuidance row targets the given model family."""
    model_family = str(model_family or "all").strip().lower()
    if model_family == "all":
        return True
    targets = _parse_model_targets(getattr(row, "model_targets", None))
    return "all" in targets or model_family in targets


def _purpose_matches(row: Any, purpose: str) -> bool:
    """Return True if a HumanGuidance row should be included for the given purpose.

    purpose="all" includes every row (no filtering).
    purpose="planning" includes rows whose purpose_targets contain "all" or "planning".
    """
    purpose = str(purpose or "all").strip().lower()
    if purpose == "all":
        return True
    targets = _parse_purpose_targets(getattr(row, "purpose_targets", None))
    return "all" in targets or purpose in targets


def _get_or_404(db: DBSession, guidance_id: int) -> HumanGuidance:
    g = db.query(HumanGuidance).filter(HumanGuidance.id == guidance_id).first()
    if not g:
        raise HTTPException(status_code=404, detail="guidance_not_found")
    return g


def create_guidance(
    db: DBSession,
    *,
    user_id: Optional[int],
    project_id: Optional[int] = None,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
    scope: str,
    message: str,
    priority: int = 0,
    expires_at: Optional[datetime] = None,
    created_by: Optional[str] = None,
    backend_targets: Optional[List[str]] = None,
    model_targets: Optional[List[str]] = None,
    purpose_targets: Optional[List[str]] = None,
) -> Tuple[HumanGuidance, bool]:
    """Create a guidance entry. Returns (entry, created); created=False on dedup."""
    message = (message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="guidance_empty")
    if len(message) > 500:
        raise HTTPException(status_code=400, detail="message_too_long")
    if priority < 0 or priority > 100:
        raise HTTPException(status_code=400, detail="invalid_priority")

    backend_targets = _parse_targets(backend_targets)
    model_targets = _parse_targets(model_targets)
    purpose_targets = _parse_targets(purpose_targets)

    existing = (
        db.query(HumanGuidance)
        .filter(
            HumanGuidance.user_id == user_id,
            HumanGuidance.message == message,
            HumanGuidance.scope == scope,
            HumanGuidance.project_id == project_id,
            HumanGuidance.session_id == session_id,
            HumanGuidance.task_id == task_id,
            HumanGuidance.status == GuidanceStatus.ACTIVE,
        )
        .first()
    )
    if existing:
        return existing, False

    entry = HumanGuidance(
        user_id=user_id,
        project_id=project_id,
        session_id=session_id,
        task_id=task_id,
        scope=scope,
        message=message,
        status=GuidanceStatus.ACTIVE,
        priority=priority,
        expires_at=expires_at,
        created_by=created_by,
        revision=1,
        backend_targets=backend_targets,
        model_targets=model_targets,
        purpose_targets=purpose_targets,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry, True


def get_guidance(db: DBSession, guidance_id: int) -> HumanGuidance:
    return _get_or_404(db, guidance_id)


def list_guidance(
    db: DBSession,
    *,
    project_id: int,
    status: str = "active",
    scope: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[HumanGuidance], int]:
    query = db.query(HumanGuidance).filter(HumanGuidance.project_id == project_id)
    if status != "all":
        query = query.filter(HumanGuidance.status == status)
    if scope:
        query = query.filter(HumanGuidance.scope == scope)
    total = query.count()
    items = (
        query.order_by(
            HumanGuidance.priority.desc(),
            HumanGuidance.created_at.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    return items, total


def update_guidance(
    db: DBSession,
    guidance_id: int,
    *,
    message=_UNSET,
    status=_UNSET,
    priority=_UNSET,
    expires_at=_UNSET,
    change_reason: Optional[str] = None,
    changed_by: Optional[str] = None,
) -> HumanGuidance:
    entry = _get_or_404(db, guidance_id)
    if entry.status == GuidanceStatus.ARCHIVED:
        raise HTTPException(status_code=400, detail="Cannot update archived guidance")

    now = datetime.now(timezone.utc)

    if message is not _UNSET:
        msg = (message or "").strip()
        if not msg:
            raise HTTPException(status_code=400, detail="guidance_empty")
        if len(msg) > 500:
            raise HTTPException(status_code=400, detail="message_too_long")
        if msg != entry.message:
            rev = HumanGuidanceRevision(
                guidance_id=entry.id,
                revision=entry.revision,
                message=entry.message,
                changed_by=changed_by,
                change_reason=change_reason,
            )
            db.add(rev)
            entry.message = msg
            entry.revision += 1

    if status is not _UNSET:
        allowed = {GuidanceStatus.ACTIVE, GuidanceStatus.DISABLED, "active", "disabled"}
        if status not in allowed:
            raise HTTPException(status_code=422, detail="immutable_field")
        entry.status = status
        if status in (GuidanceStatus.DISABLED, "disabled"):
            entry.disabled_at = now
        else:
            entry.disabled_at = None

    if priority is not _UNSET:
        if priority < 0 or priority > 100:
            raise HTTPException(status_code=400, detail="invalid_priority")
        entry.priority = priority

    if expires_at is not _UNSET:
        entry.expires_at = expires_at

    entry.updated_at = now
    db.commit()
    db.refresh(entry)
    return entry


def archive_guidance(db: DBSession, guidance_id: int) -> HumanGuidance:
    entry = _get_or_404(db, guidance_id)
    if entry.status == GuidanceStatus.ARCHIVED:
        return entry
    entry.status = GuidanceStatus.ARCHIVED
    entry.archived_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(entry)
    return entry


# ── HG-P1b: active-guidance collection ───────────────────────────────────────

_SCOPE_ORDER = case(
    (HumanGuidance.scope == GuidanceScope.TASK, 0),
    (HumanGuidance.scope == GuidanceScope.SESSION, 1),
    (HumanGuidance.scope == GuidanceScope.PROJECT, 2),
    (HumanGuidance.scope == GuidanceScope.GLOBAL, 3),
    else_=4,
)


def collect_active_guidance(
    db: DBSession,
    *,
    user_id: Optional[int],
    project_id: Optional[int],
    session_id: Optional[int],
    task_id: Optional[int],
    now: Optional[datetime] = None,
    backend: str = "all",
    model_family: str = "all",
    purpose: str = "all",
) -> List[Dict[str, Any]]:
    """Return active guidance applicable to this execution context in merge order.

    Scope order (narrowest first): task > session > project > global.
    Within scope: priority DESC, created_at ASC.
    Excludes disabled, archived, expired entries.
    When backend/model_family are specified (not "all"), only returns entries
    whose backend_targets include "all" or backend AND whose model_targets
    include "all" or model_family.
    When purpose is specified (not "all"), only returns entries whose
    purpose_targets include "all" or the given purpose.
    Returns normalized dicts compatible with working_memory.json human_guidance entries.
    """
    if db is None:
        return []
    if now is None:
        now = datetime.now(UTC)

    scope_conditions = [HumanGuidance.scope == GuidanceScope.GLOBAL]
    if project_id is not None:
        scope_conditions.append(
            and_(
                HumanGuidance.scope == GuidanceScope.PROJECT,
                HumanGuidance.project_id == project_id,
            )
        )
    if session_id is not None:
        scope_conditions.append(
            and_(
                HumanGuidance.scope == GuidanceScope.SESSION,
                HumanGuidance.session_id == session_id,
            )
        )
    if task_id is not None:
        scope_conditions.append(
            and_(
                HumanGuidance.scope == GuidanceScope.TASK,
                HumanGuidance.task_id == task_id,
            )
        )

    try:
        rows = (
            db.query(HumanGuidance)
            .filter(
                HumanGuidance.user_id == user_id,
                HumanGuidance.status == GuidanceStatus.ACTIVE,
                or_(
                    HumanGuidance.expires_at.is_(None),
                    HumanGuidance.expires_at > now,
                ),
                or_(*scope_conditions),
            )
            .order_by(
                _SCOPE_ORDER,
                HumanGuidance.priority.desc(),
                HumanGuidance.created_at.asc(),
            )
            .all()
        )
    except Exception as exc:
        logger.warning("collect_active_guidance query failed: %s", exc)
        return []

    rows = [
        r
        for r in rows
        if _backend_matches(r, backend)
        and _model_matches(r, model_family)
        and _purpose_matches(r, purpose)
    ]

    # Fetch usage counts for all matching rows in a single grouped query.
    usage_counts: Dict[int, int] = {}
    if rows:
        row_ids = [r.id for r in rows]
        try:
            usage_rows = (
                db.query(
                    HumanGuidanceUsage.guidance_id,
                    func.count(HumanGuidanceUsage.id).label("cnt"),
                )
                .filter(HumanGuidanceUsage.guidance_id.in_(row_ids))
                .group_by(HumanGuidanceUsage.guidance_id)
                .all()
            )
            usage_counts = {gid: cnt for gid, cnt in usage_rows}
        except Exception:
            usage_counts = {}

    out: List[Dict[str, Any]] = []
    for row in rows:
        usage_count = usage_counts.get(row.id, 0)
        scope_val = row.scope.value if hasattr(row.scope, "value") else str(row.scope)
        status_val = (
            row.status.value if hasattr(row.status, "value") else str(row.status)
        )
        created_at = ""
        try:
            ts = getattr(row, "created_at", None)
            if ts is not None:
                created_at = ts.isoformat()
        except Exception:
            pass
        out.append(
            {
                "id": row.id,
                "task_id": row.task_id,
                "message": row.message,
                "created_at": created_at,
                "source": "operator_guidance",
                "scope": scope_val,
                "status": status_val,
                "priority": row.priority,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "usage_count": int(usage_count),
                "backend_targets": _parse_backend_targets(
                    getattr(row, "backend_targets", None)
                ),
                "model_targets": _parse_model_targets(
                    getattr(row, "model_targets", None)
                ),
                "purpose_targets": _parse_purpose_targets(
                    getattr(row, "purpose_targets", None)
                ),
            }
        )
    return out


def list_global_guidance(
    db: DBSession,
    *,
    user_id: int,
    status: str = "active",
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[HumanGuidance], int]:
    """List global-scope guidance for a user, ordered by priority DESC, created_at ASC."""
    query = db.query(HumanGuidance).filter(
        HumanGuidance.user_id == user_id,
        HumanGuidance.scope == GuidanceScope.GLOBAL,
    )
    if status != "all":
        query = query.filter(HumanGuidance.status == status)
    total = query.count()
    items = (
        query.order_by(
            HumanGuidance.priority.desc(),
            HumanGuidance.created_at.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    return items, total


def get_guidance_history(
    db: DBSession,
    guidance_id: int,
) -> Tuple[HumanGuidance, List["HumanGuidanceRevision"]]:
    """Return (guidance_entry, revisions) ordered by revision ASC. 404 on miss."""
    entry = _get_or_404(db, guidance_id)
    revisions = (
        db.query(HumanGuidanceRevision)
        .filter(HumanGuidanceRevision.guidance_id == guidance_id)
        .order_by(HumanGuidanceRevision.revision.asc())
        .all()
    )
    return entry, revisions


def record_guidance_usage(
    db: DBSession,
    *,
    entries: List[Dict[str, Any]],
    project_id: Optional[int],
    session_id: Optional[int],
    task_id: Optional[int],
    trimmed_entries: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Write HumanGuidanceUsage rows for each guidance entry rendered into WM.

    Never raises — telemetry failures must not fail task completion.
    """
    try:
        for position, entry in enumerate(entries):
            guidance_id = entry.get("id")
            message = entry.get("message", "")
            message_hash = (
                hashlib.md5(message.encode("utf-8")).hexdigest() if message else None
            )
            row = HumanGuidanceUsage(
                guidance_id=guidance_id,
                project_id=project_id,
                session_id=session_id,
                task_id=task_id,
                rendered=True,
                selected=True,
                trimmed=False,
                selection_score=entry.get("selection_score"),
                source="human_guidance_table",
                render_position=position,
                rendered_chars=len(message),
                message_hash=message_hash,
            )
            db.add(row)
        for entry in trimmed_entries or []:
            guidance_id = entry.get("id")
            message = entry.get("message", "")
            message_hash = (
                hashlib.md5(message.encode("utf-8")).hexdigest() if message else None
            )
            row = HumanGuidanceUsage(
                guidance_id=guidance_id,
                project_id=project_id,
                session_id=session_id,
                task_id=task_id,
                rendered=False,
                selected=False,
                trimmed=True,
                selection_score=entry.get("selection_score"),
                source="human_guidance_table",
                render_position=None,
                rendered_chars=0,
                message_hash=message_hash,
            )
            db.add(row)
        db.commit()
    except Exception as exc:
        logger.warning("record_guidance_usage failed (non-fatal): %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
