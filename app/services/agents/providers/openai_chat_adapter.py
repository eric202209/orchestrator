"""OpenAI-compatible chat-completions runtime adapter."""

from __future__ import annotations

import re
import time
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.services.agents.agent_backends import get_backend_descriptor
from app.services.agents.interfaces import (
    AgentInterfaceDescriptor,
    AgentRuntimeError,
    ContextWindowPolicy,
    RetryStrategy,
    UnsupportedCapabilityError,
)
from app.services.agents.runtime_invocation import RuntimeInvocationOptions
from app.services.agents.runtime_configuration import RuntimeConfiguration
from app.services.model_adaptation import (
    get_adaptation_profile,
    resolve_adaptation_profile,
)


_STEP_SYSTEM = """You are a precise software development assistant.
Execute the given step exactly as described.
Output the result clearly. Wrap code in appropriate fences.
Do NOT invent steps that were not requested."""

_GENERIC_SYSTEM = """You are a helpful AI assistant integrated into a development orchestrator.
Answer concisely and accurately."""


def _normalize_chat_content_value(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "output_text", "content"):
            extracted = _normalize_chat_content_value(value.get(key))
            if extracted:
                return extracted
        return ""
    if isinstance(value, list):
        return "".join(_normalize_chat_content_value(item) for item in value)
    return ""


def _strip_thinking(text: Any) -> str:
    normalized = _normalize_chat_content_value(text)
    return re.sub(r"<think>.*?</think>", "", normalized, flags=re.DOTALL).strip()


class OpenAIChatCompletionsRuntime:
    """Runtime adapter for OpenAI-compatible /chat/completions endpoints."""

    def __init__(
        self,
        db: Session,
        session_id: Optional[int],
        task_id: Optional[int] = None,
        *,
        use_demo_mode: Optional[bool] = None,
        runtime_configuration: RuntimeConfiguration | None = None,
    ) -> None:
        self.db = db
        self.session_id = session_id
        self.task_id = task_id
        self.use_demo_mode = use_demo_mode
        self.runtime_configuration = runtime_configuration
        backend_name = (
            runtime_configuration.backend_name
            if runtime_configuration
            else "openai_chat_completions"
        )
        self.backend_descriptor = get_backend_descriptor(backend_name)
        self.backend_role: Optional[str] = (
            runtime_configuration.role.value if runtime_configuration else None
        )
        self.response_session_key = (
            f"openai-chat:session:{task_id or session_id or int(time.time())}"
        )

    @property
    def _base_url(self) -> str:
        return (
            settings.OPENAI_CHAT_COMPLETIONS_BASE_URL
            or settings.OPENAI_BASE_URL
            or "http://localhost:8001/v1"
        ).rstrip("/")

    def _api_key(self) -> str:
        return (
            settings.OPENAI_CHAT_COMPLETIONS_API_KEY or settings.OPENAI_API_KEY or ""
        ).strip()

    def _invocation_base_url(self, options: RuntimeInvocationOptions | None) -> str:
        if options is not None and self.backend_role in {
            "repair",
            "debug_repair",
            "completion_repair",
        }:
            if self.backend_role == "debug_repair":
                legacy_url = (
                    settings.DEBUG_REPAIR_BASE_URL or settings.PLANNING_REPAIR_BASE_URL
                )
            else:
                legacy_url = settings.PLANNING_REPAIR_BASE_URL
            if legacy_url:
                return legacy_url.rstrip("/")
        return self._base_url

    def _invocation_api_key(self, options: RuntimeInvocationOptions | None) -> str:
        if options is not None and self.backend_role in {
            "repair",
            "debug_repair",
            "completion_repair",
        }:
            if self.backend_role == "debug_repair":
                legacy_key = (
                    settings.DEBUG_REPAIR_API_KEY or settings.PLANNING_REPAIR_API_KEY
                )
            else:
                legacy_key = settings.PLANNING_REPAIR_API_KEY
            if legacy_key:
                return legacy_key.strip()
        return self._api_key()

    def _model_name(self) -> str:
        if self.runtime_configuration and self.runtime_configuration.model_family:
            return self.runtime_configuration.model_family
        # Stage A migration fallback for legacy unscoped/direct adapter calls.
        if self.backend_role == "planning" and settings.PLANNER_MODEL:
            return settings.PLANNER_MODEL
        return (
            settings.OPENAI_CHAT_COMPLETIONS_MODEL
            or settings.PLANNER_MODEL
            or settings.AGENT_MODEL
            or self.backend_descriptor.default_model_family
        ).strip()

    async def _chat(
        self,
        *,
        system: str,
        user: str,
        timeout_seconds: int,
        invocation_options: RuntimeInvocationOptions | None = None,
    ) -> str:
        headers = {"Content-Type": "application/json"}
        api_key = self._invocation_api_key(invocation_options)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        exact_contract = invocation_options is not None
        if exact_contract:
            messages = [{"role": "user", "content": user}]
            if invocation_options.system_prompt is not None:
                messages.insert(
                    0, {"role": "system", "content": invocation_options.system_prompt}
                )
            payload = {
                "model": self._model_name(),
                "messages": messages,
                "temperature": float(
                    invocation_options.temperature
                    if invocation_options.temperature is not None
                    else settings.OPENAI_CHAT_COMPLETIONS_TEMPERATURE
                ),
                "stream": bool(invocation_options.stream or False),
            }
            if invocation_options.max_output_tokens is not None:
                payload["max_tokens"] = invocation_options.max_output_tokens
            if invocation_options.reasoning_enabled is False:
                payload.update(
                    {
                        "think": False,
                        "enable_thinking": False,
                        "chat_template_kwargs": {"enable_thinking": False},
                    }
                )
            payload.update(dict(invocation_options.extra_provider_options or {}))
            if "chat_template_kwargs" in (
                invocation_options.extra_provider_options or {}
            ):
                payload["chat_template_kwargs"] = {
                    "enable_thinking": False,
                    **dict(
                        invocation_options.extra_provider_options[
                            "chat_template_kwargs"
                        ]
                    ),
                }
        else:
            payload = {
                "model": self._model_name(),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": float(settings.OPENAI_CHAT_COMPLETIONS_TEMPERATURE),
                "stream": False,
            }
            if settings.OPENAI_CHAT_COMPLETIONS_TOP_P is not None:
                payload["top_p"] = float(settings.OPENAI_CHAT_COMPLETIONS_TOP_P)
            if settings.OPENAI_CHAT_COMPLETIONS_REPEAT_PENALTY is not None:
                payload["repeat_penalty"] = float(
                    settings.OPENAI_CHAT_COMPLETIONS_REPEAT_PENALTY
                )

        effective_timeout = int(
            invocation_options.timeout_seconds
            if invocation_options is not None
            and invocation_options.timeout_seconds is not None
            else timeout_seconds
        )

        try:
            transport_timeout = (
                effective_timeout if exact_contract else effective_timeout + 30
            )
            async with httpx.AsyncClient(timeout=transport_timeout) as client:
                response = await client.post(
                    f"{self._invocation_base_url(invocation_options)}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            error = AgentRuntimeError(
                f"OpenAI-compatible chat request timed out after {effective_timeout}s."
            )
            error.runtime_diagnostics = {
                "timed_out": True,
                "timeout_boundary": "runtime_invocation",
                "timeout_seconds": effective_timeout,
            }
            raise error from exc
        except httpx.HTTPError as exc:
            error = AgentRuntimeError(f"OpenAI-compatible chat request failed: {exc}")
            error.runtime_diagnostics = {
                "timed_out": False,
                "timeout_boundary": "runtime_invocation",
                "timeout_seconds": effective_timeout,
            }
            raise error from exc

        body = response.json()
        content = _extract_chat_completion_content(body)
        return content if exact_contract else _strip_thinking(content)

    async def create_session(
        self, task_description: str, context: Optional[dict[str, Any]] = None
    ) -> str:
        return self.response_session_key

    async def execute_task(
        self,
        prompt: str,
        timeout_seconds: int = 300,
        log_callback: Any = None,
        *,
        diagnostic_label: Optional[str] = None,
        diagnostic_metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> dict[str, Any]:
        del log_callback
        planning = str(diagnostic_label or "").upper().endswith("PLANNING")
        if isinstance(diagnostic_metadata, dict):
            planning = planning or bool(diagnostic_metadata.get("planning_attempt"))
        output = await self._chat(
            system=_GENERIC_SYSTEM if planning else _STEP_SYSTEM,
            user=prompt,
            timeout_seconds=timeout_seconds,
        )
        return {"status": "completed", "output": output}

    async def invoke_prompt(
        self,
        prompt: str,
        *,
        timeout_seconds: int = 180,
        source_brain: str = "local",
        session_prefix: str = "planning",
        isolate_workspace_context: bool = False,
        no_output_timeout_seconds: Optional[int] = None,
        invocation_options: RuntimeInvocationOptions | None = None,
    ) -> dict[str, Any]:
        del source_brain
        del isolate_workspace_context
        del no_output_timeout_seconds
        # PlanningSessionService owns the artifact contract and sends it in
        # the rendered user prompt. Provider adapters must not redefine it.
        system = _GENERIC_SYSTEM
        output = await self._chat(
            system=system,
            user=prompt,
            timeout_seconds=timeout_seconds,
            invocation_options=invocation_options,
        )
        return {
            "status": "completed",
            "output": output,
            "backend": self.backend_descriptor.name,
            "model_family": self._model_name(),
            "role": self.backend_role,
            "runtime_configuration": (
                self.runtime_configuration.to_dict()
                if self.runtime_configuration is not None
                else None
            ),
        }

    async def pause_session(self) -> None:
        raise UnsupportedCapabilityError(
            "Backend 'openai_chat_completions' does not support checkpoint pause."
        )

    async def resume_session(self, checkpoint_name: Optional[str] = None) -> str:
        raise UnsupportedCapabilityError(
            "Backend 'openai_chat_completions' does not support checkpoint resume."
        )

    async def stop_session(self) -> None:
        raise UnsupportedCapabilityError(
            "Backend 'openai_chat_completions' does not support remote stop."
        )

    async def get_session_context(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "session_key": self.response_session_key,
            "backend": self.backend_descriptor.name,
            "model": self._model_name(),
        }

    def get_backend_metadata(self) -> dict[str, Any]:
        model_family = self._model_name()
        payload = {
            "backend": self.backend_descriptor.name,
            "display_name": self.backend_descriptor.display_name,
            "implementation": self.backend_descriptor.implementation,
            "model_family": model_family,
            "agent_interface": self.describe_interface().to_dict(),
            "capabilities": self.backend_descriptor.capabilities.to_dict(),
        }
        if self.runtime_configuration and self.runtime_configuration.adaptation_profile:
            payload["adaptation_profile"] = (
                self.runtime_configuration.adaptation_profile
            )
        if self.runtime_configuration is not None:
            payload["role"] = self.backend_role
            payload["runtime_configuration"] = self.runtime_configuration.to_dict()
        return payload

    def describe_interface(self) -> AgentInterfaceDescriptor:
        model_family = self._model_name()
        profile = (
            self._adaptation_profile(model_family)
            if self.runtime_configuration
            and self.runtime_configuration.adaptation_profile
            else None
        )
        return AgentInterfaceDescriptor(
            backend=self.backend_descriptor.name,
            model_family=model_family,
            planning_prompt_template="assemble_planning_prompt",
            execution_prompt_template="assemble_execution_prompt",
            prompt_dialect=(
                profile.prompt_dialect if profile else "openai_chat_completions"
            ),
            tool_capability_map={
                "shell": False,
                "filesystem": False,
                "checkpoint_resume": False,
                "streaming": False,
            },
            tool_shape=profile.tool_shape if profile else "none",
            preferred_retry_strategy=RetryStrategy(
                planning="schema_first",
                execution="single_retry_compact_prompt",
                completion="schema_first",
            ),
            context_window_policy=ContextWindowPolicy(
                max_input_tokens=self.backend_descriptor.capabilities.max_context_tokens,
                overflow_strategy="truncate_and_retry",
                compaction_strategy=(
                    profile.context_window_policy if profile else "truncate_context"
                ),
            ),
        )

    def _adaptation_profile(self, model_family: str):
        if self.runtime_configuration and self.runtime_configuration.adaptation_profile:
            return get_adaptation_profile(self.runtime_configuration.adaptation_profile)
        # Stage A migration fallback for legacy unscoped/direct adapter calls.
        profile = resolve_adaptation_profile(
            backend=self.backend_descriptor.name,
            model_family=model_family,
        )
        if (
            profile.backend == "*"
            or profile.name in self.backend_descriptor.config.adaptation_profiles
        ):
            return profile
        if self.backend_descriptor.config.adaptation_profiles:
            return get_adaptation_profile(
                self.backend_descriptor.config.adaptation_profiles[0]
            )
        return profile

    def get_interface_descriptor(self) -> AgentInterfaceDescriptor:
        return self.describe_interface()

    def reports_context_overflow(self, result: Optional[dict[str, Any]]) -> bool:
        if not result:
            return False
        for key in ("error", "output"):
            value = result.get(key)
            if isinstance(value, str):
                lowered = value.lower()
                if "context" in lowered and (
                    "exceed" in lowered or "too long" in lowered or "maximum" in lowered
                ):
                    return True
        return False


def _extract_chat_completion_content(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    return _normalize_chat_content_value(message.get("content"))


def create_runtime(*args, **kwargs) -> OpenAIChatCompletionsRuntime:
    return OpenAIChatCompletionsRuntime(*args, **kwargs)
