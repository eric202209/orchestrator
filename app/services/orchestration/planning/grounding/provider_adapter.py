"""Planning-provider adapter for the typed grounding wire contract.

This module owns only the provider boundary.  The coordinator remains the
owner of lifecycle, action validation, deterministic repository execution, and
the canonical result.  The adapter deliberately performs no JSON recovery or
semantic normalization.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

from app.services.orchestration.events.event_types import EventType

if TYPE_CHECKING:
    from app.services.planning.providers.base import (
        PlanningProvider,
        PlanningResponse,
    )

from .coordinator import (
    GroundingProviderError,
    parse_grounding_provider_response,
    render_grounding_state,
)
from .coordinator_contracts import GroundingDecisionContext


GROUNDING_PROVIDER_TIMEOUT_SECONDS = 180
MAX_CAPTURED_CANDIDATE_PREFIX = 256
MAX_CAPTURED_FAILURE_DETAIL = 500

FIRST_TURN_WIRE_EXAMPLES = (
    '{"action":"search_text","query":"needle","scopes":["app"]}',
    '{"action":"inspect_file","path":"app/example.py"}',
    '{"action":"resolve_structure","relation":"symbol_definition",'
    '"locator":{"path":"app/example.py","name":"target"}}',
    '{"action":"resolve_structure","relation":"enclosing_symbol",'
    '"locator":{"path":"app/example.py","line":42}}',
    '{"action":"resolve_structure","relation":"mounted_route",'
    '"locator":{"path":"app/routes.py","method":"GET",'
    '"decorator_path":"/items"}}',
)

POST_OBSERVATION_WIRE_EXAMPLES = (
    '{"decision":"SUFFICIENT","cited_observation_ids":'
    '["grounding-observation-..."],"rationale":"..."}',
    '{"decision":"NEED_MORE_EVIDENCE","next_action":'
    '{"action":"inspect_file","path":"app/example.py"},'
    '"rationale":"..."}',
    '{"decision":"INSUFFICIENT","reason":"..."}',
)

TERMINAL_ASSESSMENT_WIRE_EXAMPLES = (
    '{"decision":"SUFFICIENT","cited_observation_ids":'
    '["grounding-observation-..."],"rationale":"..."}',
    '{"decision":"INSUFFICIENT","reason":"..."}',
)

_REJECTION_DIAGNOSTICS = {
    "unknown_fields": "The previous object used fields outside the closed wire shape.",
    "invalid_first_turn_action": "The first turn must be exactly one legal action object.",
    "invalid_post_observation_assessment": (
        "After an observation the response must be exactly one legal assessment object."
    ),
    "invalid_request": "The proposed action failed the existing bounded request validator.",
    "invalid_request_shape": "The proposed action did not match the existing bounded request shape.",
    "invalid_locator": "The proposed locator did not match the existing bounded locator shape.",
    "unsupported_action": "The proposed action name is not in the existing closed action union.",
    "unsupported_relation": "The proposed relation is not in the existing closed relation union.",
}


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _wire_examples(after_observation: bool, terminal_only: bool = False) -> str:
    if terminal_only:
        examples = TERMINAL_ASSESSMENT_WIRE_EXAMPLES
    elif after_observation:
        examples = POST_OBSERVATION_WIRE_EXAMPLES
    else:
        examples = FIRST_TURN_WIRE_EXAMPLES
    return "\n".join(f"    {example}" for example in examples)


def _remaining_budget_json(context: GroundingDecisionContext) -> str:
    return json.dumps(
        {str(key): value for key, value in context.state.remaining_budget.items()},
        sort_keys=True,
        separators=(",", ":"),
    )


def render_first_turn_prompt(context: GroundingDecisionContext) -> str:
    """Render the exact closed first-turn action protocol."""

    return (
        "READ-ONLY GROUNDING.\n"
        "Return exactly ONE JSON object.\n"
        "Return no prose.\n"
        "Return no Markdown fence.\n"
        "Return no explanation outside JSON.\n"
        "The first turn must be an action. Do not return SUFFICIENT on the first turn.\n"
        "Do not return a Plan. Do not return mutation, write, or shell fields.\n"
        "Unknown fields are invalid. Every field set is closed and exact.\n\n"
        "Allowed first-turn shapes (use exactly one):\n"
        f"{_wire_examples(False)}\n\n"
        "search_text fields are exactly action, query, scopes.\n"
        "Each search scope is a named canonical relative product-owned file or directory path;\n"
        'the repository root "." is not a legal scope. Use a named path such as app.\n'
        "inspect_file fields are exactly action, path.\n"
        "resolve_structure fields are exactly action, relation, locator.\n"
        "relation is exactly symbol_definition, enclosing_symbol, or mounted_route;\n"
        "the locator fields must exactly match the selected relation.\n\n"
        "search_text returns candidate evidence: it names paths and snippets, not\n"
        "the source of any one file. inspect_file and resolve_structure return\n"
        "substantive evidence: the bounded source of one named file or region.\n\n"
        "## IMMUTABLE OPERATOR TASK\n"
        f"{context.operator_task}\n\n"
        "## ADVISORY ORIENTATION\n"
        f"{json.dumps(_json_safe(context.state.orientation_advisory), sort_keys=True)}\n\n"
        "## TYPED GROUNDING STATE\n"
        f"{render_grounding_state(context.state)}\n\n"
        "## REMAINING BUDGET\n"
        f"{_remaining_budget_json(context)}"
    )


def render_post_observation_prompt(context: GroundingDecisionContext) -> str:
    """Render the exact closed post-observation assessment protocol."""

    return (
        "READ-ONLY GROUNDING AFTER OBSERVATION.\n"
        "Return exactly ONE JSON object.\n"
        "Return no prose.\n"
        "Return no Markdown fence.\n"
        "Return no explanation outside JSON.\n"
        "After an observation, a bare action is invalid. Do not return a Plan.\n"
        "Return exactly one of these assessment shapes:\n"
        f"{_wire_examples(True)}\n\n"
        "Unknown fields are invalid. Assessment fields are closed and exact.\n"
        "SUFFICIENT must cite existing positive observation IDs.\n"
        "NOT_FOUND observations cannot be cited as positive evidence.\n"
        "NEED_MORE_EVIDENCE must contain exactly one legal next_action.\n\n"
        "EVIDENCE CLASSES.\n"
        "search_text is candidate/navigation evidence. It locates candidates; it\n"
        "does not carry any single file's source.\n"
        "inspect_file and resolve_structure are substantive evidence.\n"
        "SUFFICIENT must cite at least one substantive positive observation.\n"
        "Search evidence alone can never satisfy grounding.\n"
        "When a candidate is worth reading and repository-action budget remains,\n"
        "NEED_MORE_EVIDENCE with inspect_file or resolve_structure is the normal\n"
        "way to obtain substantive evidence.\n"
        "INSUFFICIENT remains legal when no candidate supports deeper inspection.\n\n"
        "## IMMUTABLE OPERATOR TASK\n"
        f"{context.operator_task}\n\n"
        "## TYPED PRIOR OBSERVATIONS AND STATE\n"
        f"{render_grounding_state(context.state)}\n\n"
        "## REMAINING BUDGET\n"
        f"{_remaining_budget_json(context)}"
    )


def render_terminal_assessment_prompt(context: GroundingDecisionContext) -> str:
    """Render the closed terminal protocol: the run has no further actions."""

    return (
        "READ-ONLY GROUNDING FINAL ASSESSMENT.\n"
        "This is the last turn of this grounding run.\n"
        "No further repository action is possible.\n"
        "Return exactly ONE JSON object.\n"
        "Return no prose.\n"
        "Return no Markdown fence.\n"
        "Return no explanation outside JSON.\n"
        "NEED_MORE_EVIDENCE is invalid on this turn. next_action is invalid.\n"
        "A bare action is invalid. Do not return a Plan.\n"
        "Assess the observations already gathered and return exactly one of:\n"
        f"{_wire_examples(True, terminal_only=True)}\n\n"
        "Unknown fields are invalid. Assessment fields are closed and exact.\n"
        "SUFFICIENT must cite existing positive observation IDs.\n"
        "NOT_FOUND observations cannot be cited as positive evidence.\n"
        "SUFFICIENT requires at least one cited substantive observation, meaning\n"
        "inspect_file or resolve_structure. Search-only evidence cannot satisfy\n"
        "final grounding, because it carries no file's source.\n"
        "If the gathered evidence does not ground the task, return INSUFFICIENT.\n\n"
        "## IMMUTABLE OPERATOR TASK\n"
        f"{context.operator_task}\n\n"
        "## TYPED PRIOR OBSERVATIONS AND STATE\n"
        f"{render_grounding_state(context.state)}\n\n"
        "## REMAINING BUDGET\n"
        f"{_remaining_budget_json(context)}"
    )


def render_rejection_correction_prompt(context: GroundingDecisionContext) -> str:
    """Render a mechanical correction without semantic replacement hints."""

    rejection = context.state.rejection_history[-1]
    after_observation = bool(context.state.observation_history)
    safe_state = replace(context.state, rejection_history=())
    diagnostic = _REJECTION_DIAGNOSTICS.get(
        rejection.code,
        "The previous response failed a bounded grounding protocol check.",
    )
    return (
        "READ-ONLY GROUNDING PROTOCOL CORRECTION.\n"
        "Return exactly ONE JSON object and no prose or Markdown fence.\n"
        f"PREVIOUS_REJECTION_CODE: {rejection.code}\n"
        f"MECHANICAL_DIAGNOSTIC: {diagnostic}\n"
        "Do not infer or replace the requested repository meaning.\n"
        "Do not return a Plan, mutation, write, or shell field.\n"
        "Unknown fields are invalid.\n\n"
        f"LEGAL WIRE SHAPES:\n{_wire_examples(after_observation)}\n\n"
        "Search scopes are named canonical relative product-owned file or directory paths;\n"
        'the repository root "." is not a legal scope.\n\n'
        "## CURRENT TYPED STATE\n"
        f"{render_grounding_state(safe_state)}\n\n"
        "## REMAINING BUDGET\n"
        f"{_remaining_budget_json(context)}"
    )


def render_grounding_provider_prompt(context: GroundingDecisionContext) -> str:
    """Select the deterministic prompt for the current coordinator turn."""

    if context.turn_mode.terminal_only:
        return render_terminal_assessment_prompt(context)
    # The lifecycle role is fixed before the call, so a spent rejection can no
    # longer make an ordinary exploration turn render as a correction.
    if context.turn_mode.is_correction:
        return render_rejection_correction_prompt(context)
    if context.state.observation_history:
        return render_post_observation_prompt(context)
    return render_first_turn_prompt(context)


def _turn_type(context: GroundingDecisionContext) -> str:
    if context.turn_mode.terminal_only:
        return "TERMINAL_ASSESSMENT"
    if context.turn_mode.is_correction:
        return "REJECTION_CORRECTION"
    if context.state.observation_history:
        return "POST_OBSERVATION_ASSESSMENT"
    return "FIRST_ACTION"


def _provider_request_id(context: GroundingDecisionContext) -> str:
    return f"grounding-provider-request-{context.state.budget.provider_requests}"


def _candidate_shape(value: Any) -> tuple[str, list[str]]:
    if isinstance(value, Mapping):
        return "object", sorted(str(key) for key in value)[:32]
    if isinstance(value, str):
        return "string", []
    if value is None:
        return "missing", []
    return type(value).__name__, []


def _candidate_diagnostic(value: Any) -> tuple[str | None, int | None, str | None]:
    if value is None:
        return None, None, None
    if isinstance(value, Mapping):
        try:
            bounded = json.dumps(
                _json_safe(value), sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError):
            bounded = str(value)
    else:
        bounded = str(value)
    return (
        hashlib.sha256(bounded.encode("utf-8", errors="replace")).hexdigest(),
        len(bounded),
        bounded[:MAX_CAPTURED_CANDIDATE_PREFIX],
    )


def _failure_layer(code: str | None) -> str | None:
    if code in {"provider_timeout", "provider_failure"}:
        return "L0_TRANSPORT"
    if code == "content_missing":
        return "L2_CONTENT_EXTRACTION"
    if code == "json_decode_failed":
        return "L3_JSON_SYNTAX"
    if code == "json_not_object":
        return "L4_JSON_ENVELOPE"
    if code == "invalid_first_turn_action":
        return "L5_ACTION_SCHEMA"
    if code == "invalid_post_observation_assessment":
        return "L6_ASSESSMENT_SCHEMA"
    return None


def _wire_rejection_code(
    payload: Any, *, after_observation: bool, terminal_only: bool = False
) -> str:
    if not isinstance(payload, Mapping):
        return "json_not_object"
    if terminal_only:
        return "invalid_terminal_assessment"
    if not after_observation:
        action = payload.get("action")
        expected = {
            "search_text": {"action", "query", "scopes"},
            "inspect_file": {"action", "path"},
            "resolve_structure": {"action", "relation", "locator"},
        }.get(action)
        if expected is not None and set(payload) - expected:
            return "unknown_fields"
        return "invalid_first_turn_action"
    decision = str(payload.get("decision") or "").upper()
    expected = {
        "SUFFICIENT": {"decision", "cited_observation_ids", "rationale"},
        "NEED_MORE_EVIDENCE": {"decision", "next_action", "rationale"},
        "INSUFFICIENT": {"decision", "reason"},
    }.get(decision)
    if expected is not None and set(payload) - expected:
        return "unknown_fields"
    return "invalid_post_observation_assessment"


def _provider_provenance(
    provider: PlanningProvider,
    response: PlanningResponse | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "provider": str(getattr(provider, "name", "unknown")),
        "provider_version": str(getattr(provider, "version", "") or "") or None,
        "backend": None,
        "model": None,
        "adaptation_profile": None,
        "supports_response_format": None,
        "supports_structured_output": None,
    }
    try:
        capabilities = provider.capabilities
        metadata["supports_response_format"] = bool(
            capabilities.supports_response_format
        )
        metadata["supports_structured_output"] = bool(
            capabilities.supports_structured_output
        )
    except Exception:
        pass
    try:
        runtime = provider.runtime_information()
        metadata["backend"] = runtime.runtime_name
        metadata["model"] = runtime.model
        metadata["adaptation_profile"] = runtime.adaptation_profile
    except Exception:
        pass
    if response is not None:
        metadata["provider"] = response.provider_name or metadata["provider"]
        metadata["provider_version"] = (
            response.provider_version or metadata["provider_version"]
        )
        runtime = response.runtime_metadata
        metadata["backend"] = runtime.runtime_name
        metadata["model"] = runtime.model
        metadata["adaptation_profile"] = runtime.adaptation_profile
        metadata["response_diagnostics"] = _json_safe(response.diagnostics.details)
    return metadata


class PlanningGroundingProviderAdapter:
    """Adapt the configured Planning Provider to GroundingDecisionProvider."""

    def __init__(
        self,
        planning_provider: Any,
        *,
        timeout_seconds: int = GROUNDING_PROVIDER_TIMEOUT_SECONDS,
        event_sink: Callable[[str, Mapping[str, Any]], Any] | None = None,
    ) -> None:
        from app.services.planning.providers.base import PlanningProvider

        if not isinstance(planning_provider, PlanningProvider):
            raise TypeError("planning_provider must implement PlanningProvider")
        if int(timeout_seconds) <= 0:
            raise ValueError("grounding provider timeout must be positive")
        self.planning_provider = planning_provider
        self.timeout_seconds = int(timeout_seconds)
        self.event_sink = event_sink

    @property
    def provider_name(self) -> str:
        return str(getattr(self.planning_provider, "name", "planning_provider"))

    @property
    def model_name(self) -> str:
        try:
            return str(self.planning_provider.runtime_information().model or "unbound")
        except Exception:
            return "unbound"

    def _emit_capture(self, details: Mapping[str, Any]) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink(EventType.GROUNDING_PROVIDER_TURN, details)
        except Exception:
            return

    def _capture(
        self,
        context: GroundingDecisionContext,
        prompt: str,
        *,
        response: PlanningResponse | None,
        candidate: Any,
        payload: Any,
        json_decode_success: bool | None,
        parser_success: bool,
        parser_rejection_code: str | None,
        failure_classification: str | None,
        detail: str | None,
        started_at: float,
    ) -> None:
        top_level_type, top_level_fields = _candidate_shape(payload)
        candidate_hash, candidate_length, candidate_prefix = _candidate_diagnostic(
            candidate
        )
        response_duration = (
            response.latency_seconds
            if response is not None
            else round(time.monotonic() - started_at, 3)
        )
        provenance = _provider_provenance(self.planning_provider, response)
        self._emit_capture(
            {
                "grounding_run_id": context.state.grounding_run_id,
                "provider_request_id": _provider_request_id(context),
                "provider_request_number": context.state.budget.provider_requests,
                "turn_type": _turn_type(context),
                "provider_provenance": provenance,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "prompt_length": len(prompt),
                "parser_input_type": (
                    type(payload).__name__ if payload is not None else None
                ),
                "parser_input_length": candidate_length,
                "provider_output_type": (
                    type(candidate).__name__ if candidate is not None else None
                ),
                "content_type": (
                    response.runtime_metadata.details.get("response_content_type")
                    if response is not None
                    and isinstance(response.runtime_metadata.details, Mapping)
                    else None
                ),
                "json_decode_success": json_decode_success,
                "top_level_json_type": top_level_type,
                "top_level_fields": top_level_fields,
                "parser_success": parser_success,
                "parser_rejection_code": parser_rejection_code,
                "failure_layer": _failure_layer(parser_rejection_code),
                "action_kind": (
                    payload.get("action") if isinstance(payload, Mapping) else None
                ),
                "decision_kind": (
                    payload.get("decision") if isinstance(payload, Mapping) else None
                ),
                "provider_duration_seconds": response_duration,
                "failure_classification": failure_classification,
                "detail": str(detail or "")[:MAX_CAPTURED_FAILURE_DETAIL] or None,
                "candidate_sha256": candidate_hash,
                "candidate_length": candidate_length,
                "candidate_prefix": candidate_prefix,
            }
        )

    def decide(self, context: GroundingDecisionContext) -> Any:
        """Invoke the configured Planning role and decode one strict JSON object."""

        from app.services.planning.providers.base import (
            PlanningArtifactKind,
            PlanningProviderExecutionError,
            PlanningRequest,
            PlanningRuntimeOptions,
        )

        after_observation = bool(context.state.observation_history)
        prompt = render_grounding_provider_prompt(context)
        request = PlanningRequest(
            artifact_kind=PlanningArtifactKind.GROUNDING,
            prompt=prompt,
            protocol_input={
                "grounding_run_id": context.state.grounding_run_id,
                "turn_type": _turn_type(context),
            },
            runtime_options=PlanningRuntimeOptions(
                timeout_seconds=self.timeout_seconds,
            ),
            metadata={
                "grounding_run_id": context.state.grounding_run_id,
                "provider_request_id": _provider_request_id(context),
                "turn_type": _turn_type(context),
            },
        )
        started_at = time.monotonic()
        try:
            response = self.planning_provider.generate(request)
        except PlanningProviderExecutionError as exc:
            code = (
                "provider_timeout"
                if exc.classification == "provider_timeout"
                else "provider_failure"
            )
            self._capture(
                context,
                prompt,
                response=None,
                candidate=None,
                payload=None,
                json_decode_success=None,
                parser_success=False,
                parser_rejection_code=code,
                failure_classification=code,
                detail=str(exc),
                started_at=started_at,
            )
            raise GroundingProviderError(f"{code}: {str(exc)[:240]}") from exc
        except Exception as exc:
            self._capture(
                context,
                prompt,
                response=None,
                candidate=None,
                payload=None,
                json_decode_success=None,
                parser_success=False,
                parser_rejection_code="provider_failure",
                failure_classification="provider_failure",
                detail=str(exc),
                started_at=started_at,
            )
            raise GroundingProviderError(f"provider_failure: {str(exc)[:240]}") from exc

        candidate = response.candidate_text
        payload: Any = candidate
        if candidate is None:
            self._capture(
                context,
                prompt,
                response=response,
                candidate=candidate,
                payload=None,
                json_decode_success=False,
                parser_success=False,
                parser_rejection_code="content_missing",
                failure_classification="content_missing",
                detail="PlanningResponse.candidate_text is missing",
                started_at=started_at,
            )
            raise GroundingProviderError("content_missing")
        if isinstance(candidate, str):
            try:
                payload = json.loads(candidate)
            except (TypeError, ValueError) as exc:
                self._capture(
                    context,
                    prompt,
                    response=response,
                    candidate=candidate,
                    payload=None,
                    json_decode_success=False,
                    parser_success=False,
                    parser_rejection_code="json_decode_failed",
                    failure_classification="json_decode_failed",
                    detail=str(exc),
                    started_at=started_at,
                )
                raise GroundingProviderError(
                    f"json_decode_failed: {str(exc)[:240]}"
                ) from exc
        elif not isinstance(candidate, Mapping):
            self._capture(
                context,
                prompt,
                response=response,
                candidate=candidate,
                payload=None,
                json_decode_success=False,
                parser_success=False,
                parser_rejection_code="json_decode_failed",
                failure_classification="json_decode_failed",
                detail="candidate_text must be a JSON string or structured mapping",
                started_at=started_at,
            )
            raise GroundingProviderError("json_decode_failed")

        if not isinstance(payload, Mapping):
            self._capture(
                context,
                prompt,
                response=response,
                candidate=candidate,
                payload=payload,
                json_decode_success=True,
                parser_success=False,
                parser_rejection_code="json_not_object",
                failure_classification="json_not_object",
                detail="grounding wire JSON must be an object",
                started_at=started_at,
            )
            raise GroundingProviderError("json_not_object")

        try:
            proposal = parse_grounding_provider_response(
                payload,
                after_observation=after_observation,
                terminal_only=context.turn_mode.terminal_only,
            )
        except (TypeError, ValueError) as exc:
            code = _wire_rejection_code(
                payload,
                after_observation=after_observation,
                terminal_only=context.turn_mode.terminal_only,
            )
            self._capture(
                context,
                prompt,
                response=response,
                candidate=candidate,
                payload=payload,
                json_decode_success=True,
                parser_success=False,
                parser_rejection_code=code,
                failure_classification=code,
                detail=str(exc),
                started_at=started_at,
            )
            raise GroundingProviderError(f"{code}: {str(exc)[:240]}") from exc

        self._capture(
            context,
            prompt,
            response=response,
            candidate=candidate,
            payload=payload,
            json_decode_success=True,
            parser_success=True,
            parser_rejection_code=None,
            failure_classification=None,
            detail=None,
            started_at=started_at,
        )
        return proposal


__all__ = [
    "FIRST_TURN_WIRE_EXAMPLES",
    "GROUNDING_PROVIDER_TIMEOUT_SECONDS",
    "MAX_CAPTURED_CANDIDATE_PREFIX",
    "PlanningGroundingProviderAdapter",
    "POST_OBSERVATION_WIRE_EXAMPLES",
    "TERMINAL_ASSESSMENT_WIRE_EXAMPLES",
    "render_first_turn_prompt",
    "render_grounding_provider_prompt",
    "render_post_observation_prompt",
    "render_rejection_correction_prompt",
    "render_terminal_assessment_prompt",
]
