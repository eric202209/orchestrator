"""Bounded, provider-injected Grounding Coordinator.

The coordinator owns only the read-only grounding lifecycle. It does not
materialize Planning context, assemble a Planning prompt, create a Plan, or
grant mutation/execution authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass, replace
import json
from typing import Any, Protocol, runtime_checkable

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.planning.source_materialization import (
    current_source_version_identity,
)

from .contracts import (
    GROUNDING_BUDGET_DIMENSIONS,
    GroundingBudgetAccounting,
    GroundingBudgetDelta,
    GroundingBudgetLimits,
    GroundingBudgetSnapshot,
    GroundingExecutionError,
    GroundingObservation,
    GroundingOutcome,
    GroundingRequest,
    GroundingRequestRejection,
    parse_grounding_request,
)
from .coordinator_contracts import (
    GroundingAssessment,
    GroundingAssessmentKind,
    GroundingCoordinatorState,
    GroundingDecisionContext,
    GroundingEvidence,
    GROUNDING_RESULT_SCHEMA_VERSION,
    GroundingLifecycleState,
    GroundingProposal,
    GroundingProviderTurnMode,
    GroundingRejection,
    GroundingResult,
    GroundingRunConfig,
    GroundingStateProjection,
    GroundingTerminalReason,
)
from .executor import GroundingExecutor


EventSink = Callable[[str, Mapping[str, Any]], Any]


class GroundingProviderError(RuntimeError):
    """Provider infrastructure or wire output failed before valid output."""


class GroundingInvariantError(RuntimeError):
    """An illegal coordinator lifecycle transition occurred."""


class _MalformedProviderResponse(ValueError):
    pass


class _InvalidSufficiency(ValueError):
    pass


class _BudgetExhausted(RuntimeError):
    pass


class _SourceVersionChanged(RuntimeError):
    pass


@runtime_checkable
class GroundingDecisionProvider(Protocol):
    """Minimal injected provider boundary used by the coordinator."""

    def decide(self, context: GroundingDecisionContext) -> Any:
        """Return one structured action or one structured assessment."""


_ALLOWED_TRANSITIONS: dict[
    GroundingLifecycleState, frozenset[GroundingLifecycleState]
] = {
    GroundingLifecycleState.NOT_STARTED: frozenset({GroundingLifecycleState.ORIENTED}),
    GroundingLifecycleState.ORIENTED: frozenset(
        {
            GroundingLifecycleState.REQUESTING,
            GroundingLifecycleState.SKIPPED,
            GroundingLifecycleState.FAILED,
            GroundingLifecycleState.INSUFFICIENT,
        }
    ),
    GroundingLifecycleState.REQUESTING: frozenset(
        {
            GroundingLifecycleState.OBSERVED,
            GroundingLifecycleState.ASSESSING,
            GroundingLifecycleState.INSUFFICIENT,
            GroundingLifecycleState.FAILED,
        }
    ),
    GroundingLifecycleState.OBSERVED: frozenset(
        {
            GroundingLifecycleState.ASSESSING,
            GroundingLifecycleState.FAILED,
            GroundingLifecycleState.INSUFFICIENT,
        }
    ),
    GroundingLifecycleState.ASSESSING: frozenset(
        {
            GroundingLifecycleState.NEED_MORE_EVIDENCE,
            GroundingLifecycleState.SUFFICIENT,
            GroundingLifecycleState.INSUFFICIENT,
            GroundingLifecycleState.FAILED,
        }
    ),
    GroundingLifecycleState.NEED_MORE_EVIDENCE: frozenset(
        {
            GroundingLifecycleState.REQUESTING,
            GroundingLifecycleState.INSUFFICIENT,
            GroundingLifecycleState.FAILED,
        }
    ),
    GroundingLifecycleState.SUFFICIENT: frozenset(),
    GroundingLifecycleState.SKIPPED: frozenset(),
    GroundingLifecycleState.INSUFFICIENT: frozenset(),
    GroundingLifecycleState.FAILED: frozenset(),
}


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return {
            field.name: _plain(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return value.value
    return value


def _unique_append(values: tuple[Any, ...], value: Any) -> tuple[Any, ...]:
    return values if value in values else (*values, value)


def _is_budget_rejection(error: GroundingRequestRejection) -> bool:
    return str(error.code).startswith("budget_")


def _protocol_rejection_code(
    error: GroundingRequestRejection,
    payload: Mapping[str, Any] | None = None,
) -> str:
    """Project legacy request errors into bounded diagnostic identities."""

    code = str(error.code)
    if code != "invalid_request":
        return code
    message = str(error).lower()
    if "unsupported grounding action" in message:
        return "unsupported_action"
    if "relation" in message or "structuralrelation" in message:
        return "unsupported_relation"
    if "locator" in message:
        return "invalid_locator"
    if isinstance(payload, Mapping):
        action = payload.get("action")
        expected = {
            "search_text": {"action", "query", "scopes"},
            "inspect_file": {"action", "path"},
            "resolve_structure": {"action", "relation", "locator"},
        }.get(action)
        if expected is not None and set(payload) - expected:
            return "unknown_fields"
        if action == "resolve_structure" and isinstance(
            payload.get("locator"), Mapping
        ):
            locator_expected = {
                "symbol_definition": {"path", "name"},
                "enclosing_symbol": {"path", "line"},
                "mounted_route": {"path", "method", "decorator_path"},
            }.get(payload.get("relation"))
            if (
                locator_expected is not None
                and set(payload["locator"]) - locator_expected
            ):
                return "unknown_fields"
    if "fields are exactly" in message or "request must be an object" in message:
        return "invalid_request_shape"
    if "path" in message:
        return "invalid_locator"
    return "invalid_request_shape"


def _remaining_budget(
    config: GroundingRunConfig, budget: GroundingBudgetSnapshot
) -> dict[str, int | None]:
    return {
        name: (
            None
            if getattr(config.budget_limits, name) is None
            else max(0, getattr(config.budget_limits, name) - getattr(budget, name))
        )
        for name in GROUNDING_BUDGET_DIMENSIONS
    }


def _transition(
    state: GroundingCoordinatorState, target: GroundingLifecycleState
) -> GroundingCoordinatorState:
    current = state.lifecycle_state
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise GroundingInvariantError(
            f"illegal grounding transition {current}->{target}"
        )
    return replace(
        state,
        lifecycle_state=target,
        lifecycle_history=(*state.lifecycle_history, target),
    )


def transition_grounding_state(
    state: GroundingCoordinatorState, target: GroundingLifecycleState
) -> GroundingCoordinatorState:
    """Apply one legal lifecycle transition, failing closed otherwise."""

    return _transition(state, target)


def render_grounding_state(state: GroundingCoordinatorState) -> str:
    """Render bounded typed state for the next provider turn."""

    payload = {
        "grounding_run_id": state.grounding_run_id,
        "lifecycle_state": state.lifecycle_state.value,
        "task_reference": _plain(state.task_reference),
        "workspace_identity": state.workspace_identity,
        "snapshot_identity": state.snapshot_identity,
        "request_history": [
            {
                "request_id": request.request_id,
                "action": request.action_identity,
                "normalized_action": _plain(request.normalized_payload),
                "action_digest": request.action_digest,
            }
            for request in state.request_history
        ],
        "observation_history": [
            {
                "observation_id": observation.observation_id,
                "request_id": observation.request_id,
                "action": observation.action_identity,
                "outcome": observation.outcome.value,
                "source_paths": list(observation.source_paths),
                "source_versions": _plain(observation.source_versions),
                "hits": [
                    {
                        "path": hit.path,
                        "line_number": hit.line_number,
                        "snippet": hit.snippet,
                    }
                    for hit in observation.hits
                ],
                "structural_identity": _plain(observation.structural_identity),
                "bounded_content": observation.bounded_content.decode(
                    "utf-8", errors="replace"
                )[:4096],
            }
            for observation in state.observation_history
        ],
        "assessment_history": [
            {
                "assessment_id": assessment.assessment_id,
                "decision": assessment.decision.value,
                "after_observation_ids": list(assessment.after_observation_ids),
                "cited_observation_ids": list(assessment.cited_observation_ids),
                "rationale": assessment.rationale,
                "next_action_digest": assessment.next_action_digest,
            }
            for assessment in state.assessment_history
        ],
        "rejections": _plain(state.rejection_history),
        "discovered_source_paths": list(state.discovered_source_paths),
        "source_versions": _plain(state.source_versions),
        "budget": _plain(state.budget),
        "remaining_budget": _plain(state.remaining_budget),
        "orientation_advisory": _plain(state.orientation_advisory),
    }
    return "## TYPED GROUNDING STATE\n" + json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )


def _parse_wire_response(
    payload: Any, *, after_observation: bool, terminal_only: bool = False
) -> GroundingProposal:
    """Parse the closed first-turn, post-observation, and terminal wire shapes."""

    if terminal_only and not after_observation:
        raise _MalformedProviderResponse(
            "terminal assessment requires at least one observation"
        )
    if isinstance(payload, GroundingProposal):
        if after_observation:
            if payload.assessment_kind is None:
                raise _MalformedProviderResponse(
                    "post-observation response must be an assessment"
                )
            if payload.assessment_kind is GroundingAssessmentKind.SUFFICIENT:
                if payload.action_payload is not None:
                    raise _MalformedProviderResponse(
                        "SUFFICIENT cannot include a next action"
                    )
            elif payload.assessment_kind is GroundingAssessmentKind.NEED_MORE_EVIDENCE:
                if terminal_only:
                    raise _MalformedProviderResponse(
                        "terminal assessment cannot request more evidence"
                    )
                if payload.action_payload is None:
                    raise _MalformedProviderResponse(
                        "NEED_MORE_EVIDENCE requires exactly one next action"
                    )
            elif payload.action_payload is not None:
                raise _MalformedProviderResponse(
                    "INSUFFICIENT cannot include a next action"
                )
        elif payload.assessment_kind is not None or payload.action_payload is None:
            raise _MalformedProviderResponse(
                "first turn must return exactly one grounding action"
            )
        return payload

    if not isinstance(payload, Mapping):
        raise _MalformedProviderResponse("provider response must be an object")
    if not after_observation:
        if "decision" in payload:
            raise _MalformedProviderResponse("assessment is invalid on first turn")
        action = payload.get("action")
        expected_fields = {
            "search_text": {"action", "query", "scopes"},
            "inspect_file": {"action", "path"},
            "resolve_structure": {"action", "relation", "locator"},
        }.get(action)
        if expected_fields is None:
            if isinstance(action, str):
                # Keep mutation-shaped/unknown actions in the typed rejection
                # path so a bounded corrective turn may be offered.
                return GroundingProposal(action_payload=dict(payload))
            raise _MalformedProviderResponse("first-turn action shape is invalid")
        if set(payload) != expected_fields:
            raise _MalformedProviderResponse("first-turn action shape is invalid")
        return GroundingProposal(action_payload=dict(payload))

    decision = payload.get("decision")
    if not isinstance(decision, str):
        raise _MalformedProviderResponse("post-observation decision is required")
    decision = decision.upper()
    if terminal_only and decision not in {
        GroundingAssessmentKind.SUFFICIENT.value,
        GroundingAssessmentKind.INSUFFICIENT.value,
    }:
        raise _MalformedProviderResponse(
            "terminal assessment must be SUFFICIENT or INSUFFICIENT"
        )
    if decision == GroundingAssessmentKind.SUFFICIENT.value:
        if set(payload) != {"decision", "cited_observation_ids", "rationale"}:
            raise _MalformedProviderResponse("SUFFICIENT assessment shape is invalid")
        cited = payload["cited_observation_ids"]
        if not isinstance(cited, (list, tuple)) or any(
            not isinstance(item, str) for item in cited
        ):
            raise _MalformedProviderResponse("citation IDs must be a list")
        if not isinstance(payload["rationale"], str):
            raise _MalformedProviderResponse("SUFFICIENT rationale must be text")
        return GroundingProposal(
            assessment_kind=GroundingAssessmentKind.SUFFICIENT,
            cited_observation_ids=tuple(cited),
            rationale=str(payload["rationale"]),
        )
    if decision == GroundingAssessmentKind.NEED_MORE_EVIDENCE.value:
        if set(payload) != {"decision", "next_action", "rationale"}:
            raise _MalformedProviderResponse(
                "NEED_MORE_EVIDENCE assessment shape is invalid"
            )
        if not isinstance(payload["next_action"], Mapping):
            raise _MalformedProviderResponse("next_action must be an object")
        if not isinstance(payload["rationale"], str):
            raise _MalformedProviderResponse(
                "NEED_MORE_EVIDENCE rationale must be text"
            )
        return GroundingProposal(
            assessment_kind=GroundingAssessmentKind.NEED_MORE_EVIDENCE,
            action_payload=payload["next_action"],
            rationale=str(payload["rationale"]),
        )
    if decision == GroundingAssessmentKind.INSUFFICIENT.value:
        if set(payload) != {"decision", "reason"}:
            raise _MalformedProviderResponse("INSUFFICIENT assessment shape is invalid")
        if not isinstance(payload["reason"], str) or not payload["reason"].strip():
            raise _MalformedProviderResponse(
                "INSUFFICIENT reason must be non-empty text"
            )
        return GroundingProposal(
            assessment_kind=GroundingAssessmentKind.INSUFFICIENT,
            rationale=str(payload["reason"]),
            terminal_reason=GroundingTerminalReason.INSUFFICIENT_GROUNDING,
        )
    raise _MalformedProviderResponse("unsupported post-observation decision")


def parse_grounding_provider_response(
    payload: Any, *, after_observation: bool, terminal_only: bool = False
) -> GroundingProposal:
    """Public strict parser used by provider-free contract tests."""

    return _parse_wire_response(
        payload, after_observation=after_observation, terminal_only=terminal_only
    )


class GroundingCoordinator:
    """Coordinate strict provider turns with deterministic read-only actions."""

    def __init__(
        self,
        *,
        executor: GroundingExecutor,
        provider: GroundingDecisionProvider,
        config: GroundingRunConfig,
        event_sink: EventSink | None = None,
    ) -> None:
        if not isinstance(provider, GroundingDecisionProvider):
            raise TypeError("provider must implement GroundingDecisionProvider")
        self.executor = executor
        self.provider = provider
        self.config = self._normalize_config(config)
        self.event_sink = event_sink
        self._budget_trace: list[GroundingBudgetSnapshot] = []

    @staticmethod
    def _normalize_config(config: GroundingRunConfig) -> GroundingRunConfig:
        limits = config.budget_limits
        updates: dict[str, int] = {}
        if limits.repository_actions is None:
            updates["repository_actions"] = config.max_steps
        if limits.provider_requests is None:
            updates["provider_requests"] = config.max_total_provider_requests
        if limits.exploration_provider_requests is None:
            updates["exploration_provider_requests"] = (
                config.max_exploration_provider_requests
            )
        if limits.terminal_assessment_requests is None:
            updates["terminal_assessment_requests"] = (
                config.max_terminal_assessment_requests
            )
        if updates:
            config = replace(config, budget_limits=replace(limits, **updates))
        return config

    def _emit(self, event_type: str, details: Mapping[str, Any]) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink(event_type, details)
        except Exception:
            return

    def _initial_state(self) -> GroundingCoordinatorState:
        budget = GroundingBudgetSnapshot()
        return GroundingCoordinatorState(
            grounding_run_id=self.config.grounding_run_id,
            task_reference=self.config.task_reference,
            workspace_identity=self.config.workspace_identity,
            snapshot_identity=self.config.snapshot_identity,
            budget=budget,
            remaining_budget=_remaining_budget(self.config, budget),
            orientation_advisory=self.config.orientation_advisory,
            lifecycle_history=(GroundingLifecycleState.NOT_STARTED,),
        )

    def _apply_budget(
        self,
        state: GroundingCoordinatorState,
        delta: GroundingBudgetDelta,
        *,
        limits: GroundingBudgetLimits | None = None,
    ) -> GroundingCoordinatorState:
        try:
            accounting = GroundingBudgetAccounting(state.budget).apply(
                delta, limits or self.config.budget_limits
            )
        except GroundingRequestRejection as exc:
            raise _BudgetExhausted(str(exc)) from exc
        self._budget_trace.append(accounting.snapshot)
        return replace(
            state,
            budget=accounting.snapshot,
            remaining_budget=_remaining_budget(self.config, accounting.snapshot),
        )

    def _check_snapshot_identity(self) -> None:
        supplier = self.config.snapshot_identity_supplier
        if supplier is None:
            return
        try:
            current = str(supplier()).strip()
        except Exception as exc:
            raise _SourceVersionChanged(
                "snapshot identity could not be established"
            ) from exc
        if current != self.config.snapshot_identity:
            raise _SourceVersionChanged("grounding snapshot identity changed")

    def _check_source_versions(self, state: GroundingCoordinatorState) -> None:
        for path, version in state.source_versions.items():
            current = current_source_version_identity(self.executor.project_dir / path)
            if current is None or current != version:
                raise _SourceVersionChanged(f"source version changed: {path}")

    def _validate_observation(
        self, state: GroundingCoordinatorState, observation: GroundingObservation
    ) -> None:
        if observation.grounding_run_id != state.grounding_run_id:
            raise _SourceVersionChanged("observation belongs to another grounding run")
        if observation.workspace_identity != state.workspace_identity:
            raise _SourceVersionChanged("observation workspace identity changed")
        if observation.snapshot_identity != state.snapshot_identity:
            raise _SourceVersionChanged("observation snapshot identity changed")
        for path, version in observation.source_versions.items():
            prior = state.source_versions.get(path)
            if not version or (prior is not None and prior != version):
                raise _SourceVersionChanged(f"incompatible source version: {path}")

    def _append_observation(
        self, state: GroundingCoordinatorState, observation: GroundingObservation
    ) -> GroundingCoordinatorState:
        self._validate_observation(state, observation)
        source_paths = state.discovered_source_paths
        structural = state.discovered_structural_identities
        versions = dict(state.source_versions)
        for path in observation.source_paths:
            source_paths = _unique_append(source_paths, path)
        if observation.structural_identity is not None:
            structural = _unique_append(structural, observation.structural_identity)
        versions.update(dict(observation.source_versions))
        new_files = len(
            set(observation.source_paths) - set(state.discovered_source_paths)
        )
        delta = replace(observation.budget_delta, distinct_files=new_files)
        try:
            accounting = GroundingBudgetAccounting(state.budget).apply(
                delta, self.config.budget_limits
            )
        except GroundingRequestRejection as exc:
            raise _BudgetExhausted(str(exc)) from exc
        normalized_observation = replace(
            observation,
            budget_delta=delta,
            budget_cumulative=accounting.snapshot,
        )
        self._budget_trace.append(accounting.snapshot)
        state = _transition(state, GroundingLifecycleState.OBSERVED)
        return replace(
            state,
            observation_history=(*state.observation_history, normalized_observation),
            discovered_source_paths=source_paths,
            discovered_structural_identities=structural,
            source_versions=versions,
            budget=accounting.snapshot,
            remaining_budget=_remaining_budget(self.config, accounting.snapshot),
        )

    def _append_request(
        self, state: GroundingCoordinatorState, request: GroundingRequest
    ) -> GroundingCoordinatorState:
        return replace(
            state,
            request_history=(*state.request_history, request),
            attempted_action_digests=_unique_append(
                state.attempted_action_digests, request.action_digest
            ),
        )

    def _append_rejection(
        self,
        state: GroundingCoordinatorState,
        rejection: GroundingRejection,
        *,
        protocol_rejection_code: str | None = None,
    ) -> GroundingCoordinatorState:
        normalized_code = protocol_rejection_code or _protocol_rejection_code(rejection)
        self._emit(
            EventType.GROUNDING_REQUEST,
            {
                "grounding_run_id": rejection.grounding_run_id,
                "provider_request_id": rejection.provider_request_id,
                "rejection_code": rejection.code,
                "protocol_rejection_code": normalized_code,
                "failure_layer": "L5_ACTION_SCHEMA",
                "action": rejection.action_kind,
                "outcome": "rejected",
                "budget": _plain(state.budget),
            },
        )
        self._emit(
            EventType.GROUNDING_PROVIDER_TURN,
            {
                "grounding_run_id": rejection.grounding_run_id,
                "provider_request_id": rejection.provider_request_id,
                "turn_type": (
                    "POST_OBSERVATION_ASSESSMENT"
                    if state.observation_history
                    else "FIRST_ACTION"
                ),
                "capture_stage": "coordinator_request_validation",
                "parser_success": True,
                "parser_rejection_code": normalized_code,
                "failure_layer": "L5_ACTION_SCHEMA",
                "failure_classification": normalized_code,
                "detail": str(rejection.message)[:240],
            },
        )
        return replace(state, rejection_history=(*state.rejection_history, rejection))

    def _emit_assessment_validation_failure(
        self,
        state: GroundingCoordinatorState,
        *,
        provider_request_id: str,
        code: str,
        failure_layer: str,
        detail: str,
    ) -> None:
        self._emit(
            EventType.GROUNDING_PROVIDER_TURN,
            {
                "grounding_run_id": state.grounding_run_id,
                "provider_request_id": provider_request_id,
                "turn_type": "POST_OBSERVATION_ASSESSMENT",
                "capture_stage": "coordinator_assessment_validation",
                "parser_success": True,
                "parser_rejection_code": code,
                "failure_layer": failure_layer,
                "failure_classification": code,
                "detail": str(detail)[:240],
            },
        )

    def _validate_citations(
        self, state: GroundingCoordinatorState, proposal: GroundingProposal
    ) -> None:
        cited_ids = tuple(proposal.cited_observation_ids)
        if any(not isinstance(item, str) for item in cited_ids):
            raise _MalformedProviderResponse("citation IDs must be strings")
        if not cited_ids or len(set(cited_ids)) != len(cited_ids):
            raise _InvalidSufficiency(
                "SUFFICIENT requires unique observation citations"
            )
        observations = {item.observation_id: item for item in state.observation_history}
        if any(item not in observations for item in cited_ids):
            raise _InvalidSufficiency("assessment cites an unknown observation")
        cited = [observations[item] for item in cited_ids]
        if any(item.grounding_run_id != state.grounding_run_id for item in cited):
            raise _InvalidSufficiency("assessment cites another grounding run")
        if any(item.outcome is not GroundingOutcome.FOUND for item in cited):
            raise _InvalidSufficiency("SUFFICIENT may cite FOUND observations only")
        if not proposal.rationale.strip():
            raise _InvalidSufficiency("SUFFICIENT requires bounded rationale")
        source_paths = tuple(proposal.cited_source_paths) or tuple(
            dict.fromkeys(path for item in cited for path in item.source_paths)
        )
        for path in source_paths:
            if not any(path in item.source_versions for item in cited):
                raise _InvalidSufficiency(
                    "cited source identity is absent from observation"
                )
        for item in cited:
            if (
                item.action_identity == "resolve_structure"
                and item.structural_identity is None
            ):
                raise _InvalidSufficiency(
                    "structural citation requires structural identity"
                )
        for identity in proposal.cited_structural_identities:
            if not any(item.structural_identity == identity for item in cited):
                raise _InvalidSufficiency(
                    "cited structural identity is absent from evidence"
                )
        self._check_source_versions(state)

    def _assessment(
        self,
        state: GroundingCoordinatorState,
        proposal: GroundingProposal,
        *,
        provider_request_id: str,
        next_action: GroundingRequest | None = None,
    ) -> GroundingAssessment:
        kind = proposal.assessment_kind
        if kind is None:
            raise _MalformedProviderResponse("assessment decision is required")
        if kind is GroundingAssessmentKind.TERMINAL_STOP:
            kind = GroundingAssessmentKind.INSUFFICIENT
        cited_ids = tuple(proposal.cited_observation_ids)
        cited_source_paths = tuple(proposal.cited_source_paths)
        if kind is GroundingAssessmentKind.SUFFICIENT:
            self._validate_citations(state, proposal)
            if not cited_source_paths:
                observations = {
                    item.observation_id: item for item in state.observation_history
                }
                cited_source_paths = tuple(
                    dict.fromkeys(
                        path
                        for observation_id in cited_ids
                        for path in observations[observation_id].source_paths
                    )
                )
            if next_action is not None or proposal.action_payload is not None:
                raise _MalformedProviderResponse(
                    "SUFFICIENT cannot carry a next action"
                )
            reason = GroundingTerminalReason.SUFFICIENT
        elif kind is GroundingAssessmentKind.NEED_MORE_EVIDENCE:
            if next_action is None:
                raise _MalformedProviderResponse(
                    "NEED_MORE_EVIDENCE requires exactly one next action"
                )
            if cited_ids or proposal.cited_source_paths:
                raise _MalformedProviderResponse(
                    "NEED_MORE_EVIDENCE cannot carry sufficient citations"
                )
            reason = None
        elif kind is GroundingAssessmentKind.INSUFFICIENT:
            if next_action is not None or proposal.action_payload is not None:
                raise _MalformedProviderResponse(
                    "INSUFFICIENT cannot carry a next action"
                )
            if cited_ids or proposal.cited_source_paths:
                raise _MalformedProviderResponse(
                    "INSUFFICIENT cannot carry sufficient citations"
                )
            if not proposal.rationale.strip():
                raise _MalformedProviderResponse("INSUFFICIENT requires a reason")
            reason = GroundingTerminalReason.INSUFFICIENT_GROUNDING
        else:  # pragma: no cover - closed enum guard
            raise _MalformedProviderResponse("unsupported assessment decision")
        return GroundingAssessment(
            assessment_id=f"grounding-assessment-{len(state.assessment_history) + 1}",
            grounding_run_id=state.grounding_run_id,
            kind=kind,
            provider_request_id=provider_request_id,
            after_observation_ids=tuple(
                observation.observation_id for observation in state.observation_history
            ),
            cited_observation_ids=cited_ids,
            cited_source_paths=cited_source_paths,
            cited_structural_identities=tuple(proposal.cited_structural_identities),
            rationale=proposal.rationale.strip(),
            unresolved_risk=proposal.unresolved_risk,
            next_action=next_action,
            next_action_digest=next_action.action_digest if next_action else None,
            terminal_reason=reason,
        )

    def _append_assessment(
        self, state: GroundingCoordinatorState, assessment: GroundingAssessment
    ) -> GroundingCoordinatorState:
        self._emit(
            EventType.GROUNDING_ASSESSMENT,
            {
                "grounding_run_id": state.grounding_run_id,
                "provider_request_id": assessment.provider_request_id,
                "assessment_id": assessment.assessment_id,
                "assessment_decision": assessment.decision.value,
                "cited_observation_ids": list(assessment.cited_observation_ids),
                "outcome": "assessed",
                "budget": _plain(state.budget),
            },
        )
        return replace(
            state, assessment_history=(*state.assessment_history, assessment)
        )

    def _result(
        self,
        state: GroundingCoordinatorState,
        terminal_state: GroundingLifecycleState,
        reason: GroundingTerminalReason,
        *,
        assessment: GroundingAssessment | None = None,
    ) -> GroundingResult:
        if not terminal_state.terminal:
            raise GroundingInvariantError("result state must be terminal")
        if state.lifecycle_state is not terminal_state:
            state = _transition(state, terminal_state)
        projection = GroundingStateProjection(
            grounding_run_id=state.grounding_run_id,
            task_reference=state.task_reference,
            workspace_identity=state.workspace_identity,
            snapshot_identity=state.snapshot_identity,
            request_digests=tuple(item.action_digest for item in state.request_history),
            observation_ids=tuple(
                item.observation_id for item in state.observation_history
            ),
            observation_outcomes=tuple(
                item.outcome.value for item in state.observation_history
            ),
            assessment_history=state.assessment_history,
            attempted_action_digests=state.attempted_action_digests,
            request_state_signals=(),
            discovered_source_paths=state.discovered_source_paths,
            discovered_structural_identities=state.discovered_structural_identities,
            source_versions=state.source_versions,
            remaining_budget=state.remaining_budget,
            termination_state=reason,
            lifecycle_state=state.lifecycle_state,
            lifecycle_history=state.lifecycle_history,
            requests=state.request_history,
            observations=state.observation_history,
            rejections=state.rejection_history,
        )
        cited_ids = assessment.cited_observation_ids if assessment else ()
        cited_paths = assessment.cited_source_paths if assessment else ()
        cited_structural = assessment.cited_structural_identities if assessment else ()
        observations = {item.observation_id: item for item in state.observation_history}
        evidence: list[GroundingEvidence] = []
        for observation_id in cited_ids:
            observation = observations.get(observation_id)
            if observation is None:
                continue
            for path in cited_paths or observation.source_paths:
                if path in observation.source_versions:
                    evidence.append(
                        GroundingEvidence(
                            observation_id=observation.observation_id,
                            source_path=path,
                            source_version=observation.source_versions[path],
                            bounded_content=observation.bounded_content,
                        )
                    )
        result = GroundingResult(
            grounding_run_id=state.grounding_run_id,
            terminal_state=state.lifecycle_state,
            terminal_reason=reason,
            state_projection=projection,
            schema_version=GROUNDING_RESULT_SCHEMA_VERSION,
            orientation_advisory=state.orientation_advisory,
            requests=state.request_history,
            observations=state.observation_history,
            assessments=state.assessment_history,
            rejections=state.rejection_history,
            budget_snapshot=state.budget,
            provider_request_count=state.budget.provider_requests,
            repository_action_count=state.budget.repository_actions,
            cited_observation_ids=tuple(cited_ids),
            cited_source_paths=tuple(cited_paths),
            cited_structural_identities=tuple(cited_structural),
            source_versions={
                path: state.source_versions[path]
                for path in cited_paths
                if path in state.source_versions
            },
            cited_source_evidence=tuple(evidence),
            budget_trace=tuple(self._budget_trace),
            unresolved_risk=(assessment.unresolved_risk if assessment else True),
            provider_model_telemetry={
                "provider": self.config.provider_name,
                "model": self.config.model_name,
                "provider_requests": state.budget.provider_requests,
                "exploration_provider_requests": (
                    state.budget.exploration_provider_requests
                ),
                "terminal_assessment_requests": (
                    state.budget.terminal_assessment_requests
                ),
            },
            grounding_diagnostics={
                "observation_count": len(state.observation_history),
                "assessment_count": len(state.assessment_history),
                "rejection_count": len(state.rejection_history),
                "repository_actions": state.budget.repository_actions,
                "exploration_provider_requests": (
                    state.budget.exploration_provider_requests
                ),
                "terminal_assessment_requests": (
                    state.budget.terminal_assessment_requests
                ),
                "source_evidence_bytes": state.budget.source_evidence_bytes,
                "distinct_files": state.budget.distinct_files,
                "positive_regions": state.budget.positive_regions,
            },
        )
        self._emit(
            EventType.GROUNDING_TERMINAL,
            {
                "grounding_run_id": result.grounding_run_id,
                "state": result.terminal_state.value,
                "terminal_state": result.terminal_state.value,
                "terminal_reason": reason.value,
                "provider_requests": state.budget.provider_requests,
                "exploration_provider_requests": (
                    state.budget.exploration_provider_requests
                ),
                "terminal_assessment_requests": (
                    state.budget.terminal_assessment_requests
                ),
                "repository_actions": state.budget.repository_actions,
                "cited_observation_ids": list(result.cited_observation_ids),
            },
        )
        return result

    def _exploration_remaining(self, state: GroundingCoordinatorState) -> int:
        return (
            self.config.max_exploration_provider_requests
            - state.budget.exploration_provider_requests
        )

    def _terminal_allowance_remaining(self, state: GroundingCoordinatorState) -> int:
        return (
            self.config.max_terminal_assessment_requests
            - state.budget.terminal_assessment_requests
        )

    def _select_turn_mode(
        self, state: GroundingCoordinatorState
    ) -> GroundingProviderTurnMode | None:
        """Fix the lifecycle role of the next turn before the provider is called.

        The terminal allowance is reserved: exploration can never spend it, and
        it is only legal once an observation exists that still needs a bounded
        final assessment.  ``None`` means no provider turn is legal at all.
        """

        if self._exploration_remaining(state) > 0:
            return GroundingProviderTurnMode.EXPLORATION
        if state.observation_history and self._terminal_allowance_remaining(state) > 0:
            return GroundingProviderTurnMode.TERMINAL_ASSESSMENT
        return None

    def _charge_provider_turn(
        self,
        state: GroundingCoordinatorState,
        turn_mode: GroundingProviderTurnMode,
    ) -> GroundingCoordinatorState:
        """Charge one provider invocation before it is made.

        The charge is unconditional so that a turn which later fails to decode
        or parse still appears in the truthful provider accounting.
        """

        if turn_mode is GroundingProviderTurnMode.TERMINAL_ASSESSMENT:
            if not state.observation_history:
                raise GroundingInvariantError(
                    "terminal assessment requires an observation"
                )
            if self._terminal_allowance_remaining(state) <= 0:
                raise _BudgetExhausted("terminal assessment allowance exhausted")
            delta = GroundingBudgetDelta(
                provider_requests=1, terminal_assessment_requests=1
            )
        else:
            if self._exploration_remaining(state) <= 0:
                raise _BudgetExhausted("exploration provider budget exhausted")
            delta = GroundingBudgetDelta(
                provider_requests=1, exploration_provider_requests=1
            )
        return self._apply_budget(state, delta)

    def _invoke_provider(
        self,
        state: GroundingCoordinatorState,
        *,
        provider_request_id: str,
        turn_mode: GroundingProviderTurnMode,
    ) -> GroundingProposal:
        context = GroundingDecisionContext(
            state=state,
            rendered_grounding_state=(
                "## OPERATOR TASK\n"
                + self.config.operator_task
                + "\n\n"
                + render_grounding_state(state)
            ),
            operator_task=self.config.operator_task,
            turn_mode=turn_mode,
        )
        self._emit(
            EventType.GROUNDING_REQUEST,
            {
                "grounding_run_id": state.grounding_run_id,
                "provider_request_id": provider_request_id,
                "provider_request_number": state.budget.provider_requests,
                "turn_mode": turn_mode.value,
                "budget": _plain(state.budget),
            },
        )
        try:
            raw = self.provider.decide(context)
            proposal = _parse_wire_response(
                raw,
                after_observation=bool(state.observation_history),
                terminal_only=turn_mode.terminal_only,
            )
        except _MalformedProviderResponse as exc:
            raise GroundingProviderError(str(exc)) from exc
        except Exception as exc:
            raise GroundingProviderError(str(exc)) from exc
        return proposal

    def _request_from_proposal(
        self, state: GroundingCoordinatorState, proposal: GroundingProposal
    ) -> GroundingRequest:
        if proposal.action_payload is None:
            raise _MalformedProviderResponse("grounding action is required")
        return parse_grounding_request(
            proposal.action_payload,
            grounding_run_id=state.grounding_run_id,
            request_id=f"grounding-request-{len(state.request_history) + 1}",
        )

    def _record_request_rejection(
        self,
        state: GroundingCoordinatorState,
        exc: GroundingRequestRejection,
        provider_request_id: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> GroundingCoordinatorState:
        return self._append_rejection(
            state,
            GroundingRejection(
                rejection_id=f"grounding-rejection-{len(state.rejection_history) + 1}",
                grounding_run_id=state.grounding_run_id,
                provider_request_id=provider_request_id,
                code=exc.code,
                action_kind=exc.action_kind,
                message=str(exc)[:240],
            ),
            protocol_rejection_code=_protocol_rejection_code(exc, payload),
        )

    def _execute_request(
        self, state: GroundingCoordinatorState, request: GroundingRequest
    ) -> GroundingCoordinatorState:
        state = self._append_request(state, request)
        self._emit(
            EventType.GROUNDING_REQUEST,
            {
                "grounding_run_id": state.grounding_run_id,
                "request_id": request.request_id,
                "action": request.action_identity,
                "normalized_request": _plain(request.normalized_payload),
                "outcome": "accepted",
                "budget": _plain(state.budget),
            },
        )
        executor_limits = replace(self.config.budget_limits, distinct_files=None)
        observation = self.executor.execute(
            request,
            budget=state.budget,
            limits=executor_limits,
        )
        state = self._append_observation(state, observation)
        self._emit(
            EventType.GROUNDING_OBSERVATION,
            {
                "grounding_run_id": state.grounding_run_id,
                "observation_id": observation.observation_id,
                "request_id": observation.request_id,
                "action": observation.action_identity,
                "outcome": observation.outcome.value,
                "normalized_request": _plain(request.normalized_payload),
                "evidence_bytes": observation.budget_delta.source_evidence_bytes,
                "budget": _plain(observation.budget_cumulative),
            },
        )
        return state

    def run(self) -> GroundingResult:
        state = self._initial_state()
        self._budget_trace.append(state.budget)
        self._emit(
            EventType.GROUNDING_STARTED,
            {
                "grounding_run_id": state.grounding_run_id,
                "state": state.lifecycle_state.value,
                "orientation_available": bool(state.orientation_advisory),
                "budget": _plain(state.budget),
            },
        )
        state = _transition(state, GroundingLifecycleState.ORIENTED)
        if self.config.mechanical_skip:
            state = _transition(state, GroundingLifecycleState.SKIPPED)
            return self._result(
                state,
                GroundingLifecycleState.SKIPPED,
                GroundingTerminalReason.SKIPPED,
            )

        correction_turns = 0
        while True:
            try:
                self._check_snapshot_identity()
                self._check_source_versions(state)
            except _SourceVersionChanged:
                state = _transition(state, GroundingLifecycleState.FAILED)
                return self._result(
                    state,
                    GroundingLifecycleState.FAILED,
                    GroundingTerminalReason.SOURCE_VERSION_CHANGED,
                )

            turn_mode = self._select_turn_mode(state)
            if turn_mode is None:
                state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                return self._result(
                    state,
                    GroundingLifecycleState.INSUFFICIENT,
                    GroundingTerminalReason.BUDGET_EXHAUSTED,
                )

            after_observation = bool(state.observation_history)
            if after_observation and state.lifecycle_state in {
                GroundingLifecycleState.OBSERVED,
                GroundingLifecycleState.REQUESTING,
            }:
                state = _transition(state, GroundingLifecycleState.ASSESSING)
            elif (
                not after_observation
                and state.lifecycle_state is GroundingLifecycleState.ORIENTED
            ):
                state = _transition(state, GroundingLifecycleState.REQUESTING)

            provider_request_id = (
                f"grounding-provider-request-{state.budget.provider_requests + 1}"
            )
            try:
                state = self._charge_provider_turn(state, turn_mode)
            except _BudgetExhausted:
                state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                return self._result(
                    state,
                    GroundingLifecycleState.INSUFFICIENT,
                    GroundingTerminalReason.BUDGET_EXHAUSTED,
                )
            try:
                proposal = self._invoke_provider(
                    state,
                    provider_request_id=provider_request_id,
                    turn_mode=turn_mode,
                )
            except GroundingProviderError:
                state = _transition(state, GroundingLifecycleState.FAILED)
                return self._result(
                    state,
                    GroundingLifecycleState.FAILED,
                    GroundingTerminalReason.EXECUTOR_FAILURE,
                )

            if not after_observation:
                try:
                    request = self._request_from_proposal(state, proposal)
                except GroundingRequestRejection as exc:
                    if _is_budget_rejection(exc):
                        state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                        return self._result(
                            state,
                            GroundingLifecycleState.INSUFFICIENT,
                            GroundingTerminalReason.BUDGET_EXHAUSTED,
                        )
                    state = self._record_request_rejection(
                        state,
                        exc,
                        provider_request_id,
                        payload=proposal.action_payload,
                    )
                    if correction_turns == 0 and self._exploration_remaining(state) > 0:
                        correction_turns += 1
                        continue
                    state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                    return self._result(
                        state,
                        GroundingLifecycleState.INSUFFICIENT,
                        GroundingTerminalReason.INVALID_MODEL_REQUEST,
                    )
                try:
                    state = self._execute_request(state, request)
                except GroundingRequestRejection as exc:
                    if _is_budget_rejection(exc):
                        state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                        return self._result(
                            state,
                            GroundingLifecycleState.INSUFFICIENT,
                            GroundingTerminalReason.BUDGET_EXHAUSTED,
                        )
                    state = self._record_request_rejection(
                        state,
                        exc,
                        provider_request_id,
                        payload=request.normalized_payload,
                    )
                    if correction_turns == 0 and self._exploration_remaining(state) > 0:
                        correction_turns += 1
                        continue
                    state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                    return self._result(
                        state,
                        GroundingLifecycleState.INSUFFICIENT,
                        GroundingTerminalReason.INVALID_MODEL_REQUEST,
                    )
                except _BudgetExhausted:
                    state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                    return self._result(
                        state,
                        GroundingLifecycleState.INSUFFICIENT,
                        GroundingTerminalReason.BUDGET_EXHAUSTED,
                    )
                except GroundingExecutionError:
                    state = _transition(state, GroundingLifecycleState.FAILED)
                    return self._result(
                        state,
                        GroundingLifecycleState.FAILED,
                        GroundingTerminalReason.EXECUTOR_FAILURE,
                    )
                correction_turns = 0
                continue

            next_action: GroundingRequest | None = None
            if proposal.assessment_kind is GroundingAssessmentKind.NEED_MORE_EVIDENCE:
                try:
                    next_action = self._request_from_proposal(state, proposal)
                except GroundingRequestRejection as exc:
                    if _is_budget_rejection(exc):
                        state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                        return self._result(
                            state,
                            GroundingLifecycleState.INSUFFICIENT,
                            GroundingTerminalReason.BUDGET_EXHAUSTED,
                        )
                    state = self._record_request_rejection(
                        state,
                        exc,
                        provider_request_id,
                        payload=proposal.action_payload,
                    )
                    if correction_turns == 0 and self._exploration_remaining(state) > 0:
                        correction_turns += 1
                        continue
                    state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                    return self._result(
                        state,
                        GroundingLifecycleState.INSUFFICIENT,
                        GroundingTerminalReason.INVALID_MODEL_REQUEST,
                    )
            try:
                assessment = self._assessment(
                    state,
                    proposal,
                    provider_request_id=provider_request_id,
                    next_action=next_action,
                )
            except _InvalidSufficiency as exc:
                self._emit_assessment_validation_failure(
                    state,
                    provider_request_id=provider_request_id,
                    code="invalid_assessment_citations",
                    failure_layer="L7_SEMANTIC_PROTOCOL_STATE",
                    detail=str(exc),
                )
                state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                return self._result(
                    state,
                    GroundingLifecycleState.INSUFFICIENT,
                    GroundingTerminalReason.INVALID_MODEL_REQUEST,
                )
            except (_MalformedProviderResponse, GroundingInvariantError) as exc:
                self._emit_assessment_validation_failure(
                    state,
                    provider_request_id=provider_request_id,
                    code=(
                        "invalid_assessment_state"
                        if isinstance(exc, GroundingInvariantError)
                        else "invalid_post_observation_assessment"
                    ),
                    failure_layer=(
                        "L7_SEMANTIC_PROTOCOL_STATE"
                        if isinstance(exc, GroundingInvariantError)
                        else "L6_ASSESSMENT_SCHEMA"
                    ),
                    detail=str(exc),
                )
                state = _transition(state, GroundingLifecycleState.FAILED)
                return self._result(
                    state,
                    GroundingLifecycleState.FAILED,
                    GroundingTerminalReason.INVALID_MODEL_REQUEST,
                )
            state = self._append_assessment(state, assessment)
            if assessment.decision is GroundingAssessmentKind.SUFFICIENT:
                state = _transition(state, GroundingLifecycleState.SUFFICIENT)
                return self._result(
                    state,
                    GroundingLifecycleState.SUFFICIENT,
                    GroundingTerminalReason.SUFFICIENT,
                    assessment=assessment,
                )
            if assessment.decision is GroundingAssessmentKind.INSUFFICIENT:
                state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                return self._result(
                    state,
                    GroundingLifecycleState.INSUFFICIENT,
                    GroundingTerminalReason.INSUFFICIENT_GROUNDING,
                    assessment=assessment,
                )

            state = _transition(state, GroundingLifecycleState.NEED_MORE_EVIDENCE)
            if next_action is None:  # defensive; constructor already enforces it
                state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                return self._result(
                    state,
                    GroundingLifecycleState.INSUFFICIENT,
                    GroundingTerminalReason.INVALID_MODEL_REQUEST,
                )
            if (
                self._exploration_remaining(state) <= 0
                and self._terminal_allowance_remaining(state) <= 0
            ):
                # Never read repository evidence that already has no bounded
                # assessment opportunity left.  Reserving the terminal
                # allowance makes this unreachable under the shipped policy;
                # it stays as the explicit fail-closed statement of the rule.
                state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                return self._result(
                    state,
                    GroundingLifecycleState.INSUFFICIENT,
                    GroundingTerminalReason.BUDGET_EXHAUSTED,
                )
            state = _transition(state, GroundingLifecycleState.REQUESTING)
            try:
                state = self._execute_request(state, next_action)
            except GroundingRequestRejection as exc:
                if _is_budget_rejection(exc):
                    state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                    return self._result(
                        state,
                        GroundingLifecycleState.INSUFFICIENT,
                        GroundingTerminalReason.BUDGET_EXHAUSTED,
                    )
                state = self._record_request_rejection(
                    state,
                    exc,
                    provider_request_id,
                    payload=next_action.normalized_payload,
                )
                if correction_turns == 0 and self._exploration_remaining(state) > 0:
                    correction_turns += 1
                    continue
                state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                return self._result(
                    state,
                    GroundingLifecycleState.INSUFFICIENT,
                    GroundingTerminalReason.INVALID_MODEL_REQUEST,
                )
            except _BudgetExhausted:
                state = _transition(state, GroundingLifecycleState.INSUFFICIENT)
                return self._result(
                    state,
                    GroundingLifecycleState.INSUFFICIENT,
                    GroundingTerminalReason.BUDGET_EXHAUSTED,
                )
            except GroundingExecutionError:
                state = _transition(state, GroundingLifecycleState.FAILED)
                return self._result(
                    state,
                    GroundingLifecycleState.FAILED,
                    GroundingTerminalReason.EXECUTOR_FAILURE,
                )
            correction_turns = 0


__all__ = [
    "GroundingCoordinator",
    "GroundingDecisionProvider",
    "GroundingInvariantError",
    "GroundingProviderError",
    "parse_grounding_provider_response",
    "render_grounding_state",
    "transition_grounding_state",
]
