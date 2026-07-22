"""Direct, application-owned OpenAI-compatible Protocol v2 provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.services.planning.providers.base import (
    ExecutionMetadata,
    PlanningProviderExecutionError,
    PlanningRequest,
    PlanningResponse,
    ProviderCapabilities,
    ProviderDiagnostics,
    ProviderFailureOrigin,
    ProviderHealth,
    ProviderRuntimeInformation,
    ProviderTokenUsage,
)


DIRECT_PROVIDER_VERSION = "28r-1"
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
HEALTH_TIMEOUT_SECONDS = 5.0


class DirectProviderConfigurationError(ValueError):
    """The direct provider cannot be safely constructed from configuration."""


class _DirectProviderFailure(RuntimeError):
    def __init__(
        self,
        classification: str,
        detail: str,
        *,
        stage: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.classification = classification
        self.stage = stage
        self.details = dict(details or {})
        super().__init__(str(detail or classification)[:500])


@dataclass(frozen=True)
class _DirectConfiguration:
    endpoint: str
    health_endpoint: str
    origin: str
    model: str
    api_key: str
    timeout_seconds: int
    max_completion_tokens: int
    temperature: float


@dataclass(frozen=True)
class _RawProviderResponse:
    status_code: int
    content_type: str
    body: bytes


class DirectOpenAICompatiblePlanningProvider:
    """Generate semantic candidates through an application-owned HTTP body."""

    def __init__(self, db: Any):
        del db
        self._configuration = _load_configuration()
        self._cancel_requested = threading.Event()
        self._active_client: httpx.Client | None = None
        self._active_client_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "direct_openai_compatible"

    @property
    def version(self) -> str:
        return DIRECT_PROVIDER_VERSION

    @property
    def capabilities(self) -> ProviderCapabilities:
        configuration = self._configuration
        return ProviderCapabilities(
            supports_reasoning_control=True,
            supports_response_format=False,
            supports_tool_calling=False,
            supports_deterministic_sampling=configuration.temperature == 0.0,
            supports_prompt_ownership=True,
            supports_request_ownership=True,
            supports_streaming=True,
            supports_cancellation=True,
            supports_timeout_control=True,
            supports_structured_output=False,
            supports_seed=False,
            supports_top_p=False,
            # /models is probed opportunistically, but gateway support varies.
            supports_health_endpoint=False,
        )

    def runtime_information(self) -> ProviderRuntimeInformation:
        configuration = self._configuration
        return ProviderRuntimeInformation(
            provider_name=self.name,
            provider_version=self.version,
            runtime_name="openai_compatible_http",
            model=configuration.model,
            adaptation_profile=None,
            details={
                "endpoint_origin": configuration.origin,
                "reasoning_mode": "chat_template_kwargs.enable_thinking=false",
                "sampling_mode": (
                    "temperature=0"
                    if configuration.temperature == 0.0
                    else "temperature=configured"
                ),
                "timeout_seconds": configuration.timeout_seconds,
                "max_completion_tokens": configuration.max_completion_tokens,
                "response_mode": "streaming_sse",
                "authentication": (
                    "bearer_configured" if configuration.api_key else "none"
                ),
            },
        )

    def health(self) -> ProviderHealth:
        """Run only the bounded, non-generation /models probe."""

        configuration = self._configuration
        started_at = time.monotonic()
        try:
            with httpx.Client(
                timeout=min(HEALTH_TIMEOUT_SECONDS, configuration.timeout_seconds)
            ) as client:
                response = client.get(
                    configuration.health_endpoint,
                    headers=_headers(configuration.api_key),
                )
            status_code = int(response.status_code)
        except httpx.TimeoutException:
            return ProviderHealth(
                available=False,
                ready=False,
                status="unreachable",
                errors=("health probe timed out",),
            )
        except httpx.HTTPError as exc:
            return ProviderHealth(
                available=False,
                ready=False,
                status="unreachable",
                errors=(f"{type(exc).__name__}: transport error",),
            )
        except Exception as exc:
            return ProviderHealth(
                available=False,
                ready=False,
                status="unreachable",
                errors=(f"{type(exc).__name__}: unexpected health failure",),
            )

        latency = round(time.monotonic() - started_at, 3)
        if 200 <= status_code < 300:
            return ProviderHealth(
                available=True,
                ready=True,
                status="reachable",
                warnings=(f"health probe latency {latency}s",),
            )
        if status_code in {404, 405, 501}:
            return ProviderHealth(
                available=True,
                ready=False,
                status="unsupported",
                errors=(f"health endpoint HTTP {status_code}",),
            )
        if status_code in {401, 403}:
            return ProviderHealth(
                available=True,
                ready=False,
                status="misconfigured",
                errors=(f"health endpoint HTTP {status_code}",),
            )
        return ProviderHealth(
            available=False,
            ready=False,
            status="unreachable",
            errors=(f"health endpoint HTTP {status_code}",),
        )

    def cancel(self) -> None:
        """Cancel the active request and close its client at the boundary."""

        self._cancel_requested.set()
        with self._active_client_lock:
            client = self._active_client
        if client is not None:
            client.close()

    def build_request_body(self, request: PlanningRequest) -> dict[str, Any]:
        """Build the complete allowlisted gateway body for bounded evidence/tests."""

        configuration = self._configuration
        if request.reasoning.enabled is True:
            raise DirectProviderConfigurationError(
                "direct provider only supports reasoning disabled"
            )
        if request.sampling.top_p is not None:
            raise DirectProviderConfigurationError(
                "direct provider does not support top_p"
            )
        if request.sampling.seed is not None:
            raise DirectProviderConfigurationError(
                "direct provider does not support seed"
            )
        temperature = (
            configuration.temperature
            if request.sampling.temperature is None
            else request.sampling.temperature
        )
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
            raise DirectProviderConfigurationError("temperature must be numeric")
        if not math.isfinite(float(temperature)) or float(temperature) < 0:
            raise DirectProviderConfigurationError(
                "temperature must be finite and non-negative"
            )
        max_tokens = request.runtime_options.max_output_tokens
        if max_tokens is None:
            max_tokens = configuration.max_completion_tokens
        if (
            not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or max_tokens < 1
        ):
            raise DirectProviderConfigurationError(
                "max completion tokens must be positive"
            )
        return {
            "model": configuration.model,
            "messages": list(build_application_owned_messages(request)),
            "temperature": float(temperature),
            "max_tokens": max_tokens,
            "stream": True,
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def generate(self, request: PlanningRequest) -> PlanningResponse:
        started_at = time.monotonic()
        configuration = self._configuration
        effective_timeout = min(
            configuration.timeout_seconds,
            request.runtime_options.timeout_seconds,
        )
        deadline = started_at + float(effective_timeout)
        base_details = {
            "provider": self.name,
            "model": configuration.model,
            "timeout_seconds": effective_timeout,
            "failure_stage": "request_construction",
        }
        try:
            self._ensure_not_cancelled(request, deadline)
            body = self.build_request_body(request)
            encoded_body = json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            if len(encoded_body) > MAX_REQUEST_BYTES:
                raise _DirectProviderFailure(
                    "provider_configuration_failure",
                    "application-owned request exceeds bounded request size",
                    stage="request_construction",
                    details={"request_length_bytes": len(encoded_body)},
                )
            self._ensure_not_cancelled(request, deadline)
            raw_response = self._post(
                encoded_body,
                request,
                deadline,
            )
            self._ensure_not_cancelled(request, deadline)
            candidate, response_metadata, token_usage = _parse_response(raw_response)
            self._ensure_not_cancelled(request, deadline)
            candidate_length_bytes = len(candidate.encode("utf-8"))
            latency = round(time.monotonic() - started_at, 3)
            diagnostics = {
                **base_details,
                "failure_stage": "completed",
                "http_status": raw_response.status_code,
                "response_content_type": raw_response.content_type,
                "response_length_bytes": len(raw_response.body),
                "candidate_length_bytes": candidate_length_bytes,
                "latency_seconds": latency,
                **response_metadata,
            }
            return PlanningResponse(
                candidate_text=candidate,
                provider_name=self.name,
                provider_version=self.version,
                diagnostics=ProviderDiagnostics(
                    category="provider_success", details=diagnostics
                ),
                latency_seconds=latency,
                completion_metadata={
                    "status": "completed",
                    "http_status": raw_response.status_code,
                    "response_mode": response_metadata["response_mode"],
                    "candidate_length_bytes": candidate_length_bytes,
                },
                token_usage=token_usage,
                runtime_metadata=ExecutionMetadata(
                    runtime_name="openai_compatible_http",
                    model=configuration.model,
                    details={
                        "http_status": raw_response.status_code,
                        "response_content_type": raw_response.content_type,
                        "response_length_bytes": len(raw_response.body),
                        "candidate_length_bytes": candidate_length_bytes,
                        "response_mode": response_metadata["response_mode"],
                    },
                ),
            )
        except DirectProviderConfigurationError as exc:
            raise _as_execution_error(
                "provider_configuration_failure",
                str(exc),
                origin=ProviderFailureOrigin.INVOCATION,
                details={**base_details, "exception_type": type(exc).__name__},
            ) from exc
        except _DirectProviderFailure as exc:
            classification = exc.classification
            if time.monotonic() >= deadline and classification != "provider_cancelled":
                classification = "provider_timeout"
            details = {
                **base_details,
                **exc.details,
                "failure_stage": exc.stage,
                "latency_seconds": round(time.monotonic() - started_at, 3),
                "exception_type": type(exc).__name__,
                "retryable": exc.details.get(
                    "retryable",
                    classification in {"provider_timeout", "transport_failure"},
                ),
            }
            raise _as_execution_error(
                classification,
                str(exc),
                origin=ProviderFailureOrigin.FAILED_RESULT,
                details=details,
            ) from exc
        except httpx.TimeoutException as exc:
            raise _as_execution_error(
                "provider_timeout",
                "direct provider request exceeded its total deadline",
                origin=ProviderFailureOrigin.INVOCATION,
                details={
                    **base_details,
                    "failure_stage": "transport",
                    "latency_seconds": round(time.monotonic() - started_at, 3),
                    "exception_type": type(exc).__name__,
                    "retryable": True,
                },
            ) from exc
        except httpx.HTTPError as exc:
            classification = (
                "provider_cancelled"
                if self._cancel_requested.is_set()
                else "transport_failure"
            )
            raise _as_execution_error(
                classification,
                "direct provider transport failed",
                origin=ProviderFailureOrigin.INVOCATION,
                details={
                    **base_details,
                    "failure_stage": "transport",
                    "latency_seconds": round(time.monotonic() - started_at, 3),
                    "exception_type": type(exc).__name__,
                    "retryable": classification == "transport_failure",
                },
            ) from exc
        except Exception as exc:
            raise _as_execution_error(
                "unexpected_provider_failure",
                "unexpected direct provider failure",
                origin=ProviderFailureOrigin.INVOCATION,
                details={
                    **base_details,
                    "failure_stage": "unexpected",
                    "latency_seconds": round(time.monotonic() - started_at, 3),
                    "exception_type": type(exc).__name__,
                    "retryable": False,
                },
            ) from exc
        finally:
            self._cancel_requested.clear()

    def _post(
        self,
        encoded_body: bytes,
        request: PlanningRequest,
        deadline: float,
    ) -> _RawProviderResponse:
        self._ensure_not_cancelled(request, deadline)
        remaining = _remaining(deadline)
        timeout = httpx.Timeout(
            remaining,
            connect=remaining,
            read=remaining,
            write=remaining,
            pool=remaining,
        )
        client = httpx.Client(timeout=timeout)
        with self._active_client_lock:
            self._active_client = client
        try:
            with client.stream(
                "POST",
                self._configuration.endpoint,
                headers=_headers(self._configuration.api_key),
                content=encoded_body,
            ) as response:
                status_code = int(response.status_code)
                content_type = str(response.headers.get("content-type", "")).strip()
                body = _read_bounded_response(response, deadline, request)
                if not 200 <= status_code < 300:
                    raise _DirectProviderFailure(
                        "provider_http_failure",
                        f"direct provider returned HTTP {status_code}",
                        stage="http_status",
                        details={
                            "http_status": status_code,
                            "response_content_type": content_type,
                            "response_length_bytes": len(body),
                            "retryable": status_code >= 500,
                        },
                    )
                return _RawProviderResponse(status_code, content_type, body)
        finally:
            with self._active_client_lock:
                if self._active_client is client:
                    self._active_client = None
            client.close()

    def _ensure_not_cancelled(self, request: PlanningRequest, deadline: float) -> None:
        event = request.metadata.get("cancel_event")
        event_cancelled = callable(getattr(event, "is_set", None)) and event.is_set()
        if self._cancel_requested.is_set() or event_cancelled:
            raise _DirectProviderFailure(
                "provider_cancelled",
                "direct provider request was cancelled",
                stage="cancellation",
                details={"retryable": False},
            )
        if _remaining(deadline) <= 0:
            raise _DirectProviderFailure(
                "provider_timeout",
                "direct provider total deadline expired",
                stage="deadline",
                details={"retryable": True},
            )


def build_application_owned_messages(
    request: PlanningRequest,
) -> tuple[dict[str, str], ...]:
    """Return the exact application-owned model-facing message boundary."""

    return ({"role": "user", "content": request.prompt},)


def _load_configuration() -> _DirectConfiguration:
    base_url = str(getattr(settings, "PLANNING_DIRECT_BASE_URL", "") or "").strip()
    model = str(getattr(settings, "PLANNING_DIRECT_MODEL", "") or "").strip()
    if not base_url:
        raise DirectProviderConfigurationError("PLANNING_DIRECT_BASE_URL is required")
    if not model:
        raise DirectProviderConfigurationError("PLANNING_DIRECT_MODEL is required")
    parsed = urlsplit(base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DirectProviderConfigurationError(
            "PLANNING_DIRECT_BASE_URL must be an http(s) URL"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DirectProviderConfigurationError(
            "PLANNING_DIRECT_BASE_URL must not contain credentials or query data"
        )
    api_key = str(getattr(settings, "PLANNING_DIRECT_API_KEY", "") or "").strip()
    if any(character in api_key for character in "\r\n"):
        raise DirectProviderConfigurationError("PLANNING_DIRECT_API_KEY is invalid")
    timeout = _positive_setting("PLANNING_DIRECT_TIMEOUT_SECONDS", 360, minimum=1)
    max_tokens = _positive_setting(
        "PLANNING_DIRECT_MAX_COMPLETION_TOKENS", 16_384, minimum=1
    )
    raw_temperature = getattr(settings, "PLANNING_DIRECT_TEMPERATURE", 0.0)
    try:
        temperature = float(raw_temperature)
    except (TypeError, ValueError) as exc:
        raise DirectProviderConfigurationError(
            "PLANNING_DIRECT_TEMPERATURE must be numeric"
        ) from exc
    if not math.isfinite(temperature) or temperature < 0:
        raise DirectProviderConfigurationError(
            "PLANNING_DIRECT_TEMPERATURE must be finite and non-negative"
        )
    normalized_base = base_url.rstrip("/")
    return _DirectConfiguration(
        endpoint=f"{normalized_base}/chat/completions",
        health_endpoint=f"{normalized_base}/models",
        origin=f"{parsed.scheme}://{parsed.netloc}",
        model=model,
        api_key=api_key,
        timeout_seconds=timeout,
        max_completion_tokens=max_tokens,
        temperature=temperature,
    )


def _positive_setting(name: str, default: int, *, minimum: int) -> int:
    value = getattr(settings, name, default)
    if isinstance(value, bool):
        raise DirectProviderConfigurationError(f"{name} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise DirectProviderConfigurationError(
            f"{name} must be a positive integer"
        ) from exc
    if str(value).strip() != str(normalized) or normalized < minimum:
        raise DirectProviderConfigurationError(f"{name} must be a positive integer")
    return normalized


def _headers(api_key: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _DirectProviderFailure(
            "provider_timeout",
            "direct provider total deadline expired",
            stage="deadline",
            details={"retryable": True},
        )
    return remaining


def _read_bounded_response(
    response: Any, deadline: float, request: PlanningRequest
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_bytes():
            if not isinstance(chunk, bytes):
                raise _DirectProviderFailure(
                    "provider_output_failure",
                    "provider response stream yielded non-bytes",
                    stage="response_stream",
                )
            if not chunk:
                continue
            _remaining(deadline)
            event = request.metadata.get("cancel_event")
            if callable(getattr(event, "is_set", None)) and event.is_set():
                raise _DirectProviderFailure(
                    "provider_cancelled",
                    "direct provider response stream was cancelled",
                    stage="response_stream",
                    details={"retryable": False},
                )
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise _DirectProviderFailure(
                    "provider_output_failure",
                    "provider response exceeds bounded response size",
                    stage="response_stream",
                    details={"response_length_bytes": total},
                )
            chunks.append(chunk)
    except _DirectProviderFailure:
        raise
    except UnicodeError as exc:
        raise _DirectProviderFailure(
            "provider_output_failure",
            "provider response is not valid UTF-8",
            stage="response_stream",
            details={"response_length_bytes": total},
        ) from exc
    return b"".join(chunks)


def _parse_response(
    response: _RawProviderResponse,
) -> tuple[str, dict[str, Any], ProviderTokenUsage | None]:
    content_type = response.content_type.lower()
    if "text/event-stream" in content_type:
        return _parse_sse(response.body)
    if "json" not in content_type:
        raise _DirectProviderFailure(
            "provider_output_failure",
            "provider response content type is unsupported",
            stage="response_validation",
            details={"response_content_type": response.content_type},
        )
    try:
        body = json.loads(response.body.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise _DirectProviderFailure(
            "provider_output_failure",
            "provider response envelope is not valid JSON",
            stage="response_validation",
        ) from exc
    return _parse_json_envelope(body)


def _parse_json_envelope(
    body: Any,
) -> tuple[str, dict[str, Any], ProviderTokenUsage | None]:
    if not isinstance(body, Mapping):
        raise _DirectProviderFailure(
            "provider_output_failure",
            "provider response envelope is not an object",
            stage="response_validation",
        )
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise _DirectProviderFailure(
            "provider_output_failure",
            "provider response must contain exactly one choice",
            stage="response_validation",
            details={
                "choice_count": len(choices) if isinstance(choices, list) else None
            },
        )
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, Mapping) else None
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        raise _DirectProviderFailure(
            "provider_output_failure",
            "provider response does not contain one final assistant message",
            stage="response_validation",
        )
    content = message.get("content")
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if not isinstance(content, str) or not content.strip():
        detail = (
            "provider response contains reasoning without assistant content"
            if reasoning
            else "provider response assistant content is empty"
        )
        raise _DirectProviderFailure(
            "provider_output_failure", detail, stage="response_validation"
        )
    if isinstance(choice, Mapping) and choice.get("finish_reason") == "length":
        raise _DirectProviderFailure(
            "provider_output_failure",
            "provider response ended at the output limit",
            stage="response_validation",
        )
    return (
        content,
        {
            "response_mode": "json",
            "finish_reason": (
                choice.get("finish_reason") if isinstance(choice, Mapping) else None
            ),
            "reasoning_content_observed": bool(reasoning),
        },
        _token_usage(body.get("usage")),
    )


def _parse_sse(
    body: bytes,
) -> tuple[str, dict[str, Any], ProviderTokenUsage | None]:
    try:
        text = body.decode("utf-8")
    except UnicodeError as exc:
        raise _DirectProviderFailure(
            "provider_output_failure",
            "provider response is not valid UTF-8",
            stage="response_validation",
        ) from exc
    content_parts: list[str] = []
    reasoning_observed = False
    finished = False
    saw_done = False
    event_data: list[str] = []
    usage: ProviderTokenUsage | None = None
    finish_reason: str | None = None

    def consume_event(data_lines: list[str]) -> None:
        nonlocal finished, saw_done, reasoning_observed, usage, finish_reason
        if not data_lines:
            return
        data = "\n".join(data_lines)
        if data == "[DONE]":
            saw_done = True
            return
        try:
            event = json.loads(data)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _DirectProviderFailure(
                "provider_output_failure",
                "provider stream event is not valid JSON",
                stage="response_validation",
            ) from exc
        if not isinstance(event, Mapping):
            raise _DirectProviderFailure(
                "provider_output_failure",
                "provider stream event is not an object",
                stage="response_validation",
            )
        choices = event.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise _DirectProviderFailure(
                "provider_output_failure",
                "provider stream event must contain exactly one choice",
                stage="response_validation",
            )
        choice = choices[0]
        delta = choice.get("delta") if isinstance(choice, Mapping) else None
        if not isinstance(delta, Mapping):
            raise _DirectProviderFailure(
                "provider_output_failure",
                "provider stream event lacks a delta",
                stage="response_validation",
            )
        part = delta.get("content")
        if part is not None:
            if not isinstance(part, str):
                raise _DirectProviderFailure(
                    "provider_output_failure",
                    "provider stream content is not text",
                    stage="response_validation",
                )
            content_parts.append(part)
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        reasoning_observed = reasoning_observed or bool(reasoning)
        current_finish_reason = (
            choice.get("finish_reason") if isinstance(choice, Mapping) else None
        )
        if current_finish_reason is not None:
            finish_reason = str(current_finish_reason)
            finished = True
            if finish_reason == "length":
                raise _DirectProviderFailure(
                    "provider_output_failure",
                    "provider stream ended at the output limit",
                    stage="response_validation",
                )
        usage = _token_usage(event.get("usage")) or usage

    for line in text.splitlines():
        if line == "":
            consume_event(event_data)
            event_data = []
        elif line.startswith(":"):
            continue
        elif line.startswith("data:"):
            event_data.append(line[5:].lstrip(" "))
        elif (
            line.startswith("event:")
            or line.startswith("id:")
            or line.startswith("retry:")
        ):
            continue
        else:
            raise _DirectProviderFailure(
                "provider_output_failure",
                "provider stream contains an unsupported field",
                stage="response_validation",
            )
    consume_event(event_data)
    if not finished or not saw_done:
        raise _DirectProviderFailure(
            "transport_failure",
            "provider response stream ended before a final candidate",
            stage="response_stream",
        )
    candidate = "".join(content_parts)
    if not candidate.strip():
        detail = (
            "provider stream contains reasoning without assistant content"
            if reasoning_observed
            else "provider stream assistant content is empty"
        )
        raise _DirectProviderFailure(
            "provider_output_failure", detail, stage="response_validation"
        )
    return (
        candidate,
        {
            "response_mode": "streaming_sse",
            "finish_reason": finish_reason,
            "reasoning_content_observed": reasoning_observed,
        },
        usage,
    )


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
        input_tokens=optional_int("prompt_tokens", "input_tokens"),
        output_tokens=optional_int("completion_tokens", "output_tokens"),
        total_tokens=optional_int("total_tokens"),
    )


def _as_execution_error(
    classification: str,
    detail: str,
    *,
    origin: ProviderFailureOrigin,
    details: Mapping[str, Any],
) -> PlanningProviderExecutionError:
    return PlanningProviderExecutionError(
        classification=classification,
        detail=detail,
        origin=origin,
        diagnostics=ProviderDiagnostics(category=classification, details=dict(details)),
    )


__all__ = [
    "DIRECT_PROVIDER_VERSION",
    "DirectOpenAICompatiblePlanningProvider",
    "DirectProviderConfigurationError",
    "build_application_owned_messages",
]
