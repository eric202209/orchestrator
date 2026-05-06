"""OpenAI Responses runtime adapter."""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.services.observability import (
    build_text_trace_payload,
    start_langfuse_observation,
    update_langfuse_observation,
)
from app.services.agents.agent_backends import get_backend_descriptor
from app.services.agents.interfaces import (
    AgentInterfaceDescriptor,
    AgentRuntimeError,
    ContextWindowPolicy,
    RetryStrategy,
    UnsupportedCapabilityError,
)
from app.services.model_adaptation import resolve_adaptation_profile
from app.services.workspace.system_settings import get_effective_agent_model_family


class OpenAIResponsesRuntime:
    """Runtime adapter for text/planning work via the OpenAI Responses API."""

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
        self.backend_descriptor = get_backend_descriptor("openai_responses_api")
        self.response_session_key = (
            f"openai:session:{task_id or session_id or int(time.time())}"
        )

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
    ) -> dict[str, Any]:
        del diagnostic_label
        del diagnostic_metadata
        return await self.invoke_prompt(
            prompt,
            timeout_seconds=timeout_seconds,
            source_brain="cloud",
            session_prefix="direct",
        )

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
        del isolate_workspace_context
        del no_output_timeout_seconds
        api_key = (settings.OPENAI_API_KEY or "").strip()
        if not api_key:
            raise AgentRuntimeError(
                "OPENAI_API_KEY is not configured for the OpenAI Responses backend."
            )

        base_url = settings.OPENAI_BASE_URL.rstrip("/")
        model_name = (
            get_effective_agent_model_family(
                settings.ORCHESTRATOR_AGENT_MODEL_FAMILY, db=self.db
            ).strip()
            or self.backend_descriptor.default_model_family
        )

        payload = {
            "model": model_name,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        trace_input = build_text_trace_payload(prompt)

        with start_langfuse_observation(
            name="openai-responses-request",
            as_type="generation",
            input=trace_input,
            metadata={
                "backend": self.backend_descriptor.name,
                "source_brain": source_brain,
                "session_prefix": session_prefix,
                "session_id": self.session_id,
                "task_id": self.task_id,
            },
            model=model_name,
        ) as observation:
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds + 30) as client:
                    response = await client.post(
                        f"{base_url}/responses",
                        headers=headers,
                        json=payload,
                    )
            except httpx.TimeoutException as exc:
                update_langfuse_observation(
                    observation,
                    level="ERROR",
                    status_message=f"timed out after {timeout_seconds}s",
                    output={"status": "failed", "reason": "timeout"},
                )
                raise AgentRuntimeError(
                    f"OpenAI Responses request timed out after {timeout_seconds}s."
                ) from exc
            except httpx.HTTPError as exc:
                update_langfuse_observation(
                    observation,
                    level="ERROR",
                    status_message=str(exc)[:500],
                    output={"status": "failed", "reason": "http_error"},
                )
                raise AgentRuntimeError(
                    f"OpenAI Responses request failed: {exc}"
                ) from exc

            body = response.json()
            if response.status_code >= 400:
                error = body.get("error") if isinstance(body, dict) else None
                message = (
                    error.get("message")
                    if isinstance(error, dict)
                    else f"OpenAI Responses returned HTTP {response.status_code}"
                )
                update_langfuse_observation(
                    observation,
                    level="ERROR",
                    status_message=message[:500],
                    output={"status": "failed", "http_status": response.status_code},
                )
                raise AgentRuntimeError(message)

            output_text = _extract_output_text(body)
            usage = body.get("usage") if isinstance(body, dict) else None
            usage_details = None
            if isinstance(usage, dict):
                usage_details = {
                    key: int(value)
                    for key, value in (
                        ("input", usage.get("input_tokens")),
                        ("output", usage.get("output_tokens")),
                    )
                    if isinstance(value, int)
                }
            update_langfuse_observation(
                observation,
                output=build_text_trace_payload(output_text),
                metadata={
                    "response_id": body.get("id"),
                    "backend": self.backend_descriptor.name,
                },
                usage_details=usage_details,
            )
            return {
                "status": "completed",
                "output": output_text,
                "response_id": body.get("id"),
                "backend": self.backend_descriptor.name,
                "model_family": model_name,
            }

    async def execute_task_with_orchestration(
        self, prompt: str, timeout_seconds: int = 300, orchestration_state: Any = None
    ) -> dict[str, Any]:
        raise UnsupportedCapabilityError(
            "Backend 'openai_responses_api' does not support full step-by-step orchestration."
        )

    async def pause_session(self) -> None:
        raise UnsupportedCapabilityError(
            "Backend 'openai_responses_api' does not support checkpoint pause."
        )

    async def resume_session(self, checkpoint_name: Optional[str] = None) -> str:
        raise UnsupportedCapabilityError(
            "Backend 'openai_responses_api' does not support checkpoint resume."
        )

    async def stop_session(self) -> None:
        raise UnsupportedCapabilityError(
            "Backend 'openai_responses_api' does not support remote stop."
        )

    async def get_session_context(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "session_key": self.response_session_key,
            "backend": self.backend_descriptor.name,
        }

    def get_backend_metadata(self) -> dict[str, Any]:
        model_family = get_effective_agent_model_family(
            settings.ORCHESTRATOR_AGENT_MODEL_FAMILY, db=self.db
        )
        adaptation_profile = resolve_adaptation_profile(
            backend=self.backend_descriptor.name,
            model_family=model_family,
        )
        return {
            "backend": self.backend_descriptor.name,
            "display_name": self.backend_descriptor.display_name,
            "implementation": self.backend_descriptor.implementation,
            "model_family": model_family,
            "adaptation_profile": adaptation_profile.name,
            "agent_interface": self.describe_interface().to_dict(),
            "capabilities": self.backend_descriptor.capabilities.to_dict(),
        }

    def describe_interface(self) -> AgentInterfaceDescriptor:
        model_family = get_effective_agent_model_family(
            settings.ORCHESTRATOR_AGENT_MODEL_FAMILY, db=self.db
        )
        profile = resolve_adaptation_profile(
            backend=self.backend_descriptor.name,
            model_family=model_family,
        )
        return AgentInterfaceDescriptor(
            backend=self.backend_descriptor.name,
            model_family=model_family,
            planning_prompt_template="assemble_planning_prompt",
            execution_prompt_template="assemble_execution_prompt",
            prompt_dialect=profile.prompt_dialect,
            tool_capability_map={
                "shell": False,
                "filesystem": False,
                "checkpoint_resume": False,
                "streaming": bool(
                    self.backend_descriptor.capabilities.supports_streaming
                ),
            },
            tool_shape=profile.tool_shape,
            preferred_retry_strategy=RetryStrategy(
                planning="structured_retry",
                execution="unsupported",
                completion="structured_retry",
            ),
            context_window_policy=ContextWindowPolicy(
                max_input_tokens=self.backend_descriptor.capabilities.max_context_tokens,
                overflow_strategy="summarize_and_retry",
                compaction_strategy=profile.context_window_policy,
            ),
        )

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


def _extract_output_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output = payload.get("output")
    if isinstance(output, list):
        text_parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
        if text_parts:
            return "".join(text_parts)

    raise AgentRuntimeError("OpenAI Responses returned no text output.")


def create_runtime(
    db: Session,
    session_id: Optional[int],
    task_id: Optional[int] = None,
    *,
    use_demo_mode: Optional[bool] = None,
) -> OpenAIResponsesRuntime:
    """Instantiate the OpenAI Responses backend runtime."""

    return OpenAIResponsesRuntime(
        db,
        session_id,
        task_id,
        use_demo_mode=use_demo_mode,
    )
