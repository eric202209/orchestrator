"""Deterministic Human Guidance selection for Working Memory injection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Dict, Iterable, List, Optional


_SCOPE_BONUS = {
    "project": 25,
    "session": 50,
    "task": 75,
}


def _scope_value(entry: Dict[str, Any]) -> str:
    scope = entry.get("scope") or ""
    return scope.value if hasattr(scope, "value") else str(scope)


def _status_value(entry: Dict[str, Any]) -> str:
    status = entry.get("status") or ""
    return status.value if hasattr(status, "value") else str(status)


def _parse_created_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=UTC)


def _parse_expires_at(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except ValueError:
            return None
    return None


def _message_line(entry: Dict[str, Any]) -> str:
    message = str(entry.get("message") or "")[:200]
    return f"  - {message}" if message else ""


def _rendered_chars(entries: Iterable[Dict[str, Any]]) -> int:
    lines = [_message_line(entry) for entry in entries]
    lines = [line for line in lines if line]
    if not lines:
        return 0
    return len("Operator Guidance\n" + "\n".join(lines))


def guidance_selection_score(entry: Dict[str, Any]) -> int:
    """Return deterministic score for a normalized guidance entry."""
    try:
        priority = int(entry.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0
    try:
        usage_count = int(entry.get("usage_count") or 0)
    except (TypeError, ValueError):
        usage_count = 0
    return (
        priority * 100
        + _SCOPE_BONUS.get(_scope_value(entry), 0)
        + min(max(usage_count, 0), 20)
    )


def select_guidance_for_injection(
    guidance_entries: List[Dict[str, Any]],
    max_chars: int,
) -> Dict[str, Any]:
    """Select guidance deterministically within a rendered character budget.

    Filtering removes archived, disabled, and expired rows. Ordering is score
    DESC, created_at DESC, then id ASC to keep identical rows stable.
    """
    now = datetime.now(UTC)
    candidates: List[Dict[str, Any]] = []

    for entry in guidance_entries or []:
        status = _status_value(entry)
        expires_at = _parse_expires_at(entry.get("expires_at"))
        if status in {"archived", "disabled", "expired"}:
            continue
        if expires_at is not None and expires_at <= now:
            continue
        enriched = dict(entry)
        enriched["selection_score"] = guidance_selection_score(enriched)
        enriched["selection_reason"] = "selected_within_budget"
        candidates.append(enriched)

    def sort_key(entry: Dict[str, Any]) -> tuple:
        raw_id = entry.get("id")
        try:
            stable_id = int(raw_id)
        except (TypeError, ValueError):
            stable_id = 0
        return (
            -int(entry.get("selection_score") or 0),
            -_parse_created_at(entry.get("created_at")).timestamp(),
            stable_id,
        )

    ordered = sorted(candidates, key=sort_key)
    selected: List[Dict[str, Any]] = []
    trimmed: List[Dict[str, Any]] = []

    for entry in ordered:
        candidate = selected + [entry]
        if max_chars > 0 and _rendered_chars(candidate) <= max_chars:
            selected.append(entry)
        else:
            excluded = dict(entry)
            excluded["selection_reason"] = "budget_exceeded"
            trimmed.append(excluded)

    return {
        "selected": selected,
        "trimmed": trimmed,
        "selection_metadata": {
            "max_chars": max_chars,
            "active_count": len(candidates),
            "selected_count": len(selected),
            "trimmed_count": len(trimmed),
            "selected_ids": [entry.get("id") for entry in selected],
            "trimmed_ids": [entry.get("id") for entry in trimmed],
            "scoring_formula": (
                "priority*100 + scope_bonus(project=25, session=50, task=75) "
                "+ min(usage_count, 20)"
            ),
        },
    }
