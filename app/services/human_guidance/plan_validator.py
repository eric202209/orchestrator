"""Human Guidance plan validation — HG-P2b.

Post-planning validator: scans generated plan step ops for content that violates
active guidance patterns. Deterministic; does not call the LLM.

Does NOT touch HG storage, selection, activation, or conflict persistence.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_REPAIR_GUIDANCE_HEADER = "## OPERATOR GUIDANCE (mandatory)"
_REPAIR_GUIDANCE_AUTHORITY = "These Human Guidance rules are mandatory unless a validator/safety rule forbids them."
_REPAIR_GUIDANCE_MAX_CHARS = 600

# (pattern_name, guidance_keywords, plan_violation_keywords)
# Same pattern_names as human_guidance_conflict_service._PATTERN_PAIRS for consistency.
_PLAN_GUIDANCE_PATTERNS: List[Tuple[str, List[str], List[str]]] = [
    (
        "mutable_default",
        ["mutable default", "use None", "initialize inside"],
        ["= []", "=[]", "= {}"],
    ),
    (
        "stdout_vs_logging",
        ["stdout", "print()"],
        ["import logging", "logging.getLogger", "logger ="],
    ),
]


def _extract_plan_write_content(plan_steps: List[Dict]) -> str:
    """Concatenate write_file content and replace_in_file new text from all plan steps."""
    parts: List[str] = []
    for step in plan_steps or []:
        if not isinstance(step, dict):
            continue
        for op in step.get("ops") or []:
            if not isinstance(op, dict):
                continue
            op_type = op.get("op")
            if op_type == "write_file":
                content = op.get("content") or ""
                if content:
                    parts.append(content)
            elif op_type == "replace_in_file":
                new_text = op.get("new") or ""
                if new_text:
                    parts.append(new_text)
    return "\n".join(parts)


def validate_plan_against_guidance(
    plan_steps: List[Dict],
    guidance_entries: List[Dict],
) -> List[str]:
    """Return violation strings for any active guidance rule violated by the plan.

    Each string is human-readable and suitable for use as a repair rejection reason.
    Returns an empty list when the plan complies with all active guidance.
    """
    if not plan_steps or not guidance_entries:
        return []

    plan_content = _extract_plan_write_content(plan_steps)
    if not plan_content:
        return []

    plan_lower = plan_content.lower()
    violations: List[str] = []

    for entry in guidance_entries:
        guidance_message = entry.get("message", "")
        if not guidance_message:
            continue
        guidance_lower = guidance_message.lower()

        for pattern_name, guidance_kws, plan_kws in _PLAN_GUIDANCE_PATTERNS:
            if not any(kw.lower() in guidance_lower for kw in guidance_kws):
                continue
            matched_kw = next((kw for kw in plan_kws if kw.lower() in plan_lower), None)
            if matched_kw is None:
                continue
            violations.append(
                f"{pattern_name}: plan writes '{matched_kw}' which violates "
                f"Operator Guidance: {guidance_message}"
            )

    return violations


def check_plan_guidance_violations_if_enabled(
    db: Any,
    *,
    project_id: Optional[int],
    session_id: int,
    task_id: Optional[int],
    user_id: Optional[int],
    plan_steps: List[Dict],
    backend: str = "all",
    model_family: str = "all",
    purpose: str = "planning",
) -> List[str]:
    """Flag-gated wrapper. Returns [] when flags are off, guidance unavailable, or no violations.

    Never raises — all errors are logged and swallowed so planning flow is unaffected.
    """
    try:
        from app.config import settings

        if not settings.HUMAN_GUIDANCE_TABLE_ENABLED:
            return []
        if not settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED:
            return []

        try:
            from app.services.human_guidance.activation import (
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
            pass  # non-fatal: proceed

        from app.services.human_guidance.service import collect_active_guidance

        guidance_entries = collect_active_guidance(
            db,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            backend=backend,
            model_family=model_family,
            purpose=purpose,
        )
        logger.debug(
            "[GUIDANCE_PLAN_VALIDATION] collected purpose=%s candidates=%d",
            purpose,
            len(guidance_entries),
        )
        if not guidance_entries:
            return []

        return validate_plan_against_guidance(plan_steps, guidance_entries)

    except Exception as exc:
        logger.warning("[GUIDANCE_PLAN_VALIDATION] Failed (non-fatal): %s", exc)
        return []


def render_active_guidance_for_repair(
    db: Any,
    *,
    project_id: Optional[int],
    session_id: int,
    task_id: Optional[int],
    user_id: Optional[int],
    backend: str = "all",
    model_family: str = "all",
    purpose: str = "repair",
) -> str:
    """Render active guidance as a concise block for inclusion in planning repair prompts.

    Returns empty string when HG is disabled, no guidance exists, or any error occurs.
    Never raises.
    """
    try:
        from app.config import settings

        if not settings.HUMAN_GUIDANCE_TABLE_ENABLED:
            return ""

        try:
            from app.services.human_guidance.activation import (
                check_activation_flag as _check_act,
            )

            if not _check_act(
                db,
                project_id=project_id,
                session_id=session_id,
                flag="injection_enabled",
            ):
                return ""
        except Exception:
            pass  # non-fatal: proceed

        from app.services.human_guidance.service import collect_active_guidance

        guidance_entries = collect_active_guidance(
            db,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            backend=backend,
            model_family=model_family,
            purpose=purpose,
        )
        logger.debug(
            "[GUIDANCE_REPAIR_RENDER] collected purpose=%s candidates=%d",
            purpose,
            len(guidance_entries),
        )
        if not guidance_entries:
            return ""

        lines = [_REPAIR_GUIDANCE_HEADER, _REPAIR_GUIDANCE_AUTHORITY]
        for entry in guidance_entries:
            message = str(entry.get("message") or "").strip()
            if message:
                lines.append(f"- {message}")
        if len(lines) <= 2:
            return ""
        return "\n".join(lines)[:_REPAIR_GUIDANCE_MAX_CHARS]

    except Exception as exc:
        logger.warning("[GUIDANCE_REPAIR_RENDER] Failed (non-fatal): %s", exc)
        return ""
