"""Thin orchestration adapter for the planning Brief stage."""

from collections.abc import Mapping
import logging
import time
from typing import Any

from app.services.planning.stage_contract import (
    StageAcceptance,
    StageContext,
    StageDefinition,
    StageExecutionPolicy,
    StageValidation,
)
from app.services.planning.planning_brief_stage_support import (
    _PROVIDER_RUNTIME_FAILURES,
    _log_brief_timing,
    _validation_reason,
    PlanningBrief,
    PlanningBriefApplicationError,
    PlanningBriefProviderInput,
    PlanningBriefProviderOutputError,
    PlanningBriefProviderRuntimeError,
    PlanningBriefStageError,
    PlanningBriefTransportError,
    build_planning_brief_provider_input,
    build_planning_brief_request,
    canonicalize_planning_brief_candidate,
    parse_planning_brief_candidate,
    validate_planning_brief,
)
from app.services.planning.providers import (
    PlanningProvider,
    PlanningProviderExecutionError,
    PlanningResponse,
    ProviderFailureOrigin,
)

logger = logging.getLogger("app.services.planning.planning_brief_stage")


class PlanningBriefStage(StageDefinition):
    """Registered Protocol v2 stage from Input Manifest to accepted Brief."""

    def __init__(self, provider: PlanningProvider):
        self.provider = provider
        super().__init__(
            "planning_brief",
            version=1,
            prerequisites=(),
            execution_policy=StageExecutionPolicy(retryable=True, max_attempts=1),
        )

    def execute(self, context: StageContext) -> PlanningBrief:
        stage_started_at = time.monotonic()
        try:
            request_started_at = time.monotonic()
            provider_input = build_planning_brief_provider_input(context)
            request = build_planning_brief_request(provider_input)
            request_construction_seconds = round(
                time.monotonic() - request_started_at, 3
            )
        except PlanningBriefStageError:
            raise
        except Exception as exc:
            raise PlanningBriefApplicationError(
                "provider input construction failed"
            ) from exc
        try:
            response = self.provider.generate(request)
        except PlanningProviderExecutionError as exc:
            if exc.classification in _PROVIDER_RUNTIME_FAILURES:
                raise PlanningBriefProviderRuntimeError(
                    exc.classification, exc.detail
                ) from exc
            message = (
                "provider invocation failed"
                if exc.origin is ProviderFailureOrigin.INVOCATION
                else "provider returned a failed result"
            )
            raise PlanningBriefTransportError(message) from exc
        except PlanningBriefStageError:
            raise
        except Exception as exc:
            raise PlanningBriefTransportError("provider invocation failed") from exc
        if not isinstance(response, PlanningResponse):
            raise PlanningBriefTransportError("provider returned a failed result")
        raw = response.candidate_text
        if not isinstance(raw, (str, Mapping)):
            raise PlanningBriefProviderOutputError(
                "provider returned no candidate output"
            )
        parser_seconds = 0.0
        canonicalization_seconds = 0.0
        try:
            parser_started_at = time.monotonic()
            candidate = parse_planning_brief_candidate(raw)
            parser_seconds = round(time.monotonic() - parser_started_at, 3)
            canonicalization_started_at = time.monotonic()
            output = canonicalize_planning_brief_candidate(
                candidate, context.input_manifest
            )
            canonicalization_seconds = round(
                time.monotonic() - canonicalization_started_at, 3
            )
            _log_brief_timing(
                request=request,
                provider_input=provider_input,
                response=response,
                request_construction_seconds=request_construction_seconds,
                parser_seconds=parser_seconds,
                canonicalization_seconds=canonicalization_seconds,
                total_seconds=round(time.monotonic() - stage_started_at, 3),
                failure_classification=None,
            )
            return output
        except PlanningBriefStageError as exc:
            _log_brief_timing(
                request=request,
                provider_input=provider_input,
                response=response,
                request_construction_seconds=request_construction_seconds,
                parser_seconds=parser_seconds
                or round(time.monotonic() - parser_started_at, 3),
                canonicalization_seconds=canonicalization_seconds,
                total_seconds=round(time.monotonic() - stage_started_at, 3),
                failure_classification=exc.classification,
            )
            raise
        except Exception as exc:
            _log_brief_timing(
                request=request,
                provider_input=provider_input,
                response=response,
                request_construction_seconds=request_construction_seconds,
                parser_seconds=parser_seconds
                or round(time.monotonic() - parser_started_at, 3),
                canonicalization_seconds=canonicalization_seconds,
                total_seconds=round(time.monotonic() - stage_started_at, 3),
                failure_classification="provider_output_failure",
            )
            raise PlanningBriefProviderOutputError(
                "candidate canonicalization failed"
            ) from exc

    def validate(self, output: Any, context: StageContext) -> StageValidation:
        validation_started_at = time.monotonic()
        if not isinstance(output, PlanningBrief):
            result = StageValidation(
                False, "provider_output_failure: output is not a Brief"
            )
            logger.info(
                "[PHASE28RV_TIMING] stage=planning_brief validation_seconds=%.3f "
                "validation_result=invalid_output",
                time.monotonic() - validation_started_at,
            )
            return result
        acceptance = validate_planning_brief(
            output, input_manifest=context.input_manifest
        )
        if not acceptance.semantically_valid:
            result = StageValidation(False, _validation_reason(acceptance))
        else:
            result = StageValidation(True)
        logger.info(
            "[PHASE28RV_TIMING] stage=planning_brief validation_seconds=%.3f "
            "validation_result=%s",
            time.monotonic() - validation_started_at,
            "accepted" if result.valid else "rejected",
        )
        return result

    def accept(self, output: Any, context: StageContext) -> StageAcceptance:
        if not isinstance(output, PlanningBrief):
            return StageAcceptance(
                False, "provider_output_failure: output is not a Brief"
            )
        acceptance = validate_planning_brief(
            output, input_manifest=context.input_manifest
        )
        if not acceptance.protocol_acceptable:
            return StageAcceptance(False, _validation_reason(acceptance))
        return StageAcceptance(True)


__all__ = ["PlanningBriefStage"]
