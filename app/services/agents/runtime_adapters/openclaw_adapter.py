"""OpenClaw runtime result normalization."""

from __future__ import annotations

from typing import Any, Mapping

from app.services.agents.runtime_adapters.base import runtime_result_from_mapping


def _classify_openclaw_failure(exit_reason: str) -> str:
    reason = (exit_reason or "").lower()
    if any(k in reason for k in ("capacity", "lock", "slot", "busy")):
        return "backend_capacity_limit"
    if any(k in reason for k in ("time limit", "timeout", "timed out")):
        return "backend_timeout"
    if any(
        k in reason
        for k in ("transport", "connect", "unavailable", "config", "cli_not_found")
    ):
        return "backend_transport_error"
    if any(k in reason for k in ("validation", "validator", "contract")):
        return "validation_failure"
    if any(k in reason for k in ("governance", "review", "hold", "permission")):
        return "governance_hold"
    return "execution_failure"


def normalize_openclaw_execution_result(
    result: Mapping[str, Any],
    *,
    backend_id: str,
    role: str,
    duration_seconds: float = 0.0,
):
    """Translate local OpenClaw execution output into RuntimeBackendResult."""

    normalized = runtime_result_from_mapping(
        result,
        backend_id=backend_id,
        role=role,
        duration_seconds=duration_seconds,
        default_failure_category=None,
    )
    if normalized.success:
        return normalized
    failure_category = normalized.failure_category or _classify_openclaw_failure(
        normalized.exit_reason
    )
    terminal_reason = normalized.terminal_reason
    if failure_category == "backend_timeout" and not terminal_reason:
        terminal_reason = "timeout_before_backend_completion"
    return normalized.__class__(
        backend_id=normalized.backend_id,
        role=normalized.role,
        success=normalized.success,
        exit_reason=normalized.exit_reason,
        output=normalized.output,
        duration_seconds=normalized.duration_seconds,
        failure_category=failure_category,
        terminal_reason=terminal_reason,
        tokens_in=normalized.tokens_in,
        tokens_out=normalized.tokens_out,
        token_source=normalized.token_source,
    )
