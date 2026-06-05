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


_PLAN_SYSTEM = """You are a precise software development orchestrator.
Analyse the task and produce an execution plan using ONLY file operations.

Output ONLY a valid JSON array. Each element:
{
  "step": <int>,
  "title": <str>,
  "description": <str>,
  "type": "implementation",
  "ops": [
    {"op": "write_file", "path": "<relative_path>", "content": "<file_content>"}
  ]
}

STRICT RULES:
- Use ONLY the "ops" array for all actions
- Supported ops: write_file, mkdir, replace_in_file, delete_file
- NEVER include "commands" field
- NEVER include "verification" field
- NEVER include "rollback" field
- NEVER use "step_number", always use "step"
- Do NOT generate shell commands like find, ls, python, node
- No markdown fences, no preamble, no explanation outside the JSON
- Maximum 5 steps"""

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
    ) -> None:
        self.db = db
        self.session_id = session_id
        self.task_id = task_id
        self.use_demo_mode = use_demo_mode
        self.backend_descriptor = get_backend_descriptor("openai_chat_completions")
        self.backend_role: Optional[str] = None
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

    def _model_name(self) -> str:
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
    ) -> str:
        headers = {"Content-Type": "application/json"}
        api_key = self._api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

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

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds + 30) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise AgentRuntimeError(
                f"OpenAI-compatible chat request timed out after {timeout_seconds}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentRuntimeError(
                f"OpenAI-compatible chat request failed: {exc}"
            ) from exc

        body = response.json()
        return _strip_thinking(_extract_chat_completion_content(body))

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
            system=_PLAN_SYSTEM if planning else _STEP_SYSTEM,
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
    ) -> dict[str, Any]:
        del source_brain
        del isolate_workspace_context
        del no_output_timeout_seconds
        system = _PLAN_SYSTEM if session_prefix == "planning" else _GENERIC_SYSTEM
        output = await self._chat(
            system=system,
            user=prompt,
            timeout_seconds=timeout_seconds,
        )
        return {
            "status": "completed",
            "output": output,
            "backend": self.backend_descriptor.name,
            "model_family": self._model_name(),
        }

    async def execute_task_with_orchestration(
        self,
        prompt: str,
        timeout_seconds: int = 300,
        orchestration_state: Any = None,
    ) -> dict[str, Any]:
        raise UnsupportedCapabilityError(
            "Backend 'openai_chat_completions' does not support full step-by-step orchestration."
        )

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
        return {
            "backend": self.backend_descriptor.name,
            "display_name": self.backend_descriptor.display_name,
            "implementation": self.backend_descriptor.implementation,
            "model_family": self._model_name(),
            "agent_interface": self.describe_interface().to_dict(),
            "capabilities": self.backend_descriptor.capabilities.to_dict(),
        }

    def describe_interface(self) -> AgentInterfaceDescriptor:
        return AgentInterfaceDescriptor(
            backend=self.backend_descriptor.name,
            model_family=self._model_name(),
            planning_prompt_template="assemble_planning_prompt",
            execution_prompt_template="assemble_execution_prompt",
            prompt_dialect="openai_chat_completions",
            tool_capability_map={
                "shell": False,
                "filesystem": False,
                "checkpoint_resume": False,
                "streaming": False,
            },
            tool_shape="none",
            preferred_retry_strategy=RetryStrategy(
                planning="schema_first",
                execution="single_retry_compact_prompt",
                completion="schema_first",
            ),
            context_window_policy=ContextWindowPolicy(
                max_input_tokens=self.backend_descriptor.capabilities.max_context_tokens,
                overflow_strategy="truncate_and_retry",
                compaction_strategy="truncate_context",
            ),
        )

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
