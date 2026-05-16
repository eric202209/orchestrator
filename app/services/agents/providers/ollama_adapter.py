"""Ollama runtime adapter — direct Ollama API, no OpenClaw dependency."""

from __future__ import annotations

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


class OllamaRuntime:
    """Runtime adapter for text/planning work via native Ollama /api/chat."""

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
        self.backend_descriptor = get_backend_descriptor("direct_ollama")

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
    ) -> dict[str, Any]:
        del diagnostic_label
        del diagnostic_metadata
        return await self.invoke_prompt(prompt, timeout_seconds=timeout_seconds)

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
        base_url = (settings.OLLAMA_BASE_URL or "").rstrip("/")
        model = (settings.OLLAMA_AGENT_MODEL or "").strip()
        if not base_url or not model:
            raise AgentRuntimeError(
                "OLLAMA_BASE_URL and OLLAMA_AGENT_MODEL must be set for direct_ollama backend."
            )

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"num_ctx": settings.OLLAMA_NUM_CTX},
        }

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds + 30) as client:
                response = await client.post(
                    f"{base_url}/api/chat",
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise AgentRuntimeError(
                f"Ollama request timed out after {timeout_seconds}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentRuntimeError(f"Ollama request failed: {exc}") from exc

        if response.status_code >= 400:
            raise AgentRuntimeError(
                f"Ollama returned HTTP {response.status_code}: {response.text[:500]}"
            )

        body = response.json()
        message = body.get("message") or {}
        output_text = message.get("content") or ""
        if not output_text.strip():
            raise AgentRuntimeError("Ollama returned no text output.")

        return {
            "status": "completed",
            "output": output_text,
            "backend": self.backend_descriptor.name,
            "model_family": model,
        }

    async def execute_task_with_orchestration(
        self, prompt: str, timeout_seconds: int = 300, orchestration_state: Any = None
    ) -> dict[str, Any]:
        raise UnsupportedCapabilityError(
            "Backend 'direct_ollama' does not support full step-by-step orchestration."
        )

    async def pause_session(self) -> None:
        raise UnsupportedCapabilityError(
            "Backend 'direct_ollama' does not support checkpoint pause."
        )

    async def resume_session(self, checkpoint_name: Optional[str] = None) -> str:
        raise UnsupportedCapabilityError(
            "Backend 'direct_ollama' does not support checkpoint resume."
        )

    async def stop_session(self) -> None:
        raise UnsupportedCapabilityError(
            "Backend 'direct_ollama' does not support remote stop."
        )

    async def get_session_context(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "backend": self.backend_descriptor.name,
        }

    def get_backend_metadata(self) -> dict[str, Any]:
        model_family = (settings.OLLAMA_AGENT_MODEL or "").strip()
        return {
            "backend": self.backend_descriptor.name,
            "display_name": self.backend_descriptor.display_name,
            "implementation": self.backend_descriptor.implementation,
            "model_family": model_family,
            "agent_interface": self.describe_interface().to_dict(),
            "capabilities": self.backend_descriptor.capabilities.to_dict(),
        }

    def describe_interface(self) -> AgentInterfaceDescriptor:
        model_family = (settings.OLLAMA_AGENT_MODEL or "").strip()
        return AgentInterfaceDescriptor(
            backend=self.backend_descriptor.name,
            model_family=model_family,
            planning_prompt_template="assemble_planning_prompt",
            execution_prompt_template="assemble_execution_prompt",
            prompt_dialect="ollama_chat",
            tool_capability_map={
                "shell": False,
                "filesystem": False,
                "checkpoint_resume": False,
                "streaming": False,
            },
            tool_shape="none",
            preferred_retry_strategy=RetryStrategy(
                planning="schema_first",
                execution="unsupported",
                completion="schema_first",
            ),
            context_window_policy=ContextWindowPolicy(
                max_input_tokens=settings.OLLAMA_NUM_CTX,
                overflow_strategy="truncate_and_retry",
                compaction_strategy="truncate_context",
            ),
        )

    def reports_context_overflow(self, result: Optional[dict[str, Any]]) -> bool:
        if not result:
            return False
        output = result.get("output") or ""
        if isinstance(output, str):
            lower = output.lower()
            if "context" in lower and (
                "exceed" in lower or "too long" in lower or "maximum" in lower
            ):
                return True
        return False


def create_runtime(
    db: Session,
    session_id: Optional[int],
    task_id: Optional[int] = None,
    *,
    use_demo_mode: Optional[bool] = None,
) -> OllamaRuntime:
    return OllamaRuntime(db, session_id, task_id, use_demo_mode=use_demo_mode)
