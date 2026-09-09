"""PHASE35-PVH1 durable, provider-validation harness.

This module is validation infrastructure only.  It imports the production
grounding contracts and stores bounded provider events before doing any report
normalization.  It does not run providers, alter prompts, or participate in
the application runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.planning.grounding.contracts import (
    GroundingBudgetDelta,
    GroundingBudgetSnapshot,
    GroundingObservation,
    GroundingOutcome,
    GroundingRequest,
    GroundingSearchHit,
    StructuralIdentity,
    parse_grounding_request,
)
from app.services.orchestration.planning.grounding.coordinator_contracts import (
    GroundingAssessment,
    GroundingEvidence,
    GroundingLifecycleState,
    GroundingRejection,
    GroundingRequestStateSignal,
    GroundingResult,
    GroundingStateProjection,
    GroundingTaskReference,
    GroundingTerminalReason,
)


VALIDATION_LABELS = ("A1", "A2", "A3", "B1", "B2", "B3")

VALIDATION_CAPTURE_SCHEMA_VERSION = "pvh1-capture/2"
ACTION_CAPTURE_SCHEMA_VERSION = "grounding-action-capture/1"
PROMPT_CAPTURE_SCHEMA_VERSION = "grounding-prompt-capture/1"
MAX_CAPTURED_STRING = 500
MAX_CAPTURED_PREFIX = 256
MAX_CAPTURED_ITEMS = 32
# The current task admission boundary is 50,000 characters.  This larger
# validation-only ceiling leaves room for the bounded typed grounding state
# and is fail-closed if a future provider prompt exceeds it; it never silently
# truncates a prompt called replayable.
MAX_CAPTURED_PROMPT_BYTES = 256 * 1024
MAX_CAPTURED_PROVIDER_CANDIDATE_BYTES = 64 * 1024
# Legal normalized action fields are bounded by the production contract.  The
# capture keeps the typed mapping exact; this value is a proof/reporting
# ceiling for the currently exercised bounded action shapes, not a truncation
# limit for legal normalized mappings.
MAX_LEGAL_ACTION_BYTES = 16 * 1024

_SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|cookie|password|secret|credential|api[_-]?key|"
    r"api[_-]?token|access[_-]?token|refresh[_-]?token|bearer|headers?)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT_RE = re.compile(
    r"(?:authorization|cookie|password|secret|credential|api[_-]?key|"
    r"api[_-]?token|access[_-]?token|refresh[_-]?token|bearer)"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+",
    re.IGNORECASE,
)

_PROVIDER_TURN_FIELDS = frozenset(
    {
        "grounding_run_id",
        "provider_request_id",
        "provider_request_number",
        "turn_type",
        "provider_provenance",
        "prompt_sha256",
        "prompt_length",
        "parser_input_type",
        "parser_input_length",
        "provider_output_type",
        "content_type",
        "json_decode_success",
        "top_level_json_type",
        "top_level_fields",
        "parser_success",
        "parser_rejection_code",
        "failure_layer",
        "action_kind",
        "decision_kind",
        "provider_duration_seconds",
        "failure_classification",
        "detail",
        "candidate_sha256",
        "candidate_length",
        "candidate_prefix",
        "capture_stage",
    }
)

_EVENT_FIELDS = {
    EventType.GROUNDING_STARTED: frozenset(
        {"grounding_run_id", "state", "orientation_available", "budget"}
    ),
    EventType.GROUNDING_REQUEST: frozenset(
        {
            "grounding_run_id",
            "request_id",
            "provider_request_id",
            "provider_request_number",
            "action",
            "normalized_request",
            "rejection_code",
            "protocol_rejection_code",
            "failure_layer",
            "outcome",
            "budget",
        }
    ),
    EventType.GROUNDING_PROVIDER_TURN: _PROVIDER_TURN_FIELDS,
    EventType.GROUNDING_OBSERVATION: frozenset(
        {
            "grounding_run_id",
            "observation_id",
            "request_id",
            "action",
            "outcome",
            "normalized_request",
            "evidence_bytes",
            "budget",
        }
    ),
    EventType.GROUNDING_ASSESSMENT: frozenset(
        {
            "grounding_run_id",
            "provider_request_id",
            "assessment_id",
            "assessment_decision",
            "cited_observation_ids",
            "outcome",
            "budget",
        }
    ),
    EventType.GROUNDING_TERMINAL: frozenset(
        {
            "grounding_run_id",
            "state",
            "terminal_state",
            "terminal_reason",
            "provider_requests",
            "repository_actions",
            "cited_observation_ids",
        }
    ),
}


def normalize_event_type(event_type: object) -> str:
    """Return the current event identity without assuming EventType is an Enum.

    The production ``EventType`` is a constants class containing strings.  A
    real Enum is accepted for generic callers, but ``.value`` is only read
    after the object has been proven to be an Enum instance.
    """

    if isinstance(event_type, str):
        return event_type
    if isinstance(event_type, Enum):
        enum_value = getattr(event_type, "value", None)
        if isinstance(enum_value, str):
            return enum_value
        if isinstance(event_type.name, str):
            return event_type.name
    raise TypeError("event type must be a canonical string or Enum identity")


def _enum_identity(value: object) -> object:
    if isinstance(value, Enum):
        enum_value = getattr(value, "value", None)
        return enum_value if isinstance(enum_value, str) else value.name
    return value


def _bounded_bytes(value: bytes) -> dict[str, object]:
    return {
        "length": len(value),
        "sha256": hashlib.sha256(value).hexdigest(),
        "prefix": value[:MAX_CAPTURED_PREFIX].decode("utf-8", errors="replace"),
        "prefix_length": min(len(value), MAX_CAPTURED_PREFIX),
    }


def _is_sensitive_key(value: object) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(str(value)))


def _safe_text(value: str) -> str:
    return _SENSITIVE_TEXT_RE.sub("<redacted-secret>", value)


def _exact_json(value: object) -> object:
    """Convert already-typed JSON data without the diagnostic truncation."""

    if isinstance(value, Mapping):
        return {
            str(key): _exact_json(item)
            for key, item in value.items()
            if not _is_sensitive_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_exact_json(item) for item in value]
    value = _enum_identity(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    raise TypeError(f"value is not JSON-safe: {type(value).__name__}")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _exact_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _bounded_json(value: object, *, _depth: int = 0) -> object:
    """Keep diagnostics JSON-safe and bounded without retaining raw payloads."""

    if _depth > 4:
        return "<bounded-depth>"
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_json(item, _depth=_depth + 1)
            for key, item in list(value.items())[:MAX_CAPTURED_ITEMS]
            if not _is_sensitive_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded_json(item, _depth=_depth + 1)
            for item in list(value)[:MAX_CAPTURED_ITEMS]
        ]
    if isinstance(value, bytes):
        return _bounded_bytes(value)
    value = _enum_identity(value)
    if isinstance(value, str):
        return _safe_text(value[:MAX_CAPTURED_STRING])
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:MAX_CAPTURED_STRING]


def _event_details(event_type: str, details: Mapping[str, Any]) -> Mapping[str, Any]:
    allowed = _EVENT_FIELDS.get(event_type, frozenset())
    bounded = {
        key: (
            _exact_json(details[key])
            if key == "normalized_request"
            else _bounded_json(details[key])
        )
        for key in sorted(allowed)
        if key in details and not _is_sensitive_key(key)
    }
    unknown = sorted(
        str(key)
        for key in details
        if str(key) not in allowed and not _is_sensitive_key(key)
    )
    if unknown:
        bounded["unrecorded_field_names"] = unknown[:MAX_CAPTURED_ITEMS]
    return MappingProxyType(bounded)


def _serialize_budget_delta(value: GroundingBudgetDelta) -> dict[str, int]:
    return {
        "provider_requests": value.provider_requests,
        "repository_actions": value.repository_actions,
        "source_evidence_bytes": value.source_evidence_bytes,
        "distinct_files": value.distinct_files,
        "positive_regions": value.positive_regions,
    }


def _serialize_budget_snapshot(value: GroundingBudgetSnapshot) -> dict[str, int]:
    return {
        "provider_requests": value.provider_requests,
        "repository_actions": value.repository_actions,
        "source_evidence_bytes": value.source_evidence_bytes,
        "distinct_files": value.distinct_files,
        "positive_regions": value.positive_regions,
    }


def serialize_structural_identity(
    identity: StructuralIdentity | None,
) -> dict[str, object] | None:
    """Serialize the actual production StructuralIdentity fields."""

    if identity is None:
        return None
    return {
        "relation": _enum_identity(identity.relation),
        "source_path": identity.source_path,
        "symbol_name": identity.symbol_name,
        "handler_name": identity.handler_name,
        "http_method": identity.http_method,
        "decorator_path": identity.decorator_path,
        "local_router_prefix": identity.local_router_prefix,
        "effective_route_path": identity.effective_route_path,
        "mount_chain": list(identity.mount_chain),
        "start_line": identity.start_line,
        "end_line": identity.end_line,
        "start_byte": identity.start_byte,
        "end_byte": identity.end_byte,
        "handler_identity": identity.handler_identity,
    }


def _serialize_hit(hit: GroundingSearchHit) -> dict[str, object]:
    return {
        "path": hit.path,
        "line_number": hit.line_number,
        "snippet": hit.snippet[:MAX_CAPTURED_STRING],
    }


def serialize_grounding_observation(
    observation: GroundingObservation,
) -> dict[str, object]:
    """Serialize only fields present on the production observation contract."""

    if not isinstance(observation, GroundingObservation):
        raise TypeError("observation must be the production GroundingObservation")
    return {
        "schema_version": observation.schema_version,
        "observation_id": observation.observation_id,
        "grounding_run_id": observation.grounding_run_id,
        "request_id": observation.request_id,
        "action_identity": observation.action_identity,
        "normalized_action": _exact_json(observation.normalized_action),
        "outcome": _enum_identity(observation.outcome),
        "source_paths": list(observation.source_paths),
        "source_scopes": list(observation.source_scopes),
        "normalized_query": observation.normalized_query,
        "structural_identity": serialize_structural_identity(
            observation.structural_identity
        ),
        "hits": [_serialize_hit(hit) for hit in observation.hits],
        "bounded_content": _bounded_bytes(observation.bounded_content),
        "structural_facts": _bounded_json(observation.structural_facts),
        "source_versions": _bounded_json(observation.source_versions),
        "source_hashes": _bounded_json(observation.source_hashes),
        "workspace_identity": observation.workspace_identity,
        "snapshot_identity": observation.snapshot_identity,
        "provenance": _enum_identity(observation.provenance),
        "truncated": observation.truncated,
        "result_count": observation.result_count,
        "result_limit": observation.result_limit,
        "budget_delta": _serialize_budget_delta(observation.budget_delta),
        "budget_cumulative": _serialize_budget_snapshot(observation.budget_cumulative),
    }


def _serialize_request(request: GroundingRequest) -> dict[str, object]:
    return {
        "grounding_run_id": request.grounding_run_id,
        "request_id": request.request_id,
        "action_identity": request.action_identity,
        "normalized_action": _exact_json(request.normalized_payload),
        "provenance": _enum_identity(request.provenance),
        "action_digest": request.action_digest,
    }


def _serialize_task_reference(value: GroundingTaskReference) -> dict[str, object]:
    if not isinstance(value, GroundingTaskReference):
        raise TypeError("task reference must be the production task reference")
    return {"task_id": value.task_id, "task_execution_id": value.task_execution_id}


def _serialize_lifecycle_state(value: GroundingLifecycleState) -> str:
    if not isinstance(value, GroundingLifecycleState):
        raise TypeError("terminal state must be the production lifecycle enum")
    return str(_enum_identity(value))


def _serialize_terminal_reason(value: GroundingTerminalReason) -> str:
    if not isinstance(value, GroundingTerminalReason):
        raise TypeError("terminal reason must be the production terminal enum")
    return str(_enum_identity(value))


def _serialize_rejection(rejection: GroundingRejection) -> dict[str, object]:
    return {
        "rejection_id": rejection.rejection_id,
        "grounding_run_id": rejection.grounding_run_id,
        "provider_request_id": rejection.provider_request_id,
        "code": rejection.code,
        "action_kind": rejection.action_kind,
        "message": rejection.message[:MAX_CAPTURED_STRING],
    }


def _serialize_assessment(assessment: GroundingAssessment) -> dict[str, object]:
    return {
        "assessment_id": assessment.assessment_id,
        "grounding_run_id": assessment.grounding_run_id,
        "kind": _enum_identity(assessment.kind),
        "provider_request_id": assessment.provider_request_id,
        "after_observation_ids": list(assessment.after_observation_ids),
        "next_action": (
            _serialize_request(assessment.next_action)
            if assessment.next_action is not None
            else None
        ),
        "cited_observation_ids": list(assessment.cited_observation_ids),
        "cited_source_paths": list(assessment.cited_source_paths),
        "cited_structural_identities": [
            serialize_structural_identity(identity)
            for identity in assessment.cited_structural_identities
        ],
        "rationale": assessment.rationale[:MAX_CAPTURED_STRING],
        "unresolved_risk": assessment.unresolved_risk,
        "next_action_digest": assessment.next_action_digest,
        "terminal_reason": _enum_identity(assessment.terminal_reason),
    }


def _serialize_signal(signal: GroundingRequestStateSignal) -> dict[str, object]:
    return {
        "kind": _enum_identity(signal.kind),
        "grounding_run_id": signal.grounding_run_id,
        "action_digest": signal.action_digest,
        "source_observation_id": signal.source_observation_id,
    }


def _serialize_projection(
    projection: GroundingStateProjection,
) -> dict[str, object]:
    return {
        "grounding_run_id": projection.grounding_run_id,
        "task_reference": _serialize_task_reference(projection.task_reference),
        "workspace_identity": projection.workspace_identity,
        "snapshot_identity": projection.snapshot_identity,
        "request_digests": list(projection.request_digests),
        "observation_ids": list(projection.observation_ids),
        "observation_outcomes": list(projection.observation_outcomes),
        "assessment_history": [
            _serialize_assessment(item) for item in projection.assessment_history
        ],
        "attempted_action_digests": list(projection.attempted_action_digests),
        "request_state_signals": [
            _serialize_signal(item) for item in projection.request_state_signals
        ],
        "discovered_source_paths": list(projection.discovered_source_paths),
        "discovered_structural_identities": [
            serialize_structural_identity(item)
            for item in projection.discovered_structural_identities
        ],
        "source_versions": _bounded_json(projection.source_versions),
        "remaining_budget": _bounded_json(projection.remaining_budget),
        "termination_state": _enum_identity(projection.termination_state),
        "lifecycle_state": _enum_identity(projection.lifecycle_state),
        "lifecycle_history": [
            _enum_identity(item) for item in projection.lifecycle_history
        ],
        "requests": [_serialize_request(item) for item in projection.requests],
        "observations": [
            serialize_grounding_observation(item) for item in projection.observations
        ],
        "rejections": [_serialize_rejection(item) for item in projection.rejections],
    }


def _serialize_evidence(evidence: GroundingEvidence) -> dict[str, object]:
    return {
        "observation_id": evidence.observation_id,
        "source_path": evidence.source_path,
        "source_version": evidence.source_version,
        "bounded_content": _bounded_bytes(evidence.bounded_content),
    }


def serialize_grounding_result(result: GroundingResult) -> dict[str, object]:
    """Serialize the actual production GroundingResult, without a report-only field."""

    if not isinstance(result, GroundingResult):
        raise TypeError("result must be the production GroundingResult")
    return {
        "grounding_run_id": result.grounding_run_id,
        "terminal_state": _serialize_lifecycle_state(result.terminal_state),
        "terminal_reason": _serialize_terminal_reason(result.terminal_reason),
        "state_projection": _serialize_projection(result.state_projection),
        "schema_version": result.schema_version,
        "orientation_advisory": _bounded_json(result.orientation_advisory),
        "requests": [_serialize_request(item) for item in result.requests],
        "observations": [
            serialize_grounding_observation(item) for item in result.observations
        ],
        "assessments": [_serialize_assessment(item) for item in result.assessments],
        "rejections": [_serialize_rejection(item) for item in result.rejections],
        "budget_snapshot": _serialize_budget_snapshot(result.budget_snapshot),
        "provider_request_count": result.provider_request_count,
        "repository_action_count": result.repository_action_count,
        "cited_observation_ids": list(result.cited_observation_ids),
        "cited_source_paths": list(result.cited_source_paths),
        "cited_structural_identities": [
            serialize_structural_identity(item)
            for item in result.cited_structural_identities
        ],
        "source_versions": _bounded_json(result.source_versions),
        "cited_source_evidence": [
            _serialize_evidence(item) for item in result.cited_source_evidence
        ],
        "budget_trace": [
            _serialize_budget_snapshot(item) for item in result.budget_trace
        ],
        "unresolved_risk": result.unresolved_risk,
        "provider_model_telemetry": _bounded_json(result.provider_model_telemetry),
        "grounding_diagnostics": _bounded_json(result.grounding_diagnostics),
    }


@dataclass(frozen=True, slots=True)
class RawCapturedEvent:
    """One bounded event retained before report normalization."""

    sequence: int
    event_type: str
    details: Mapping[str, Any]


class ValidationCaptureError(RuntimeError):
    """A validation artifact would be incomplete or unsafe to retain."""


@dataclass(frozen=True, slots=True)
class RawProviderTurnCapture:
    """Exact bounded provider-boundary data captured before adapter parsing."""

    sequence: int
    grounding_run_id: str
    provider_request_id: str
    provider_request_number: int
    turn_mode: str | None
    turn_type: str | None
    provider_provenance: Mapping[str, object]
    provider_config_fingerprint: str
    prompt_sha256: str
    prompt_length: int
    prompt_utf8_length: int
    exact_prompt_utf8: str
    candidate_sha256: str | None = None
    candidate_length: int | None = None
    candidate_prefix: str | None = None
    candidate_raw_utf8: str | None = None
    candidate_payload: Mapping[str, object] | None = None
    action_candidate_payload: Mapping[str, object] | None = None
    action_kind: str | None = None
    normalized_action: Mapping[str, object] | None = None
    action_digest: str | None = None
    request_id: str | None = None
    parser_input_type: str | None = None
    provider_output_type: str | None = None
    response_present: bool = False
    failure_classification: str | None = None


def _provider_request_number(provider_request_id: str, fallback: int) -> int:
    match = re.search(r"-(\d+)$", provider_request_id)
    return int(match.group(1)) if match else fallback


def _turn_mode(turn_type: object) -> str | None:
    normalized = str(turn_type or "").strip()
    if normalized == "REJECTION_CORRECTION":
        return "CORRECTION"
    if normalized == "TERMINAL_ASSESSMENT":
        return "TERMINAL_ASSESSMENT"
    if normalized in {"FIRST_ACTION", "POST_OBSERVATION_ASSESSMENT"}:
        return "EXPLORATION"
    return normalized or None


def _provider_identity(provider: object) -> tuple[dict[str, object], str]:
    """Keep provider identity useful without retaining config or diagnostics."""

    runtime_name = model = adaptation_profile = None
    try:
        runtime = provider.runtime_information()  # type: ignore[attr-defined]
        runtime_name = getattr(runtime, "runtime_name", None)
        model = getattr(runtime, "model", None)
        adaptation_profile = getattr(runtime, "adaptation_profile", None)
    except Exception:
        pass
    capabilities: Mapping[str, object] = {}
    try:
        value = provider.capabilities  # type: ignore[attr-defined]
        capabilities = value.to_dict() if hasattr(value, "to_dict") else {}
    except Exception:
        pass
    identity = {
        "provider": str(getattr(provider, "name", "unknown")),
        "provider_version": str(getattr(provider, "version", "") or "") or None,
        "backend": str(runtime_name or "") or None,
        "model": str(model or "") or None,
        "adaptation_profile": str(adaptation_profile or "") or None,
        "capabilities": _exact_json(capabilities),
    }
    fingerprint = hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()
    return identity, fingerprint


def _candidate_payload(
    candidate: object,
) -> tuple[Mapping[str, object] | None, bool | None]:
    if isinstance(candidate, Mapping):
        return candidate, True
    if isinstance(candidate, str):
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            return None, False
        return (value, True) if isinstance(value, Mapping) else (None, True)
    return None, None


def _action_candidate(
    payload: Mapping[str, object] | None
) -> Mapping[str, object] | None:
    if payload is None:
        return None
    if "action" in payload:
        return payload
    next_action = payload.get("next_action")
    if isinstance(next_action, Mapping) and "action" in next_action:
        return next_action
    return None


def _recognized_action_fields(
    payload: Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    """Retain known action fields even when an invalid object is oversized."""

    action = _action_candidate(payload)
    if action is None:
        return None
    action_kind = action.get("action")
    if action_kind == "search_text":
        names = ("action", "query", "scopes")
    elif action_kind == "inspect_file":
        names = ("action", "path")
    elif action_kind == "resolve_structure":
        names = ("action", "relation", "locator")
    else:
        names = ("action",)
    recognized = {name: action[name] for name in names if name in action}
    locator = recognized.get("locator")
    relation = recognized.get("relation")
    if isinstance(locator, Mapping) and relation in {
        "symbol_definition",
        "enclosing_symbol",
        "mounted_route",
    }:
        locator_names = {
            "symbol_definition": ("path", "name"),
            "enclosing_symbol": ("path", "line"),
            "mounted_route": ("path", "method", "decorator_path"),
        }[str(relation)]
        recognized["locator"] = {
            name: locator[name] for name in locator_names if name in locator
        }
    return recognized


def _candidate_bytes(candidate: object, payload: Mapping[str, object] | None) -> bytes:
    if isinstance(candidate, bytes):
        return candidate
    if isinstance(candidate, str):
        return candidate.encode("utf-8")
    if payload is not None:
        try:
            return _canonical_json_bytes(payload)
        except TypeError:
            pass
    return str(candidate).encode("utf-8", errors="replace")


def _bounded_exact_mapping(
    value: Mapping[str, object] | None, *, maximum_bytes: int
) -> Mapping[str, object] | None:
    if value is None:
        return None
    try:
        normalized = _exact_json(value)
        encoded = _canonical_json_bytes(normalized)
    except TypeError:
        return None
    if len(encoded) > maximum_bytes:
        return None
    if not isinstance(normalized, Mapping):
        return None
    return dict(normalized)


def _provider_turn_mapping(turn: RawProviderTurnCapture) -> dict[str, object]:
    return {
        "capture_schema_version": VALIDATION_CAPTURE_SCHEMA_VERSION,
        "action_capture_schema_version": ACTION_CAPTURE_SCHEMA_VERSION,
        "prompt_capture_schema_version": PROMPT_CAPTURE_SCHEMA_VERSION,
        "sequence": turn.sequence,
        "grounding_run_id": turn.grounding_run_id,
        "provider_request_id": turn.provider_request_id,
        "provider_request_number": turn.provider_request_number,
        "turn_mode": turn.turn_mode,
        "turn_type": turn.turn_type,
        "provider_provenance": _exact_json(turn.provider_provenance),
        "provider_config_fingerprint": turn.provider_config_fingerprint,
        "prompt_sha256": turn.prompt_sha256,
        "prompt_length": turn.prompt_length,
        "prompt_utf8_length": turn.prompt_utf8_length,
        "exact_prompt_utf8": turn.exact_prompt_utf8,
        "candidate_sha256": turn.candidate_sha256,
        "candidate_length": turn.candidate_length,
        "candidate_prefix": turn.candidate_prefix,
        "candidate_raw_utf8": turn.candidate_raw_utf8,
        "candidate_payload": (
            _exact_json(turn.candidate_payload)
            if turn.candidate_payload is not None
            else None
        ),
        "action_candidate_payload": (
            _exact_json(turn.action_candidate_payload)
            if turn.action_candidate_payload is not None
            else None
        ),
        "action_kind": turn.action_kind,
        "normalized_action": (
            _exact_json(turn.normalized_action)
            if turn.normalized_action is not None
            else None
        ),
        "action_digest": turn.action_digest,
        "request_id": turn.request_id,
        "parser_input_type": turn.parser_input_type,
        "provider_output_type": turn.provider_output_type,
        "response_present": turn.response_present,
        "failure_classification": turn.failure_classification,
    }


@dataclass(frozen=True, slots=True)
class RawRunCapture:
    """Failure-resistant per-label capture snapshot."""

    label: str
    grounding_run_id: str | None
    events: tuple[RawCapturedEvent, ...]
    results: tuple[GroundingResult, ...]
    provider_turns: tuple[RawProviderTurnCapture, ...] = ()

    @property
    def provider_turn_events(self) -> tuple[RawCapturedEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.event_type == EventType.GROUNDING_PROVIDER_TURN
        )


@dataclass(frozen=True, slots=True)
class ValidationArtifact:
    """Independent JSON artifact used for post-process and restart replay."""

    payload: Mapping[str, object]

    @classmethod
    def from_raw_capture(cls, raw: RawRunCapture) -> "ValidationArtifact":
        return cls(serialize_validation_artifact(raw))

    @classmethod
    def load(cls, path: str | Path) -> "ValidationArtifact":
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(loaded, Mapping):
            raise ValidationCaptureError("validation artifact must be a JSON object")
        if loaded.get("schema_version") != VALIDATION_CAPTURE_SCHEMA_VERSION:
            raise ValidationCaptureError("unsupported validation artifact schema")
        return cls(dict(loaded))

    def to_dict(self) -> dict[str, object]:
        value = _exact_json(self.payload)
        if not isinstance(value, dict):
            raise ValidationCaptureError("validation artifact payload is not an object")
        return value

    def to_json_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def write(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.to_json_bytes())

    @property
    def grounding_run_id(self) -> str | None:
        value = self.payload.get("grounding_run_id")
        return str(value) if value is not None else None

    def _turn(self, run_id: str, provider_request_number: int) -> Mapping[str, object]:
        if self.grounding_run_id != run_id:
            raise KeyError(f"validation artifact does not contain run {run_id}")
        turns = self.payload.get("provider_turns", ())
        if not isinstance(turns, Sequence) or isinstance(turns, (str, bytes)):
            raise ValidationCaptureError(
                "validation artifact provider turns are invalid"
            )
        for turn in turns:
            if not isinstance(turn, Mapping):
                continue
            if turn.get("provider_request_number") == provider_request_number:
                return turn
        raise KeyError(
            f"validation artifact does not contain provider turn {provider_request_number}"
        )

    def exact_normalized_action(
        self, run_id: str, provider_request_number: int
    ) -> Mapping[str, object]:
        action = self._turn(run_id, provider_request_number).get("normalized_action")
        if not isinstance(action, Mapping):
            raise KeyError("provider turn has no accepted normalized grounding action")
        return dict(action)

    def exact_provider_prompt(self, run_id: str, provider_request_number: int) -> str:
        turn = self._turn(run_id, provider_request_number)
        prompt = turn.get("exact_prompt_utf8")
        if not isinstance(prompt, str):
            raise KeyError("provider turn has no exact prompt capture")
        encoded = prompt.encode("utf-8")
        if turn.get("prompt_sha256") != hashlib.sha256(encoded).hexdigest():
            raise ValidationCaptureError("exact prompt hash does not match artifact")
        if turn.get("prompt_utf8_length") != len(encoded):
            raise ValidationCaptureError("exact prompt length does not match artifact")
        return prompt

    def exact_candidate_payload(
        self, run_id: str, provider_request_number: int
    ) -> Mapping[str, object]:
        candidate = self._turn(run_id, provider_request_number).get("candidate_payload")
        if not isinstance(candidate, Mapping):
            raise KeyError("provider turn has no bounded candidate object")
        return dict(candidate)


def serialize_validation_artifact(raw: RawRunCapture) -> dict[str, object]:
    """Serialize durable capture without evaluator/report-only state."""

    return {
        "schema_version": VALIDATION_CAPTURE_SCHEMA_VERSION,
        "label": raw.label,
        "grounding_run_id": raw.grounding_run_id,
        # Use the joined view so coordinator rejection codes and accepted
        # request links survive restart alongside the exact boundary capture.
        "provider_turns": list(_provider_turn_records(raw)),
        "events": [
            {
                "sequence": event.sequence,
                "event_type": event.event_type,
                "details": _exact_json(event.details),
            }
            for event in raw.events
        ],
        "results": [serialize_grounding_result(item) for item in raw.results],
    }


def exact_normalized_action(
    artifact: ValidationArtifact | str | Path,
    run_id: str,
    provider_request_number: int,
) -> Mapping[str, object]:
    value = (
        artifact
        if isinstance(artifact, ValidationArtifact)
        else ValidationArtifact.load(artifact)
    )
    return value.exact_normalized_action(run_id, provider_request_number)


def exact_provider_prompt(
    artifact: ValidationArtifact | str | Path,
    run_id: str,
    provider_request_number: int,
) -> str:
    value = (
        artifact
        if isinstance(artifact, ValidationArtifact)
        else ValidationArtifact.load(artifact)
    )
    return value.exact_provider_prompt(run_id, provider_request_number)


class RawBoundedCaptureStore:
    """Append-only bounded store owned by one future validation label."""

    def __init__(self, label: str, grounding_run_id: str | None = None) -> None:
        normalized_label = str(label).strip()
        if not normalized_label:
            raise ValueError("validation label must be non-empty")
        self.label = normalized_label
        self.grounding_run_id = grounding_run_id
        self._events: list[RawCapturedEvent] = []
        self._results: list[GroundingResult] = []
        self._provider_turns: list[RawProviderTurnCapture] = []
        self._provider_request_objects: dict[int, int] = {}

    def append_event(
        self, event_type: object, details: Mapping[str, Any]
    ) -> RawCapturedEvent:
        normalized_type = normalize_event_type(event_type)
        if not isinstance(details, Mapping):
            raise TypeError("grounding event details must be a mapping")
        event_run_id = details.get("grounding_run_id")
        if event_run_id is not None:
            event_run_id = str(event_run_id)
            if self.grounding_run_id is None:
                self.grounding_run_id = event_run_id
            elif event_run_id != self.grounding_run_id:
                raise ValueError("event belongs to another validation run")
        event = RawCapturedEvent(
            sequence=len(self._events) + 1,
            event_type=normalized_type,
            details=_event_details(normalized_type, details),
        )
        self._events.append(event)
        return event

    def capture_result(self, result: GroundingResult) -> GroundingResult:
        """Append the completed typed result before any report work occurs."""

        if not isinstance(result, GroundingResult):
            raise TypeError("result must be the production GroundingResult")
        if self.grounding_run_id is None:
            self.grounding_run_id = result.grounding_run_id
        elif result.grounding_run_id != self.grounding_run_id:
            raise ValueError("result belongs to another validation run")
        self._results.append(result)
        return result

    def capture_provider_request(self, request: Any, provider: object) -> int:
        """Capture the application request before the provider is invoked."""

        prompt = getattr(request, "prompt", None)
        if not isinstance(prompt, str) or not prompt:
            raise ValidationCaptureError(
                "provider request prompt must be non-empty text"
            )
        prompt_bytes = prompt.encode("utf-8")
        if len(prompt_bytes) > MAX_CAPTURED_PROMPT_BYTES:
            raise ValidationCaptureError(
                "provider prompt exceeds the exact validation capture ceiling"
            )
        request_metadata = getattr(request, "metadata", {})
        if not isinstance(request_metadata, Mapping):
            request_metadata = {}
        protocol_input = getattr(request, "protocol_input", {})
        if not isinstance(protocol_input, Mapping):
            protocol_input = {}
        provider_request_id = str(
            request_metadata.get("provider_request_id")
            or f"validation-provider-request-{len(self._provider_turns) + 1}"
        )
        provider_request_number = _provider_request_number(
            provider_request_id, len(self._provider_turns) + 1
        )
        provenance, config_fingerprint = _provider_identity(provider)
        turn = RawProviderTurnCapture(
            sequence=len(self._provider_turns) + 1,
            grounding_run_id=str(
                request_metadata.get("grounding_run_id") or self.grounding_run_id or ""
            ),
            provider_request_id=provider_request_id,
            provider_request_number=provider_request_number,
            turn_mode=(
                _turn_mode(
                    request_metadata.get("turn_mode")
                    or protocol_input.get("turn_mode")
                    or request_metadata.get("turn_type")
                )
                if request_metadata.get("turn_mode") is not None
                or protocol_input.get("turn_mode") is not None
                or request_metadata.get("turn_type") is not None
                else None
            ),
            turn_type=(
                str(request_metadata["turn_type"])
                if request_metadata.get("turn_type") is not None
                else None
            ),
            provider_provenance=provenance,
            provider_config_fingerprint=config_fingerprint,
            prompt_sha256=hashlib.sha256(prompt_bytes).hexdigest(),
            prompt_length=len(prompt),
            prompt_utf8_length=len(prompt_bytes),
            exact_prompt_utf8=prompt,
        )
        self._provider_request_objects[id(request)] = len(self._provider_turns)
        self._provider_turns.append(turn)
        if self.grounding_run_id is None:
            self.grounding_run_id = turn.grounding_run_id or None
        return turn.sequence

    def capture_provider_response(self, request: Any, response: Any) -> None:
        """Capture candidate evidence before adapter parsing can reject it."""

        index = self._provider_request_objects.get(id(request))
        if index is None:
            raise ValidationCaptureError("provider response has no captured request")
        candidate = getattr(response, "candidate_text", None)
        payload, _ = _candidate_payload(candidate)
        raw_bytes = (
            _candidate_bytes(candidate, payload) if candidate is not None else b""
        )
        exact_raw = (
            candidate
            if isinstance(candidate, str)
            and len(raw_bytes) <= MAX_CAPTURED_PROVIDER_CANDIDATE_BYTES
            else None
        )
        bounded_payload = _bounded_exact_mapping(
            payload, maximum_bytes=MAX_CAPTURED_PROVIDER_CANDIDATE_BYTES
        )
        action_payload = _action_candidate(payload)
        exact_action_payload = _bounded_exact_mapping(
            _recognized_action_fields(payload),
            maximum_bytes=MAX_LEGAL_ACTION_BYTES,
        )
        action_kind = (
            str(action_payload.get("action"))
            if action_payload is not None and action_payload.get("action") is not None
            else None
        )
        request_id = None
        normalized_action = None
        action_digest = None
        if action_payload is not None:
            attempted_request_id = self._next_candidate_request_id()
            try:
                parsed = parse_grounding_request(
                    action_payload,
                    grounding_run_id=self.grounding_run_id
                    or self._provider_turns[index].grounding_run_id,
                    request_id=attempted_request_id,
                )
            except (TypeError, ValueError):
                request_id = attempted_request_id
            else:
                request_id = parsed.request_id
                normalized_action = dict(_exact_json(parsed.normalized_payload))
                action_digest = parsed.action_digest
        current = self._provider_turns[index]
        self._provider_turns[index] = replace(
            current,
            candidate_sha256=(
                hashlib.sha256(raw_bytes).hexdigest() if candidate is not None else None
            ),
            candidate_length=(len(raw_bytes) if candidate is not None else None),
            candidate_prefix=(
                raw_bytes[:MAX_CAPTURED_PREFIX].decode("utf-8", errors="replace")
                if candidate is not None
                else None
            ),
            candidate_raw_utf8=exact_raw,
            candidate_payload=bounded_payload,
            action_candidate_payload=exact_action_payload,
            action_kind=action_kind,
            normalized_action=normalized_action,
            action_digest=action_digest,
            request_id=request_id,
            parser_input_type=(type(payload).__name__ if payload is not None else None),
            provider_output_type=(
                type(candidate).__name__ if candidate is not None else None
            ),
            response_present=True,
        )

    def capture_provider_failure(self, request: Any, error: BaseException) -> None:
        """Record failure identity without inventing a provider response."""

        index = self._provider_request_objects.get(id(request))
        if index is None:
            raise ValidationCaptureError("provider failure has no captured request")
        current = self._provider_turns[index]
        classification = getattr(error, "classification", None) or type(error).__name__
        self._provider_turns[index] = replace(
            current, failure_classification=str(classification), response_present=False
        )

    def _next_candidate_request_id(self) -> str:
        accepted_or_parsed = sum(
            turn.normalized_action is not None for turn in self._provider_turns
        )
        return f"grounding-request-{accepted_or_parsed + 1}"

    def snapshot(self) -> RawRunCapture:
        return RawRunCapture(
            label=self.label,
            grounding_run_id=self.grounding_run_id,
            events=tuple(self._events),
            results=tuple(self._results),
            provider_turns=tuple(self._provider_turns),
        )


class ValidationRun:
    """Run-local façade used as an event sink and finalization boundary."""

    def __init__(self, label: str, grounding_run_id: str | None = None) -> None:
        self.store = RawBoundedCaptureStore(label, grounding_run_id)

    @property
    def label(self) -> str:
        return self.store.label

    def event_sink(self, event_type: object, details: Mapping[str, Any]) -> None:
        self.store.append_event(event_type, details)

    def capture_result(self, result: GroundingResult) -> GroundingResult:
        return self.store.capture_result(result)

    def capture_provider(self, provider: object) -> "ValidationProviderProxy":
        """Wrap a provider so capture starts before its first invocation."""

        return ValidationProviderProxy(provider, self)

    def persist_artifact(self, path: str | Path) -> "ValidationArtifact":
        artifact = ValidationArtifact.from_raw_capture(self.raw_capture())
        artifact.write(path)
        return artifact

    def raw_capture(self) -> RawRunCapture:
        return self.store.snapshot()

    def finalize(
        self,
        result: GroundingResult | None = None,
        *,
        evaluator_case: str | None = None,
        canonical_handoff_result: Mapping[str, Any] | None = None,
        repository_cleanliness_result: Mapping[str, Any] | None = None,
        normalizer: Callable[[RawRunCapture], "ValidationRunRecord"] | None = None,
        artifact_path: str | Path | None = None,
    ) -> "ValidationRunRecord":
        if result is not None:
            self.capture_result(result)
        raw = self.raw_capture()
        if artifact_path is not None:
            ValidationArtifact.from_raw_capture(raw).write(artifact_path)
        if normalizer is not None:
            return normalizer(raw)
        return build_validation_run_record(
            raw,
            evaluator_case=evaluator_case,
            canonical_handoff_result=canonical_handoff_result,
            repository_cleanliness_result=repository_cleanliness_result,
        )


class ValidationProviderProxy:
    """Validation-only PlanningProvider proxy; production never imports it."""

    def __init__(self, provider: object, run: ValidationRun) -> None:
        self._provider = provider
        self._run = run

    @property
    def name(self) -> str:
        return str(getattr(self._provider, "name", "validation-provider"))

    @property
    def version(self) -> str | None:
        value = getattr(self._provider, "version", None)
        return str(value) if value is not None else None

    @property
    def capabilities(self) -> object:
        return getattr(self._provider, "capabilities")

    def health(self) -> object:
        return self._provider.health()  # type: ignore[attr-defined]

    def runtime_information(self) -> object:
        return self._provider.runtime_information()  # type: ignore[attr-defined]

    def generate(self, request: object) -> object:
        self._run.store.capture_provider_request(request, self._provider)
        try:
            response = self._provider.generate(request)  # type: ignore[attr-defined]
        except Exception as exc:
            self._run.store.capture_provider_failure(request, exc)
            raise
        self._run.store.capture_provider_response(request, response)
        return response


class ProviderValidationHarness:
    """Durable matrix coordinator with isolated A1–B3 run stores."""

    def __init__(self, labels: Sequence[str] = VALIDATION_LABELS) -> None:
        self.labels = tuple(str(label) for label in labels)
        if len(set(self.labels)) != len(self.labels):
            raise ValueError("validation labels must be unique")
        self._runs: dict[str, ValidationRun] = {}
        self._records: dict[str, ValidationRunRecord] = {}

    def start_run(
        self, label: str, grounding_run_id: str | None = None
    ) -> ValidationRun:
        if label in self._runs:
            raise ValueError(f"validation run already exists: {label}")
        run = ValidationRun(label, grounding_run_id)
        self._runs[label] = run
        return run

    def run(self, label: str) -> ValidationRun:
        try:
            return self._runs[label]
        except KeyError as exc:
            raise KeyError(f"validation run has not started: {label}") from exc

    def finalize(self, label: str, *args: Any, **kwargs: Any) -> ValidationRunRecord:
        record = self.run(label).finalize(*args, **kwargs)
        self._records[label] = record
        return record

    def raw_capture(self, label: str) -> RawRunCapture:
        return self.run(label).raw_capture()

    def matrix_records(self) -> tuple[ValidationRunRecord, ...]:
        return tuple(
            self._records[label] for label in self.labels if label in self._records
        )

    def matrix_summary(self) -> tuple[dict[str, object], ...]:
        return tuple(record.as_dict() for record in self.matrix_records())


@dataclass(frozen=True, slots=True)
class FrozenCaseTruth:
    case: str
    source_path: str
    handler_name: str | None = None
    http_method: str | None = None
    effective_route_path: str | None = None
    symbol_names: tuple[str, ...] = ()


CASE_A_TRUTH = FrozenCaseTruth(
    case="A",
    source_path="app/api/v1/endpoints/projects.py",
    handler_name="get_projects",
    http_method="GET",
    effective_route_path="/projects",
)
CASE_B_TRUTH = FrozenCaseTruth(
    case="B",
    source_path="app/services/auth/rate_limit.py",
    symbol_names=("enforce_auth_rate_limit", "RateLimitBucket"),
)
FROZEN_CASE_TRUTH = {"A": CASE_A_TRUTH, "B": CASE_B_TRUTH}


#: The authoritative, orthogonal evaluator metrics.  They replace the single
#: collapsed ``FOUND_RELEVANT`` boolean as the measure of grounding progress.
EVALUATOR_TARGET_METRICS = (
    "target_path_reached",
    "target_content_inspected",
    "target_structure_resolved",
)


@dataclass(frozen=True, slots=True)
class ObservationEvaluation:
    """Evaluator-only per-observation facts.  Never a provider input."""

    observation_id: str
    action_identity: str
    outcome: str
    classification: str
    target_path_reached: bool
    target_content_inspected: bool
    target_structure_resolved: bool
    bounded_content_length: int = 0
    structural_identity_present: bool = False


def _truth_for_case(case: str) -> FrozenCaseTruth:
    normalized = str(case).strip().upper()
    try:
        return FROZEN_CASE_TRUTH[normalized[0]]
    except (KeyError, IndexError) as exc:
        raise ValueError("evaluator case must be A or B") from exc


def classify_observation(
    observation: GroundingObservation, truth: FrozenCaseTruth
) -> str:
    """Legacy evaluator label, retained for report compatibility only.

    This collapses every acquisition path into one structural-identity-dependent
    boolean, which PHASE35-TBD1 established is stronger than the production
    sufficiency contract: production cites and hands off ``inspect_file`` FOUND
    evidence that carries no ``StructuralIdentity``.  The authoritative
    evaluator facts are the orthogonal metrics produced by
    :func:`evaluate_observation`; this function is no longer a success metric.
    It is never a provider input.
    """

    if observation.outcome is GroundingOutcome.NOT_FOUND:
        return "NOT_FOUND"
    if observation.outcome is GroundingOutcome.AMBIGUOUS:
        return "AMBIGUOUS"
    if observation.outcome is not GroundingOutcome.FOUND:
        return "UNCLASSIFIED"
    identity = observation.structural_identity
    relevant = identity is not None and identity.source_path == truth.source_path
    if truth.handler_name is not None:
        relevant = relevant and (
            identity.handler_name == truth.handler_name
            and identity.http_method == truth.http_method
            and identity.effective_route_path == truth.effective_route_path
        )
    if truth.symbol_names:
        relevant = relevant and identity.symbol_name in truth.symbol_names
    return "FOUND_RELEVANT" if relevant else "FOUND_NON_RELEVANT"


def _structure_matches_truth(
    identity: StructuralIdentity | None, truth: FrozenCaseTruth
) -> bool:
    """Exact frozen structural match; path agreement alone is never enough."""

    if identity is None or identity.source_path != truth.source_path:
        return False
    if truth.handler_name is not None and not (
        identity.handler_name == truth.handler_name
        and identity.http_method == truth.http_method
        and identity.effective_route_path == truth.effective_route_path
    ):
        return False
    if truth.symbol_names and identity.symbol_name not in truth.symbol_names:
        return False
    return True


def evaluate_observation(
    observation: GroundingObservation, truth: FrozenCaseTruth
) -> "ObservationEvaluation":
    """Score one retained observation on orthogonal, action-sensitive metrics.

    ``target_path_reached`` records that the frozen path was established.
    ``target_content_inspected`` records that bounded source evidence for that
    exact path was retrieved.  ``target_structure_resolved`` records an exact
    frozen structural match.  They are independent facts, not one ranking, and
    none of them is ever visible to a provider.
    """

    if not isinstance(observation, GroundingObservation):
        raise TypeError("observation must be the production GroundingObservation")
    action = observation.action_identity
    found = observation.outcome is GroundingOutcome.FOUND
    identity = observation.structural_identity

    path_reached = False
    content_inspected = False
    structure_resolved = False
    if found:
        if action == "search_text":
            path_reached = truth.source_path in observation.source_paths or any(
                hit.path == truth.source_path for hit in observation.hits
            )
        elif action == "inspect_file":
            path_reached = truth.source_path in observation.source_paths
            content_inspected = path_reached
        elif action == "resolve_structure":
            path_reached = (
                identity is not None and identity.source_path == truth.source_path
            )
            structure_resolved = _structure_matches_truth(identity, truth)

    return ObservationEvaluation(
        observation_id=observation.observation_id,
        action_identity=action,
        outcome=str(_enum_identity(observation.outcome)),
        classification=classify_observation(observation, truth),
        target_path_reached=path_reached,
        target_content_inspected=content_inspected,
        target_structure_resolved=structure_resolved,
        bounded_content_length=len(observation.bounded_content),
        structural_identity_present=identity is not None,
    )


def _observation_depth(evaluation: "ObservationEvaluation") -> str:
    if evaluation.target_structure_resolved:
        return "STRUCTURE_RESOLVED"
    if evaluation.target_content_inspected:
        return "CONTENT_INSPECTED"
    if evaluation.target_path_reached:
        return "CANDIDATE_ONLY"
    return "NO_TARGET_EVIDENCE"


def evaluate_frozen_case(result: GroundingResult, case: str) -> dict[str, object]:
    """Compare retained observations to frozen Case A/Case B truth.

    Aggregates are a monotonic OR over qualifying observations: once the frozen
    path, its content, or its structure has genuinely been reached, a later
    unrelated observation cannot erase that fact.
    """

    truth = _truth_for_case(case)
    evaluations = [
        evaluate_observation(observation, truth) for observation in result.observations
    ]
    observations = [
        {
            "observation_id": item.observation_id,
            "action_identity": item.action_identity,
            "outcome": item.outcome,
            "classification": item.classification,
            "target_path_reached": item.target_path_reached,
            "target_content_inspected": item.target_content_inspected,
            "target_structure_resolved": item.target_structure_resolved,
            "bounded_content_length": item.bounded_content_length,
            "structural_identity_present": item.structural_identity_present,
            "depth": _observation_depth(item),
        }
        for item in evaluations
    ]
    first = evaluations[0] if evaluations else None
    first_classification = first.classification if first is not None else None
    first_depth = _observation_depth(first) if first is not None else None
    # Refinement is measurable whenever the first observation did not already
    # deliver target evidence depth.  A candidate-only hit on the frozen path
    # acquires the right candidate and still needs a second action, so it stays
    # eligible; only content or structure at the target ends eligibility.
    refinement_eligible = bool(
        result.requests
        and evaluations
        and not (
            first.target_content_inspected  # type: ignore[union-attr]
            or first.target_structure_resolved  # type: ignore[union-attr]
        )
    )
    return {
        "case": truth.case,
        "truth": {
            "source_path": truth.source_path,
            "handler_name": truth.handler_name,
            "http_method": truth.http_method,
            "effective_route_path": truth.effective_route_path,
            "symbol_names": list(truth.symbol_names),
        },
        "observations": observations,
        "first_observation_classification": first_classification,
        "first_observation_depth": first_depth,
        "refinement_eligible": refinement_eligible,
        "target_path_reached": any(item.target_path_reached for item in evaluations),
        "target_content_inspected": any(
            item.target_content_inspected for item in evaluations
        ),
        "target_structure_resolved": any(
            item.target_structure_resolved for item in evaluations
        ),
        "legacy_classification_authoritative": False,
    }


def _provider_turn_records(
    raw: RawRunCapture,
) -> tuple[dict[str, object], ...]:
    merged: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for turn in raw.provider_turns:
        details = _provider_turn_mapping(turn)
        identity = details.get("provider_request_id")
        key = str(identity) if identity else f"capture-{turn.sequence}"
        merged[key] = dict(details)
        merged[key]["event_sequences"] = []
        merged[key]["capture_stages"] = ["provider_boundary"]
        order.append(key)
    for event in raw.provider_turn_events:
        details = dict(event.details)
        identity = details.get("provider_request_id")
        key = str(identity) if identity else f"event-{event.sequence}"
        if key not in merged:
            merged[key] = {
                "provider_request_id": identity,
                "event_sequences": [event.sequence],
                "capture_stages": [],
            }
            order.append(key)
        else:
            merged[key]["event_sequences"].append(event.sequence)
        stage = details.get("capture_stage")
        if stage is not None:
            merged[key]["capture_stages"].append(stage)
        for name, value in details.items():
            if name in {"capture_stage", "unrecorded_field_names"}:
                continue
            if name == "failure_classification" and value in {
                "provider_timeout",
                "provider_failure",
            }:
                merged[key][name] = value
                continue
            if value is not None and (
                name not in merged[key] or merged[key][name] is None
            ):
                merged[key][name] = value
    # Rejection details are emitted on GROUNDING_REQUEST with the same
    # provider identity as the adapter turn.  Join them without changing the
    # production event payload or relying on report prose.
    for event in raw.events:
        if event.event_type != EventType.GROUNDING_REQUEST:
            continue
        details = dict(event.details)
        identity = details.get("provider_request_id")
        if not identity:
            continue
        key = str(identity)
        if key not in merged:
            continue
        for name in (
            "rejection_code",
            "protocol_rejection_code",
            "failure_layer",
            "outcome",
        ):
            value = details.get(name)
            if value is not None:
                merged[key][name] = value
    return tuple(merged[key] for key in order)


def _next_action_digest(result: GroundingResult | None) -> str | None:
    if result is None:
        return None
    if len(result.requests) >= 2:
        return result.requests[1].action_digest
    for assessment in result.assessments:
        if assessment.next_action_digest:
            return assessment.next_action_digest
    return None


def _hypothesis_changed(
    first_action_digest: str | None, next_action_digest: str | None
) -> bool | None:
    if first_action_digest is None or next_action_digest is None:
        return None
    return first_action_digest != next_action_digest


def _runtime_failures(turns: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    values: list[str] = []
    for turn in turns:
        classification = turn.get("failure_classification")
        if classification in {"provider_timeout", "provider_failure"}:
            if str(classification) not in values:
                values.append(str(classification))
        elif turn.get("failure_layer") == "L0_TRANSPORT":
            values.append(str(classification or "runtime_failure"))
    return tuple(values)


def _is_parser_failure(turn: Mapping[str, object]) -> bool:
    if turn.get("parser_success") is not False:
        return False
    return turn.get("failure_classification") not in {
        "provider_timeout",
        "provider_failure",
    }


def _is_compliant(turn: Mapping[str, object]) -> bool:
    return bool(
        turn.get("parser_success") is True
        and not turn.get("parser_rejection_code")
        and turn.get("failure_classification") is None
    )


@dataclass(frozen=True, slots=True)
class ValidationRunRecord:
    """Validation-only report record; never imported by application runtime."""

    label: str
    grounding_run_id: str | None
    provider_turns: tuple[Mapping[str, object], ...]
    observations: tuple[Mapping[str, object], ...]
    assessments: tuple[Mapping[str, object], ...]
    rejections: tuple[Mapping[str, object], ...]
    terminal_state: str | None
    terminal_reason: str | None
    provider_turn_count: int
    first_turn_count: int
    correction_turn_count: int
    post_observation_assessment_count: int
    compliant_turn_count: int
    parser_failure_count: int
    runtime_failures: tuple[str, ...]
    accepted_repository_actions: int
    observation_count: int
    observation_outcomes: tuple[str, ...]
    observed_paths: tuple[str, ...]
    structural_identities: tuple[Mapping[str, object], ...]
    request_digests: tuple[str, ...]
    second_action_differs_from_first: bool | None
    refinement_eligible: bool | None
    first_action_digest: str | None
    next_action_digest: str | None
    hypothesis_changed: bool | None
    evaluator_result: Mapping[str, object] | None
    target_path_reached: bool | None
    target_content_inspected: bool | None
    target_structure_resolved: bool | None
    first_observation_depth: str | None
    canonical_handoff_result: Mapping[str, object] | None
    repository_cleanliness_result: Mapping[str, object] | None
    serialized_result: Mapping[str, object] | None

    def as_dict(self) -> dict[str, object]:
        return {
            name: _bounded_json(getattr(self, name))
            for name in self.__dataclass_fields__
        }


def _metric(evaluator_result: Mapping[str, Any] | None, name: str) -> bool | None:
    """Read one aggregate evaluator metric without inventing a default."""

    if evaluator_result is None:
        return None
    return bool(evaluator_result.get(name))


def build_validation_run_record(
    raw: RawRunCapture,
    *,
    evaluator_case: str | None = None,
    canonical_handoff_result: Mapping[str, Any] | None = None,
    repository_cleanliness_result: Mapping[str, Any] | None = None,
) -> ValidationRunRecord:
    """Normalize one retained run without mutating or replacing raw capture."""

    result = raw.results[-1] if raw.results else None
    provider_turns = _provider_turn_records(raw)
    serialized_result = serialize_grounding_result(result) if result else None
    observations = (
        tuple(serialize_grounding_observation(item) for item in result.observations)
        if result
        else ()
    )
    assessments = (
        tuple(_serialize_assessment(item) for item in result.assessments)
        if result
        else ()
    )
    rejections = (
        tuple(_serialize_rejection(item) for item in result.rejections)
        if result
        else ()
    )
    request_digests = (
        tuple(item.action_digest for item in result.requests) if result else ()
    )
    first_action_digest = request_digests[0] if request_digests else None
    next_action_digest = _next_action_digest(result)
    evaluator_result = (
        evaluate_frozen_case(result, evaluator_case)
        if result is not None and evaluator_case is not None
        else None
    )
    refinement_eligible = (
        bool(evaluator_result.get("refinement_eligible"))
        if evaluator_result is not None
        else None
    )
    all_paths = tuple(
        dict.fromkeys(
            path
            for item in (result.observations if result else ())
            for path in item.source_paths
        )
    )
    structural_identities = tuple(
        identity
        for item in observations
        if (identity := item.get("structural_identity")) is not None
    )
    return ValidationRunRecord(
        label=raw.label,
        grounding_run_id=(result.grounding_run_id if result else raw.grounding_run_id),
        provider_turns=provider_turns,
        observations=observations,
        assessments=assessments,
        rejections=rejections,
        terminal_state=(
            str(_enum_identity(result.terminal_state))
            if result
            else _terminal_event(raw)
        ),
        terminal_reason=(
            str(_enum_identity(result.terminal_reason))
            if result
            else _terminal_event(raw, reason=True)
        ),
        provider_turn_count=len(provider_turns),
        first_turn_count=sum(
            turn.get("turn_type") == "FIRST_ACTION" for turn in provider_turns
        ),
        correction_turn_count=sum(
            turn.get("turn_type") == "REJECTION_CORRECTION" for turn in provider_turns
        ),
        post_observation_assessment_count=sum(
            turn.get("turn_type") == "POST_OBSERVATION_ASSESSMENT"
            for turn in provider_turns
        ),
        compliant_turn_count=sum(_is_compliant(turn) for turn in provider_turns),
        parser_failure_count=sum(_is_parser_failure(turn) for turn in provider_turns),
        runtime_failures=_runtime_failures(provider_turns),
        accepted_repository_actions=(result.repository_action_count if result else 0),
        observation_count=len(observations),
        observation_outcomes=tuple(
            str(item["outcome"]) for item in observations if "outcome" in item
        ),
        observed_paths=all_paths,
        structural_identities=structural_identities,
        request_digests=request_digests,
        second_action_differs_from_first=(
            None
            if len(request_digests) < 2
            else request_digests[1] != request_digests[0]
        ),
        refinement_eligible=refinement_eligible,
        first_action_digest=first_action_digest,
        next_action_digest=next_action_digest,
        hypothesis_changed=_hypothesis_changed(first_action_digest, next_action_digest),
        evaluator_result=(
            _bounded_json(evaluator_result) if evaluator_result is not None else None
        ),
        target_path_reached=_metric(evaluator_result, "target_path_reached"),
        target_content_inspected=_metric(evaluator_result, "target_content_inspected"),
        target_structure_resolved=_metric(
            evaluator_result, "target_structure_resolved"
        ),
        first_observation_depth=(
            None
            if evaluator_result is None
            else evaluator_result.get("first_observation_depth")
        ),
        canonical_handoff_result=(
            _bounded_json(canonical_handoff_result)
            if canonical_handoff_result is not None
            else None
        ),
        repository_cleanliness_result=(
            _bounded_json(repository_cleanliness_result)
            if repository_cleanliness_result is not None
            else None
        ),
        serialized_result=serialized_result,
    )


def _terminal_event(raw: RawRunCapture, *, reason: bool = False) -> str | None:
    for event in reversed(raw.events):
        if event.event_type != EventType.GROUNDING_TERMINAL:
            continue
        key = "terminal_reason" if reason else "terminal_state"
        value = event.details.get(key) or event.details.get("state")
        return str(value) if value is not None else None
    return None


__all__ = [
    "ACTION_CAPTURE_SCHEMA_VERSION",
    "CASE_A_TRUTH",
    "CASE_B_TRUTH",
    "FROZEN_CASE_TRUTH",
    "FrozenCaseTruth",
    "EVALUATOR_TARGET_METRICS",
    "MAX_CAPTURED_PROMPT_BYTES",
    "MAX_LEGAL_ACTION_BYTES",
    "ObservationEvaluation",
    "ProviderValidationHarness",
    "RawBoundedCaptureStore",
    "RawCapturedEvent",
    "RawProviderTurnCapture",
    "RawRunCapture",
    "VALIDATION_LABELS",
    "VALIDATION_CAPTURE_SCHEMA_VERSION",
    "PROMPT_CAPTURE_SCHEMA_VERSION",
    "ValidationArtifact",
    "ValidationCaptureError",
    "ValidationProviderProxy",
    "ValidationRun",
    "ValidationRunRecord",
    "build_validation_run_record",
    "classify_observation",
    "evaluate_frozen_case",
    "evaluate_observation",
    "exact_normalized_action",
    "exact_provider_prompt",
    "normalize_event_type",
    "serialize_validation_artifact",
    "serialize_grounding_observation",
    "serialize_grounding_result",
    "serialize_structural_identity",
]
