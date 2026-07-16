"""Ollama runtime adapter — direct Ollama API, no OpenClaw dependency."""

from __future__ import annotations

import re
import logging
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
)
from app.services.agents.runtime_configuration import RuntimeConfiguration
from app.services.agents.runtime_invocation import RuntimeInvocationOptions
from app.services.model_adaptation import (
    get_adaptation_profile,
    resolve_adaptation_profile,
)
from app.services.workspace.system_settings import (
    AGENT_MODEL_FAMILY_KEY,
    get_setting_value_runtime,
)

logger = logging.getLogger(__name__)

_STEP_SYSTEM = """You are a precise software development assistant.
Execute the given step exactly as described.
Output the result clearly. Wrap code in appropriate fences.
Do NOT invent steps that were not requested."""

_GENERIC_SYSTEM = """You are a helpful AI assistant integrated into a development orchestrator.
Answer concisely and accurately."""


def _normalize_ollama_content_value(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "output_text", "content"):
            extracted = _normalize_ollama_content_value(value.get(key))
            if extracted:
                return extracted
        return ""
    if isinstance(value, list):
        return "".join(_normalize_ollama_content_value(item) for item in value)
    return ""


def _extract_ollama_chat_content(body: dict[str, Any]) -> str:
    content = body["choices"][0]["message"]["content"]
    return _normalize_ollama_content_value(content)


def _strip_thinking(text: Any) -> str:
    """Remove <think>...</think> blocks from model output."""
    normalized = _normalize_ollama_content_value(text)
    return re.sub(r"<think>.*?</think>", "", normalized, flags=re.DOTALL).strip()


def _no_think_suffix() -> str:
    """Return /no_think suffix if thinking should be disabled."""
    if getattr(settings, "PLANNING_REPAIR_DISABLE_THINKING", True):
        return " /no_think"
    return ""


class OllamaRuntime:
    """Runtime adapter for text/planning work via Ollama OpenAI-compatible API."""

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
        self.task_execution_id: Optional[int] = None
        self.runtime_configuration = runtime_configuration
        self.backend_role: Optional[str] = (
            runtime_configuration.role.value if runtime_configuration else None
        )
        backend_name = (
            runtime_configuration.backend_name
            if runtime_configuration
            else "direct_ollama"
        )
        self.backend_descriptor = get_backend_descriptor(backend_name)

        self._base_url = (settings.OLLAMA_BASE_URL or "http://localhost:11434").rstrip(
            "/"
        )
        persisted_model = get_setting_value_runtime(
            AGENT_MODEL_FAMILY_KEY, None, db=self.db
        )
        if runtime_configuration and runtime_configuration.model_family:
            selected_model = runtime_configuration.model_family
        # Stage A migration fallback for legacy unscoped/direct adapter calls.
        elif (
            persisted_model
            and str(settings.AGENT_BACKEND or "").strip() == "direct_ollama"
        ):
            selected_model = persisted_model
        else:
            selected_model = settings.OLLAMA_AGENT_MODEL
        self._model = (
            selected_model or self.backend_descriptor.default_model_family
        ).strip()
        self._num_ctx = int(getattr(settings, "OLLAMA_NUM_CTX", 4096))
        self._timeout = int(settings.PLANNING_REPAIR_TIMEOUT_SECONDS or 120)
        self._planning_timeout = max(
            0, int(getattr(settings, "OLLAMA_PLANNING_TIMEOUT_SECONDS", 0) or 0)
        )

    # ── core chat ───────────────────────────────────────────────────────────

    def _effective_timeout(
        self, timeout: Optional[int], *, planning: bool = False
    ) -> float:
        base_timeout = int(timeout or self._timeout)
        if planning and self._planning_timeout > 0:
            return float(max(base_timeout, self._planning_timeout))
        return float(base_timeout)

    async def _chat(
        self,
        system: str,
        user: str,
        timeout: Optional[int] = None,
        *,
        planning: bool = False,
        invocation_options: RuntimeInvocationOptions | None = None,
    ) -> str:
        exact_contract = invocation_options is not None
        base_url = self._base_url
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if exact_contract and self.backend_role in {
            "repair",
            "debug_repair",
            "completion_repair",
        }:
            if self.backend_role == "debug_repair":
                base_url = (
                    settings.DEBUG_REPAIR_BASE_URL
                    or settings.PLANNING_REPAIR_BASE_URL
                    or base_url
                ).rstrip("/")
                api_key = (
                    settings.DEBUG_REPAIR_API_KEY
                    or settings.PLANNING_REPAIR_API_KEY
                    or ""
                ).strip()
            else:
                base_url = (settings.PLANNING_REPAIR_BASE_URL or base_url).rstrip("/")
                api_key = (settings.PLANNING_REPAIR_API_KEY or "").strip()
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        if base_url.endswith("/v1"):
            url = f"{base_url}/chat/completions"
        else:
            url = f"{base_url}/v1/chat/completions"

        if exact_contract:
            messages = [{"role": "user", "content": user}]
            if invocation_options.system_prompt is not None:
                messages.insert(
                    0, {"role": "system", "content": invocation_options.system_prompt}
                )
            payload = {
                "model": self._model,
                "messages": messages,
                "stream": bool(invocation_options.stream or False),
                "temperature": float(
                    invocation_options.temperature
                    if invocation_options.temperature is not None
                    else 0.0
                ),
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
            user_content = user + _no_think_suffix()
            payload = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
                "stream": False,
                "temperature": 0.1,
                "think": False,  # Disable Ollama's internal "thinking" phase
                "options": {
                    "num_ctx": self._num_ctx,
                },
            }
        effective_timeout = (
            int(invocation_options.timeout_seconds)
            if invocation_options is not None
            and invocation_options.timeout_seconds is not None
            else self._effective_timeout(timeout, planning=planning)
        )
        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                content = _extract_ollama_chat_content(resp.json())
                return content if exact_contract else _strip_thinking(content)
        except httpx.TimeoutException as exc:
            logger.error(
                "[OLLAMA] Timeout after %.0fs calling %s", effective_timeout, url
            )
            error = AgentRuntimeError(f"Ollama timed out after {effective_timeout}s")
            error.runtime_diagnostics = {
                "timed_out": True,
                "timeout_boundary": "runtime_invocation",
                "timeout_seconds": effective_timeout,
            }
            raise error from exc
        except httpx.HTTPStatusError as exc:
            logger.error(
                "[OLLAMA] HTTP %s: %s",
                exc.response.status_code,
                exc.response.text[:400],
            )
            error = AgentRuntimeError(f"Ollama HTTP {exc.response.status_code}")
            error.runtime_diagnostics = {
                "timed_out": False,
                "timeout_boundary": "runtime_invocation",
                "timeout_seconds": effective_timeout,
            }
            raise error from exc
        except httpx.ConnectError as exc:
            logger.error("[OLLAMA] Cannot connect to %s", self._base_url)
            error = AgentRuntimeError(f"Cannot connect to Ollama at {self._base_url}")
            error.runtime_diagnostics = {
                "timed_out": False,
                "timeout_boundary": "runtime_invocation",
                "timeout_seconds": effective_timeout,
            }
            raise error from exc
        except Exception as exc:
            logger.error("[OLLAMA] Unexpected error: %s", exc)
            error = AgentRuntimeError(str(exc))
            error.runtime_diagnostics = {
                "timed_out": False,
                "timeout_boundary": "runtime_invocation",
                "timeout_seconds": effective_timeout,
            }
            raise error from exc

    # ── AgentRuntime Protocol ───────────────────────────────────────────────

    async def create_session(
        self, task_description: str, context: Optional[dict[str, Any]] = None
    ) -> str:
        return f"ollama:session:{self.task_id or self.session_id}"

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
        planning = str(diagnostic_label or "").upper().endswith("PLANNING")
        if isinstance(diagnostic_metadata, dict):
            planning = planning or bool(diagnostic_metadata.get("planning_attempt"))
        output = await self._chat(
            system=_GENERIC_SYSTEM if planning else _STEP_SYSTEM,
            user=prompt,
            timeout=timeout_seconds,
            planning=planning,
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
        # PlanningSessionService owns the artifact contract and sends it in
        # the rendered user prompt. Provider adapters must not redefine it.
        system = _GENERIC_SYSTEM
        output = await self._chat(
            system=system,
            user=prompt,
            timeout=timeout_seconds,
            planning=session_prefix == "planning",
            invocation_options=invocation_options,
        )
        return {
            "status": "completed",
            "output": output,
            "backend": self.backend_descriptor.name,
            "model_family": self._model,
            "role": self.backend_role,
            "runtime_configuration": (
                self.runtime_configuration.to_dict()
                if self.runtime_configuration is not None
                else None
            ),
        }

    async def pause_session(self) -> None:
        """No-op: Ollama is stateless."""

    async def resume_session(self, checkpoint_name: Optional[str] = None) -> str:
        """No-op: Ollama is stateless."""
        return f"ollama-resumed-{self.session_id}"

    async def stop_session(self) -> None:
        """No-op: Ollama is stateless."""

    async def get_session_context(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "backend": self.backend_descriptor.name,
            "model": self._model,
        }

    def get_backend_metadata(self) -> dict[str, Any]:
        payload = {
            "backend": self.backend_descriptor.name,
            "display_name": self.backend_descriptor.display_name,
            "implementation": self.backend_descriptor.implementation,
            "model_family": self._model,
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
        profile = (
            self._adaptation_profile()
            if self.runtime_configuration
            and self.runtime_configuration.adaptation_profile
            else None
        )
        return AgentInterfaceDescriptor(
            backend=self.backend_descriptor.name,
            model_family=self._model,
            planning_prompt_template="assemble_planning_prompt",
            execution_prompt_template="assemble_execution_prompt",
            prompt_dialect=profile.prompt_dialect if profile else "ollama_chat",
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
                max_input_tokens=self._num_ctx,
                overflow_strategy="truncate_and_retry",
                compaction_strategy=(
                    profile.context_window_policy if profile else "truncate_context"
                ),
            ),
        )

    def _adaptation_profile(self):
        if self.runtime_configuration and self.runtime_configuration.adaptation_profile:
            return get_adaptation_profile(self.runtime_configuration.adaptation_profile)
        # Stage A migration fallback for legacy unscoped/direct adapter calls.
        return resolve_adaptation_profile(
            backend=self.backend_descriptor.name,
            model_family=self._model,
        )

    def reports_context_overflow(self, result: Optional[dict[str, Any]]) -> bool:
        if not result:
            return False
        output = str(result.get("output") or "").lower()
        return any(s in output for s in ("context", "exceed", "too long", "maximum"))


def create_runtime(
    db: Session,
    session_id: Optional[int],
    task_id: Optional[int] = None,
    *,
    use_demo_mode: Optional[bool] = None,
    runtime_configuration: RuntimeConfiguration | None = None,
) -> OllamaRuntime:
    return OllamaRuntime(
        db,
        session_id,
        task_id,
        use_demo_mode=use_demo_mode,
        runtime_configuration=runtime_configuration,
    )
