"""HG Hardening Phase 2 — post-write advisory checker.

Scans files changed by a task for Human Guidance violations when structured
P2b plan-time validation was not eligible (local_openclaw inline path or any
backend that produced no structured plan_steps).

Advisory-only: never blocks task completion, never raises.
Persists HumanGuidanceConflict rows with source="post_write_check", severity="advisory".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_POST_WRITE_SOURCE = "post_write_check"
_LOG_PREFIX = "[GUIDANCE_POST_WRITE_WARNING]"
_FILE_SIZE_LIMIT_BYTES = 100 * 1024  # 100 KB

_SKIP_EXTENSIONS = frozenset(
    {
        ".pyc",
        ".pyo",
        ".so",
        ".dll",
        ".exe",
        ".bin",
        ".gz",
        ".zip",
        ".tar",
        ".bz2",
        ".xz",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".svg",
        ".ico",
        ".pdf",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".sqlite",
        ".db",
        ".lock",
        ".pkl",
        ".pickle",
    }
)


def _backend_bypasses_structured_planning(
    plan_steps: Any,
    execution_backend: str,
) -> bool:
    """True when P2b plan-time validation was not eligible for this task."""
    if execution_backend == "local_openclaw":
        return True
    if not plan_steps:
        return True
    return False


def _is_skippable_file(path: Any) -> bool:
    """True if file should be skipped (binary by extension)."""
    suffix = Path(str(path)).suffix.lower()
    return suffix in _SKIP_EXTENSIONS


def _read_file_safe(
    path: Any,
    limit_bytes: int = _FILE_SIZE_LIMIT_BYTES,
) -> Optional[str]:
    """Read file as UTF-8 text. Returns None if binary, too large, or missing."""
    try:
        p = Path(str(path))
        if not p.is_file():
            return None
        if p.stat().st_size > limit_bytes:
            logger.debug(
                "[GUIDANCE_POST_WRITE] skipping large file (%d bytes): %s",
                p.stat().st_size,
                p,
            )
            return None
        return p.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, PermissionError, OSError):
        return None
    except Exception:
        return None


def _get_open_post_write_conflict(
    db: Any,
    guidance_id: Optional[int],
    task_id: Optional[int],
    pattern_name: str,
    project_id: int,
) -> Optional[Any]:
    """Return an existing open post_write_check conflict row for dedup, or None."""
    try:
        from app.models import HumanGuidanceConflict

        return (
            db.query(HumanGuidanceConflict)
            .filter(
                HumanGuidanceConflict.project_id == project_id,
                HumanGuidanceConflict.guidance_id == guidance_id,
                HumanGuidanceConflict.task_id == task_id,
                HumanGuidanceConflict.status == "open",
                HumanGuidanceConflict.source == _POST_WRITE_SOURCE,
                HumanGuidanceConflict.conflict_patterns.like(f'%"{pattern_name}"%'),
            )
            .first()
        )
    except Exception:
        return None


def _collect_guidance_for_post_write(
    db: Any,
    *,
    user_id: Optional[int],
    project_id: Optional[int],
    session_id: Optional[int],
    task_id: Optional[int],
    guidance_backend: str,
    guidance_model_family: str,
) -> List[Dict]:
    """Collect guidance with validation purpose first, fall back to planning."""
    from app.services.human_guidance.service import collect_active_guidance

    entries = collect_active_guidance(
        db,
        user_id=user_id,
        project_id=project_id,
        session_id=session_id,
        task_id=task_id,
        backend=guidance_backend,
        model_family=guidance_model_family,
        purpose="validation",
    )
    if entries:
        return entries
    return collect_active_guidance(
        db,
        user_id=user_id,
        project_id=project_id,
        session_id=session_id,
        task_id=task_id,
        backend=guidance_backend,
        model_family=guidance_model_family,
        purpose="planning",
    )


def run_post_write_guidance_check(
    db: Any,
    *,
    project_id: Optional[int],
    session_id: Optional[int],
    task_id: Optional[int],
    user_id: Optional[int],
    project_dir: Any,
    changed_files: Iterable[str],
    execution_backend: str = "all",
    guidance_backend: str = "all",
    guidance_model_family: str = "all",
    plan_steps: Any = None,
    task_title: str = "",
) -> List[Dict[str, Any]]:
    """Scan changed files for guidance violations. Returns list of advisory conflict dicts.

    Never raises. Non-fatal: any per-file or per-guidance error is logged and skipped.
    """
    from app.services.human_guidance.plan_validator import _PLAN_GUIDANCE_PATTERNS

    try:
        guidance_entries = _collect_guidance_for_post_write(
            db,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            guidance_backend=guidance_backend,
            guidance_model_family=guidance_model_family,
        )
    except Exception as exc:
        logger.warning(
            "%s collect_active_guidance failed (non-fatal): %s", _LOG_PREFIX, exc
        )
        return []

    if not guidance_entries:
        return []

    # Read changed files (deduplicated, skippable filtered out)
    file_contents: Dict[str, str] = {}
    for raw_path in dict.fromkeys(str(p) for p in (changed_files or [])):
        if _is_skippable_file(raw_path):
            continue
        try:
            content = _read_file_safe(raw_path)
        except Exception as exc:
            logger.warning(
                "%s file read error (non-fatal) path=%s: %s", _LOG_PREFIX, raw_path, exc
            )
            continue
        if content is not None:
            file_contents[raw_path] = content

    if not file_contents:
        return []

    results: List[Dict[str, Any]] = []

    for entry in guidance_entries:
        guidance_message = entry.get("message", "")
        guidance_id = entry.get("id")
        guidance_scope = entry.get("scope", "")
        if not guidance_message:
            continue

        guidance_lower = guidance_message.lower()

        for pattern_name, guidance_kws, violation_kws in _PLAN_GUIDANCE_PATTERNS:
            if not any(kw.lower() in guidance_lower for kw in guidance_kws):
                continue

            for file_path, content in file_contents.items():
                content_lower = content.lower()
                matched_kw = next(
                    (kw for kw in violation_kws if kw.lower() in content_lower), None
                )
                if matched_kw is None:
                    continue

                if project_id is not None:
                    try:
                        existing = _get_open_post_write_conflict(
                            db, guidance_id, task_id, pattern_name, project_id
                        )
                        if existing is not None:
                            continue
                    except Exception:
                        pass

                # extract excerpt from file content around matched keyword
                from app.services.human_guidance.conflicts import (
                    _extract_excerpt,
                )

                excerpt = _extract_excerpt(content, matched_kw)
                patterns_json = json.dumps([pattern_name])

                try:
                    if project_id is not None:
                        from app.models import HumanGuidanceConflict

                        db.add(
                            HumanGuidanceConflict(
                                guidance_id=guidance_id,
                                project_id=project_id,
                                session_id=session_id,
                                task_id=task_id,
                                task_title=task_title or "",
                                guidance_scope=guidance_scope,
                                guidance_message=guidance_message,
                                conflict_excerpt=excerpt,
                                conflict_patterns=patterns_json,
                                severity="advisory",
                                status="open",
                                source=_POST_WRITE_SOURCE,
                            )
                        )

                    from app.models import LogEntry

                    log_msg = (
                        f"{_LOG_PREFIX} guidance={guidance_id}"
                        f" task={task_id} pattern={pattern_name}"
                    )
                    db.add(
                        LogEntry(
                            session_id=session_id,
                            task_id=task_id,
                            level="WARNING",
                            message=log_msg,
                            log_metadata=json.dumps(
                                {
                                    "source": _POST_WRITE_SOURCE,
                                    "file": file_path,
                                    "pattern": pattern_name,
                                    "guidance_id": guidance_id,
                                    "matched_keyword": matched_kw,
                                }
                            ),
                        )
                    )
                    db.commit()

                    logger.warning(
                        "%s guidance=%s task=%s pattern=%s file=%s",
                        _LOG_PREFIX,
                        guidance_id,
                        task_id,
                        pattern_name,
                        Path(file_path).name,
                    )
                    results.append(
                        {
                            "source": _POST_WRITE_SOURCE,
                            "severity": "advisory",
                            "guidance_id": guidance_id,
                            "pattern": pattern_name,
                            "file": file_path,
                            "excerpt": excerpt,
                        }
                    )
                except Exception as exc:
                    logger.warning(
                        "%s persist failed (non-fatal) pattern=%s: %s",
                        _LOG_PREFIX,
                        pattern_name,
                        exc,
                    )
                    try:
                        db.rollback()
                    except Exception:
                        pass

    return results


def run_post_write_check_if_enabled(
    ctx: Any,
    *,
    reported_changed_files: List[str],
) -> None:
    """Flag-gated wrapper. Called from completion_flow after write_working_memory.

    Never raises. Logs a warning and returns on any error.
    """
    try:
        from app.config import settings

        if not settings.HUMAN_GUIDANCE_TABLE_ENABLED:
            return
        if not settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED:
            return

        plan_steps = getattr(ctx.orchestration_state, "plan", None) or []
        execution_backend = getattr(ctx, "execution_backend", "all") or "all"

        if not _backend_bypasses_structured_planning(plan_steps, execution_backend):
            return

        try:
            from app.services.human_guidance.activation import (
                check_activation_flag as _check_act,
            )

            if not _check_act(
                ctx.db,
                project_id=getattr(ctx.project, "id", None),
                session_id=ctx.session_id,
                flag="conflict_detection_enabled",
            ):
                return
        except Exception:
            pass  # non-fatal: proceed

        task = ctx.task
        task_title = ""
        if task is not None:
            task_title = str(getattr(task, "title", "") or "")

        run_post_write_guidance_check(
            ctx.db,
            project_id=getattr(ctx.project, "id", None),
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            user_id=getattr(ctx.project, "user_id", None),
            project_dir=ctx.orchestration_state.project_dir,
            changed_files=reported_changed_files,
            execution_backend=execution_backend,
            guidance_backend=getattr(ctx, "guidance_backend", "all") or "all",
            guidance_model_family=getattr(ctx, "guidance_model_family", "all") or "all",
            plan_steps=plan_steps,
            task_title=task_title,
        )
    except Exception as exc:
        try:
            logger.warning(
                "%s run_post_write_check_if_enabled failed (non-fatal): %s",
                _LOG_PREFIX,
                exc,
            )
        except Exception:
            pass
