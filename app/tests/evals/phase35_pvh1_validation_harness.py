"""PHASE35-PVH1 durable, provider-validation harness.

This module is validation infrastructure only.  It imports the production
grounding contracts and stores bounded provider events before doing any report
normalization.  It does not run providers, alter prompts, or participate in
the application runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import hashlib
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
MAX_CAPTURED_STRING = 500
MAX_CAPTURED_PREFIX = 256
MAX_CAPTURED_ITEMS = 32

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


def _bounded_json(value: object, *, _depth: int = 0) -> object:
    """Keep diagnostics JSON-safe and bounded without retaining raw payloads."""

    if _depth > 4:
        return "<bounded-depth>"
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_json(item, _depth=_depth + 1)
            for key, item in list(value.items())[:MAX_CAPTURED_ITEMS]
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
        return value[:MAX_CAPTURED_STRING]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:MAX_CAPTURED_STRING]


def _event_details(event_type: str, details: Mapping[str, Any]) -> Mapping[str, Any]:
    allowed = _EVENT_FIELDS.get(event_type, frozenset())
    bounded = {
        key: _bounded_json(details[key]) for key in sorted(allowed) if key in details
    }
    unknown = sorted(str(key) for key in details if str(key) not in allowed)
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
        "normalized_action": _bounded_json(observation.normalized_action),
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
        "normalized_action": _bounded_json(request.normalized_payload),
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


@dataclass(frozen=True, slots=True)
class RawRunCapture:
    """Failure-resistant per-label capture snapshot."""

    label: str
    grounding_run_id: str | None
    events: tuple[RawCapturedEvent, ...]
    results: tuple[GroundingResult, ...]

    @property
    def provider_turn_events(self) -> tuple[RawCapturedEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.event_type == EventType.GROUNDING_PROVIDER_TURN
        )


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

    def snapshot(self) -> RawRunCapture:
        return RawRunCapture(
            label=self.label,
            grounding_run_id=self.grounding_run_id,
            events=tuple(self._events),
            results=tuple(self._results),
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
    ) -> "ValidationRunRecord":
        if result is not None:
            self.capture_result(result)
        raw = self.raw_capture()
        if normalizer is not None:
            return normalizer(raw)
        return build_validation_run_record(
            raw,
            evaluator_case=evaluator_case,
            canonical_handoff_result=canonical_handoff_result,
            repository_cleanliness_result=repository_cleanliness_result,
        )


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


def _truth_for_case(case: str) -> FrozenCaseTruth:
    normalized = str(case).strip().upper()
    try:
        return FROZEN_CASE_TRUTH[normalized[0]]
    except (KeyError, IndexError) as exc:
        raise ValueError("evaluator case must be A or B") from exc


def classify_observation(
    observation: GroundingObservation, truth: FrozenCaseTruth
) -> str:
    """Evaluator-only classification; it is never a provider input."""

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


def evaluate_frozen_case(result: GroundingResult, case: str) -> dict[str, object]:
    """Compare retained observations to frozen Case A/Case B truth."""

    truth = _truth_for_case(case)
    classifications = [
        {
            "observation_id": observation.observation_id,
            "classification": classify_observation(observation, truth),
        }
        for observation in result.observations
    ]
    first_classification = (
        classifications[0]["classification"] if classifications else None
    )
    refinement_eligible = bool(
        result.requests
        and result.observations
        and first_classification in {"NOT_FOUND", "AMBIGUOUS", "FOUND_NON_RELEVANT"}
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
        "observations": classifications,
        "first_observation_classification": first_classification,
        "refinement_eligible": refinement_eligible,
    }


def _provider_turn_records(
    raw: RawRunCapture,
) -> tuple[dict[str, object], ...]:
    merged: dict[str, dict[str, object]] = {}
    order: list[str] = []
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
            if value is not None and (
                name not in merged[key] or merged[key][name] is None
            ):
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
    canonical_handoff_result: Mapping[str, object] | None
    repository_cleanliness_result: Mapping[str, object] | None
    serialized_result: Mapping[str, object] | None

    def as_dict(self) -> dict[str, object]:
        return {
            name: _bounded_json(getattr(self, name))
            for name in self.__dataclass_fields__
        }


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
    "CASE_A_TRUTH",
    "CASE_B_TRUTH",
    "FROZEN_CASE_TRUTH",
    "FrozenCaseTruth",
    "ProviderValidationHarness",
    "RawBoundedCaptureStore",
    "RawCapturedEvent",
    "RawRunCapture",
    "VALIDATION_LABELS",
    "ValidationRun",
    "ValidationRunRecord",
    "build_validation_run_record",
    "classify_observation",
    "evaluate_frozen_case",
    "normalize_event_type",
    "serialize_grounding_observation",
    "serialize_grounding_result",
    "serialize_structural_identity",
]
