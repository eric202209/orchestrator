"""Typed, metadata-only schema for new OpenClaw operational log records."""

from __future__ import annotations

import math
import re
from enum import Enum
from typing import Any, Literal, Mapping, TypedDict


class OpenClawDiagnosticEvent(str, Enum):
    OUTPUT_CHUNK = "openclaw_output_chunk"
    STREAM_PROGRESS = "openclaw_stream_progress"
    REPAIR_DIAGNOSTICS = "openclaw_repair_diagnostics"
    PLANNING_DIAGNOSTICS = "openclaw_planning_diagnostics"
    PLANNING_DISCOVERY_DIAGNOSTICS = "openclaw_planning_discovery_diagnostics"
    DEBUG_REPAIR_DIAGNOSTICS = "openclaw_debug_repair_diagnostics"
    EVALUATOR_DIAGNOSTICS = "openclaw_evaluator_diagnostics"
    CLI_RETRY = "openclaw_cli_retry"
    PROCESS_RESULT = "openclaw_process_result"
    EXECUTION_FAILURE = "openclaw_execution_failure"
    EXECUTION_RETRY = "openclaw_execution_retry"
    SESSION_START = "openclaw_session_start"
    SESSION_READY = "openclaw_session_ready"
    LOG_STREAM_START = "openclaw_log_stream_start"
    RESPONSE_PARSER = "openclaw_response_parser"
    WORKSPACE_GUARD = "openclaw_workspace_guard"
    GENERIC_DIAGNOSTICS = "openclaw_diagnostics"


DiagnosticCategory = Literal[
    "caller_cancelled",
    "diagnostic_persistence_failure",
    "execution_failure",
    "inference_progress",
    "provider_failure",
    "provider_success",
    "response_parse_failure",
    "silent_inference",
    "slow_inference",
    "stream_output",
    "timeout",
    "workspace_contract_failure",
    "runtime_pollution",
]

PollutionClassification = Literal[
    "canonical_scaffold",
    "runtime_workspace_scaffold",
    "unclassified_entries",
]

RuntimeStatus = Literal[
    "completed",
    "failed",
    "interrupted",
    "not_started",
    "running",
    "timed_out",
    "unknown",
]

ResponseChannel = Literal["none", "stderr", "stdout"]

ParserOutcome = Literal[
    "failed",
    "invalid_json",
    "invalid_envelope",
    "not_run",
    "parsed",
    "response_missing",
]

ExtractionCategory = Literal[
    "EMPTY_ASSISTANT_CONTENT",
    "JSON_PARSE_FAILED",
    "STDERR_DIAGNOSTIC_ONLY",
    "UNSUPPORTED_RESPONSE_ENVELOPE",
    "VALID_ASSISTANT_CONTENT_EXTRACTED",
]

FailureCategory = Literal[
    "cli_nonzero_exit",
    "diagnostic_persistence_failure",
    "interrupted",
    "response_parse_failure",
    "runtime_error",
    "timeout",
    "workspace_contract_failure",
]


class OpenClawDiagnosticMetadataV2(TypedDict, total=False):
    """The only fields allowed in a newly persisted OpenClaw diagnostic."""

    diagnostic_schema_version: Literal[2]
    content_omitted: Literal[True]
    diagnostic_event: OpenClawDiagnosticEvent
    diagnostic_category: DiagnosticCategory
    runtime_status: RuntimeStatus
    failure_category: FailureCategory
    cli_return_code: int
    duration_seconds: float
    timeout_seconds: float
    first_output_delay_seconds: float
    max_silent_gap_seconds: float
    cleanup_seconds: float
    stdout_byte_count: int
    stderr_byte_count: int
    stdout_character_count: int
    stderr_character_count: int
    stdout_line_count: int
    stderr_line_count: int
    stdout_sha256: str
    stderr_sha256: str
    stream_channel: ResponseChannel
    stream_byte_count: int
    stream_sha256: str
    response_present: bool
    stderr_diagnostic_only: bool
    selected_response_channel: ResponseChannel
    recognized_response_field: str
    parser_outcome: ParserOutcome
    extraction_category: ExtractionCategory
    activity_classification: str
    activity_state: str
    timeout_boundary: str
    cleanup_status: str
    invocation_kind: Literal["debug_repair", "planning"]
    retry_attempt: int
    retry_max_attempts: int
    retry_delay_seconds: float
    timed_out: bool
    interrupted: bool
    no_output_timeout: bool
    stream_stalled: bool
    truncated: bool
    capture_error: bool
    provider_invocation_started: bool
    response_boundary_reached: bool
    correlation_id: str
    pollution_classification: PollutionClassification
    pollution_entry_count: int


_DIAGNOSTIC_CATEGORIES = frozenset(
    {
        "caller_cancelled",
        "diagnostic_persistence_failure",
        "execution_failure",
        "inference_progress",
        "provider_failure",
        "provider_success",
        "response_parse_failure",
        "silent_inference",
        "slow_inference",
        "stream_output",
        "timeout",
        "workspace_contract_failure",
        "runtime_pollution",
    }
)
_RUNTIME_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "interrupted",
        "not_started",
        "running",
        "timed_out",
        "unknown",
    }
)
_FAILURE_CATEGORIES = frozenset(
    {
        "cli_nonzero_exit",
        "diagnostic_persistence_failure",
        "interrupted",
        "response_parse_failure",
        "runtime_error",
        "timeout",
        "workspace_contract_failure",
    }
)
_PARSER_OUTCOMES = frozenset(
    {
        "failed",
        "invalid_json",
        "invalid_envelope",
        "not_run",
        "parsed",
        "response_missing",
    }
)
_EXTRACTION_CATEGORIES = frozenset(
    {
        "EMPTY_ASSISTANT_CONTENT",
        "JSON_PARSE_FAILED",
        "STDERR_DIAGNOSTIC_ONLY",
        "UNSUPPORTED_RESPONSE_ENVELOPE",
        "VALID_ASSISTANT_CONTENT_EXTRACTED",
    }
)
_ACTIVITY_CLASSIFICATIONS = frozenset(
    {
        "caller_cancelled",
        "completed",
        "missing_result",
        "no_output_timeout",
        "partial_stream_stall",
        "provider_process_failure",
        "provider_process_timeout",
    }
)
_ACTIVITY_STATES = frozenset(
    {
        "active_output",
        "caller_cancelled",
        "partial_response_wait",
        "startup_wait",
        "stream_stalled",
        "terminal_response",
    }
)
_TIMEOUT_BOUNDARIES = frozenset(
    {
        "caller_cancelled",
        "debug_repair_wait_for",
        "planning_wait_for",
        "process_timeout",
        "provider_timeout",
    }
)
_CLEANUP_STATUSES = frozenset(
    {"completed", "not_started", "process_exited", "signalled", "timed_out", "unknown"}
)
_RESPONSE_FIELDS = frozenset(
    {
        "content",
        "content_text",
        "finalAssistantVisibleText",
        "final_assistant_visible_text",
        "message",
        "output_text",
        "payloads",
        "text",
    }
)
_CHANNELS = frozenset({"none", "stderr", "stdout"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(
    r"^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)

_EVENT_MESSAGES = {
    OpenClawDiagnosticEvent.OUTPUT_CHUNK: "[OPENCLAW][OUTPUT] content omitted",
    OpenClawDiagnosticEvent.STREAM_PROGRESS: "[OPENCLAW][STREAM_PROGRESS] content omitted",
    OpenClawDiagnosticEvent.REPAIR_DIAGNOSTICS: "[OPENCLAW][REPAIR_DIAGNOSTICS] content omitted",
    OpenClawDiagnosticEvent.PLANNING_DIAGNOSTICS: "[OPENCLAW][PLANNING_DIAGNOSTICS] content omitted",
    OpenClawDiagnosticEvent.PLANNING_DISCOVERY_DIAGNOSTICS: "[OPENCLAW][PLANNING_DISCOVERY_DIAGNOSTICS] content omitted",
    OpenClawDiagnosticEvent.DEBUG_REPAIR_DIAGNOSTICS: "[OPENCLAW][BOUNDED_EXECUTION_DEBUG_REPAIR_DIAGNOSTICS] content omitted",
    OpenClawDiagnosticEvent.EVALUATOR_DIAGNOSTICS: "[OPENCLAW][EVALUATOR_RESPONSE_EVIDENCE_DIAGNOSTICS] content omitted",
    OpenClawDiagnosticEvent.CLI_RETRY: "[OPENCLAW][CLI_LOCK_RETRY] content omitted",
    OpenClawDiagnosticEvent.PROCESS_RESULT: "[OPENCLAW][PROCESS_RESULT] content omitted",
    OpenClawDiagnosticEvent.EXECUTION_FAILURE: "[OPENCLAW][EXECUTION_DIAGNOSTICS] content omitted",
    OpenClawDiagnosticEvent.EXECUTION_RETRY: "[OPENCLAW][EXECUTION_RETRY] content omitted",
    OpenClawDiagnosticEvent.SESSION_START: "[OPENCLAW][SESSION_START] content omitted",
    OpenClawDiagnosticEvent.SESSION_READY: "[OPENCLAW][SESSION_READY] content omitted",
    OpenClawDiagnosticEvent.LOG_STREAM_START: "[OPENCLAW][LOG_STREAM_START] content omitted",
    OpenClawDiagnosticEvent.RESPONSE_PARSER: "[OPENCLAW][RESPONSE_PARSER] content omitted",
    OpenClawDiagnosticEvent.WORKSPACE_GUARD: "[OPENCLAW][WORKSPACE_GUARD] content omitted",
    OpenClawDiagnosticEvent.GENERIC_DIAGNOSTICS: "[OPENCLAW][DIAGNOSTICS] content omitted",
}

_LABEL_EVENTS = {
    "BOUNDED_EXECUTION_DEBUG_REPAIR": OpenClawDiagnosticEvent.DEBUG_REPAIR_DIAGNOSTICS,
    "PHASE7F_DEBUG_REPAIR": OpenClawDiagnosticEvent.DEBUG_REPAIR_DIAGNOSTICS,
    "EVALUATOR_RESPONSE_EVIDENCE": OpenClawDiagnosticEvent.EVALUATOR_DIAGNOSTICS,
    "PLANNING": OpenClawDiagnosticEvent.PLANNING_DIAGNOSTICS,
    "MINIMAL_PLANNING": OpenClawDiagnosticEvent.PLANNING_DIAGNOSTICS,
    "ULTRA_MINIMAL_PLANNING": OpenClawDiagnosticEvent.PLANNING_DIAGNOSTICS,
    "PLANNING_DISCOVERY": OpenClawDiagnosticEvent.PLANNING_DISCOVERY_DIAGNOSTICS,
}


def event_for_diagnostic_label(label: Any) -> OpenClawDiagnosticEvent:
    """Map reviewed diagnostic labels to fixed event labels."""

    if type(label) is str:
        return _LABEL_EVENTS.get(label, OpenClawDiagnosticEvent.GENERIC_DIAGNOSTICS)
    return OpenClawDiagnosticEvent.GENERIC_DIAGNOSTICS


def event_for_invocation_kind(kind: Any) -> OpenClawDiagnosticEvent:
    """Map the bounded runtime kind to its fixed event label."""

    if kind == "debug_repair":
        return OpenClawDiagnosticEvent.DEBUG_REPAIR_DIAGNOSTICS
    if kind == "planning":
        return OpenClawDiagnosticEvent.PLANNING_DIAGNOSTICS
    return OpenClawDiagnosticEvent.GENERIC_DIAGNOSTICS


def openclaw_diagnostic_message(event: OpenClawDiagnosticEvent | str) -> str:
    """Return a fixed, content-free message for a metadata-only event."""

    normalized_event = _event_value(event)
    return _EVENT_MESSAGES[normalized_event]


def build_openclaw_diagnostic_metadata(
    event: OpenClawDiagnosticEvent | str,
    diagnostics: Mapping[str, Any] | None = None,
    *,
    failure_category: str | None = None,
) -> OpenClawDiagnosticMetadataV2:
    """Project runtime diagnostics into the strict v2 metadata allowlist."""

    source = diagnostics if isinstance(diagnostics, Mapping) else {}
    response_details = source.get("response_diagnostics")
    if not isinstance(response_details, Mapping):
        response_details = {}

    candidate: dict[str, Any] = {
        "diagnostic_schema_version": 2,
        "content_omitted": True,
        "diagnostic_event": _event_value(event),
    }
    _copy_enum(
        candidate,
        "diagnostic_category",
        source.get("diagnostic_category"),
        _DIAGNOSTIC_CATEGORIES,
    )
    _copy_enum(
        candidate,
        "failure_category",
        failure_category or source.get("failure_category"),
        _FAILURE_CATEGORIES,
    )

    return_code = source.get("return_code", source.get("cli_return_code"))
    if _is_int(return_code) and -255 <= return_code <= 255:
        candidate["cli_return_code"] = return_code

    _copy_number(
        candidate,
        "duration_seconds",
        _first(source, "duration_seconds", "planning_duration"),
    )
    _copy_number(candidate, "timeout_seconds", source.get("timeout_seconds"))
    _copy_number(
        candidate,
        "first_output_delay_seconds",
        _first(source, "first_output_delay_seconds", "first_output_after_seconds"),
    )
    _copy_number(
        candidate, "max_silent_gap_seconds", source.get("max_silent_gap_seconds")
    )
    _copy_number(candidate, "cleanup_seconds", source.get("cleanup_seconds"))
    _copy_count(
        candidate,
        "stdout_byte_count",
        _first(source, "stdout_byte_count", "stdout_byte_length"),
    )
    _copy_count(
        candidate,
        "stderr_byte_count",
        _first(source, "stderr_byte_count", "stderr_byte_length"),
    )
    _copy_count(
        candidate,
        "stdout_character_count",
        _first(source, "stdout_character_count", "stdout_chars"),
    )
    _copy_count(
        candidate,
        "stderr_character_count",
        _first(source, "stderr_character_count", "stderr_chars"),
    )
    _copy_count(
        candidate,
        "stdout_line_count",
        _first(source, "stdout_line_count", "stdout_lines"),
    )
    _copy_count(
        candidate,
        "stderr_line_count",
        _first(source, "stderr_line_count", "stderr_lines"),
    )

    for key in ("stdout_sha256", "stderr_sha256"):
        value = source.get(key)
        if type(value) is str and _SHA256.fullmatch(value):
            candidate[key] = value

    stream_channel = source.get("stream_channel")
    _copy_enum(candidate, "stream_channel", stream_channel, _CHANNELS)
    _copy_count(candidate, "stream_byte_count", source.get("stream_byte_count"))
    stream_digest = source.get("stream_sha256")
    if type(stream_digest) is str and _SHA256.fullmatch(stream_digest):
        candidate["stream_sha256"] = stream_digest

    response_present = _first(
        source,
        "response_present",
        "partial_response_seen",
        "provider_response_received",
    )
    _copy_bool(candidate, "response_present", response_present)
    _copy_bool(
        candidate,
        "stderr_diagnostic_only",
        _first(source, "stderr_diagnostic_only", "stderr_contains_only_logs"),
    )
    response_channel = _first(
        source,
        "selected_response_channel",
        "response_channel",
        "output_channel_used",
        "selected_assistant_content_source",
    )
    _copy_enum(candidate, "selected_response_channel", response_channel, _CHANNELS)

    recognized_field = _first(
        source,
        "recognized_response_field",
        "selected_assistant_content_field",
    )
    if recognized_field is None:
        recognized_fields = response_details.get("recognized_assistant_content_fields")
        if isinstance(recognized_fields, list):
            recognized_field = next(
                (
                    item
                    for item in recognized_fields
                    if type(item) is str and item in _RESPONSE_FIELDS
                ),
                None,
            )
    _copy_enum(
        candidate, "recognized_response_field", recognized_field, _RESPONSE_FIELDS
    )

    parser_outcome = source.get("parser_outcome")
    if parser_outcome is None:
        parser_outcome = source.get("json_parsing_outcome") or response_details.get(
            "json_parsing_outcome"
        )
        if parser_outcome == "FAILED":
            parser_outcome = "invalid_json"
        elif type(parser_outcome) is str and parser_outcome in {"SUCCEEDED", "SUCCESS"}:
            parser_outcome = "parsed"
    _copy_enum(candidate, "parser_outcome", parser_outcome, _PARSER_OUTCOMES)

    extraction_category = _first(source, "extraction_category", "diagnostic_category")
    if extraction_category is None:
        extraction_category = response_details.get("diagnostic_category")
    _copy_enum(
        candidate, "extraction_category", extraction_category, _EXTRACTION_CATEGORIES
    )

    _copy_enum(
        candidate,
        "activity_classification",
        source.get("activity_classification"),
        _ACTIVITY_CLASSIFICATIONS,
    )
    _copy_enum(
        candidate,
        "pollution_classification",
        source.get("pollution_classification"),
        {
            "canonical_scaffold",
            "runtime_workspace_scaffold",
            "unclassified_entries",
        },
    )
    _copy_count(candidate, "pollution_entry_count", source.get("pollution_entry_count"))
    _copy_enum(
        candidate, "activity_state", source.get("activity_state"), _ACTIVITY_STATES
    )
    _copy_enum(
        candidate,
        "timeout_boundary",
        source.get("timeout_boundary"),
        _TIMEOUT_BOUNDARIES,
    )
    _copy_enum(
        candidate, "cleanup_status", source.get("cleanup_status"), _CLEANUP_STATUSES
    )

    invocation_kind = source.get("invocation_kind")
    if type(invocation_kind) is str and invocation_kind in {"planning", "debug_repair"}:
        candidate["invocation_kind"] = invocation_kind

    _copy_count(candidate, "retry_attempt", source.get("retry_attempt"))
    _copy_count(candidate, "retry_max_attempts", source.get("retry_max_attempts"))
    _copy_number(candidate, "retry_delay_seconds", source.get("retry_delay_seconds"))

    _copy_bool(candidate, "timed_out", source.get("timed_out"))
    _copy_bool(candidate, "interrupted", _first(source, "interrupted", "cancelled"))
    _copy_bool(candidate, "no_output_timeout", source.get("no_output_timeout"))
    _copy_bool(candidate, "stream_stalled", source.get("stream_stalled"))
    _copy_bool(
        candidate, "truncated", _first(source, "truncated", "truncated_output_detected")
    )
    capture_error = source.get("capture_error")
    if capture_error is None:
        capture_errors = [
            source.get("stdout_capture_error"),
            source.get("stderr_capture_error"),
        ]
        if any(type(value) is bool for value in capture_errors):
            capture_error = any(value is True for value in capture_errors)
    _copy_bool(candidate, "capture_error", capture_error)
    _copy_bool(
        candidate,
        "provider_invocation_started",
        source.get("provider_invocation_started"),
    )
    _copy_bool(
        candidate, "response_boundary_reached", source.get("response_boundary_reached")
    )

    runtime_status = source.get("runtime_status")
    if type(runtime_status) is not str or runtime_status not in _RUNTIME_STATUSES:
        if source.get("timed_out") is True:
            runtime_status = "timed_out"
        elif source.get("cancelled") is True:
            runtime_status = "interrupted"
        elif _is_int(return_code):
            runtime_status = "completed" if return_code == 0 else "failed"
        elif source.get("provider_invocation_started") is True:
            runtime_status = "running"
        else:
            runtime_status = "unknown"
    candidate["runtime_status"] = runtime_status

    correlation_id = source.get("correlation_id", source.get("invocation_id"))
    if type(correlation_id) is str and _UUID.fullmatch(correlation_id):
        candidate["correlation_id"] = correlation_id

    return normalize_openclaw_diagnostic_metadata(candidate) or {
        "diagnostic_schema_version": 2,
        "content_omitted": True,
        "diagnostic_event": _event_value(event),
        "runtime_status": "unknown",
    }


def normalize_openclaw_diagnostic_metadata(
    value: Any,
) -> OpenClawDiagnosticMetadataV2 | None:
    """Validate untrusted stored v2 metadata and drop every non-contract field."""

    if (
        not isinstance(value, Mapping)
        or type(value.get("diagnostic_schema_version")) is not int
    ):
        return None
    if value.get("diagnostic_schema_version") != 2:
        return None

    event_raw = value.get("diagnostic_event")
    event = _event_value(event_raw)
    normalized: OpenClawDiagnosticMetadataV2 = {
        "diagnostic_schema_version": 2,
        "content_omitted": True,
        "diagnostic_event": event,
    }
    for key, choices in (
        ("diagnostic_category", _DIAGNOSTIC_CATEGORIES),
        ("failure_category", _FAILURE_CATEGORIES),
        ("parser_outcome", _PARSER_OUTCOMES),
        ("extraction_category", _EXTRACTION_CATEGORIES),
        ("activity_classification", _ACTIVITY_CLASSIFICATIONS),
        ("activity_state", _ACTIVITY_STATES),
        ("timeout_boundary", _TIMEOUT_BOUNDARIES),
        ("cleanup_status", _CLEANUP_STATUSES),
        ("stream_channel", _CHANNELS),
        ("selected_response_channel", _CHANNELS),
        ("recognized_response_field", _RESPONSE_FIELDS),
        ("invocation_kind", {"planning", "debug_repair"}),
        ("runtime_status", _RUNTIME_STATUSES),
        (
            "pollution_classification",
            {
                "canonical_scaffold",
                "runtime_workspace_scaffold",
                "unclassified_entries",
            },
        ),
    ):
        _copy_enum(normalized, key, value.get(key), choices)

    for key in (
        "cli_return_code",
        "stdout_byte_count",
        "stderr_byte_count",
        "stdout_character_count",
        "stderr_character_count",
        "stdout_line_count",
        "stderr_line_count",
        "stream_byte_count",
        "retry_attempt",
        "retry_max_attempts",
        "pollution_entry_count",
    ):
        item = value.get(key)
        if key == "cli_return_code":
            if _is_int(item) and -255 <= item <= 255:
                normalized[key] = item  # type: ignore[literal-required]
        else:
            _copy_count(normalized, key, item)

    for key in (
        "duration_seconds",
        "timeout_seconds",
        "first_output_delay_seconds",
        "max_silent_gap_seconds",
        "cleanup_seconds",
        "retry_delay_seconds",
    ):
        _copy_number(normalized, key, value.get(key))

    for key in ("stdout_sha256", "stderr_sha256", "stream_sha256"):
        item = value.get(key)
        if type(item) is str and _SHA256.fullmatch(item):
            normalized[key] = item  # type: ignore[literal-required]

    for key in (
        "response_present",
        "stderr_diagnostic_only",
        "timed_out",
        "interrupted",
        "no_output_timeout",
        "stream_stalled",
        "truncated",
        "capture_error",
        "provider_invocation_started",
        "response_boundary_reached",
    ):
        _copy_bool(normalized, key, value.get(key))

    correlation_id = value.get("correlation_id")
    if type(correlation_id) is str and _UUID.fullmatch(correlation_id):
        normalized["correlation_id"] = correlation_id

    return normalized


def _event_value(value: Any) -> OpenClawDiagnosticEvent:
    if isinstance(value, OpenClawDiagnosticEvent):
        return value
    if type(value) is str:
        try:
            return OpenClawDiagnosticEvent(value)
        except ValueError:
            pass
    return OpenClawDiagnosticEvent.GENERIC_DIAGNOSTICS


def _first(source: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return None


def _is_int(value: Any) -> bool:
    return type(value) is int


def _copy_count(target: dict[str, Any], key: str, value: Any) -> None:
    if _is_int(value) and 0 <= value <= 2**63 - 1:
        target[key] = value


def _copy_number(target: dict[str, Any], key: str, value: Any) -> None:
    if type(value) not in {int, float}:
        return
    number = float(value)
    if math.isfinite(number) and 0 <= number <= 2**31:
        target[key] = number


def _copy_bool(target: dict[str, Any], key: str, value: Any) -> None:
    if type(value) is bool:
        target[key] = value


def _copy_enum(
    target: dict[str, Any], key: str, value: Any, choices: frozenset[str] | set[str]
) -> None:
    if type(value) is str and value in choices:
        target[key] = value
