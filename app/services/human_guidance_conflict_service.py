"""Human Guidance conflict detection — HG-P1c-2 / HG-P1d.

Heuristic pattern matching between active guidance messages and task descriptions.
Warning-only: no task rejection, no planner mutation, no WM mutation.

HG-P1d: conflicts are persisted to human_guidance_conflicts table.
LogEntry warning events are still written for backward compat/audit.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session as DBSession

from app.models import LogEntry

logger = logging.getLogger(__name__)

_CONFLICT_PREFIX = "[GUIDANCE_CONFLICT_WARNING]"

# (pattern_name, guidance_keywords, task_keywords)
# Guidance must match at least one guidance keyword AND task text must match at least one task keyword.
_PATTERN_PAIRS: List[Tuple[str, List[str], List[str]]] = [
    (
        "stdout_vs_logging",
        ["stdout", "print()"],
        ["logging", "logger", "logger.info", "logging.getLogger"],
    ),
    (
        "mutable_default",
        ["mutable default", "use None", "initialize inside"],
        ["= []", "=[]", "list[str] = []", "dict = {}", "= {}"],
    ),
    (
        "dataclass_vs_dict",
        ["dataclass", "dataclasses"],
        [
            "-> dict",
            "plain dict",
            "plain dictionary",
            "return dictionary",
            "return a dict",
        ],
    ),
    (
        "no_loggers",
        ["never create loggers", "no logging"],
        ["getLogger", "logger ="],
    ),
]


def _contains_any(text: str, keywords: List[str]) -> Optional[str]:
    """Return the first matching keyword (case-insensitive), or None."""
    lower = text.lower()
    for kw in keywords:
        if kw.lower() in lower:
            return kw
    return None


def _extract_excerpt(text: str, keyword: str, context: int = 60) -> str:
    """Return a short excerpt of text surrounding the keyword."""
    lower = text.lower()
    idx = lower.find(keyword.lower())
    if idx == -1:
        return keyword
    start = max(0, idx - context)
    end = min(len(text), idx + len(keyword) + context)
    excerpt = text[start:end].strip()
    if start > 0:
        excerpt = "…" + excerpt
    if end < len(text):
        excerpt = excerpt + "…"
    return excerpt


def _dedup_exists(db: DBSession, session_id: int, dedup_message: str) -> bool:
    """Return True if a conflict LogEntry with this exact message already exists."""
    return (
        db.query(LogEntry)
        .filter(
            LogEntry.session_id == session_id,
            LogEntry.message == dedup_message,
        )
        .first()
        is not None
    )


def _get_open_conflict_row(
    db: DBSession,
    guidance_id: Optional[int],
    task_id: Optional[int],
    pattern_name: str,
    project_id: int,
) -> Optional[Any]:
    """Return an existing open conflict row for dedup, or None."""
    try:
        from app.models import HumanGuidanceConflict

        return (
            db.query(HumanGuidanceConflict)
            .filter(
                HumanGuidanceConflict.project_id == project_id,
                HumanGuidanceConflict.guidance_id == guidance_id,
                HumanGuidanceConflict.task_id == task_id,
                HumanGuidanceConflict.status == "open",
                HumanGuidanceConflict.conflict_patterns.like(f'%"{pattern_name}"%'),
            )
            .first()
        )
    except Exception:
        return None


def _normalize_conflict_row(row: Any) -> Dict[str, Any]:
    """Serialize a HumanGuidanceConflict row to a warning dict."""
    try:
        patterns = json.loads(row.conflict_patterns or "[]")
    except Exception:
        patterns = []
    return {
        "event_type": "guidance_conflict_warning",
        "severity": row.severity,
        "guidance_id": row.guidance_id,
        "guidance_scope": row.guidance_scope,
        "guidance_message": row.guidance_message,
        "task_id": row.task_id,
        "task_title": row.task_title or "",
        "conflict_excerpt": row.conflict_excerpt or "",
        "conflict_patterns": patterns,
        "detected_at": row.detected_at.isoformat() if row.detected_at else None,
        "action": (
            "none — planner receives both; guidance takes precedence per policy"
        ),
    }


def detect_guidance_task_conflicts(
    db: DBSession,
    *,
    project_id: Optional[int],
    session_id: int,
    task_id: Optional[int],
    user_id: Optional[int],
    task_title: str,
    task_description: str,
) -> List[Dict[str, Any]]:
    """Scan active guidance vs task text for heuristic conflicts.

    HG-P1d: persists each new conflict to human_guidance_conflicts (deduped by
    guidance_id + task_id + pattern_name + status=open). Still writes a LogEntry
    warning for backward compat/audit. Never raises.
    """
    try:
        from app.services.human_guidance_service import collect_active_guidance

        guidance_entries = collect_active_guidance(
            db,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
        )
    except Exception as exc:
        logger.warning("[GUIDANCE_CONFLICT] collect_active_guidance failed: %s", exc)
        return []

    if not guidance_entries:
        return []

    task_text = f"{task_title}\n{task_description}".strip()
    if not task_text:
        return []

    warnings: List[Dict[str, Any]] = []
    now = datetime.now(UTC).isoformat()

    for entry in guidance_entries:
        guidance_message = entry.get("message", "")
        guidance_id = entry.get("id")
        guidance_scope = entry.get("scope", "")
        if not guidance_message:
            continue

        for pattern_name, guidance_kws, task_kws in _PATTERN_PAIRS:
            matched_guidance_kw = _contains_any(guidance_message, guidance_kws)
            if not matched_guidance_kw:
                continue
            matched_task_kw = _contains_any(task_text, task_kws)
            if not matched_task_kw:
                continue

            dedup_msg = (
                f"{_CONFLICT_PREFIX} guidance={guidance_id} "
                f"task={task_id} pattern={pattern_name}"
            )

            try:
                if project_id is not None:
                    # Primary dedup: conflict table
                    existing_row = _get_open_conflict_row(
                        db, guidance_id, task_id, pattern_name, project_id
                    )
                    if existing_row is not None:
                        warnings.append(_normalize_conflict_row(existing_row))
                        continue
                else:
                    # Fallback dedup via LogEntry for project-less calls
                    if _dedup_exists(db, session_id, dedup_msg):
                        continue

                excerpt = _extract_excerpt(task_text, matched_task_kw)
                patterns_json = json.dumps([pattern_name])
                payload: Dict[str, Any] = {
                    "event_type": "guidance_conflict_warning",
                    "severity": "warning",
                    "guidance_id": guidance_id,
                    "guidance_scope": guidance_scope,
                    "guidance_message": guidance_message,
                    "task_id": task_id,
                    "task_title": task_title,
                    "conflict_excerpt": excerpt,
                    "conflict_patterns": [pattern_name],
                    "detected_at": now,
                    "action": (
                        "none — planner receives both; "
                        "guidance takes precedence per policy"
                    ),
                }

                if project_id is not None:
                    from app.models import HumanGuidanceConflict

                    db.add(
                        HumanGuidanceConflict(
                            guidance_id=guidance_id,
                            project_id=project_id,
                            session_id=session_id,
                            task_id=task_id,
                            task_title=task_title,
                            guidance_scope=guidance_scope,
                            guidance_message=guidance_message,
                            conflict_excerpt=excerpt,
                            conflict_patterns=patterns_json,
                            severity="warning",
                            status="open",
                            source="heuristic",
                        )
                    )

                db.add(
                    LogEntry(
                        session_id=session_id,
                        task_id=task_id,
                        level="WARNING",
                        message=dedup_msg,
                        log_metadata=json.dumps(payload),
                    )
                )
                db.commit()
                warnings.append(payload)
                logger.warning(
                    "%s guidance=%s task=%s pattern=%s",
                    _CONFLICT_PREFIX,
                    guidance_id,
                    task_id,
                    pattern_name,
                )
            except Exception as exc:
                logger.warning(
                    "[GUIDANCE_CONFLICT] Failed to write warning (non-fatal): %s", exc
                )
                try:
                    db.rollback()
                except Exception:
                    pass

    return warnings


def run_conflict_detection_if_enabled(
    db: DBSession,
    *,
    project_id: Optional[int],
    session_id: int,
    task_id: Optional[int],
    user_id: Optional[int],
    task_title: str,
    task_description: str,
) -> List[Dict[str, Any]]:
    """Flag-gated wrapper. Returns [] without touching DB if either flag is off."""
    from app.config import settings

    if not settings.HUMAN_GUIDANCE_TABLE_ENABLED:
        return []
    if not settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED:
        return []

    # P1f: check per-project/session activation (non-fatal, backward compat: no row = True)
    try:
        from app.services.human_guidance_activation_service import (
            check_activation_flag as _check_act,
        )

        if not _check_act(
            db,
            project_id=project_id,
            session_id=session_id,
            flag="conflict_detection_enabled",
        ):
            return []
    except Exception:
        pass  # non-fatal: proceed with detection

    return detect_guidance_task_conflicts(
        db,
        project_id=project_id,
        session_id=session_id,
        task_id=task_id,
        user_id=user_id,
        task_title=task_title,
        task_description=task_description,
    )
