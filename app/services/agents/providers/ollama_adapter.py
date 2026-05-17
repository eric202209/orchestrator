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

logger = logging.getLogger(__name__)

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


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks from model output."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


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
    ) -> None:
        self.db = db
        self.session_id = session_id
        self.task_id = task_id
        self.task_execution_id: Optional[int] = None
        self.backend_descriptor = get_backend_descriptor("direct_ollama")

        self._base_url = (settings.OLLAMA_BASE_URL or "http://localhost:11434").rstrip(
            "/"
        )
        # OLLAMA_AGENT_MODEL 優先，fallback 到 PLANNING_REPAIR_MODEL
        self._model = (
            getattr(settings, "OLLAMA_AGENT_MODEL", None)
            or settings.PLANNING_REPAIR_MODEL
            or "qwen3:4b-q4_K_M"
        ).strip()
        self._num_ctx = int(getattr(settings, "OLLAMA_NUM_CTX", 4096))
        self._timeout = int(settings.PLANNING_REPAIR_TIMEOUT_SECONDS or 120)

    # ── core chat ───────────────────────────────────────────────────────────

    async def _chat(
        self,
        system: str,
        user: str,
        timeout: Optional[int] = None,
    ) -> str:
        url = f"{self._base_url}/v1/chat/completions"
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
        effective_timeout = float(timeout or self._timeout)
        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return _strip_thinking(content)
        except httpx.TimeoutException as exc:
            logger.error(
                "[OLLAMA] Timeout after %.0fs calling %s", effective_timeout, url
            )
            raise AgentRuntimeError(
                f"Ollama timed out after {effective_timeout}s"
            ) from exc
        except httpx.HTTPStatusError as exc:
            logger.error(
                "[OLLAMA] HTTP %s: %s",
                exc.response.status_code,
                exc.response.text[:400],
            )
            raise AgentRuntimeError(f"Ollama HTTP {exc.response.status_code}") from exc
        except httpx.ConnectError as exc:
            logger.error("[OLLAMA] Cannot connect to %s", self._base_url)
            raise AgentRuntimeError(
                f"Cannot connect to Ollama at {self._base_url}"
            ) from exc
        except Exception as exc:
            logger.error("[OLLAMA] Unexpected error: %s", exc)
            raise AgentRuntimeError(str(exc)) from exc

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
        output = await self._chat(
            system=_STEP_SYSTEM,
            user=prompt,
            timeout=timeout_seconds,
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
        system = _PLAN_SYSTEM if session_prefix == "planning" else _GENERIC_SYSTEM
        output = await self._chat(system=system, user=prompt, timeout=timeout_seconds)
        return {
            "status": "completed",
            "output": output,
            "backend": self.backend_descriptor.name,
            "model_family": self._model,
        }

    async def execute_task_with_orchestration(
        self,
        prompt: str,
        timeout_seconds: int = 300,
        orchestration_state: Any = None,
    ) -> dict[str, Any]:
        """Used by orchestration step loop — route to planning or step execution."""
        project_context = ""
        if orchestration_state is not None:
            project_context = getattr(orchestration_state, "project_context", "") or ""

        is_planning = orchestration_state is not None and not getattr(
            orchestration_state, "plan", None
        )
        system = _PLAN_SYSTEM if is_planning else _STEP_SYSTEM
        user = f"{project_context}\n\n{prompt}".strip() if project_context else prompt

        output = await self._chat(system=system, user=user, timeout=timeout_seconds)
        return {"status": "completed", "output": output}

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
        return {
            "backend": self.backend_descriptor.name,
            "display_name": self.backend_descriptor.display_name,
            "implementation": self.backend_descriptor.implementation,
            "model_family": self._model,
            "agent_interface": self.describe_interface().to_dict(),
            "capabilities": self.backend_descriptor.capabilities.to_dict(),
        }

    def describe_interface(self) -> AgentInterfaceDescriptor:
        return AgentInterfaceDescriptor(
            backend=self.backend_descriptor.name,
            model_family=self._model,
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
                execution="single_retry_compact_prompt",
                completion="schema_first",
            ),
            context_window_policy=ContextWindowPolicy(
                max_input_tokens=self._num_ctx,
                overflow_strategy="truncate_and_retry",
                compaction_strategy="truncate_context",
            ),
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
) -> OllamaRuntime:
    return OllamaRuntime(db, session_id, task_id, use_demo_mode=use_demo_mode)
