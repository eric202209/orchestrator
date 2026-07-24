"""Thin orchestration adapter for the structured Task Plan stage."""

from collections.abc import Mapping
from typing import Any

from app.services.planning.stage_contract import (
    StageAcceptance,
    StageContext,
    StageDefinition,
    StageExecutionPolicy,
    StageValidation,
)
from app.services.planning.structured_task_plan_stage_support import (
    DEFAULT_TASK_PLAN_CANDIDATE_BYTES,
    _configuration_value,
    _policy,
    _validation_reason,
    StructuredTaskPlan,
    StructuredTaskPlanAcceptanceError,
    StructuredTaskPlanApplicationError,
    StructuredTaskPlanGraphValidationError,
    StructuredTaskPlanIntegrityError,
    StructuredTaskPlanProviderInput,
    StructuredTaskPlanProviderOutputError,
    StructuredTaskPlanProviderRuntimeError,
    StructuredTaskPlanReferenceResolutionError,
    StructuredTaskPlanStageError,
    StructuredTaskPlanTransportError,
    build_structured_task_plan_provider_input,
    build_structured_task_plan_request,
    canonicalize_structured_task_plan_candidate,
    parse_structured_task_plan_candidate,
    validate_structured_task_plan,
)
from app.services.planning.providers import (
    PlanningProvider,
    PlanningProviderExecutionError,
    PlanningResponse,
    ProviderFailureOrigin,
)


class StructuredTaskPlanStage(StageDefinition):
    """Generate and accept one canonical Task Plan from an accepted Brief."""

    def __init__(self, provider: PlanningProvider):
        self.provider = provider
        super().__init__(
            "structured_task_plan",
            version=1,
            prerequisites=("planning_brief",),
            execution_policy=StageExecutionPolicy(retryable=True, max_attempts=1),
        )

    def execute(self, context: StageContext) -> StructuredTaskPlan:
        try:
            provider_input = build_structured_task_plan_provider_input(context)
            request = build_structured_task_plan_request(provider_input)
        except StructuredTaskPlanStageError:
            raise
        except Exception as exc:
            raise StructuredTaskPlanApplicationError(
                "provider input construction failed"
            ) from exc
        try:
            response = self.provider.generate(request)
        except PlanningProviderExecutionError as exc:
            if (
                exc.classification
                in StructuredTaskPlanProviderRuntimeError._ALLOWED_CLASSIFICATIONS
            ):
                raise StructuredTaskPlanProviderRuntimeError(
                    exc.classification, exc.detail
                ) from exc
            message = (
                "provider invocation failed"
                if exc.origin is ProviderFailureOrigin.INVOCATION
                else "provider returned a failed result"
            )
            raise StructuredTaskPlanTransportError(message) from exc
        except StructuredTaskPlanStageError:
            raise
        except Exception as exc:
            raise StructuredTaskPlanTransportError(
                "provider invocation failed"
            ) from exc
        if not isinstance(response, PlanningResponse):
            raise StructuredTaskPlanTransportError("provider returned a failed result")
        raw = response.candidate_text
        if not isinstance(raw, (str, bytes, Mapping)):
            raise StructuredTaskPlanProviderOutputError(
                "provider returned no candidate output"
            )
        try:
            candidate = parse_structured_task_plan_candidate(
                raw,
                max_bytes=_configuration_value(
                    context.configuration,
                    "max_candidate_bytes",
                    DEFAULT_TASK_PLAN_CANDIDATE_BYTES,
                ),
            )
            return canonicalize_structured_task_plan_candidate(candidate, context)
        except StructuredTaskPlanStageError:
            raise
        except Exception as exc:
            raise StructuredTaskPlanProviderOutputError(
                f"candidate canonicalization failed: {str(exc)[:300]}"
            ) from exc

    def validate(self, output: Any, context: StageContext) -> StageValidation:
        if not isinstance(output, StructuredTaskPlan):
            return StageValidation(
                False, "provider_output_failure: output is not a Task Plan"
            )
        validation = validate_structured_task_plan(
            output,
            brief=context.planning_brief,
            input_manifest=context.input_manifest,
            policy=_policy(context.configuration),
        )
        if not validation.schema_valid or not validation.semantically_valid:
            return StageValidation(False, _validation_reason(validation))
        return StageValidation(True)

    def accept(self, output: Any, context: StageContext) -> StageAcceptance:
        if not isinstance(output, StructuredTaskPlan):
            return StageAcceptance(
                False, "provider_output_failure: output is not a Task Plan"
            )
        validation = validate_structured_task_plan(
            output,
            brief=context.planning_brief,
            input_manifest=context.input_manifest,
            policy=_policy(context.configuration),
        )
        if not validation.protocol_acceptable:
            return StageAcceptance(False, _validation_reason(validation))
        return StageAcceptance(True)


__all__ = ["StructuredTaskPlanStage"]
