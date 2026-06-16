"""WorkingMemory: persistence (Slice H), rendering (Slice I), injection (Slice J).

All three capabilities are off by default via feature flags.
No prompt injection occurs unless WORKING_MEMORY_INJECTION_ENABLED=True.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings

SCHEMA_VERSION = 1
_FILENAME = "working_memory.json"
_INJECTION_BUDGET = 2000  # max chars injected into project_context
_SUMMARY_STORAGE_LIMIT = 1200  # chars stored per implementation_strategy entry
_SUMMARY_RENDER_LIMIT = (
    600  # chars rendered per implementation_strategy entry (no API Contract)
)
_API_CONTRACT_RENDER_LIMIT = 400  # chars for extracted API Contract block
_SUMMARY_PROSE_RENDER_LIMIT = 120  # chars for prose rendered after API Contract


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _empty_schema(project_dir: str) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project_dir": project_dir,
        "last_updated": "",
        "files_by_task": {},
        "known_good_commands": [],
        "active_constraints": [],
        "human_guidance": [],
        "implementation_strategy": [],
        "unresolved_failures": [],
    }


def _load(openclaw_dir: Path, project_dir: str) -> Dict[str, Any]:
    """Load existing working_memory.json or return empty schema."""
    path = openclaw_dir / _FILENAME
    if path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.loads(fh.read())
            if isinstance(data, dict) and data.get("schema_version") == SCHEMA_VERSION:
                return data
        except Exception:
            pass
    return _empty_schema(project_dir)


def _extract_known_good_commands(orchestration_state: Any) -> List[Dict[str, Any]]:
    """Return per-step command lists from the completed plan."""
    entries = []
    for step in getattr(orchestration_state, "plan", None) or []:
        if not isinstance(step, dict):
            continue
        cmds = [
            c for c in (step.get("commands") or []) if isinstance(c, str) and c.strip()
        ]
        if cmds:
            entries.append({"step": step.get("description", ""), "commands": cmds})
    return entries


def _extract_active_constraints(orchestration_state: Any) -> List[str]:
    """Extract unique rejection reasons from validation_history."""
    seen: set = set()
    out: List[str] = []
    for vh in getattr(orchestration_state, "validation_history", None) or []:
        if not isinstance(vh, dict):
            continue
        for reason in vh.get("reasons") or []:
            if isinstance(reason, str) and reason and reason not in seen:
                seen.add(reason)
                out.append(reason)
    return out[:20]


_HUMAN_GUIDANCE_LIMIT = 10  # max entries stored/rendered


def _extract_operator_guidance(
    db: Any,
    session_id: Any,
    task_id: Any,
) -> List[Dict[str, Any]]:
    """Return [OPERATOR_GUIDANCE] log entries for this session as structured dicts."""
    if db is None:
        return []
    try:
        from app.models import LogEntry

        try:
            numeric_session_id = int(session_id)
        except (TypeError, ValueError):
            return []
        entries = (
            db.query(LogEntry)
            .filter(LogEntry.session_id == numeric_session_id)
            .filter(LogEntry.message.like("[OPERATOR_GUIDANCE]%"))
            .order_by(LogEntry.id.asc())
            .all()
        )
    except Exception:
        return []

    out: List[Dict[str, Any]] = []
    for entry in entries:
        text = str(entry.message or "").replace("[OPERATOR_GUIDANCE]", "", 1).strip()
        if not text:
            continue
        created_at = ""
        try:
            ts = getattr(entry, "created_at", None)
            if ts is not None:
                created_at = ts.isoformat()
        except Exception:
            pass
        out.append(
            {
                "task_id": int(entry.task_id) if entry.task_id is not None else task_id,
                "message": text,
                "created_at": created_at,
                "source": "operator_guidance",
            }
        )
    return out


def _extract_api_contract(summary: str) -> tuple:
    """Split summary into (api_contract_block, prose_remainder).

    Returns ("", summary) when no 'API Contract:' section is found, allowing
    callers to fall back to the existing render path without change.

    The api_contract_block includes the 'API Contract:' header line and all
    subsequent bullet/indented lines. It ends at the first non-empty,
    non-bullet, non-indented line (e.g. 'Changed Files:', 'Verification:').
    prose_remainder is everything else joined together.
    """
    marker = "API Contract:"
    idx = summary.find(marker)
    if idx == -1:
        return "", summary

    rest_lines = summary[idx:].split("\n")
    end_idx = len(rest_lines)
    for i, line in enumerate(rest_lines[1:], 1):
        stripped = line.strip()
        if stripped and not stripped.startswith("-") and line[:1] not in (" ", "\t"):
            end_idx = i
            break

    api_block = "\n".join(rest_lines[:end_idx]).strip()
    before = summary[:idx].strip()
    after = "\n".join(rest_lines[end_idx:]).strip()
    prose = "\n".join(p for p in [before, after] if p)
    return api_block, prose


def _render_content(wm: Dict[str, Any]) -> str:
    """Build the === WORKING MEMORY === block from a loaded schema dict.

    Section order: Operator Guidance first (highest planning priority — must
    survive the 400-char planning context trim), Implementation Strategy second
    (API Contract rendered first within IS section), Constraints last. Known
    Good Commands and Recent Files are stored in working_memory.json but omitted
    from render — they are redundant with workspace truth and progress_notes
    already injected into the planning prompt.

    When the summary contains an 'API Contract:' section, that block is extracted
    and rendered before the prose so critical API keys (failure return, code,
    sentinels) survive the ~400-char planning context trim deterministically.
    """
    lines: List[str] = ["=== WORKING MEMORY ===", ""]

    guidance: List = wm.get("human_guidance") or []
    if guidance:
        lines.append("Operator Guidance")
        for g in guidance[-_HUMAN_GUIDANCE_LIMIT:]:
            if isinstance(g, dict):
                msg = g.get("message", "")[:200]
                if msg:
                    lines.append(f"  - {msg}")
            elif isinstance(g, str):
                lines.append(f"  - {g[:200]}")
        lines.append("")

    strategies: List = wm.get("implementation_strategy") or []
    if strategies:
        lines.append("Implementation Strategy")
        for s in strategies[-2:]:
            if isinstance(s, dict):
                title = s.get("task_title", "")
                summary = s.get("summary", "")
                if title:
                    lines.append(f"  Task: {title}")
                if summary:
                    api_block, prose = _extract_api_contract(summary)
                    if api_block:
                        lines.append(f"  {api_block[:_API_CONTRACT_RENDER_LIMIT]}")
                        if prose:
                            prose_short = prose.strip()
                            if prose_short.startswith("Task Summary:"):
                                prose_short = prose_short[
                                    len("Task Summary:") :
                                ].strip()
                            lines.append(
                                f"  Summary: {prose_short[:_SUMMARY_PROSE_RENDER_LIMIT]}"
                            )
                    else:
                        lines.append(f"  {summary[:_SUMMARY_RENDER_LIMIT]}")
        lines.append("")

    constraints: List = wm.get("active_constraints") or []
    if constraints:
        lines.append("Constraints")
        for c in constraints[-5:]:
            if isinstance(c, dict):
                lines.append(f"  - {c.get('constraint', '')[:100]}")
            elif isinstance(c, str):
                lines.append(f"  - {c[:100]}")
        lines.append("")

    lines.append("=== END WORKING MEMORY ===")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HG-P1b: table-backed guidance helpers
# ---------------------------------------------------------------------------


def _resolve_session_context(db: Any, session_id: Any):
    """Return (project_id, user_id) for a session, or (None, None) on failure."""
    try:
        from app.models import Project
        from app.models import Session as SessionModel

        numeric_sid = int(session_id)
        session = db.query(SessionModel).filter(SessionModel.id == numeric_sid).first()
        if not session:
            return None, None
        project = (
            db.query(Project).filter(Project.id == session.project_id).first()
            if session.project_id
            else None
        )
        return (session.project_id, project.user_id if project else None)
    except Exception:
        return None, None


def _wm_write_table_guidance(
    *,
    db: Any,
    wm: Dict[str, Any],
    session_id: Any,
    task_id: Any,
    logger: Any,
) -> None:
    """HG-P1b: populate wm['human_guidance'] from HumanGuidance table and record usage."""
    try:
        from app.services.human_guidance_service import (
            collect_active_guidance,
            record_guidance_usage,
        )

        project_id, user_id = _resolve_session_context(db, session_id)

        numeric_session_id: Optional[int] = None
        try:
            numeric_session_id = int(session_id)
        except (TypeError, ValueError):
            pass

        numeric_task_id: Optional[int] = None
        try:
            numeric_task_id = int(task_id) if task_id is not None else None
        except (TypeError, ValueError):
            pass

        table_entries = collect_active_guidance(
            db,
            user_id=user_id,
            project_id=project_id,
            session_id=numeric_session_id,
            task_id=numeric_task_id,
        )

        existing_guidance: List[Dict[str, Any]] = wm.get("human_guidance") or []
        seen_messages = {
            g.get("message", "") if isinstance(g, dict) else str(g)
            for g in existing_guidance
        }
        to_add: List[Dict[str, Any]] = []
        for g in table_entries:
            msg = g.get("message", "")
            if msg and msg not in seen_messages:
                seen_messages.add(msg)
                to_add.append(g)

        if to_add:
            all_guidance = existing_guidance + to_add
            wm["human_guidance"] = all_guidance[-_HUMAN_GUIDANCE_LIMIT:]
            rendered_entries = wm["human_guidance"]
        else:
            rendered_entries = []

        if rendered_entries:
            try:
                record_guidance_usage(
                    db,
                    entries=rendered_entries,
                    project_id=project_id,
                    session_id=numeric_session_id,
                    task_id=numeric_task_id,
                )
            except Exception as telemetry_exc:
                logger.warning(
                    "[WORKING_MEMORY] Guidance usage telemetry failed (non-fatal): %s",
                    telemetry_exc,
                )
    except Exception as exc:
        logger.warning(
            "[WORKING_MEMORY] Table-backed guidance write failed (non-fatal): %s", exc
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_working_memory(
    *,
    orchestration_state: Any,
    task: Any,
    summary: str,
    logger: Any,
    db: Any = None,
) -> None:
    """Persist WorkingMemory to .agent/working_memory.json after task success.

    Slice H. Only runs when WORKING_MEMORY_PERSISTENCE_ENABLED=True.
    Never raises — logs warnings on failure.
    """
    if not settings.WORKING_MEMORY_PERSISTENCE_ENABLED:
        return
    try:
        project_dir = getattr(orchestration_state, "project_dir", None)
        if not project_dir:
            return
        openclaw_dir = Path(project_dir) / ".agent"
        openclaw_dir.mkdir(parents=True, exist_ok=True)

        task_id = getattr(task, "id", None) or 0
        task_title = getattr(task, "title", "") or ""
        task_key = str(task_id)

        wm = _load(openclaw_dir, str(project_dir))
        wm["last_updated"] = datetime.now(UTC).isoformat()
        wm["project_dir"] = str(project_dir)

        # files_by_task
        changed = list(getattr(orchestration_state, "changed_files", None) or [])
        wm["files_by_task"][task_key] = {
            "task_id": task_id,
            "task_title": task_title,
            "added": changed,
            "modified": [],
            "deleted": [],
        }

        # known_good_commands
        steps = _extract_known_good_commands(orchestration_state)
        if steps:
            wm["known_good_commands"].append(
                {"task_id": task_id, "task_title": task_title, "steps": steps}
            )

        # active_constraints — deduplicate against existing
        existing_constraints = {
            c.get("constraint", "") if isinstance(c, dict) else str(c)
            for c in (wm.get("active_constraints") or [])
        }
        for reason in _extract_active_constraints(orchestration_state):
            if reason not in existing_constraints:
                existing_constraints.add(reason)
                wm["active_constraints"].append(
                    {
                        "task_id": task_id,
                        "constraint": reason,
                        "source": "validation_rejection",
                    }
                )

        # human_guidance — two paths gated by HUMAN_GUIDANCE_TABLE_ENABLED
        session_id = getattr(orchestration_state, "session_id", None)
        if db is not None and settings.HUMAN_GUIDANCE_TABLE_ENABLED:
            # HG-P1b: table-backed path
            _wm_write_table_guidance(
                db=db,
                wm=wm,
                session_id=session_id,
                task_id=task_id,
                logger=logger,
            )
        elif db is not None:
            # Legacy LogEntry path (default)
            existing_guidance: List[Dict[str, Any]] = wm.get("human_guidance") or []
            seen_messages = {
                g.get("message", "") if isinstance(g, dict) else str(g)
                for g in existing_guidance
            }
            new_entries = _extract_operator_guidance(db, session_id, task_id)
            to_add: List[Dict[str, Any]] = []
            for g in new_entries:
                msg = g.get("message", "")
                if msg and msg not in seen_messages:
                    seen_messages.add(msg)
                    to_add.append(g)
            if to_add:
                all_guidance = existing_guidance + to_add
                wm["human_guidance"] = all_guidance[-_HUMAN_GUIDANCE_LIMIT:]
        elif "human_guidance" not in wm:
            wm["human_guidance"] = []

        # implementation_strategy
        if summary:
            wm["implementation_strategy"].append(
                {
                    "task_id": task_id,
                    "task_title": task_title,
                    "summary": summary[:_SUMMARY_STORAGE_LIMIT],
                }
            )

        path = openclaw_dir / _FILENAME
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(wm, fh, indent=2)
        logger.info("[WORKING_MEMORY] Written to %s", path)
    except Exception as exc:
        logger.warning("[WORKING_MEMORY] Failed to write working memory: %s", exc)


def render_working_memory(project_dir: Any, logger: Any) -> str:
    """Render WorkingMemory to a text block.

    Slice I. Only runs when WORKING_MEMORY_RENDER_ENABLED=True.
    Returns empty string when flag is off or file is absent.
    Does not inject into planner.
    """
    if not settings.WORKING_MEMORY_RENDER_ENABLED:
        return ""
    return _render_working_memory_content(project_dir, logger)


def _render_working_memory_content(project_dir: Any, logger: Any) -> str:
    """Render without checking the feature flag (used by injection path)."""
    try:
        openclaw_dir = Path(str(project_dir)) / ".agent"
        wm = _load(openclaw_dir, str(project_dir))
        if (
            not wm.get("implementation_strategy")
            and not wm.get("active_constraints")
            and not wm.get("human_guidance")
        ):
            return ""
        return _render_content(wm)
    except Exception as exc:
        logger.warning("[WORKING_MEMORY] Failed to render working memory: %s", exc)
        return ""


def inject_working_memory_into_context(
    *,
    orchestration_state: Any,
    task: Any,
    logger: Any,
) -> None:
    """Inject rendered WorkingMemory into project_context for Task 2+ planning.

    Slice J. Only runs when WORKING_MEMORY_INJECTION_ENABLED=True
    and task.plan_position >= 2. Budget-capped. Failure-safe.
    """
    if not settings.WORKING_MEMORY_INJECTION_ENABLED:
        return
    plan_position = getattr(task, "plan_position", None)
    if not plan_position or plan_position < 2:
        return
    try:
        project_dir = getattr(orchestration_state, "project_dir", None)
        if not project_dir:
            return
        rendered = _render_working_memory_content(project_dir, logger)
        if not rendered:
            return
        if len(rendered) > _INJECTION_BUDGET:
            rendered = rendered[:_INJECTION_BUDGET]
        existing = orchestration_state.project_context or ""
        orchestration_state.project_context = (rendered + "\n\n" + existing).strip()
        logger.info(
            "[WORKING_MEMORY] Injected %d chars into project_context (plan_position=%s)",
            len(rendered),
            plan_position,
        )
    except Exception as exc:
        logger.warning("[WORKING_MEMORY] Failed to inject working memory: %s", exc)
