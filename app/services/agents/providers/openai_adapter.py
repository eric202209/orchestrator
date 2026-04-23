"""OpenAI Responses runtime adapter."""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.services.agents.agent_backends import get_backend_descriptor
from app.services.agents.interfaces import AgentRuntimeError
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
        self, prompt: str, timeout_seconds: int = 300, log_callback: Any = None
    ) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
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

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds + 30) as client:
                response = await client.post(
                    f"{base_url}/responses",
                    headers=headers,
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise AgentRuntimeError(
                f"OpenAI Responses request timed out after {timeout_seconds}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentRuntimeError(f"OpenAI Responses request failed: {exc}") from exc

        body = response.json()
        if response.status_code >= 400:
            error = body.get("error") if isinstance(body, dict) else None
            message = (
                error.get("message")
                if isinstance(error, dict)
                else f"OpenAI Responses returned HTTP {response.status_code}"
            )
            raise AgentRuntimeError(message)

        output_text = _extract_output_text(body)
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
        raise AgentRuntimeError(
            "Backend 'openai_responses_api' supports direct prompt execution and planning, "
            "but not full step-by-step orchestration yet."
        )

    async def pause_session(self) -> None:
        raise AgentRuntimeError(
            "Backend 'openai_responses_api' does not support checkpoint pause."
        )

    async def resume_session(self, checkpoint_name: Optional[str] = None) -> str:
        raise AgentRuntimeError(
            "Backend 'openai_responses_api' does not support checkpoint resume."
        )

    async def stop_session(self) -> None:
        raise AgentRuntimeError(
            "Backend 'openai_responses_api' does not support remote stop after request completion."
        )

    async def get_session_context(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "session_key": self.response_session_key,
            "backend": self.backend_descriptor.name,
        }

    def get_backend_metadata(self) -> dict[str, Any]:
        return {
            "backend": self.backend_descriptor.name,
            "display_name": self.backend_descriptor.display_name,
            "implementation": self.backend_descriptor.implementation,
            "model_family": get_effective_agent_model_family(
                settings.ORCHESTRATOR_AGENT_MODEL_FAMILY, db=self.db
            ),
            "capabilities": self.backend_descriptor.capabilities.to_dict(),
        }

    def build_cli_agent_command(
        self,
        prompt: str,
        *,
        source_brain: str = "local",
        timeout_seconds: int = 180,
        session_prefix: str = "planning",
    ) -> list[str]:
        raise AgentRuntimeError(
            "Backend 'openai_responses_api' does not expose a local CLI command path."
        )

    def parse_cli_response(self, proc: Any) -> dict[str, Any]:
        raise AgentRuntimeError(
            "Backend 'openai_responses_api' does not parse CLI subprocess output."
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
