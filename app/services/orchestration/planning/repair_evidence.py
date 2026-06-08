"""Diagnostic-only evidence capture for failed planning repair arbitration."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.services.workspace.permissions import ensure_shared_permissions


_PendingKey = Tuple[str, int, int, int]
_PENDING_TRIPLETS: Dict[_PendingKey, Dict[str, Any]] = {}

_SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|authorization)", re.I)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization)\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|secret|password|authorization)\s*[:=]\s*([^\s,;]+)"
    ),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def record_pending_planning_repair_triplet(
    *,
    project_dir: Any,
    session_id: Optional[int],
    task_id: Optional[int],
    repair_attempt: int,
    previous_plan_text: str,
    repair_prompt: str,
    repaired_plan_text: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Keep a repair triplet in memory until arbitration decides it failed."""

    if session_id is None or task_id is None:
        return
    key = _pending_key(project_dir, session_id, task_id, repair_attempt)
    _PENDING_TRIPLETS[key] = {
        "project_dir": str(project_dir),
        "session_id": session_id,
        "task_id": task_id,
        "repair_attempt": repair_attempt,
        "previous_plan_text": previous_plan_text,
        "repair_prompt": repair_prompt,
        "repaired_plan_text": repaired_plan_text,
        "metadata": dict(metadata or {}),
        "captured_at": datetime.now(UTC).isoformat(),
    }


def write_failed_planning_repair_triplet(
    *,
    project_dir: Any,
    session_id: int,
    task_id: int,
    repair_attempt: int,
    previous_plan: Any,
    repaired_plan: Any,
    repaired_output_text: str,
    arbitration: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Persist a redacted triplet artifact for failed repair arbitration."""

    key = _pending_key(project_dir, session_id, task_id, repair_attempt)
    pending = _PENDING_TRIPLETS.pop(key, None)
    if pending is None:
        return None

    artifact_dir = Path(project_dir) / ".agent" / "planning-repair-evidence"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    ensure_shared_permissions(artifact_dir)

    path = (
        artifact_dir
        / f"session_{session_id}_task_{task_id}_repair_attempt_{repair_attempt}_failed.json"
    )
    payload = {
        "schema_version": 1,
        "artifact_type": "planning_repair_failed_arbitration_triplet",
        "generated_at": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "task_id": task_id,
        "repair_attempt": repair_attempt,
        "redaction": {
            "applied": True,
            "secret_key_patterns": [
                "api_key",
                "token",
                "secret",
                "password",
                "authorization",
                "bearer",
                "sk-*",
            ],
        },
        "arbitration": _redact_value(arbitration),
        "previous_plan": _best_effort_json_or_text(
            pending.get("previous_plan_text"), fallback=previous_plan
        ),
        "repair_prompt": _redact_text(str(pending.get("repair_prompt") or "")),
        "repaired_plan": _redact_value(repaired_plan),
        "repaired_plan_raw": _redact_text(
            str(pending.get("repaired_plan_text") or repaired_output_text or "")
        ),
        "metadata": _redact_value(pending.get("metadata") or {}),
        "digests": {
            "previous_plan_sha256": _sha256_text(
                str(pending.get("previous_plan_text") or "")
            ),
            "repair_prompt_sha256": _sha256_text(
                str(pending.get("repair_prompt") or "")
            ),
            "repaired_plan_raw_sha256": _sha256_text(
                str(pending.get("repaired_plan_text") or repaired_output_text or "")
            ),
        },
        "sizes": {
            "previous_plan_chars": len(str(pending.get("previous_plan_text") or "")),
            "repair_prompt_chars": len(str(pending.get("repair_prompt") or "")),
            "repaired_plan_raw_chars": len(
                str(pending.get("repaired_plan_text") or repaired_output_text or "")
            ),
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    ensure_shared_permissions(path)
    return {
        "artifact_path": str(path),
        "artifact_type": payload["artifact_type"],
        "schema_version": payload["schema_version"],
        "repair_attempt": repair_attempt,
        "redacted": True,
    }


def _pending_key(
    project_dir: Any, session_id: int, task_id: int, repair_attempt: int
) -> _PendingKey:
    return (str(Path(project_dir)), int(session_id), int(task_id), int(repair_attempt))


def _best_effort_json_or_text(value: Any, *, fallback: Any) -> Any:
    text = str(value or "")
    try:
        return _redact_value(json.loads(text))
    except Exception:
        if fallback is not None:
            return _redact_value(fallback)
        return _redact_text(text)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_KEY_RE.search(key_text):
                redacted[key_text] = "<redacted>"
            else:
                redacted[key_text] = _redact_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    text = str(value or "")
    for pattern in _SECRET_TEXT_PATTERNS:
        if pattern.pattern.startswith("(?i)(authorization)"):
            text = pattern.sub(lambda m: f"{m.group(1)}=<redacted>", text)
        elif pattern.pattern.startswith("(?i)bearer"):
            text = pattern.sub("bearer <redacted>", text)
        elif pattern.pattern.startswith("\\bsk-"):
            text = pattern.sub("sk-<redacted>", text)
        else:
            text = pattern.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    return text


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
