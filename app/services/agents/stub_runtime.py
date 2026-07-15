"""Test-only runtime backends for backend contract tests."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.services.agents.interfaces import (
    AgentInterfaceDescriptor,
    ContextWindowPolicy,
    RetryStrategy,
    RuntimeBackendResult,
)
from app.services.agents.runtime_configuration import RoleRuntimeConfiguration


class StubRuntime:
    """Minimal test runtime. Not registered in the production backend registry."""

    def __init__(
        self,
        db: Session,
        session_id: Optional[int],
        task_id: Optional[int] = None,
        *,
        backend_id: str,
        use_demo_mode: Optional[bool] = None,
        runtime_configuration: RoleRuntimeConfiguration | None = None,
    ):
        self.db = db
        self.session_id = session_id
        self.task_id = task_id
        self.backend_id = backend_id
        self.use_demo_mode = use_demo_mode
        self.runtime_configuration = runtime_configuration
        self.backend_role = (
            runtime_configuration.role.value if runtime_configuration else None
        )
        self.backend_descriptor = None

    async def create_session(
        self, task_description: str, context: Optional[dict[str, Any]] = None
    ) -> str:
        return f"{self.backend_id}:session:{self.session_id or 'none'}"

    async def execute_task(
        self,
        prompt: str,
        timeout_seconds: int = 300,
        log_callback: Any = None,
        *,
        diagnostic_label: Optional[str] = None,
        diagnostic_metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if self.backend_id == "stub_capacity":
            return {
                "status": "failed",
                "exit_reason": "backend_at_capacity",
                "error": "backend_at_capacity",
                "failure_category": "backend_capacity_limit",
                "output": "",
            }
        return {
            "status": "completed",
            "exit_reason": "completed",
            "output": "stub_success completed",
            "files_changed": [],
        }

    async def pause_session(self) -> None:
        return None

    async def resume_session(self, checkpoint_name: Optional[str] = None) -> str:
        return "resumed"

    async def stop_session(self) -> None:
        return None

    async def get_session_context(self) -> dict[str, Any]:
        return {"runtime": self.backend_id}

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
        return await self.execute_task(prompt, timeout_seconds=timeout_seconds)

    def get_backend_metadata(self) -> dict[str, Any]:
        payload = {
            "backend": self.backend_id,
            "display_name": self.backend_id,
            "implementation": "test_only",
            "model_family": "stub",
            "adaptation_profile": "stub",
            "capabilities": {
                "supports_planning": True,
                "supports_step_execution": True,
                "supports_debug_repair": True,
            },
        }
        if self.runtime_configuration is not None:
            payload["role"] = self.backend_role
            payload["runtime_configuration"] = self.runtime_configuration.to_dict()
        return payload

    def describe_interface(self) -> AgentInterfaceDescriptor:
        return AgentInterfaceDescriptor(
            backend=self.backend_id,
            model_family="stub",
            planning_prompt_template="stub",
            execution_prompt_template="stub",
            prompt_dialect="stub",
            preferred_retry_strategy=RetryStrategy(
                planning="none",
                execution="none",
                completion="none",
            ),
            context_window_policy=ContextWindowPolicy(
                max_input_tokens=None,
                overflow_strategy="none",
                compaction_strategy="none",
            ),
        )

    def reports_context_overflow(self, result: Optional[dict[str, Any]]) -> bool:
        return False

    def normalize_execution_result(
        self,
        result: dict[str, Any],
        *,
        role: str = "execution",
        duration_seconds: float = 0.0,
    ) -> RuntimeBackendResult:
        success = str(result.get("status") or "").lower() in {
            "completed",
            "done",
            "success",
        }
        return RuntimeBackendResult(
            backend_id=self.backend_id,
            role=role,
            success=success,
            exit_reason=str(
                result.get("exit_reason")
                or result.get("error")
                or ("completed" if success else "execution_failed")
            ),
            output=str(result.get("output") or ""),
            duration_seconds=duration_seconds,
            failure_category=None if success else result.get("failure_category"),
        )


def create_stub_runtime(
    db: Session,
    session_id: Optional[int],
    task_id: Optional[int] = None,
    *,
    use_demo_mode: Optional[bool] = None,
    backend_id: str,
    runtime_configuration: RoleRuntimeConfiguration | None = None,
) -> StubRuntime:
    return StubRuntime(
        db,
        session_id,
        task_id,
        backend_id=backend_id,
        use_demo_mode=use_demo_mode,
        runtime_configuration=runtime_configuration,
    )
