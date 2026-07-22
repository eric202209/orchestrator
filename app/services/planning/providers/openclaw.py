"""OpenClaw adapter for the provider-independent Planning Provider seam."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

from app.services.agents.agent_backends import get_backend_descriptor
from app.services.agents.agent_runtime import (
    BackendRole,
    invoke_runtime_prompt,
    resolve_planning_runtime_configuration,
)
from app.services.agents.openclaw_service import PINNED_OPENCLAW_VERSION
from app.services.planning.providers.base import (
    ExecutionMetadata,
    PlanningArtifactKind,
    PlanningProviderExecutionError,
    PlanningRequest,
    PlanningResponse,
    PROVIDER_RUNTIME_FAILURES,
    ProviderCapabilities,
    ProviderDiagnostics,
    ProviderFailureOrigin,
    ProviderHealth,
    ProviderRuntimeInformation,
    ProviderTokenUsage,
)


_SESSION_PREFIXES = {
    PlanningArtifactKind.PLANNING_BRIEF: "planning-brief",
    PlanningArtifactKind.STRUCTURED_TASK_PLAN: "structured-task-plan",
}


class OpenClawPlanningProvider:
    """Sole Phase 28Q adapter; preserves the existing OpenClaw invocation."""

    _CAPABILITIES = ProviderCapabilities(
        supports_reasoning_control=True,
        supports_response_format=False,
        supports_tool_calling=False,
        supports_deterministic_sampling=True,
        supports_prompt_ownership=False,
        supports_request_ownership=False,
        supports_streaming=False,
        supports_cancellation=True,
        supports_timeout_control=True,
        supports_structured_output=False,
        supports_seed=False,
        supports_top_p=False,
        supports_health_endpoint=False,
    )

    def __init__(self, db: Any):
        self.db = db

    @property
    def name(self) -> str:
        return "openclaw"

    @property
    def version(self) -> str:
        return PINNED_OPENCLAW_VERSION

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._CAPABILITIES

    def runtime_information(self) -> ProviderRuntimeInformation:
        configuration = resolve_planning_runtime_configuration(self.db)
        return ProviderRuntimeInformation(
            provider_name=self.name,
            provider_version=self.version,
            runtime_name=configuration.backend_name,
            model=configuration.model_family,
            adaptation_profile=configuration.adaptation_profile,
        )

    def health(self) -> ProviderHealth:
        try:
            runtime = self.runtime_information()
            descriptor = get_backend_descriptor(runtime.runtime_name)
        except Exception as exc:
            return ProviderHealth(
                available=False,
                ready=False,
                status="configuration_failure",
                errors=(str(exc)[:500],),
            )
        return ProviderHealth(
            available=descriptor.health.available,
            ready=descriptor.health.ready,
            status=descriptor.health.status,
            errors=tuple(descriptor.health.errors),
            warnings=tuple(descriptor.health.warnings),
        )

    def generate(self, request: PlanningRequest) -> PlanningResponse:
        started_at = time.monotonic()
        session_prefix = _SESSION_PREFIXES[request.artifact_kind]
        try:
            result = invoke_runtime_prompt(
                self.db,
                request.prompt,
                project_id=request.project_id,
                source_brain="local",
                timeout_seconds=request.runtime_options.timeout_seconds,
                session_prefix=session_prefix,
                role=BackendRole.PLANNING,
            )
        except Exception as exc:
            classification = getattr(
                exc, "provider_failure_classification", None
            ) or getattr(exc, "classification", None)
            raise PlanningProviderExecutionError(
                classification=(
                    classification
                    if classification in PROVIDER_RUNTIME_FAILURES
                    else "transport_failure"
                ),
                detail=str(exc),
                origin=ProviderFailureOrigin.INVOCATION,
            ) from exc

        if not isinstance(result, Mapping) or result.get("status") == "failed":
            classification = (
                result.get("failure_classification")
                if isinstance(result, Mapping)
                else None
            )
            detail = (
                str(result.get("error") or classification)
                if isinstance(result, Mapping)
                else "provider returned a failed result"
            )
            raise PlanningProviderExecutionError(
                classification=(
                    classification
                    if classification in PROVIDER_RUNTIME_FAILURES
                    else "transport_failure"
                ),
                detail=detail,
                origin=ProviderFailureOrigin.FAILED_RESULT,
            )

        diagnostics = _normalized_diagnostics(result.get("runtime_diagnostics"))
        duration = diagnostics.get("duration_seconds")
        latency_seconds = (
            float(duration)
            if isinstance(duration, (int, float))
            else round(time.monotonic() - started_at, 3)
        )
        token_usage = _token_usage(result.get("token_usage"))
        runtime_metadata = ExecutionMetadata(
            runtime_name=_optional_text(diagnostics.get("backend")),
            model=_optional_text(diagnostics.get("model_family")),
            adaptation_profile=_optional_text(diagnostics.get("adaptation_profile")),
            details={
                "role": diagnostics.get("role"),
                "operation": request.artifact_kind.value,
            },
        )
        return PlanningResponse(
            candidate_text=result.get("output"),
            provider_name=self.name,
            provider_version=_optional_text(result.get("openclaw_version"))
            or self.version,
            diagnostics=ProviderDiagnostics(
                category=str(
                    diagnostics.get("diagnostic_category") or "provider_success"
                ),
                details=diagnostics,
            ),
            latency_seconds=latency_seconds,
            completion_metadata={"status": str(result.get("status") or "completed")},
            token_usage=token_usage,
            runtime_metadata=runtime_metadata,
        )


def _optional_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _normalized_diagnostics(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    supported_fields = (
        "diagnostic_category",
        "timed_out",
        "cancelled",
        "duration_seconds",
        "first_output_after_seconds",
        "last_output_after_seconds",
        "max_silent_gap_seconds",
        "stdout_chars",
        "stderr_chars",
        "output_token_estimate",
        "truncated",
        "return_code",
        "role",
        "backend",
        "model_family",
        "adaptation_profile",
        "timeout_seconds",
        "provider_deadline_seconds",
    )
    return {key: value[key] for key in supported_fields if key in value}


def _token_usage(value: Any) -> ProviderTokenUsage | None:
    if not isinstance(value, Mapping):
        return None

    def optional_int(*keys: str) -> int | None:
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                return candidate
        return None

    return ProviderTokenUsage(
        input_tokens=optional_int("input_tokens", "prompt_tokens"),
        output_tokens=optional_int("output_tokens", "completion_tokens"),
        total_tokens=optional_int("total_tokens"),
    )


__all__ = ["OpenClawPlanningProvider"]
