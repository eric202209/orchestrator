"""OpenClaw CLI response parsing and stream diagnostics."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from typing import Any, Dict

from app.services.orchestration.validation.parsing import extract_structured_text


def stream_diagnostics_summary(diagnostics: Dict[str, Any]) -> str:
    first_output_after = diagnostics.get("first_output_after_seconds")
    last_output_after = diagnostics.get("last_output_after_seconds")
    max_silent_gap = diagnostics.get("max_silent_gap_seconds")
    planning_prompt_size = diagnostics.get("planning_prompt_size")
    first_rendered = (
        "none" if first_output_after is None else f"{first_output_after:.2f}s"
    )
    last_rendered = "none" if last_output_after is None else f"{last_output_after:.2f}s"
    gap_rendered = "none" if max_silent_gap is None else f"{max_silent_gap:.2f}s"
    parts = []
    if planning_prompt_size is not None:
        parts.append(f"planning_prompt_size={planning_prompt_size}")
    invocation = diagnostics.get("invocation") or {}
    if invocation:
        parts.extend(
            [
                f"invocation_kind={invocation.get('invocation_kind')}",
                f"invocation_cwd={invocation.get('cwd')}",
                "invocation_isolated=" f"{invocation.get('isolate_workspace_context')}",
                f"invocation_timeout_arg={invocation.get('timeout_arg')}",
                "invocation_no_output_timeout="
                f"{invocation.get('no_output_timeout_seconds')}",
            ]
        )
    parts.extend(
        [
            f"duration={diagnostics.get('duration_seconds', 0):.2f}s "
            f"timeout={diagnostics.get('timeout_seconds')}s",
            f"timed_out={diagnostics.get('timed_out')}",
            f"cancelled={diagnostics.get('cancelled')}",
            f"return_code={diagnostics.get('return_code')}",
            f"first_output_after={first_rendered}",
            f"last_output_after={last_rendered}",
            f"max_silent_gap={gap_rendered}",
            f"no_output_timeout={diagnostics.get('no_output_timeout')}",
            f"stdout_chars={diagnostics.get('stdout_chars', 0)}",
            f"stderr_chars={diagnostics.get('stderr_chars', 0)}",
            f"output_token_estimate={diagnostics.get('output_token_estimate', 0)}",
            f"stdout_lines={diagnostics.get('stdout_lines', 0)}",
            f"stderr_lines={diagnostics.get('stderr_lines', 0)}",
            f"output_channel_used={diagnostics.get('output_channel_used')}",
            "stderr_contains_model_content="
            f"{diagnostics.get('stderr_contains_model_content')}",
            f"stderr_contains_only_logs={diagnostics.get('stderr_contains_only_logs')}",
            f"stream_stalled={diagnostics.get('stream_stalled')}",
            f"truncated={diagnostics.get('truncated')}",
            f"contract_violation_type={diagnostics.get('contract_violation_type')}",
        ]
    )
    return " ".join(parts)


def looks_like_openclaw_diagnostic_payload(payload: Dict[str, Any]) -> bool:
    diagnostic_keys = {
        "aborted",
        "source",
        "generatedAt",
        "workspaceDir",
        "systemPrompt",
        "sandbox",
        "bootstrapMaxChars",
        "projectContextChars",
        "nonProjectContextChars",
        "lastCallUsage",
        "agentMeta",
        "durationMs",
        "stopReason",
        "livenessState",
    }
    return bool(set(payload.keys()) & diagnostic_keys) and not bool(
        {"payloads", "finalAssistantVisibleText", "text"} & set(payload.keys())
    )


def payload_contains_model_content(payload: Any) -> bool:
    if isinstance(payload, dict):
        if looks_like_openclaw_diagnostic_payload(payload):
            return False
        final_text = payload.get("finalAssistantVisibleText")
        if isinstance(final_text, str) and final_text.strip():
            return True
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return True
        payloads = payload.get("payloads")
        if isinstance(payloads, list):
            return any(payload_contains_model_content(item) for item in payloads)
        return False
    if isinstance(payload, list):
        return any(payload_contains_model_content(item) for item in payload)
    return False


def text_contains_model_content(text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate:
        return False
    try:
        return payload_contains_model_content(json.loads(candidate))
    except json.JSONDecodeError:
        return False


def channel_metadata(stdout_text: str, stderr_text: str) -> Dict[str, Any]:
    stdout_has_content = bool((stdout_text or "").strip())
    stderr_has_content = bool((stderr_text or "").strip())
    stderr_contains_model_content = bool(
        text_contains_model_content(stderr_text)
        or recover_json_like_output_from_stderr(stderr_text)
    )
    stderr_contains_only_logs = bool(
        stderr_has_content and not stderr_contains_model_content
    )

    if stdout_has_content and stderr_contains_model_content:
        output_channel_used = "mixed"
    elif stdout_has_content:
        output_channel_used = "stdout"
    elif stderr_contains_model_content:
        output_channel_used = "stderr"
    else:
        output_channel_used = "none"

    return {
        "output_channel_used": output_channel_used,
        "stderr_contains_model_content": stderr_contains_model_content,
        "stderr_contains_only_logs": stderr_contains_only_logs,
    }


def parse_openclaw_response(result: Any, log_entry) -> Dict[str, Any]:
    """Parse OpenClaw CLI response with unified error handling"""

    # Handle subprocess.CompletedProcess object
    if isinstance(result, subprocess.CompletedProcess):
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        return_code = result.returncode
        metadata = channel_metadata(stdout, stderr)

        if return_code != 0 and stderr:
            log_entry("ERROR", f"OpenClaw CLI error: {stderr[:500]}")
    else:
        # Already a string (from streaming mode)
        stdout = result.strip()
        return_code = 0
        stderr = ""
        metadata = channel_metadata(stdout, stderr)

    cli_error_message = summarize_cli_error(stderr) if stderr else ""
    cli_error_lower = cli_error_message.lower()

    if "context size has been exceeded" in cli_error_lower or (
        "context" in cli_error_lower and "exceeded" in cli_error_lower
    ):
        log_entry("ERROR", f"Context window exceeded: {cli_error_message}")
        return {
            "status": "failed",
            "mode": "real",
            "output": stdout,
            "error": "Context window exceeded",
            **metadata,
            "logs": [
                {
                    "level": "ERROR",
                    "message": cli_error_message,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            ],
        }

    if (not stdout or stdout in ['""', "''", '"', "'"]) and stderr:
        recovered_output = recover_json_like_output_from_stderr(stderr)
        if recovered_output:
            metadata = channel_metadata("", recovered_output)
            log_entry(
                "WARN",
                "[OPENCLAW] stdout was empty; normalized model response from stderr",
                metadata=json.dumps(metadata),
            )
            stdout = recovered_output

    # CRITICAL FIX: Validate response before parsing
    if not stdout or stdout in ['""', "''", '"', "'"]:
        log_entry("ERROR", "[OPENCLAW] CRITICAL: Empty or invalid response")
        return {
            "status": "failed",
            "mode": "real",
            "output": "",
            "error": cli_error_message or "Empty or invalid response from OpenClaw CLI",
            **metadata,
            "logs": [],
        }

    # Parse JSON with error recovery
    try:
        output_data = json.loads(stdout)

        # Extract text from payloads if present
        if isinstance(output_data, dict) and "payloads" in output_data:
            payloads = output_data.get("payloads", [])
            if isinstance(payloads, list) and len(payloads) > 0:
                output_text = payloads[0].get("text", "")
            else:
                output_text = extract_structured_text(output_data) or json.dumps(
                    output_data
                )
        else:
            output_text = extract_structured_text(output_data) or json.dumps(
                output_data
            )

        if isinstance(output_data, dict) and output_data.get("aborted") is True:
            timeout_error = cli_error_message or "OpenClaw run was aborted"
            lowered_output_text = str(output_text or "").lower()
            if "timeout" in lowered_output_text:
                timeout_error = "Task timed out before a valid response was generated"
            elif "timeout" in cli_error_lower or "timed out" in cli_error_lower:
                timeout_error = cli_error_message or "Task timed out"
            log_entry(
                "ERROR",
                f"[OPENCLAW] Aborted structured response surfaced as failure: {timeout_error}",
            )
            return {
                "status": "failed",
                "mode": "real",
                "output": output_text,
                "error": timeout_error,
                **metadata,
                "logs": [],
            }

        return {
            "status": "completed" if return_code == 0 else "failed",
            "mode": "real",
            "output": output_text,
            "error": cli_error_message if return_code != 0 else "",
            **metadata,
            "logs": [],
        }

    except json.JSONDecodeError:
        # Only apply garbled detection after JSON parsing actually fails.
        garbled_patterns = [
            "\"'",
            '"", "',
            "garbled",
            "corrupted",
        ]
        stdout_lower = stdout.lower()
        if stdout.strip() in {"\"'", "'"} or any(
            pattern in stdout_lower for pattern in garbled_patterns
        ):
            log_entry(
                "ERROR",
                f"[OPENCLAW] DETECTED GARBLED OUTPUT AFTER JSON PARSE FAILURE: '{stdout[:200]}'",
            )
            return {
                "status": "failed",
                "mode": "real",
                "output": "",
                "error": "Execution failed with unclear error (garbled output detected). See logs for details.",
                **metadata,
                "logs": [
                    {
                        "level": "ERROR",
                        "message": f"Garbled output detected: '{stdout[:500]}'",
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                ],
                "execution_time": 0.0,
            }

        # Fallback to raw text if it isn't valid JSON but still looks coherent.
        log_entry("WARN", "Failed to parse JSON, using raw output")
        return {
            "status": "completed" if return_code == 0 else "failed",
            "mode": "real",
            "output": stdout,
            "error": cli_error_message if return_code != 0 else "",
            **metadata,
            "logs": [],
        }

    except Exception as e:
        error_str = str(e)
        # Handle specific error types
        if "context" in error_str.lower() and "token" in error_str.lower():
            # Context window error - provide helpful message
            log_entry("ERROR", f"Context window exceeded: {error_str}")
            return {
                "status": "failed",
                "mode": "real",
                "output": "Context window exceeded. Prompt is too long for the model.",
                **metadata,
                "logs": [
                    {
                        "level": "ERROR",
                        "message": f"Context window exceeded: {error_str}",
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                ],
                "execution_time": 0.0,
                "error": "Context window exceeded",
            }
        elif "signal" in error_str.lower() or "killed" in error_str.lower():
            # Process was killed (likely OOM or timeout)
            log_entry("ERROR", f"Process was killed: {error_str}")
            return {
                "status": "failed",
                "mode": "real",
                "output": f"Process was killed: {error_str}",
                **metadata,
                "logs": [
                    {
                        "level": "ERROR",
                        "message": f"Process was killed: {error_str}",
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                ],
                "execution_time": 0.0,
                "error": "Process killed",
            }
        else:
            log_entry("ERROR", f"Error executing task via OpenClaw: {error_str}")
            return {
                "status": "failed",
                "mode": "real",
                "output": f"Execution error: {error_str}",
                **metadata,
                "logs": [
                    {
                        "level": "ERROR",
                        "message": f"Error: {error_str}",
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                ],
                "execution_time": 0.0,
                "error": error_str,
            }


def recover_json_like_output_from_stderr(stderr: str) -> str:
    """Recover a structured JSON-ish payload from stderr when stdout is empty."""
    ansi_pattern = re.compile(r"\x1B\[[0-9;]*[A-Za-z]")
    lines = []
    for raw_line in (stderr or "").splitlines():
        cleaned = ansi_pattern.sub("", raw_line).strip()
        if cleaned:
            lines.append(cleaned)

    if not lines:
        return ""

    candidate_indexes = [
        index
        for index, line in enumerate(lines)
        if line in {"{", "["}
        or line.startswith("{")
        or line.startswith("[")
        or line.startswith('"payloads"')
        or line.startswith('"finalAssistantVisibleText"')
    ]

    for index in reversed(candidate_indexes):
        candidate = "\n".join(lines[index:]).strip()
        candidates = [candidate]
        if candidate.startswith('"payloads"') or candidate.startswith(
            '"finalAssistantVisibleText"'
        ):
            candidates.append("{" + candidate.rstrip().rstrip(",") + "}")

        for normalized_candidate in candidates:
            try:
                parsed = json.loads(normalized_candidate)
            except Exception:
                continue
            if payload_contains_model_content(parsed):
                return normalized_candidate

    return ""


def summarize_cli_error(stderr: str) -> str:
    """Return a compact user-facing summary from OpenClaw stderr."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return ""

    for line in lines:
        lowered = line.lower()
        if "[openclaw] cli failed:" in lowered:
            return line[:500]

    for line in lines:
        lowered = line.lower()
        if lowered.startswith("at ") or "jiti/dist/jiti.cjs" in lowered:
            continue
        if "referenceerror:" in lowered:
            return f"[openclaw] CLI failed: {line[:450]}"
        return line[:500]

    return lines[0][:500]
