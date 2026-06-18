"""Runtime adapter primitives shared by backend-specific adapters."""

from __future__ import annotations

from typing import Any, Mapping

from app.services.agents.interfaces import RuntimeBackendResult


def _coerce_output(result: Mapping[str, Any]) -> str | None:
    output = result.get("output")
    if output is None:
        output = result.get("message")
    if output is None:
        output = result.get("error")
    if output is None:
        return None
    return str(output)


def runtime_result_from_mapping(
    result: Mapping[str, Any],
    *,
    backend_id: str,
    role: str,
    duration_seconds: float = 0.0,
    default_failure_category: str | None = "execution_failure",
) -> RuntimeBackendResult:
    """Translate a legacy backend result dict into RuntimeBackendResult."""

    status = str(result.get("status") or "").strip().lower()
    success = bool(result.get("success")) or status in {"completed", "done", "success"}
    exit_reason = str(
        result.get("exit_reason")
        or result.get("reason")
        or result.get("error")
        or ("completed" if success else "execution_failed")
    ).strip() or ("completed" if success else "execution_failed")
    raw_duration = result.get("duration_seconds", duration_seconds)
    try:
        normalized_duration = float(raw_duration or 0.0)
    except (TypeError, ValueError):
        normalized_duration = 0.0
    failure_category = (
        None
        if success
        else (result.get("failure_category") or default_failure_category)
    )
    tokens_in: int | None = None
    tokens_out: int | None = None
    token_source: str | None = None
    usage = result.get("usage")
    if isinstance(usage, dict):
        raw_in = usage.get("input_tokens") or usage.get("prompt_tokens")
        raw_out = usage.get("output_tokens") or usage.get("completion_tokens")
        try:
            tokens_in = int(raw_in) if raw_in is not None else None
        except (TypeError, ValueError):
            tokens_in = None
        try:
            tokens_out = int(raw_out) if raw_out is not None else None
        except (TypeError, ValueError):
            tokens_out = None
        if tokens_in is not None or tokens_out is not None:
            token_source = "openai_usage"
    return RuntimeBackendResult(
        backend_id=backend_id,
        role=role,
        success=success,
        exit_reason=exit_reason,
        output=_coerce_output(result),
        duration_seconds=normalized_duration,
        failure_category=failure_category,
        terminal_reason=result.get("terminal_reason"),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        token_source=token_source,
    )
