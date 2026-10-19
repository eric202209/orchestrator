"""OpenClaw CLI response parsing and stream diagnostics."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from typing import Any, Dict

from app.services.orchestration.validation.parsing import extract_structured_text

_PARTIAL_VISIBLE_TEXT_KEYS = (
    "finalAssistantVisibleText",
    "final_assistant_visible_text",
    "text",
    "output_text",
    "content_text",
)


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
                f"prompt_sha256_12={invocation.get('prompt_sha256_12')}",
            ]
        )
    parts.extend(
        [
            f"duration={diagnostics.get('duration_seconds', 0):.2f}s "
            f"timeout={diagnostics.get('timeout_seconds')}s",
            f"timed_out={diagnostics.get('timed_out')}",
            f"cancelled={diagnostics.get('cancelled')}",
            f"diagnostic_category={diagnostics.get('diagnostic_category')}",
            f"return_code={diagnostics.get('return_code')}",
            f"process_pid={diagnostics.get('process_pid')}",
            "subprocess_started_after="
            f"{diagnostics.get('subprocess_started_after_seconds')}",
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
            f"activity_state={diagnostics.get('activity_state')}",
            f"activity_classification={diagnostics.get('activity_classification')}",
            f"terminal_reason={diagnostics.get('terminal_reason')}",
            f"partial_response_seen={diagnostics.get('partial_response_seen')}",
            f"cleanup_status={diagnostics.get('cleanup_status')}",
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
        {"payloads", *_PARTIAL_VISIBLE_TEXT_KEYS} & set(payload.keys())
    )


def payload_contains_model_content(payload: Any) -> bool:
    if isinstance(payload, dict):
        if looks_like_openclaw_diagnostic_payload(payload):
            return False
        if any(
            isinstance(payload.get(key), str) and payload[key].strip()
            for key in _PARTIAL_VISIBLE_TEXT_KEYS
        ):
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


def partial_model_content_seen(stdout_text: str, stderr_text: str) -> bool:
    """Return whether bounded stream text contains a model-content fragment.

    This deliberately reuses the response extractor's documented visible-text
    keys.  Plain text is eligible only on stdout, matching the legacy response
    parser; stderr requires a documented model envelope or visible-text
    fragment so diagnostic-only stderr never becomes partial model content.
    """

    for text, allow_plain_text in ((stdout_text, True), (stderr_text, False)):
        candidate = (text or "").strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and looks_like_openclaw_diagnostic_payload(parsed):
            continue
        if text_contains_model_content(candidate):
            return True
        if allow_plain_text and not candidate.startswith(("{", "[")):
            return True
        if not any(f'"{key}"' in candidate for key in _PARTIAL_VISIBLE_TEXT_KEYS):
            continue
        fragment_text = extract_structured_text(candidate)
        if (
            fragment_text
            and fragment_text.strip()
            and fragment_text.strip() != candidate
        ):
            return True
    return False


def extract_payloads_text(payloads: list[Any]) -> str:
    """Extract model-visible text from OpenClaw payload objects."""

    parts = [
        text
        for payload in payloads
        if isinstance(payload, dict)
        if (text := extract_structured_text(payload))
    ]
    if parts:
        return "\n".join(parts)
    return extract_structured_text(payloads)


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


def _parse_openclaw_response_impl(
    result: Any,
    log_entry,
    *,
    expected_session_id: str | None = None,
) -> Dict[str, Any]:
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

        response_session_id = None
        response_meta: Dict[str, Any] = {}
        if isinstance(output_data, dict):
            response_meta = output_data.get("meta") or {}
            response_session_id = str(
                (response_meta.get("agentMeta") or {}).get("sessionId", "") or ""
            ).strip()
        if expected_session_id and response_session_id != expected_session_id:
            error = (
                "OpenClaw response provenance mismatch: expected session "
                f"{expected_session_id}, received "
                f"{response_session_id or 'no session identity'}"
            )
            log_entry("ERROR", f"[OPENCLAW] {error}")
            return {
                "status": "failed",
                "mode": "real",
                "output": "",
                "error": error,
                "response_session_id": response_session_id or None,
                "expected_session_id": expected_session_id,
                **metadata,
                "logs": [],
            }

        # Extract text from payloads if present
        if isinstance(output_data, dict) and "payloads" in output_data:
            payloads = output_data.get("payloads", [])
            if isinstance(payloads, list) and len(payloads) > 0:
                output_text = extract_payloads_text(payloads)
            else:
                output_text = extract_structured_text(output_data) or json.dumps(
                    output_data
                )
        else:
            output_text = extract_structured_text(output_data) or json.dumps(
                output_data
            )

        if isinstance(output_data, dict) and (
            output_data.get("aborted") is True or response_meta.get("aborted") is True
        ):
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
            "response_session_id": response_session_id or None,
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


_DIAGNOSTIC_CONTENT_FIELDS = (
    *_PARTIAL_VISIBLE_TEXT_KEYS,
    "content",
    "message",
    "payloads",
)
_DIAGNOSTIC_MAX_DEPTH = 16
_DIAGNOSTIC_MAX_NODES = 10000


def _response_field_metadata(value: Any) -> tuple[list[str], str | None]:
    """Describe supported response fields without retaining any field values."""
    recognized: set[str] = set()
    visited = 0

    def collect(item: Any, depth: int = 0) -> None:
        nonlocal visited
        if depth > _DIAGNOSTIC_MAX_DEPTH or visited >= _DIAGNOSTIC_MAX_NODES:
            return
        visited += 1
        if isinstance(item, dict):
            recognized.update(key for key in _DIAGNOSTIC_CONTENT_FIELDS if key in item)
            for nested in item.values():
                if isinstance(nested, (dict, list)):
                    collect(nested, depth + 1)
                elif isinstance(nested, str) and nested.lstrip().startswith(("{", "[")):
                    try:
                        collect(json.loads(nested), depth + 1)
                    except (TypeError, ValueError):
                        pass
        elif isinstance(item, list):
            for nested in item:
                collect(nested, depth + 1)

    visited = 0
    collect(value)

    visited = 0

    def select(item: Any, depth: int = 0) -> str | None:
        nonlocal visited
        if depth > _DIAGNOSTIC_MAX_DEPTH or visited >= _DIAGNOSTIC_MAX_NODES:
            return None
        visited += 1
        if isinstance(item, dict):
            for key in _PARTIAL_VISIBLE_TEXT_KEYS:
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return key
            for key in ("content", "message", "payloads"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return key
                if isinstance(candidate, (dict, list)):
                    selected = select(candidate, depth + 1)
                    if selected:
                        return selected
            for nested in item.values():
                if isinstance(nested, (dict, list)):
                    selected = select(nested, depth + 1)
                    if selected:
                        return selected
                elif isinstance(nested, str):
                    if nested.lstrip().startswith(("{", "[")):
                        try:
                            selected = select(json.loads(nested), depth + 1)
                        except (TypeError, ValueError):
                            selected = None
                        if selected:
                            return selected
                    if nested.strip():
                        return "GENERIC_TEXT_FALLBACK"
        elif isinstance(item, list):
            for nested in item:
                selected = select(nested, depth + 1)
                if selected:
                    return selected
        elif isinstance(item, str) and item.strip():
            return "GENERIC_TEXT_FALLBACK"
        return None

    ordered_fields = [key for key in _DIAGNOSTIC_CONTENT_FIELDS if key in recognized]
    return ordered_fields, select(value)


def _response_diagnostics(result: Any, parsed_result: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(result, subprocess.CompletedProcess):
        stdout = str(result.stdout or "").strip()
        stderr = str(result.stderr or "").strip()
    else:
        stdout = str(result or "").strip()
        stderr = ""

    empty_stdout = not stdout or stdout in {'""', "''", '"', "'"}
    candidate = None if empty_stdout else stdout
    source = "stdout" if candidate is not None else None
    if candidate is None and stderr:
        recovered = recover_json_like_output_from_stderr(stderr)
        if recovered:
            candidate = recovered
            source = "stderr"

    json_outcome = "NOT_ATTEMPTED"
    parsed: Any = None
    if candidate is not None:
        try:
            parsed = json.loads(candidate)
            json_outcome = "SUCCEEDED"
        except (TypeError, ValueError):
            json_outcome = "FAILED"

    envelope_type = "UNAVAILABLE"
    selected_field = None
    recognized_fields: list[str] = []
    assistant_content_nonempty = False
    if json_outcome == "SUCCEEDED":
        if isinstance(parsed, dict):
            envelope_type = "object"
        elif isinstance(parsed, list):
            envelope_type = "array"
        elif isinstance(parsed, str):
            envelope_type = "string"
        elif isinstance(parsed, bool):
            envelope_type = "boolean"
        elif isinstance(parsed, (int, float)):
            envelope_type = "number"
        elif parsed is None:
            envelope_type = "null"
        recognized_fields, selected_field = _response_field_metadata(parsed)
        if selected_field is None and isinstance(parsed, str) and parsed.strip():
            selected_field = "JSON_STRING"
        assistant_content_nonempty = selected_field is not None
    elif json_outcome == "FAILED" and source == "stdout":
        # The existing parser deliberately falls back to raw non-JSON stdout.
        output = parsed_result.get("output")
        assistant_content_nonempty = isinstance(output, str) and bool(output.strip())
        if assistant_content_nonempty:
            selected_field = "PLAIN_TEXT_FALLBACK"

    if candidate is None:
        if empty_stdout and stderr and not text_contains_model_content(stderr):
            category = "STDERR_DIAGNOSTIC_ONLY"
        else:
            category = "EMPTY_STDOUT"
    elif json_outcome == "FAILED":
        category = "JSON_PARSE_FAILED"
    elif assistant_content_nonempty:
        category = "VALID_ASSISTANT_CONTENT_EXTRACTED"
    elif recognized_fields:
        category = "EMPTY_ASSISTANT_CONTENT"
    else:
        category = "UNSUPPORTED_RESPONSE_ENVELOPE"

    return {
        "json_parsing_outcome": json_outcome,
        "parsed_envelope_type": envelope_type,
        "recognized_assistant_content_fields": recognized_fields,
        "selected_assistant_content_source": source,
        "selected_assistant_content_field": selected_field,
        "assistant_content_nonempty": assistant_content_nonempty,
        "diagnostic_category": category,
        "extraction_failure_category": (
            None if category == "VALID_ASSISTANT_CONTENT_EXTRACTED" else category
        ),
    }


def parse_openclaw_response(
    result: Any,
    log_entry,
    *,
    expected_session_id: str | None = None,
    capture_diagnostics: bool = False,
) -> Dict[str, Any]:
    """Parse an OpenClaw response and optionally attach metadata-only evidence."""
    parsed_result = _parse_openclaw_response_impl(
        result,
        log_entry,
        expected_session_id=expected_session_id,
    )
    if capture_diagnostics:
        parsed_result["response_diagnostics"] = _response_diagnostics(
            result, parsed_result
        )
    return parsed_result


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
