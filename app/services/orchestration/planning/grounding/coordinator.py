"""Bounded production Grounding Coordinator over the PGI1 executor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass, replace
import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.services.orchestration.planning.source_materialization import (
    current_source_version_identity,
)
from app.services.orchestration.events.event_types import EventType

from .contracts import (
    GroundingBudgetAccounting,
    GroundingBudgetDelta,
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
    GroundingProposal,
    GroundingRequestStateSignal,
    GroundingRequestStateSignalKind,
    GroundingResult,
    GroundingRunConfig,
    GroundingStateProjection,
    GroundingTerminalReason,
)
from .executor import GroundingExecutor


EventSink = Callable[[str, Mapping[str, Any]], Any]


class GroundingProviderError(RuntimeError):
    """Provider infrastructure failed before a semantic proposal was returned."""


@runtime_checkable
class GroundingDecisionProvider(Protocol):
    """Narrow semantic proposal boundary used by the coordinator."""

    def decide(self, context: GroundingDecisionContext) -> GroundingProposal:
        """Return one typed action/assessment proposal from rendered state."""


class _InvalidModelRequest(ValueError):
    pass


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


def _remaining_budget(config: GroundingRunConfig, budget: Any) -> dict[str, int | None]:
    remaining: dict[str, int | None] = {}
    for name in (
        "provider_requests",
        "repository_actions",
        "source_evidence_bytes",
        "distinct_files",
        "positive_regions",
    ):
        limit = getattr(config.budget_limits, name)
        remaining[name] = (
            None if limit is None else max(0, limit - getattr(budget, name))
        )
    return remaining


def render_grounding_state(state: GroundingCoordinatorState) -> str:
    """Render all typed history required for the next epistemic decision."""

    requests = [
        {
            "request_id": request.request_id,
            "action": request.action_identity,
            "normalized_action": _plain(request.normalized_payload),
            "action_digest": request.action_digest,
        }
        for request in state.request_history
    ]
    observations = []
    for observation in state.observation_history:
        observations.append(
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
        )
    assessments = [
        {
            "assessment_id": assessment.assessment_id,
            "kind": assessment.kind.value,
            "cited_observation_ids": list(assessment.cited_observation_ids),
            "cited_source_paths": list(assessment.cited_source_paths),
            "cited_structural_identities": _plain(
                assessment.cited_structural_identities
            ),
            "rationale": assessment.rationale,
            "unresolved_risk": assessment.unresolved_risk,
            "next_action_digest": assessment.next_action_digest,
            "terminal_reason": (
                assessment.terminal_reason.value if assessment.terminal_reason else None
            ),
        }
        for assessment in state.assessment_history
    ]
    negative_facts = []
    for observation in state.observation_history:
        if observation.outcome is not GroundingOutcome.NOT_FOUND:
            continue
        request = next(
            (
                request
                for request in state.request_history
                if request.request_id == observation.request_id
            ),
            None,
        )
        if request is not None:
            negative_facts.append(
                {
                    "observation_id": observation.observation_id,
                    "action_digest": request.action_digest,
                    "outcome": GroundingOutcome.NOT_FOUND.value,
                }
            )
    payload = {
        "grounding_run_id": state.grounding_run_id,
        "task_reference": _plain(state.task_reference),
        "workspace_identity": state.workspace_identity,
        "snapshot_identity": state.snapshot_identity,
        "request_history": requests,
        "attempted_action_digests": list(state.attempted_action_digests),
        "observation_history": observations,
        "assessment_history": assessments,
        "discovered_source_paths": list(state.discovered_source_paths),
        "discovered_structural_identities": _plain(
            state.discovered_structural_identities
        ),
        "source_versions": _plain(state.source_versions),
        "budget": _plain(state.budget),
        "remaining_budget": _plain(state.remaining_budget),
        "request_state_signals": _plain(state.request_state_signals),
        "previous_terminal_negative_facts": negative_facts,
        "orientation_advisory": _plain(state.orientation_advisory),
        "termination_state": (
            state.termination_state.value if state.termination_state else None
        ),
    }
    return "## TYPED GROUNDING STATE\n" + json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )


class GroundingCoordinator:
    """Coordinate provider proposals and deterministic read-only observations."""

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
        self._budget_trace: list[Any] = []

    @staticmethod
    def _normalize_config(config: GroundingRunConfig) -> GroundingRunConfig:
        limits = config.budget_limits
        if limits.repository_actions is None or limits.provider_requests is None:
            limits = replace(
                limits,
                repository_actions=(
                    config.max_steps
                    if limits.repository_actions is None
                    else limits.repository_actions
                ),
                provider_requests=(
                    config.max_provider_requests
                    if limits.provider_requests is None
                    else limits.provider_requests
                ),
            )
            config = replace(config, budget_limits=limits)
        return config

    def _emit(self, event_type: str, details: Mapping[str, Any]) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink(event_type, details)
        except Exception:
            # Event durability must not grant the coordinator new authority or
            # turn an otherwise valid provider-free read into a mutation path.
            return

    def _initial_state(self) -> GroundingCoordinatorState:
        from .contracts import GroundingBudgetSnapshot

        budget = GroundingBudgetSnapshot()
        return GroundingCoordinatorState(
            grounding_run_id=self.config.grounding_run_id,
            task_reference=self.config.task_reference,
            workspace_identity=self.config.workspace_identity,
            snapshot_identity=self.config.snapshot_identity,
            budget=budget,
            remaining_budget=_remaining_budget(self.config, budget),
            orientation_advisory=self.config.orientation_advisory,
        )

    def _with_budget(self, state: GroundingCoordinatorState, budget: Any):
        return replace(
            state,
            budget=budget,
            remaining_budget=_remaining_budget(self.config, budget),
        )

    def _apply_budget(
        self,
        state: GroundingCoordinatorState,
        delta: GroundingBudgetDelta,
    ) -> GroundingCoordinatorState:
        try:
            accounting = GroundingBudgetAccounting(state.budget).apply(
                delta, self.config.budget_limits
            )
        except GroundingRequestRejection as exc:
            raise _BudgetExhausted(str(exc)) from exc
        next_state = self._with_budget(state, accounting.snapshot)
        self._budget_trace.append(accounting.snapshot)
        return next_state

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
            if not version:
                raise _SourceVersionChanged(f"missing source version: {path}")
            prior = state.source_versions.get(path)
            if prior is not None and prior != version:
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
        self._budget_trace.append(observation.budget_cumulative)
        return replace(
            state,
            observation_history=(*state.observation_history, observation),
            discovered_source_paths=source_paths,
            discovered_structural_identities=structural,
            source_versions=versions,
            budget=observation.budget_cumulative,
            remaining_budget=_remaining_budget(
                self.config, observation.budget_cumulative
            ),
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

    def _duplicate_negative_observation(
        self, state: GroundingCoordinatorState, request: GroundingRequest
    ) -> GroundingObservation | None:
        for prior_request in state.request_history:
            if prior_request.action_digest != request.action_digest:
                continue
            for observation in state.observation_history:
                if (
                    observation.request_id == prior_request.request_id
                    and observation.outcome is GroundingOutcome.NOT_FOUND
                ):
                    return observation
        return None

    def _validate_citations(
        self,
        state: GroundingCoordinatorState,
        proposal: GroundingProposal,
        *,
        sufficient: bool,
    ) -> None:
        observations = {item.observation_id: item for item in state.observation_history}
        cited_ids = tuple(dict.fromkeys(proposal.cited_observation_ids))
        if any(observation_id not in observations for observation_id in cited_ids):
            raise _InvalidModelRequest("assessment cites an unknown observation")
        if sufficient and not cited_ids:
            raise _InvalidModelRequest("SUFFICIENT requires cited observations")
        if sufficient and not proposal.cited_source_paths:
            raise _InvalidModelRequest("SUFFICIENT requires cited source paths")
        if sufficient and not proposal.rationale.strip():
            raise _InvalidModelRequest("SUFFICIENT requires bounded rationale")
        cited_observations = [observations[item] for item in cited_ids]
        for path in proposal.cited_source_paths:
            if not any(
                path in observation.source_paths for observation in cited_observations
            ):
                raise _InvalidModelRequest(
                    "cited source path is absent from cited evidence"
                )
            if not any(
                path in observation.source_versions
                for observation in cited_observations
            ):
                raise _InvalidModelRequest("cited source path has no version identity")
        for identity in proposal.cited_structural_identities:
            if not any(
                observation.structural_identity == identity
                for observation in cited_observations
            ):
                raise _InvalidModelRequest(
                    "cited structural identity is absent from cited evidence"
                )
            if identity.source_path not in proposal.cited_source_paths:
                raise _InvalidModelRequest(
                    "cited structural identity path is not cited"
                )
        if proposal.rationale and len(proposal.rationale) > 1000:
            raise _InvalidModelRequest("assessment rationale exceeds bound")

    def _assessment(
        self,
        state: GroundingCoordinatorState,
        proposal: GroundingProposal,
        *,
        next_action_digest: str | None = None,
    ) -> GroundingAssessment:
        if proposal.assessment_kind is None:
            raise _InvalidModelRequest(
                "assessment kind is required after an observation"
            )
        if proposal.assessment_kind is GroundingAssessmentKind.SUFFICIENT:
            self._validate_citations(state, proposal, sufficient=True)
            terminal_reason = GroundingTerminalReason.SUFFICIENT
        elif proposal.assessment_kind is GroundingAssessmentKind.NEED_MORE_EVIDENCE:
            self._validate_citations(state, proposal, sufficient=False)
            terminal_reason = None
        else:
            terminal_reason = proposal.terminal_reason
            if terminal_reason not in (
                GroundingTerminalReason.INSUFFICIENT_GROUNDING,
                GroundingTerminalReason.BUDGET_EXHAUSTED,
            ):
                raise _InvalidModelRequest(
                    "provider terminal stop reason is unsupported"
                )
        return GroundingAssessment(
            assessment_id=(f"grounding-assessment-{len(state.assessment_history) + 1}"),
            grounding_run_id=state.grounding_run_id,
            kind=proposal.assessment_kind,
            cited_observation_ids=tuple(proposal.cited_observation_ids),
            cited_source_paths=tuple(proposal.cited_source_paths),
            cited_structural_identities=tuple(proposal.cited_structural_identities),
            rationale=proposal.rationale.strip(),
            unresolved_risk=proposal.unresolved_risk,
            next_action_digest=next_action_digest,
            terminal_reason=terminal_reason,
        )

    def _append_assessment(
        self, state: GroundingCoordinatorState, assessment: GroundingAssessment
    ) -> GroundingCoordinatorState:
        self._emit(
            EventType.GROUNDING_ASSESSMENT,
            {
                "grounding_run_id": state.grounding_run_id,
                "assessment_id": assessment.assessment_id,
                "kind": assessment.kind.value,
                "cited_observation_ids": list(assessment.cited_observation_ids),
                "cited_source_paths": list(assessment.cited_source_paths),
                "rationale": assessment.rationale[:240],
                "unresolved_risk": assessment.unresolved_risk,
            },
        )
        return replace(
            state, assessment_history=(*state.assessment_history, assessment)
        )

    def _result(
        self,
        state: GroundingCoordinatorState,
        reason: GroundingTerminalReason,
        *,
        assessment: GroundingAssessment | None = None,
    ) -> GroundingResult:
        terminal_state = replace(state, termination_state=reason)
        projection = GroundingStateProjection(
            grounding_run_id=terminal_state.grounding_run_id,
            task_reference=terminal_state.task_reference,
            workspace_identity=terminal_state.workspace_identity,
            snapshot_identity=terminal_state.snapshot_identity,
            request_digests=tuple(
                request.action_digest for request in terminal_state.request_history
            ),
            observation_ids=tuple(
                observation.observation_id
                for observation in terminal_state.observation_history
            ),
            observation_outcomes=tuple(
                observation.outcome.value
                for observation in terminal_state.observation_history
            ),
            assessment_history=terminal_state.assessment_history,
            attempted_action_digests=terminal_state.attempted_action_digests,
            request_state_signals=terminal_state.request_state_signals,
            discovered_source_paths=terminal_state.discovered_source_paths,
            discovered_structural_identities=terminal_state.discovered_structural_identities,
            source_versions=terminal_state.source_versions,
            remaining_budget=terminal_state.remaining_budget,
            termination_state=reason,
        )
        cited_ids = assessment.cited_observation_ids if assessment else ()
        cited_paths = assessment.cited_source_paths if assessment else ()
        cited_structural = assessment.cited_structural_identities if assessment else ()
        observations = {
            item.observation_id: item for item in terminal_state.observation_history
        }
        evidence: list[GroundingEvidence] = []
        for path in cited_paths:
            observation = next(
                (
                    item
                    for item_id in cited_ids
                    for item in (observations.get(item_id),)
                    if item is not None and path in item.source_versions
                ),
                None,
            )
            if observation is not None:
                evidence.append(
                    GroundingEvidence(
                        observation_id=observation.observation_id,
                        source_path=path,
                        source_version=observation.source_versions[path],
                        bounded_content=observation.bounded_content,
                    )
                )
        result = GroundingResult(
            grounding_run_id=terminal_state.grounding_run_id,
            terminal_reason=reason,
            state_projection=projection,
            cited_observation_ids=tuple(cited_ids),
            cited_source_paths=tuple(cited_paths),
            cited_structural_identities=tuple(cited_structural),
            source_versions={
                path: terminal_state.source_versions[path] for path in cited_paths
            },
            cited_source_evidence=tuple(evidence),
            budget_trace=tuple(self._budget_trace),
            unresolved_risk=(assessment.unresolved_risk if assessment else True),
            provider_model_telemetry={
                "provider": self.config.provider_name,
                "model": self.config.model_name,
                "provider_requests": terminal_state.budget.provider_requests,
            },
            grounding_diagnostics={
                "observation_count": len(terminal_state.observation_history),
                "assessment_count": len(terminal_state.assessment_history),
                "duplicate_negative_signal_count": len(
                    terminal_state.request_state_signals
                ),
            },
        )
        self._emit(
            EventType.GROUNDING_TERMINAL,
            {
                "grounding_run_id": result.grounding_run_id,
                "terminal_reason": reason.value,
                "observation_count": len(terminal_state.observation_history),
                "cited_observation_ids": list(result.cited_observation_ids),
                "cited_source_paths": list(result.cited_source_paths),
                "source_versions": dict(result.source_versions),
            },
        )
        return result

    def run(self) -> GroundingResult:
        state = self._initial_state()
        self._budget_trace.append(state.budget)
        self._emit(
            EventType.GROUNDING_STARTED,
            {
                "grounding_run_id": state.grounding_run_id,
                "task_reference": _plain(state.task_reference),
                "workspace_identity": state.workspace_identity,
                "snapshot_identity": state.snapshot_identity,
                "max_steps": self.config.max_steps,
                "max_provider_requests": self.config.max_provider_requests,
            },
        )
        while True:
            try:
                self._check_snapshot_identity()
                self._check_source_versions(state)
            except _SourceVersionChanged:
                return self._result(
                    state, GroundingTerminalReason.SOURCE_VERSION_CHANGED
                )

            if state.budget.provider_requests >= self.config.max_provider_requests:
                return self._result(state, GroundingTerminalReason.BUDGET_EXHAUSTED)

            decision_context = GroundingDecisionContext(
                state=state,
                rendered_grounding_state=(
                    "## OPERATOR TASK\n"
                    + self.config.operator_task
                    + "\n\n"
                    + render_grounding_state(state)
                ),
                operator_task=self.config.operator_task,
            )
            try:
                state = self._apply_budget(
                    state, GroundingBudgetDelta(provider_requests=1)
                )
                proposal = self.provider.decide(decision_context)
            except _BudgetExhausted:
                return self._result(state, GroundingTerminalReason.BUDGET_EXHAUSTED)
            except _InvalidModelRequest:
                return self._result(
                    state, GroundingTerminalReason.INVALID_MODEL_REQUEST
                )
            except GroundingProviderError:
                return self._result(state, GroundingTerminalReason.EXECUTOR_FAILURE)
            except Exception:
                return self._result(state, GroundingTerminalReason.EXECUTOR_FAILURE)
            if not isinstance(proposal, GroundingProposal):
                return self._result(
                    state, GroundingTerminalReason.INVALID_MODEL_REQUEST
                )

            has_observation = bool(state.observation_history)
            if not has_observation and proposal.assessment_kind is not None:
                return self._result(
                    state, GroundingTerminalReason.INVALID_MODEL_REQUEST
                )
            if has_observation and proposal.assessment_kind is None:
                return self._result(
                    state, GroundingTerminalReason.INVALID_MODEL_REQUEST
                )
            if proposal.action_payload is None:
                if not has_observation:
                    return self._result(
                        state, GroundingTerminalReason.INVALID_MODEL_REQUEST
                    )
                try:
                    assessment = self._assessment(state, proposal)
                except (ValueError, _InvalidModelRequest):
                    return self._result(
                        state, GroundingTerminalReason.INVALID_MODEL_REQUEST
                    )
                state = self._append_assessment(state, assessment)
                if assessment.kind is GroundingAssessmentKind.SUFFICIENT:
                    try:
                        self._check_source_versions(state)
                    except _SourceVersionChanged:
                        return self._result(
                            state, GroundingTerminalReason.SOURCE_VERSION_CHANGED
                        )
                    return self._result(
                        state, GroundingTerminalReason.SUFFICIENT, assessment=assessment
                    )
                if assessment.kind is GroundingAssessmentKind.TERMINAL_STOP:
                    return self._result(
                        state,
                        assessment.terminal_reason
                        or GroundingTerminalReason.INSUFFICIENT_GROUNDING,
                        assessment=assessment,
                    )
                return self._result(
                    state, GroundingTerminalReason.INVALID_MODEL_REQUEST
                )

            try:
                request = parse_grounding_request(
                    proposal.action_payload,
                    grounding_run_id=state.grounding_run_id,
                    request_id=f"grounding-request-{len(state.request_history) + 1}",
                )
            except GroundingRequestRejection:
                return self._result(
                    state, GroundingTerminalReason.INVALID_MODEL_REQUEST
                )

            next_digest = request.action_digest
            assessment = None
            if has_observation:
                try:
                    assessment = self._assessment(
                        state, proposal, next_action_digest=next_digest
                    )
                except (ValueError, _InvalidModelRequest):
                    return self._result(
                        state, GroundingTerminalReason.INVALID_MODEL_REQUEST
                    )
                state = self._append_assessment(state, assessment)

            duplicate = self._duplicate_negative_observation(state, request)
            state = self._append_request(state, request)
            self._emit(
                EventType.GROUNDING_REQUEST,
                {
                    "grounding_run_id": state.grounding_run_id,
                    "request_id": request.request_id,
                    "action": request.action_identity,
                    "action_digest": request.action_digest,
                    "duplicate_terminal_not_found": duplicate is not None,
                },
            )
            if duplicate is not None:
                signal = GroundingRequestStateSignal(
                    kind=GroundingRequestStateSignalKind.DUPLICATE_TERMINAL_NOT_FOUND,
                    grounding_run_id=state.grounding_run_id,
                    action_digest=request.action_digest,
                    source_observation_id=duplicate.observation_id,
                )
                state = replace(
                    state,
                    request_state_signals=(*state.request_state_signals, signal),
                )
                continue

            if state.budget.repository_actions >= self.config.max_steps:
                return self._result(state, GroundingTerminalReason.BUDGET_EXHAUSTED)
            try:
                observation = self.executor.execute(
                    request,
                    budget=state.budget,
                    limits=self.config.budget_limits,
                )
                state = self._append_observation(state, observation)
            except GroundingRequestRejection as exc:
                if str(exc.code).startswith("budget_"):
                    return self._result(state, GroundingTerminalReason.BUDGET_EXHAUSTED)
                return self._result(
                    state, GroundingTerminalReason.INVALID_MODEL_REQUEST
                )
            except GroundingExecutionError as exc:
                source_codes = {
                    "source_stability_failed",
                    "source_changed_during_read",
                    "source_changed_during_search",
                    "source_disappeared",
                }
                reason = (
                    GroundingTerminalReason.SOURCE_VERSION_CHANGED
                    if str(exc.args[0] if exc.args else "") in source_codes
                    else GroundingTerminalReason.EXECUTOR_FAILURE
                )
                return self._result(state, reason)
            except Exception:
                return self._result(state, GroundingTerminalReason.EXECUTOR_FAILURE)
            self._emit(
                EventType.GROUNDING_OBSERVATION,
                {
                    "grounding_run_id": state.grounding_run_id,
                    "observation_id": observation.observation_id,
                    "request_id": observation.request_id,
                    "outcome": observation.outcome.value,
                    "source_paths": list(observation.source_paths),
                    "source_versions": dict(observation.source_versions),
                    "budget": _plain(observation.budget_cumulative),
                },
            )


class _BudgetExhausted(RuntimeError):
    pass


class _SourceVersionChanged(RuntimeError):
    pass


__all__ = [
    "GroundingCoordinator",
    "GroundingDecisionProvider",
    "GroundingProviderError",
    "render_grounding_state",
]
