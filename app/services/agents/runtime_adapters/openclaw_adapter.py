"""OpenClaw runtime result normalization."""

from __future__ import annotations

import json
import subprocess
from typing import Any, Mapping

from app.services.agents.runtime_adapters.base import runtime_result_from_mapping


OPENCLAW_PROVIDER_RESULT_MAX_BYTES = 1024 * 1024
OPENCLAW_PROVIDER_CANDIDATE_MAX_BYTES = 512 * 1024
OPENCLAW_PROVIDER_RESULT_CONTRACT = "openclaw-agent-json-v1"
_OPENCLAW_RESULT_KEYS = frozenset({"payloads", "meta"})


class OpenClawProviderContractError(RuntimeError):
    """Bounded failure from the documented OpenClaw agent result contract."""

    def __init__(
        self,
        classification: str,
        detail: str,
        *,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        self.classification = str(classification)
        self.provider_failure_classification = self.classification
        self.detail = str(detail or self.classification)[:500]
        self.runtime_diagnostics = dict(diagnostics or {})
        super().__init__(self.detail)


def _bounded_channel_text(value: Any, channel: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OpenClawProviderContractError(
                "provider_output_failure",
                f"OpenClaw {channel} result is not valid UTF-8",
            ) from exc
    elif isinstance(value, str):
        text = value
    else:
        raise OpenClawProviderContractError(
            "provider_output_failure",
            f"OpenClaw {channel} result has an unsupported type",
        )
    if len(text.encode("utf-8")) > OPENCLAW_PROVIDER_RESULT_MAX_BYTES:
        raise OpenClawProviderContractError(
            "provider_output_failure",
            f"OpenClaw {channel} result exceeds the bounded output limit",
        )
    return text


def _parse_documented_envelope(text: str) -> Mapping[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    if set(parsed) != _OPENCLAW_RESULT_KEYS:
        return None
    if not isinstance(parsed.get("meta"), Mapping):
        return None
    if not isinstance(parsed.get("payloads"), list):
        return None
    return parsed


def has_verified_openclaw_provider_result(stdout: Any, stderr: Any) -> bool:
    """Return whether one complete channel contains a documented result."""

    try:
        stdout_text = _bounded_channel_text(stdout, "stdout")
        stderr_text = _bounded_channel_text(stderr, "stderr")
    except OpenClawProviderContractError:
        return False
    envelopes = [
        _parse_documented_envelope(stdout_text),
        _parse_documented_envelope(stderr_text),
    ]
    return sum(envelope is not None for envelope in envelopes) == 1


def parse_openclaw_provider_result(
    result: subprocess.CompletedProcess[str],
    *,
    expected_session_id: str | None = None,
) -> dict[str, Any]:
    """Extract exactly one final semantic text from OpenClaw's JSON envelope.

    OpenClaw 2026.4.10 routes JSON-mode ``runtime.log`` output to stderr when
    stdout is suppressed.  The result is still the documented complete JSON
    envelope, not arbitrary stderr text.  A non-envelope channel is retained
    only as bounded diagnostics and is never used as candidate content.
    """

    stdout_text = _bounded_channel_text(result.stdout, "stdout")
    stderr_text = _bounded_channel_text(result.stderr, "stderr")
    stdout_envelope = _parse_documented_envelope(stdout_text)
    stderr_envelope = _parse_documented_envelope(stderr_text)
    envelopes = [
        ("stdout", stdout_envelope),
        ("stderr", stderr_envelope),
    ]
    present = [(channel, envelope) for channel, envelope in envelopes if envelope]
    base_diagnostics = {
        "provider_result_contract": OPENCLAW_PROVIDER_RESULT_CONTRACT,
        "provider_result_channel": None,
        "stdout_bytes": len(stdout_text.encode("utf-8")),
        "stderr_bytes": len(stderr_text.encode("utf-8")),
        "return_code": result.returncode,
        "stderr_diagnostic_bytes": 0,
    }
    if len(present) > 1:
        raise OpenClawProviderContractError(
            "provider_result_ambiguous",
            "OpenClaw emitted multiple structured result envelopes",
            diagnostics=base_diagnostics,
        )
    if not present:
        classification = (
            "provider_process_failure"
            if result.returncode
            else "provider_result_missing"
        )
        detail = (
            f"OpenClaw exited with code {result.returncode} without a documented result"
            if result.returncode
            else "OpenClaw emitted no complete documented result envelope"
        )
        raise OpenClawProviderContractError(
            classification,
            detail,
            diagnostics=base_diagnostics,
        )

    channel, envelope = present[0]
    assert envelope is not None
    diagnostics = {
        **base_diagnostics,
        "provider_result_channel": channel,
        "stderr_diagnostic_bytes": (
            len(stderr_text.encode("utf-8")) if channel == "stdout" else 0
        ),
    }
    payloads = envelope["payloads"]
    meta = envelope["meta"]
    if len(payloads) != 1:
        raise OpenClawProviderContractError(
            (
                "provider_result_ambiguous"
                if len(payloads) > 1
                else "provider_result_missing"
            ),
            "OpenClaw result must contain exactly one final payload",
            diagnostics=diagnostics,
        )
    payload = payloads[0]
    if not isinstance(payload, Mapping) or not isinstance(payload.get("text"), str):
        raise OpenClawProviderContractError(
            "provider_result_missing",
            "OpenClaw result contains no final semantic text payload",
            diagnostics=diagnostics,
        )
    output = payload["text"]
    if not output.strip():
        raise OpenClawProviderContractError(
            "provider_result_missing",
            "OpenClaw final semantic text payload is empty",
            diagnostics=diagnostics,
        )
    if payload.get("isError") is True or payload.get("isReasoning") is True:
        raise OpenClawProviderContractError(
            "provider_process_failure",
            "OpenClaw final payload is marked as error or reasoning output",
            diagnostics=diagnostics,
        )
    if len(output.encode("utf-8")) > OPENCLAW_PROVIDER_CANDIDATE_MAX_BYTES:
        raise OpenClawProviderContractError(
            "provider_output_failure",
            "OpenClaw semantic result exceeds the bounded candidate limit",
            diagnostics=diagnostics,
        )

    agent_meta = meta.get("agentMeta")
    response_session_id = (
        str(agent_meta.get("sessionId") or "").strip()
        if isinstance(agent_meta, Mapping)
        else ""
    )
    if expected_session_id and response_session_id != expected_session_id:
        raise OpenClawProviderContractError(
            "provider_process_failure",
            "OpenClaw response provenance does not match the invocation session",
            diagnostics={
                **diagnostics,
                "expected_session_id_present": True,
                "response_session_id_present": bool(response_session_id),
            },
        )

    if meta.get("aborted") is True:
        reason = str(meta.get("stopReason") or "OpenClaw run was aborted")
        classification = (
            "provider_timeout"
            if "timeout" in reason.lower()
            else "provider_process_failure"
        )
        raise OpenClawProviderContractError(
            classification,
            reason,
            diagnostics=diagnostics,
        )

    return {
        "status": "completed",
        "mode": "real",
        "output": output,
        "output_channel_used": channel,
        "provider_result_diagnostics": {
            **diagnostics,
            "candidate_bytes": len(output.encode("utf-8")),
            "response_session_id_present": bool(response_session_id),
            "process_warning": bool(result.returncode),
        },
    }


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
