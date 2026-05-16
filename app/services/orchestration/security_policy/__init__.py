"""Security boundary detection and policy signal helpers.

Command checks are detection-only. Callers decide whether to block, warn, or
route the resulting warning flags through review policy.
"""

from __future__ import annotations

import logging
from typing import Any

from .command_policy import CommandViolation, check_command, is_high_risk
from .path_policy import check_ops_for_secret_paths, is_secret_path
from .retention_policy import (
    RetentionResult,
    SNAPSHOT_MAX_AGE_DAYS,
    SNAPSHOT_MAX_COUNT,
    enforce_snapshot_retention,
)
from .workspace_quota import (
    QuotaViolation,
    WORKSPACE_MAX_CHANGED_FILES,
    WORKSPACE_MAX_FILE_WRITE_BYTES,
    WORKSPACE_QUOTA_MAX_BYTES,
    check_change_set_file_count,
    check_workspace_size,
    check_write_size,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CommandViolation",
    "check_command",
    "is_high_risk",
    "audit_plan_commands",
    "warning_flags_for_security_events",
    "check_ops_for_secret_paths",
    "is_secret_path",
    "QuotaViolation",
    "WORKSPACE_MAX_CHANGED_FILES",
    "WORKSPACE_MAX_FILE_WRITE_BYTES",
    "WORKSPACE_QUOTA_MAX_BYTES",
    "check_change_set_file_count",
    "check_workspace_size",
    "check_write_size",
    "RetentionResult",
    "SNAPSHOT_MAX_AGE_DAYS",
    "SNAPSHOT_MAX_COUNT",
    "enforce_snapshot_retention",
]


def warning_flags_for_security_events(events: list[dict[str, Any]]) -> list[str]:
    """Map audit events to durable review-policy warning flags."""

    flags: set[str] = set()
    for event in events or []:
        pattern = str(event.get("pattern_name") or "")
        risk = str(event.get("risk_level") or "")
        source = str(event.get("source") or "")
        if pattern == "secret_path_write":
            flags.add("secret_path_write")
            continue
        if source == "command" and risk == "high":
            flags.add("security_high_risk_command")
        elif source in {"command", "verification_or_rollback"} and risk == "medium":
            flags.add("security_medium_risk_command")
    return sorted(flags)


def audit_plan_commands(
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Scan every command and op in a normalized plan for security violations.

    Returns a list of event dicts (one per violation) suitable for logging.
    Never raises; detection errors are silently skipped.
    """
    events: list[dict[str, Any]] = []
    for step_index, step in enumerate(plan or [], start=1):
        for cmd in step.get("commands") or []:
            try:
                violations = check_command(str(cmd))
            except Exception:
                continue
            for v in violations:
                events.append(
                    {
                        "step": step_index,
                        "source": "command",
                        "command": str(cmd)[:200],
                        "pattern_name": v.pattern_name,
                        "matched_text": v.matched_text[:100],
                        "risk_level": v.risk_level,
                        "event_code": "security_violation",
                    }
                )

        for cmd in [step.get("verification"), step.get("rollback")]:
            if not cmd:
                continue
            try:
                violations = check_command(str(cmd))
            except Exception:
                continue
            for v in violations:
                events.append(
                    {
                        "step": step_index,
                        "source": "verification_or_rollback",
                        "command": str(cmd)[:200],
                        "pattern_name": v.pattern_name,
                        "matched_text": v.matched_text[:100],
                        "risk_level": v.risk_level,
                        "event_code": "security_violation",
                    }
                )

        secret_paths = check_ops_for_secret_paths(step.get("ops") or [])
        for path in secret_paths:
            events.append(
                {
                    "step": step_index,
                    "source": "file_op",
                    "path": path,
                    "pattern_name": "secret_path_write",
                    "risk_level": "high",
                    "event_code": "security_violation",
                }
            )

    return events
