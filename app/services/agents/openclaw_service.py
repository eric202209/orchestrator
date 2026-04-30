"""OpenClaw Session Service

Integration service for orchestrating AI development tasks via OpenClaw sessions.
Handles session lifecycle, tool execution tracking, and log streaming.
Implements multi-step orchestration workflow: PLANNING → EXECUTING → DEBUGGING → PLAN_REVISION → DONE

OPTIMIZATIONS:
- Reduced planning time through context caching and prompt optimization
- Reduced execution time by minimizing logging overhead
- Added streaming for better user experience
- Implemented request compression
- Enhanced error handling with intelligent recovery

"""

import json
import subprocess
import logging
import asyncio
import os
import shutil
import shlex
import time
import re
from typing import Optional, Dict, Any, List, Callable
from datetime import UTC, datetime
from pathlib import Path
from sqlalchemy.orm import Session
from app.models import Session as SessionModel, Task, TaskStatus, LogEntry, Project
from app.config import settings
from app.services.prompt_templates import (
    OrchestrationStatus,
    OrchestrationState,
    StepResult,
    PromptTemplates,
)
from app.services.agents.agent_backends import BackendDescriptor, get_backend_descriptor
from app.services.agents.interfaces import (
    AgentInterfaceDescriptor,
    AgentRuntimeError,
    ContextWindowPolicy,
    RetryStrategy,
)
from app.services.model_adaptation import resolve_adaptation_profile
from app.services.workspace.project_isolation_service import (
    ProjectIsolationService,
    resolve_project_workspace_path,
)
from app.services.orchestration.task_rules import (
    should_execute_in_canonical_project_root,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.validation.parsing import extract_structured_text
from app.services.orchestration.persistence import append_orchestration_event
from app.services.permission_service import PermissionApprovalService
from app.services.task_service import TaskService
from app.services.performance_optimizations import (
    optimize_prompt,
    compress_context,
    perf_tracker,
)
from app.services.workspace.checkpoint_service import CheckpointService, CheckpointError
from app.services.tool_tracking_service import ToolTrackingService
from app.services.workspace.system_settings import (
    get_effective_agent_backend,
    get_effective_agent_model_family,
)

logger = logging.getLogger(__name__)


_NOISY_OPENCLAW_STDERR_PATTERNS = (
    re.compile(r"^[\[\]{}],?$"),
    re.compile(r'^"payloads":\s*\[$'),
    re.compile(r'^"text":\s*".*",?$'),
    re.compile(r'^"mediaUrl":\s*(null|".*"),?$'),
    re.compile(r'^"meta":\s*{$'),
    re.compile(r'^"durationMs":\s*\d+,?$'),
    re.compile(r'^"agentMeta":\s*{$'),
    re.compile(r'^"sessionId":\s*"[^"]+",?$'),
    re.compile(r'^"sessionKey":\s*"[^"]+",?$'),
    re.compile(r'^"provider":\s*"[^"]+",?$'),
    re.compile(r'^"model":\s*"[^"]+",?$'),
    re.compile(r'^"lastCallUsage":\s*{$'),
    re.compile(r'^"input":\s*\d+,?$'),
    re.compile(r'^"output":\s*\d+,?$'),
    re.compile(r'^"cacheRead":\s*\d+,?$'),
    re.compile(r'^"cacheWrite":\s*\d+,?$'),
    re.compile(r'^"listChars":\s*\d+,?$'),
    re.compile(r'^"tools":\s*{$'),
    re.compile(r'^"propertiesCount":\s*\d+,?$'),
    re.compile(r'^"schemaChars":\s*\d+,?$'),
    re.compile(r'^"summaryChars":\s*\d+,?$'),
    re.compile(r'^"promptChars":\s*\d+,?$'),
    re.compile(r'^"blockChars":\s*\d+,?$'),
    re.compile(r'^"rawChars":\s*\d+,?$'),
    re.compile(r'^"injectedChars":\s*\d+,?$'),
    re.compile(r'^"truncated":\s*(true|false),?$'),
    re.compile(r'^"missing":\s*(true|false),?$'),
    re.compile(r'^"replayInvalid":\s*(true|false),?$'),
    re.compile(r'^"livenessState":\s*"[^"]+",?$'),
    re.compile(r'^"stopReason":\s*"[^"]+",?$'),
    re.compile(r'^"path":\s*".*",?$'),
    re.compile(r'^"name":\s*"[^"]+",?$'),
    re.compile(r'^"entries":\s*\[$'),
    re.compile(r'^"skills":\s*{$'),
)


class OpenClawSessionError(AgentRuntimeError):
    """Custom exception for OpenClaw session errors"""

    pass


class OpenClawSessionService:
    """Service for managing OpenClaw session orchestration"""

    MAX_PROMPT_LENGTH = 50000  # Leave room for model overhead
    STREAM_READ_LIMIT = 262144  # Allow large JSON/log lines from newer OpenClaw builds

    def __init__(
        self,
        db: Session,
        session_id: int,
        task_id: Optional[int] = None,
        use_demo_mode: Optional[bool] = None,
    ):
        self.db = db
        self.session_id = session_id
        self.task_id = task_id
        # Use config value if not explicitly provided
        self.use_demo_mode = (
            use_demo_mode if use_demo_mode is not None else settings.DEMO_MODE
        )
        self.session_model = (
            db.query(SessionModel).filter(SessionModel.id == session_id).first()
        )
        self.task_model = (
            db.query(Task).filter(Task.id == task_id).first() if task_id else None
        )
        self._safety_prompt_injected = False
        self.openclaw_session_key: Optional[str] = None
        self._task_session_id: Optional[str] = None
        self.process: Optional[subprocess.Popen] = None
        self.backend_descriptor: BackendDescriptor = get_backend_descriptor(
            get_effective_agent_backend(settings.ORCHESTRATOR_AGENT_BACKEND, db=db)
        )
        # Initialize checkpoint service
        from app.services.workspace.checkpoint_service import CheckpointService

        self.checkpoint_service = CheckpointService(db)

    def get_backend_metadata(self) -> Dict[str, Any]:
        """Return normalized backend metadata for logs, APIs, and orchestration."""

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
                "shell": True,
                "filesystem": True,
                "checkpoint_resume": bool(
                    self.backend_descriptor.capabilities.supports_checkpoint_resume
                ),
                "streaming": bool(
                    self.backend_descriptor.capabilities.supports_streaming
                ),
            },
            tool_shape=profile.tool_shape,
            preferred_retry_strategy=RetryStrategy(
                planning="minimal_prompt_then_repair",
                execution="compact_prompt_then_debug",
                completion="repair_step_then_revalidate",
            ),
            context_window_policy=ContextWindowPolicy(
                max_input_tokens=self.backend_descriptor.capabilities.max_context_tokens,
                overflow_strategy="retry_compact",
                compaction_strategy=profile.context_window_policy,
            ),
        )

    def build_cli_agent_command(
        self,
        prompt: str,
        *,
        source_brain: str = "local",
        timeout_seconds: int = 180,
        session_prefix: str = "planning",
    ) -> List[str]:
        """Build a one-shot CLI agent command for synchronous planning-style tasks."""

        cmd = self._resolve_openclaw_command()
        runtime_session_key = f"{session_prefix}-{int(time.time())}"
        full_cmd = [*cmd, "agent"]
        if source_brain == "local":
            full_cmd.append("--local")
        full_cmd.extend(
            [
                "--session-id",
                runtime_session_key,
                "--message",
                prompt,
                "--json",
                "--timeout",
                str(timeout_seconds),
            ]
        )
        return full_cmd

    def parse_cli_response(
        self, proc: subprocess.CompletedProcess[str]
    ) -> Dict[str, Any]:
        """Normalize subprocess output into the same payload shape as streaming execution."""

        return self._parse_openclaw_response(proc)

    def reports_context_overflow(self, result: Optional[Dict[str, Any]]) -> bool:
        """Detect overflow errors for the local OpenClaw CLI/runtime."""

        return self._is_context_overflow_result(result)

    def _resolve_openclaw_command(self) -> List[str]:
        """Resolve the OpenClaw CLI command with fallback locations."""
        configured_args = shlex.split((settings.OPENCLAW_CLI_ARGS or "").strip())
        configured_path = (settings.OPENCLAW_CLI_PATH or "").strip()
        candidates: List[str] = []

        if configured_path:
            candidates.append(configured_path)

        detected_path = shutil.which("openclaw")
        if detected_path:
            candidates.append(detected_path)

        candidates.extend(
            [
                "/usr/local/bin/openclaw",
                "/usr/bin/openclaw",
                str(Path.home() / ".local" / "bin" / "openclaw"),
                "/root/.local/bin/openclaw",
            ]
        )

        for candidate in dict.fromkeys(candidates):
            if (
                candidate
                and os.path.isfile(candidate)
                and os.access(candidate, os.X_OK)
            ):
                return [candidate, *configured_args]

        node_executable = shutil.which("node")
        node_entrypoints = [
            "/opt/openclaw/dist/index.js",
            "/root/.openclaw/app/dist/index.js",
        ]
        for entrypoint in node_entrypoints:
            if node_executable and os.path.isfile(entrypoint):
                return [node_executable, entrypoint, *configured_args]

        raise OpenClawSessionError(
            "OpenClaw CLI not found. Install `openclaw`, add it to PATH, set "
            "`OPENCLAW_CLI_PATH`, or configure `OPENCLAW_CLI_ARGS` for a Node entrypoint."
        )

    def _resolve_execution_cwd(self) -> Optional[str]:
        """Resolve the best working directory for OpenClaw subprocess execution."""
        try:
            project_model = None
            if self.session_model and self.session_model.project_id:
                project_model = (
                    self.db.query(Project)
                    .filter(Project.id == self.session_model.project_id)
                    .first()
                )
            elif self.task_model and self.task_model.project_id:
                project_model = (
                    self.db.query(Project)
                    .filter(Project.id == self.task_model.project_id)
                    .first()
                )

            if not project_model:
                return None

            project_workspace = resolve_project_workspace_path(
                project_model.workspace_path, project_model.name
            )

            if self.task_model and should_execute_in_canonical_project_root(
                self.task_model,
                getattr(self.task_model, "execution_profile", None),
                getattr(self.task_model, "title", None),
                getattr(self.task_model, "description", None),
            ):
                return str(project_workspace.resolve())

            if self.task_model and self.task_model.task_subfolder:
                return str(
                    (project_workspace / self.task_model.task_subfolder).resolve()
                )

            return str(project_workspace.resolve())
        except Exception as exc:
            self._log_entry(
                "WARN",
                f"[OPENCLAW] Failed to resolve execution cwd, falling back to default: {exc}",
            )
            return None

    def _append_runtime_event(
        self, event_type: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        """Best-effort typed runtime event append for local OpenClaw flows."""

        if not self.task_model:
            return

        project_dir = self._resolve_execution_cwd()
        if not project_dir:
            return
        if (
            self.task_model
            and not should_execute_in_canonical_project_root(
                self.task_model,
                getattr(self.task_model, "execution_profile", None),
                getattr(self.task_model, "title", None),
                getattr(self.task_model, "description", None),
            )
            and getattr(self.task_model, "task_subfolder", None)
        ):
            project_dir = str(
                (Path(project_dir) / str(self.task_model.task_subfolder)).resolve()
            )

        try:
            append_orchestration_event(
                project_dir=project_dir,
                session_id=self.session_id,
                task_id=self.task_model.id,
                event_type=event_type,
                details=details or {},
            )
        except Exception:
            pass

    @staticmethod
    def _should_emit_stderr_line(line_text: str) -> bool:
        """Hide raw OpenClaw JSON telemetry from live logs while keeping it in buffers."""

        trimmed = (line_text or "").strip()
        if not trimmed:
            return False

        if (
            '"propertiesCount"' in trimmed
            or '"schemaChars"' in trimmed
            or '"summaryChars"' in trimmed
            or '"promptChars"' in trimmed
            or '"blockChars"' in trimmed
            or '"rawChars"' in trimmed
            or '"injectedChars"' in trimmed
            or '"bootstrapTotalMaxChars"' in trimmed
            or '"bootstrapTruncation"' in trimmed
            or '"systemPromptReport"' in trimmed
            or '"injectedWorkspaceFiles"' in trimmed
            or '"payloads"' in trimmed
            or '"mediaUrl"' in trimmed
            or '"agentMeta"' in trimmed
            or '"durationMs"' in trimmed
            or '"lastCallUsage"' in trimmed
            or '"cacheRead"' in trimmed
            or '"cacheWrite"' in trimmed
            or '"listChars"' in trimmed
            or '"tools"' in trimmed
            or '"replayInvalid"' in trimmed
            or '"livenessState"' in trimmed
            or '"stopReason"' in trimmed
        ):
            return False

        return not any(
            pattern.match(trimmed) for pattern in _NOISY_OPENCLAW_STDERR_PATTERNS
        )

    async def create_session(
        self, task_description: str, context: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Create a new OpenClaw session for task execution

        Args:
            task_description: Description of the task to execute
            context: Additional context (previous logs, project info, etc.)

        Returns:
            OpenClaw session key

        Raises:
            OpenClawSessionError: If session creation fails
        """
        try:
            # Log session creation (commit immediately so WS stream shows it)
            self._log_entry(
                "INFO",
                f"Creating OpenClaw session for task: {task_description[:100]}",
                commit=True,
            )

            # Use the main OpenClaw session that already exists
            self.openclaw_session_key = "agent:main:main"

            # Generate a stable session ID for this task run; all steps share it
            # so the AI agent retains context across the entire task lifecycle.
            task_id_str = str(self.task_id or self.session_id)
            self._task_session_id = (
                f"orchestrator-task-{task_id_str}-{int(time.time())}"
            )

            self._log_entry(
                "INFO",
                f"✅ OpenClaw session set to: {self.openclaw_session_key}",
                commit=True,
            )

            return self.openclaw_session_key

        except Exception as e:
            error_msg = f"Failed to create OpenClaw session: {str(e)}"
            self._log_entry("ERROR", error_msg)
            raise OpenClawSessionError(error_msg)

    async def create_openclaw_session(
        self, task_description: str, context: Optional[Dict[str, Any]] = None
    ) -> str:
        """Backward-compatible alias for older callers."""

        return await self.create_session(task_description, context=context)

    async def execute_task(
        self,
        prompt: str,
        timeout_seconds: int = 300,
        log_callback: Optional[Callable[..., None]] = None,
    ) -> Dict[str, Any]:
        """
        Execute a task via OpenClaw session (legacy single-mode)

        OPTIMIZATIONS:
        - Optimize prompt to reduce planning time
        - Compress context to reduce token usage
        - Track performance metrics

        Args:
            prompt: The prompt/task to execute
            timeout_seconds: Maximum execution time
            log_callback: Optional callback for real-time log streaming

        Returns:
            Execution result with logs and status

        Raises:
            OpenClawSessionError: If execution fails
        """
        try:
            # OPTIMIZATION: Track start time
            perf_tracker.start("execute_task")
            start_time = time.time()

            # OPTIMIZATION: Optimize prompt to reduce planning time
            optimized_prompt = optimize_prompt(prompt, max_tokens=25000)

            # Check if we should use demo mode or real execution
            if self.use_demo_mode:
                # DEMO MODE: Return mock logs (for UI testing)
                result = await self._execute_demo_mode(optimized_prompt)
                # Demo mode always completes successfully (by design)
                result["status"] = "completed"
            else:
                # REAL MODE: Execute task via OpenClaw HTTP API
                result = await self.execute_task_with_streaming(
                    optimized_prompt, timeout_seconds, log_callback
                )
                if self._is_context_overflow_result(result):
                    retry_prompt = optimize_prompt(
                        prompt,
                        max_tokens=700,
                        hard_char_limit=1800,
                    )
                    if retry_prompt != optimized_prompt:
                        self._log_entry(
                            "WARN",
                            "[OPENCLAW] Context overflow detected; retrying once with a compact prompt",
                        )
                        result = await self.execute_task_with_streaming(
                            retry_prompt, timeout_seconds, log_callback
                        )

            # OPTIMIZATION: Log performance metrics
            duration = time.time() - start_time
            self._log_entry(
                "INFO",
                f"[PERFORMANCE] Task executed in {duration:.2f}s (optimized prompt)",
            )

            result_status = result.get("status")
            if result_status == "completed":
                self._log_entry(
                    "INFO",
                    "[OPENCLAW] Request returned output; awaiting orchestration validation",
                )
            elif result_status == "failed":
                self._log_entry(
                    "ERROR",
                    f"[OPENCLAW] Request failed before orchestration validation: "
                    f"{result.get('error', 'Execution failed')}",
                )
            else:
                self._log_entry(
                    "WARNING",
                    f"[OPENCLAW] Request returned unexpected status before orchestration validation: "
                    f"{result_status or 'unknown'}",
                )

            result.setdefault("backend", self.backend_descriptor.name)
            result.setdefault("model_family", settings.ORCHESTRATOR_AGENT_MODEL_FAMILY)
            result.setdefault(
                "backend_capabilities",
                self.backend_descriptor.capabilities.to_dict(),
            )
            return result

        except Exception as e:
            error_msg = f"Task execution failed: {str(e)}"
            self._log_entry("ERROR", error_msg)

            raise OpenClawSessionError(error_msg)

    @staticmethod
    def _is_context_overflow_result(result: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(result, dict):
            return False

        candidates = [
            str(result.get("error") or ""),
            str(result.get("output") or ""),
        ]
        lowered = "\n".join(candidates).lower()
        markers = (
            "context window exceeded",
            "context size has been exceeded",
            "context length exceeded",
            "prompt is too long for the model",
        )
        return any(marker in lowered for marker in markers)

    async def _check_and_request_permission(
        self,
        operation_type: str,
        target_path: Optional[str] = None,
        command: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        """
        Check if permission is required and request if needed

        Args:
            operation_type: Type of operation
            target_path: Target path
            command: Shell command
            description: User-friendly description

        Returns:
            True if permission granted or not required
        """
        if not self.session_model or not self.session_model.project_id:
            return True  # No project context, skip permission check

        try:
            permission_service = PermissionApprovalService(self.db)

            # Check if permission is required
            if not permission_service.check_permission_required(
                operation_type, target_path
            ):
                self._log_entry(
                    "INFO", f"Permission not required for: {operation_type}"
                )
                return True

            # Check if already granted
            if permission_service.is_permission_granted(
                self.session_model.project_id,
                operation_type,
                target_path or "",
                self.session_model.id,
            ):
                self._log_entry(
                    "INFO", f"Permission already granted for: {operation_type}"
                )
                return True

            # Request permission
            self._log_entry(
                "WARN",
                f"Permission required for: {operation_type} on {target_path or command}",
            )

            permission = permission_service.create_permission_request(
                project_id=self.session_model.project_id,
                session_id=self.session_model.id,
                task_id=self.task_model.id if self.task_model else None,
                operation_type=operation_type,
                target_path=target_path,
                command=command,
                description=description,
                expires_in_minutes=30,
            )

            self._log_entry(
                "INFO",
                f"Permission request created: {permission.id}, waiting for approval",
            )
            self._append_runtime_event(
                EventType.WAITING_FOR_INPUT,
                {
                    "kind": "permission_request",
                    "permission_request_id": permission.id,
                    "operation_type": operation_type,
                    "target_path": target_path,
                    "command": command,
                    "description": description,
                },
            )

            # Return False to indicate permission is pending
            return False

        except (ValueError, TypeError, AttributeError) as e:
            self._log_entry(
                "ERROR",
                f"Permission check failed and operation was allowed: {str(e)}",
            )
            # Fail open - allow operation if permission system fails
            return True

    async def _execute_task_with_permission_check(
        self,
        prompt: str,
        operation_type: str,
        target_path: Optional[str] = None,
        command: Optional[str] = None,
        timeout_seconds: int = 300,
    ) -> Dict[str, Any]:
        """
        Execute task with permission check

        Args:
            prompt: Task prompt
            operation_type: Operation type
            target_path: Target path
            command: Shell command
            timeout_seconds: Timeout

        Returns:
            Execution result
        """
        # Check permission first
        permission_granted = await self._check_and_request_permission(
            operation_type=operation_type,
            target_path=target_path,
            command=command,
            description=f"Execute: {prompt[:100]}...",
        )

        if not permission_granted:
            # Permission pending - return error
            raise OpenClawSessionError(
                f"Permission required for {operation_type}. "
                f"Please approve in the UI or use demo mode."
            )

        # Permission granted, execute task
        return await self.execute_task(prompt, timeout_seconds)

    async def execute_task_with_orchestration(
        self,
        prompt: str,
        timeout_seconds: int = 300,
        orchestration_state: Optional[OrchestrationState] = None,
    ) -> Dict[str, Any]:
        """
        Execute a task with multi-step orchestration workflow

        OPTIMIZATIONS:
        - Compress project context to reduce token usage
        - Optimize planning prompt for faster execution
        - Reduce logging overhead during orchestration

        Workflow:
        1. PLANNING → Generate step plan
        2. EXECUTING → Execute each step
        3. DEBUGGING → Fix failed steps
        4. PLAN_REVISION → Revise plan if needed
        5. DONE → Summarize completion

        Args:
            prompt: Task prompt
            timeout_seconds: Maximum execution time
            orchestration_state: Orchestration state to track workflow

        Returns:
            Execution result with orchestration state
        """
        try:
            # OPTIMIZATION: Compress context to reduce token usage
            project_context = ""
            task_service = TaskService(self.db)
            if self.session_model and self.session_model.project_id:
                try:
                    isolation_service = ProjectIsolationService(self.db)
                    safety_prompt = isolation_service.get_safety_prompt(
                        self.session_model.project_id
                    )
                    prompt = f"{safety_prompt}\n\n{prompt}"
                    # OPTIMIZATION: Only log safety prompt injection once
                    if not self._safety_prompt_injected:
                        self._log_entry(
                            "INFO", "Project isolation safety prompt injected"
                        )
                        self._safety_prompt_injected = True
                except Exception as e:
                    self._log_entry("WARN", f"Failed to inject safety prompt: {str(e)}")

            # OPTIMIZATION: Reduced logging overhead
            self._log_entry(
                "INFO", f"[ORCHESTRATION] Starting optimization: {prompt[:80]}..."
            )

            project_model = None
            if self.session_model and self.session_model.project_id:
                project_model = (
                    self.db.query(Project)
                    .filter(Project.id == self.session_model.project_id)
                    .first()
                )
            elif self.task_model and self.task_model.project_id:
                project_model = (
                    self.db.query(Project)
                    .filter(Project.id == self.task_model.project_id)
                    .first()
                )

            if orchestration_state is None:
                orchestration_state = OrchestrationState(
                    session_id=str(self.session_id),
                    task_description=prompt,
                    project_name=(
                        project_model.name
                        if project_model
                        else (
                            self.session_model.name if self.session_model else "Unknown"
                        )
                    ),
                    project_context=project_context,
                    task_id=self.task_model.id if self.task_model else None,
                )

            if project_model and project_model.workspace_path:
                orchestration_state._workspace_path_override = str(
                    resolve_project_workspace_path(
                        project_model.workspace_path, project_model.name
                    )
                )

            runs_in_canonical_baseline = bool(
                project_model
                and self.task_model
                and should_execute_in_canonical_project_root(
                    self.task_model,
                    getattr(self.task_model, "execution_profile", None),
                    getattr(self.task_model, "title", None),
                    getattr(self.task_model, "description", None),
                )
            )

            if self.task_model and self.task_model.task_subfolder:
                orchestration_state._task_subfolder_override = (
                    self.task_model.task_subfolder
                )

            if runs_in_canonical_baseline and project_model:
                canonical_baseline_dir = task_service.get_project_baseline_dir(
                    project_model
                )
                canonical_baseline_dir.mkdir(parents=True, exist_ok=True)
                orchestration_state._project_dir_override = str(canonical_baseline_dir)

            os.makedirs(orchestration_state.project_dir, exist_ok=True)

            if project_model and self.task_model:
                hydration_result = (
                    task_service.hydrate_task_workspace(
                        project=project_model,
                        current_task=self.task_model,
                        target_dir=orchestration_state.project_dir,
                    )
                    if not runs_in_canonical_baseline
                    else {"hydrated": False, "source_tasks": [], "files_copied": 0}
                )
                project_context = task_service.build_project_execution_context(
                    project=project_model,
                    current_task=self.task_model,
                    max_chars=4000,
                )
                if hydration_result.get("hydrated"):
                    hydrated_sources = ", ".join(
                        f"#{item.get('task_id')} {item.get('title')}"
                        for item in hydration_result.get("source_tasks", [])[:6]
                    )
                    project_context = (
                        f"{project_context}\n"
                        f"Hydrated baseline sources available directly in this workspace: {hydrated_sources}"
                    )[:5000]
                    self._log_entry(
                        "INFO",
                        (
                            f"[ORCHESTRATION] Hydrated workspace with {hydration_result.get('files_copied', 0)} "
                            "files from completed/promoted prior tasks"
                        ),
                    )
                orchestration_state.project_context = project_context
            elif self.session_model:
                project_context = (
                    compress_context(
                        {"description": self.session_model.description or ""}
                    ).get("description", "")[:2000]
                    if self.session_model
                    else ""
                )
                orchestration_state.project_context = project_context

            # Phase 1: PLANNING (OPTIMIZED)
            orchestration_state.status = OrchestrationStatus.PLANNING
            self._log_entry("INFO", "[ORCHESTRATION] PLANNING phase")

            if (
                self.task_model
                and self.task_model.steps
                and not orchestration_state.plan
            ):
                try:
                    stored_plan_payload = json.loads(self.task_model.steps)
                    from app.services.orchestration.validation.parsing import (
                        extract_plan_steps,
                    )

                    stored_plan = extract_plan_steps(stored_plan_payload)
                    if stored_plan:
                        orchestration_state.plan = stored_plan
                        self._log_entry(
                            "INFO",
                            f"[ORCHESTRATION] Reusing saved plan with {len(stored_plan)} steps",
                        )
                except Exception as stored_plan_error:
                    self._log_entry(
                        "WARN",
                        f"[ORCHESTRATION] Failed to reuse saved task plan: {stored_plan_error}",
                    )

            if not orchestration_state.plan:
                # OPTIMIZATION: Compress project context in planning prompt
                planning_prompt = PromptTemplates.build_planning_prompt(
                    task_description=prompt,
                    project_context=(
                        orchestration_state.project_context[:4000]
                        if orchestration_state.project_context
                        else ""
                    ),
                    execution_profile=(
                        getattr(self.task_model, "execution_profile", None)
                        or getattr(
                            self.session_model, "default_execution_profile", None
                        )
                        or "full_lifecycle"
                    ),
                )

                # OPTIMIZATION: Increased timeout for planning (180s to avoid timeouts on complex tasks)
                planning_result = await self.execute_task(
                    planning_prompt, timeout_seconds=180
                )

                if planning_result.get("status") == "failed":
                    planning_error = planning_result.get(
                        "error", "Planning failed during OpenClaw execution"
                    )
                    self._log_entry(
                        "ERROR", f"[ORCHESTRATION] Planning failed: {planning_error}"
                    )
                    raise OpenClawSessionError(planning_error)

                # Parse plan from result
                try:
                    output_text = planning_result.get("output", "[]")

                    # OpenClaw returns: { "payloads": [ { "text": "..." } ] }
                    # Extract the actual text content
                    if isinstance(output_text, str):
                        try:
                            output_data = json.loads(output_text)
                            if (
                                isinstance(output_data, dict)
                                and "payloads" in output_data
                            ):
                                payloads = output_data.get("payloads", [])
                                if isinstance(payloads, list) and len(payloads) > 0:
                                    first_payload = payloads[0]
                                    if isinstance(first_payload, dict):
                                        output_text = first_payload.get(
                                            "text", output_text
                                        )
                        except json.JSONDecodeError:
                            pass

                    # Strip Markdown code fences if present
                    if isinstance(output_text, str):
                        import re

                        markdown_pattern = r"^\s*```(?:json)?\s*|\s*```$"
                        output_text = re.sub(markdown_pattern, "", output_text.strip())

                    self._log_entry(
                        "INFO",
                        f"[PLANNING] Output type: {type(output_text)}, content: {output_text[:200]}...",
                    )
                    plan = json.loads(output_text)
                    if isinstance(plan, list):
                        orchestration_state.plan = plan
                        self._log_entry(
                            "INFO", f"[ORCHESTRATION] Generated {len(plan)} steps"
                        )
                    else:
                        self._log_entry(
                            "WARN",
                            "[ORCHESTRATION] Planning output was not a step list; aborting instead of executing raw prompt",
                        )
                        raise OpenClawSessionError(
                            "Planning output was not a valid step list"
                        )
                except json.JSONDecodeError:
                    self._log_entry(
                        "WARN",
                        "[ORCHESTRATION] Planning output was not valid JSON; aborting instead of executing raw prompt",
                    )
                    raise OpenClawSessionError("Planning output was not valid JSON")

            # Phase 2: EXECUTING
            orchestration_state.status = OrchestrationStatus.EXECUTING
            self._log_entry("INFO", "[ORCHESTRATION] Starting EXECUTING phase")

            max_retries = 3
            for step_index, step in enumerate(orchestration_state.plan):
                self._log_entry(
                    "INFO",
                    f"[ORCHESTRATION] Executing step {step_index + 1}/{len(orchestration_state.plan)}",
                )

                execution_result = await self._execute_step_with_retry(
                    step, step_index, orchestration_state, max_retries
                )

                if execution_result.status == "failed":
                    # Phase 3: DEBUGGING
                    orchestration_state.status = OrchestrationStatus.DEBUGGING
                    self._log_entry("INFO", "[ORCHESTRATION] Starting DEBUGGING phase")

                    debug_result = await self._debug_step(
                        step, step_index, orchestration_state, execution_result
                    )

                    if debug_result.get("fix_type") == "revise_plan":
                        # Phase 4: PLAN_REVISION
                        orchestration_state.status = OrchestrationStatus.REVISING_PLAN
                        self._log_entry(
                            "INFO", "[ORCHESTRATION] Starting PLAN_REVISION phase"
                        )

                        revised_plan = await self._revise_plan(
                            orchestration_state, debug_result
                        )

                        # Continue with revised plan
                        orchestration_state.plan = revised_plan
                        orchestration_state.current_step_index = step_index
                        continue

                    debug_analysis = debug_result.get("analysis", "Unknown failure")
                    self._log_entry(
                        "ERROR",
                        f"[ORCHESTRATION] Step {step_index + 1} failed permanently: {debug_analysis}",
                    )
                    raise OpenClawSessionError(
                        f"Step {step_index + 1} failed after {max_retries} attempts: {execution_result.error_message or debug_analysis}"
                    )

            # Phase 5: DONE
            orchestration_state.status = OrchestrationStatus.DONE
            self._log_entry("INFO", "[ORCHESTRATION] Execution steps completed")

            # Generate summary using the summary template
            execution_results_summary = orchestration_state.prior_results_summary()
            summary_prompt = PromptTemplates.build_task_summary(
                task_description=prompt,
                plan_summary=json.dumps(orchestration_state.plan, indent=2)[:500],
                execution_results_summary=execution_results_summary,
                changed_files=orchestration_state.changed_files,
                num_debug_attempts=len(orchestration_state.debug_attempts),
                final_status="completed",
                execution_profile=(
                    getattr(self.task_model, "execution_profile", None)
                    or getattr(self.session_model, "default_execution_profile", None)
                    or "full_lifecycle"
                ),
            )

            self._log_entry("INFO", "[ORCHESTRATION] Generating summary...")
            summary_result = await self.execute_task(summary_prompt, timeout_seconds=60)

            if summary_result.get("status") == "failed":
                summary_error = summary_result.get(
                    "error", "Summary generation failed during OpenClaw execution"
                )
                self._log_entry(
                    "ERROR", f"[ORCHESTRATION] Summary failed: {summary_error}"
                )
                raise OpenClawSessionError(summary_error)

            self._log_entry(
                "INFO", f"[ORCHESTRATION] Summary result type: {type(summary_result)}"
            )
            if isinstance(summary_result, str):
                self._log_entry(
                    "ERROR",
                    f"[ORCHESTRATION] Summary result is string, not dict! Content: {summary_result[:200]}",
                )
                raise OpenClawSessionError(
                    f"Summary result is not a dict: {type(summary_result)}"
                )

            return {
                "status": "completed",
                "mode": "orchestration",
                "output": summary_result.get("output", "Task completed"),
                "backend": self.backend_descriptor.name,
                "model_family": settings.ORCHESTRATOR_AGENT_MODEL_FAMILY,
                "orchestration_state": {
                    "status": orchestration_state.status.value,
                    "plan_length": len(orchestration_state.plan),
                    "steps_completed": len(orchestration_state.execution_results),
                    "debug_attempts": len(orchestration_state.debug_attempts),
                    "backend": self.backend_descriptor.name,
                    "backend_capabilities": self.backend_descriptor.capabilities.to_dict(),
                },
            }

        except Exception as e:
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = str(e)
            self._log_entry("ERROR", f"[ORCHESTRATION] Failed: {str(e)}")
            if isinstance(e, OpenClawSessionError):
                raise
            raise OpenClawSessionError(f"Orchestration failed: {str(e)}")

    async def _execute_step_with_retry(
        self,
        step: Dict[str, Any],
        step_index: int,
        orchestration_state: OrchestrationState,
        max_retries: int = 3,
    ) -> StepResult:
        """Execute a single step with retry logic and timeout protection"""

        step_description = step.get("description", "Unknown step")
        step_commands = step.get("commands", [])

        self._log_entry("INFO", f"[STEP] Executing: {step_description[:100]}...")

        for attempt in range(max_retries):
            try:
                # Build execution prompt (optimized - no redundant context)
                execution_prompt = PromptTemplates.build_execution_prompt(
                    step_description=step_description,
                    step_commands=step_commands,
                    project_dir=str(orchestration_state.project_dir),
                    verification_command=step.get("verification"),
                    rollback_command=step.get("rollback"),
                    expected_files=step.get("expected_files", []),
                    completed_steps_summary=orchestration_state.prior_results_summary(),
                    project_context=orchestration_state.project_context,
                    execution_profile=(
                        getattr(self.task_model, "execution_profile", None)
                        or getattr(
                            self.session_model, "default_execution_profile", None
                        )
                        or "full_lifecycle"
                    ),
                )

                # OPTIMIZATION: Enforce strict timeout per attempt (60s max)
                result = await self.execute_task(
                    execution_prompt, timeout_seconds=min(60, 180 // max_retries)
                )

                # Check if successful
                is_success = result.get("status") == "completed"

                step_result = StepResult(
                    step_number=step_index + 1,
                    status="success" if is_success else "failed",
                    output=result.get("output", ""),
                    verification_output=result.get("verification_output", ""),
                    error_message=result.get("error", "") if not is_success else "",
                    attempt=attempt + 1,
                )

                if is_success:
                    orchestration_state.record_success(step_result)
                    self._log_entry(
                        "INFO", f"[STEP] Step {step_index + 1} completed successfully"
                    )
                    return step_result
                else:
                    orchestration_state.record_failure(step_result)
                    self._log_entry(
                        "WARN",
                        f"[STEP] Step {step_index + 1} failed (attempt {attempt + 1}/{max_retries})",
                    )

            except OpenClawSessionError as e:
                # Handle timeout errors specifically
                if "timed out" in str(e).lower():
                    orchestration_state.record_failure(
                        StepResult(
                            step_number=step_index + 1,
                            status="failed",
                            error_message=f"Timeout after {60}s (attempt {attempt + 1}/{max_retries})",
                            attempt=attempt + 1,
                        )
                    )
                    self._log_entry(
                        "WARN", f"[STEP] Step {step_index + 1} timed out, retrying..."
                    )
                else:
                    # Other errors - don't retry
                    orchestration_state.record_failure(
                        StepResult(
                            step_number=step_index + 1,
                            status="failed",
                            error_message=str(e),
                            attempt=attempt + 1,
                        )
                    )
                    raise

            except Exception as e:
                # Handle garbled errors without retrying
                orchestration_state.record_failure(
                    StepResult(
                        step_number=step_index + 1,
                        status="failed",
                        error_message=str(e)[:500],
                        attempt=attempt + 1,
                    )
                )
                self._log_entry(
                    "ERROR", f"[STEP] Step {step_index + 1} error: {str(e)}"
                )

        # All retries failed - return final failure result
        return StepResult(
            step_number=step_index + 1,
            status="failed",
            error_message=f"All {max_retries} attempts failed (timeout protection enabled)",
            attempt=max_retries,
        )

    async def _debug_step(
        self,
        step: Dict[str, Any],
        step_index: int,
        orchestration_state: OrchestrationState,
        failed_result: StepResult,
    ) -> Dict[str, Any]:
        """Debug a failed step"""
        self._log_entry("INFO", f"[DEBUG] Analyzing failure for step {step_index + 1}")

        # Build debugging prompt
        debugging_prompt = PromptTemplates.build_debugging_prompt(
            step_description=step.get("description", "Unknown step"),
            error_message=failed_result.error_message,
            command_output=failed_result.output[:2000],
            verification_output=failed_result.verification_output,
            attempt_number=failed_result.attempt,
            max_attempts=3,
            prior_debug_attempts=orchestration_state.debug_attempts,
            project_name=(
                self.task_model.project.name
                if self.task_model and self.task_model.project
                else (self.session_model.name if self.session_model else "")
            ),
            workspace_root=str(orchestration_state.workspace_root),
            project_dir=str(orchestration_state.project_dir),
        )

        # Execute debugging
        debug_result = await self.execute_task(debugging_prompt, timeout_seconds=120)

        # Parse fix type
        try:
            fix_data = json.loads(debug_result.get("output", "{}"))
            return {
                "fix_type": fix_data.get("fix_type", "code_fix"),
                "analysis": fix_data.get("analysis", "Unknown"),
                "fix": fix_data.get("fix", ""),
            }
        except json.JSONDecodeError:
            return {
                "fix_type": "code_fix",
                "analysis": "Failed to parse debug result",
                "fix": debug_result.get("output", ""),
            }

    async def _revise_plan(
        self, orchestration_state: OrchestrationState, debug_result: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Revise the plan based on debug analysis"""
        self._log_entry("INFO", "[PLAN_REVISION] Revising plan")

        # Build revision prompt using PLAN_REVISION template
        failed_steps = [
            StepResult(
                step_number=orchestration_state.current_step_index + 1,
                status="failed",
                error_message=debug_result.get("analysis", "Unknown error"),
            )
        ]
        revision_prompt = PromptTemplates.build_plan_revision_prompt(
            original_plan=orchestration_state.plan,
            failed_steps=failed_steps,
            debug_analysis=debug_result.get("analysis", "Unknown error"),
            completed_steps=orchestration_state.completed_steps,
            workspace_root=str(orchestration_state.workspace_root),
            project_dir=str(orchestration_state.project_dir),
        )

        # Execute revision
        revision_result = await self.execute_task(revision_prompt, timeout_seconds=180)

        # Parse revised plan
        try:
            revised_plan = json.loads(revision_result.get("output", "[]"))
            if isinstance(revised_plan, list):
                self._log_entry(
                    "INFO", f"[PLAN_REVISION] Revised to {len(revised_plan)} steps"
                )
                return revised_plan
        except json.JSONDecodeError:
            pass

        return orchestration_state.plan

    async def _execute_demo_mode(self, prompt: str) -> Dict[str, Any]:
        """
        Demo mode: Return mock logs for UI testing

        Args:
            prompt: Task prompt

        Returns:
            Mock execution result
        """
        # Get recent logs from database
        recent_logs = (
            self.db.query(LogEntry)
            .filter(LogEntry.session_id == self.session_id)
            .order_by(LogEntry.created_at.desc())
            .limit(10)
            .all()
        )

        self._log_entry("INFO", "Running in DEMO MODE - no actual task execution")

        return {
            "status": "completed",
            "mode": "demo",
            "output": f"Demo execution of: {prompt[:50]}...",
            "logs": [
                {
                    "level": log.level,
                    "message": log.message,
                    "timestamp": log.created_at.isoformat(),
                }
                for log in recent_logs
            ],
            "execution_time": 0.0,
            "note": "Demo mode - no actual task was executed. Enable real mode to execute via OpenClaw API.",
        }

    def _parse_openclaw_response(self, result: Any) -> Dict[str, Any]:
        """Parse OpenClaw CLI response with unified error handling"""

        # Handle subprocess.CompletedProcess object
        if isinstance(result, subprocess.CompletedProcess):
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()
            return_code = result.returncode

            if return_code != 0 and stderr:
                self._log_entry("ERROR", f"OpenClaw CLI error: {stderr[:500]}")
        else:
            # Already a string (from streaming mode)
            stdout = result.strip()
            return_code = 0
            stderr = ""

        cli_error_message = self._summarize_cli_error(stderr) if stderr else ""
        cli_error_lower = cli_error_message.lower()

        if "context size has been exceeded" in cli_error_lower or (
            "context" in cli_error_lower and "exceeded" in cli_error_lower
        ):
            self._log_entry("ERROR", f"Context window exceeded: {cli_error_message}")
            return {
                "status": "failed",
                "mode": "real",
                "output": stdout,
                "error": "Context window exceeded",
                "logs": [
                    {
                        "level": "ERROR",
                        "message": cli_error_message,
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                ],
            }

        if (not stdout or stdout in ['""', "''", '"', "'"]) and stderr:
            recovered_output = self._recover_json_like_output_from_stderr(stderr)
            if recovered_output:
                self._log_entry(
                    "WARN",
                    "[OPENCLAW] stdout was empty; recovered structured response from stderr",
                )
                stdout = recovered_output

        # CRITICAL FIX: Validate response before parsing
        if not stdout or stdout in ['""', "''", '"', "'"]:
            self._log_entry("ERROR", "[OPENCLAW] CRITICAL: Empty or invalid response")
            return {
                "status": "failed",
                "mode": "real",
                "output": "",
                "error": cli_error_message
                or "Empty or invalid response from OpenClaw CLI",
                "logs": [],
            }

        # Parse JSON with error recovery
        try:
            output_data = json.loads(stdout)

            # Extract text from payloads if present
            if isinstance(output_data, dict) and "payloads" in output_data:
                payloads = output_data.get("payloads", [])
                if isinstance(payloads, list) and len(payloads) > 0:
                    output_text = payloads[0].get("text", "")
                else:
                    output_text = extract_structured_text(output_data) or json.dumps(
                        output_data
                    )
            else:
                output_text = extract_structured_text(output_data) or json.dumps(
                    output_data
                )

            return {
                "status": "completed" if return_code == 0 else "failed",
                "mode": "real",
                "output": output_text,
                "error": cli_error_message if return_code != 0 else "",
                "logs": [],
            }

        except json.JSONDecodeError:
            # Only apply garbled detection after JSON parsing actually fails.
            garbled_patterns = [
                "\"'",
                '"", "',
                "garbled",
                "corrupted",
            ]
            stdout_lower = stdout.lower()
            if stdout.strip() in {"\"'", "'"} or any(
                pattern in stdout_lower for pattern in garbled_patterns
            ):
                self._log_entry(
                    "ERROR",
                    f"[OPENCLAW] DETECTED GARBLED OUTPUT AFTER JSON PARSE FAILURE: '{stdout[:200]}'",
                )
                return {
                    "status": "failed",
                    "mode": "real",
                    "output": "",
                    "error": "Execution failed with unclear error (garbled output detected). See logs for details.",
                    "logs": [
                        {
                            "level": "ERROR",
                            "message": f"Garbled output detected: '{stdout[:500]}'",
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                    ],
                    "execution_time": 0.0,
                }

            # Fallback to raw text if it isn't valid JSON but still looks coherent.
            self._log_entry("WARN", "Failed to parse JSON, using raw output")
            return {
                "status": "completed" if return_code == 0 else "failed",
                "mode": "real",
                "output": stdout,
                "error": cli_error_message if return_code != 0 else "",
                "logs": [],
            }

        except Exception as e:
            error_str = str(e)
            # Handle specific error types
            if "context" in error_str.lower() and "token" in error_str.lower():
                # Context window error - provide helpful message
                self._log_entry("ERROR", f"Context window exceeded: {error_str}")
                return {
                    "status": "failed",
                    "mode": "real",
                    "output": "Context window exceeded. Prompt is too long for the model.",
                    "logs": [
                        {
                            "level": "ERROR",
                            "message": f"Context window exceeded: {error_str}",
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                    ],
                    "execution_time": 0.0,
                    "error": "Context window exceeded",
                }
            elif "signal" in error_str.lower() or "killed" in error_str.lower():
                # Process was killed (likely OOM or timeout)
                self._log_entry("ERROR", f"Process was killed: {error_str}")
                return {
                    "status": "failed",
                    "mode": "real",
                    "output": f"Process was killed: {error_str}",
                    "logs": [
                        {
                            "level": "ERROR",
                            "message": f"Process was killed: {error_str}",
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                    ],
                    "execution_time": 0.0,
                    "error": "Process killed",
                }
            else:
                self._log_entry(
                    "ERROR", f"Error executing task via OpenClaw: {error_str}"
                )
                return {
                    "status": "failed",
                    "mode": "real",
                    "output": f"Execution error: {error_str}",
                    "logs": [
                        {
                            "level": "ERROR",
                            "message": f"Error: {error_str}",
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                    ],
                    "execution_time": 0.0,
                    "error": error_str,
                }

    def _recover_json_like_output_from_stderr(self, stderr: str) -> str:
        """Recover a structured JSON-ish payload from stderr when stdout is empty."""
        ansi_pattern = re.compile(r"\x1B\[[0-9;]*[A-Za-z]")
        lines = []
        for raw_line in (stderr or "").splitlines():
            cleaned = ansi_pattern.sub("", raw_line).strip()
            if cleaned:
                lines.append(cleaned)

        if not lines:
            return ""

        candidate_indexes = [
            index
            for index, line in enumerate(lines)
            if line in {"{", "["}
            or line.startswith("{")
            or line.startswith("[")
            or line.startswith('"payloads"')
            or line.startswith('"stopReason"')
        ]

        for index in reversed(candidate_indexes):
            candidate = "\n".join(lines[index:]).strip()
            try:
                json.loads(candidate)
                return candidate
            except Exception:
                continue

        return ""

    def _summarize_cli_error(self, stderr: str) -> str:
        """Return a compact user-facing summary from OpenClaw stderr."""
        lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
        if not lines:
            return ""

        for line in lines:
            lowered = line.lower()
            if "[openclaw] cli failed:" in lowered:
                return line[:500]

        for line in lines:
            lowered = line.lower()
            if lowered.startswith("at ") or "jiti/dist/jiti.cjs" in lowered:
                continue
            if "referenceerror:" in lowered:
                return f"[openclaw] CLI failed: {line[:450]}"
            return line[:500]

        return lines[0][:500]

    async def stream_logs(self, callback: callable) -> None:
        """
        Stream logs from OpenClaw session to callback

        Args:
            callback: Function to receive log entries (log_level, message, metadata)
        """
        try:
            self._log_entry("INFO", "Starting log streaming")

            # TODO: Implement actual log streaming
            # This would read from OpenClaw session logs and emit via callback

            while True:
                # Simulate log stream
                await callback(
                    "INFO", "Log stream active...", {"session_id": self.session_id}
                )
                # Break for now (would be infinite loop in production)
                break

        except Exception as e:
            self._log_entry("ERROR", f"Log streaming failed: {str(e)}")
            raise

    async def execute_task_with_streaming(
        self,
        prompt: str,
        timeout_seconds: int = 300,
        log_callback: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        """Execute task via OpenClaw CLI with real-time log streaming (optimized)"""

        # Reuse the stable per-task session ID set in create_openclaw_session so
        # all steps in this task share one AI context window.
        task_id_str = str(self.task_id or self.session_id)
        new_session_id = (
            self._task_session_id
            or f"orchestrator-task-{task_id_str}-{int(time.time())}"
        )

        try:
            openclaw_command = self._resolve_openclaw_command()
            execution_cwd = self._resolve_execution_cwd()
            process = await asyncio.create_subprocess_exec(
                *openclaw_command,
                "agent",
                "--local",
                "--session-id",
                new_session_id,
                "--message",
                prompt,
                "--json",
                "--timeout",
                str(timeout_seconds),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.STREAM_READ_LIMIT,
                cwd=execution_cwd,
            )

            stdout_chunks: List[str] = []
            stderr_chunks: List[str] = []

            async def stream_output(
                stream,
                level: str,
                chunks: List[str],
                emit_live_logs: bool = True,
            ) -> None:
                while True:
                    line = await stream.readline()
                    if not line:
                        break

                    line_text = line.decode("utf-8", errors="replace").strip()
                    chunks.append(line_text)

                    if line_text:
                        if emit_live_logs:
                            if level == "WARN" and not self._should_emit_stderr_line(
                                line_text
                            ):
                                continue
                            # Commit each streamed line so the session websocket,
                            # which polls the database, can surface it immediately.
                            self._log_entry(level, line_text, commit=True)

                            if log_callback:
                                await log_callback(level, line_text)

            await asyncio.wait_for(
                asyncio.gather(
                    # OpenClaw emits its final machine-readable JSON on stdout.
                    # Buffer it for parsing, but don't flood Live Logs with raw JSON lines.
                    stream_output(
                        process.stdout, "INFO", stdout_chunks, emit_live_logs=False
                    ),
                    # Keep stderr visible because it contains actionable warnings/errors.
                    stream_output(
                        process.stderr, "WARN", stderr_chunks, emit_live_logs=True
                    ),
                ),
                timeout=timeout_seconds + 30,
            )

            return_code = await asyncio.wait_for(
                process.wait(), timeout=timeout_seconds + 30
            )
            stdout_text = "\n".join(filter(None, stdout_chunks)).strip()
            stderr_text = "\n".join(filter(None, stderr_chunks)).strip()

            self._log_entry(
                "INFO",
                f"[OPENCLAW] Return code: {return_code}, stdout_len: {len(stdout_text)}, stderr_len: {len(stderr_text)}",
                commit=True,
            )

            completed = subprocess.CompletedProcess(
                args=[
                    *openclaw_command,
                    "agent",
                    "--local",
                    "--session-id",
                    new_session_id,
                ],
                returncode=return_code,
                stdout=stdout_text,
                stderr=stderr_text,
            )
            return self._parse_openclaw_response(completed)

        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except Exception as exc:
                logger.debug(
                    "[OPENCLAW] Failed to terminate timed out process cleanly: %s",
                    exc,
                )

            raise OpenClawSessionError(f"Task timed out after {timeout_seconds}s")

        except Exception as e:
            self._log_entry("ERROR", f"Real mode execution failed: {str(e)}")
            raise

    async def track_tool_execution(
        self, tool_name: str, params: Dict[str, Any], result: Any, success: bool
    ) -> None:
        """
        Track tool execution for audit trail

        Args:
            tool_name: Name of the tool executed
            params: Tool parameters
            result: Tool execution result
            success: Whether execution was successful
        """
        try:
            tool_log = {
                "tool": tool_name,
                "params": params,
                "result": str(result)[:1000],  # Truncate long results
                "success": success,
                "timestamp": datetime.now(UTC).isoformat(),
                "session_id": self.session_id,
                "task_id": self.task_id,
            }

            level = "INFO" if success else "ERROR"
            message = (
                f"Tool '{tool_name}' executed {'successfully' if success else 'failed'}"
            )

            self._log_entry(level, message, metadata=json.dumps(tool_log))

        except Exception as e:
            logger.error(f"Failed to track tool execution: {str(e)}")

    async def get_session_context(self) -> Dict[str, Any]:
        """
        Get current session context for LLM prompts

        Returns:
            Dictionary with session state, recent logs, task info
        """
        # Get recent log entries
        recent_logs = (
            self.db.query(LogEntry)
            .filter(LogEntry.session_id == self.session_id)
            .order_by(LogEntry.created_at.desc())
            .limit(20)
            .all()
        )

        logs_data = [
            {
                "level": log.level,
                "message": log.message,
                "timestamp": log.created_at.isoformat(),
                "metadata": json.loads(log.log_metadata) if log.log_metadata else {},
            }
            for log in recent_logs
        ]

        # Get task context if available
        task_context = None
        if self.task_model:
            task_context = {
                "id": self.task_model.id,
                "title": self.task_model.title,
                "status": self.task_model.status.value,
                "description": self.task_model.description,
                "steps": (
                    json.loads(self.task_model.steps) if self.task_model.steps else None
                ),
                "current_step": self.task_model.current_step,
            }

        return {
            "session_id": self.session_id,
            "session_name": (
                self.session_model.name
                if self.session_model
                else (
                    self.task_model.project.name
                    if self.task_model and self.task_model.project
                    else "Unknown"
                )
            ),
            "task": task_context,
            "recent_logs": logs_data,
            "openclaw_session_key": self.openclaw_session_key,
            "project_workspace_path": (
                str(
                    resolve_project_workspace_path(
                        self.task_model.project.workspace_path,
                        self.task_model.project.name,
                    ).resolve()
                )
                if self.task_model
                and self.task_model.project
                and self.task_model.project.workspace_path
                else None
            ),
            "task_workspace_path": self._resolve_execution_cwd(),
            "execution_cwd": self._resolve_execution_cwd(),
        }

    async def invoke_prompt(
        self,
        prompt: str,
        *,
        timeout_seconds: int = 180,
        source_brain: str = "local",
        session_prefix: str = "planning",
    ) -> Dict[str, Any]:
        """Run a one-shot prompt using the CLI request path."""

        full_cmd = self.build_cli_agent_command(
            prompt,
            source_brain=source_brain,
            timeout_seconds=timeout_seconds,
            session_prefix=session_prefix,
        )
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                full_cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds + 30,
            )
        except subprocess.TimeoutExpired as exc:
            raise OpenClawSessionError(
                f"Prompt invocation timed out after {timeout_seconds}s"
            ) from exc
        return self.parse_cli_response(proc)

    def _log_entry(
        self,
        level: str,
        message: str,
        metadata: Optional[str] = None,
        commit: bool = False,
    ) -> LogEntry:
        """Create database log entry with instance tracking

        Args:
            level: Log level (INFO, WARN, ERROR, etc.)
            message: Log message
            metadata: Optional metadata
            commit: If True, commit immediately. If False, batch commit (default False)

        Returns:
            LogEntry object
        """
        # Get instance_id from session if available
        session_instance_id = None
        if self.session_model:
            session_instance_id = self.session_model.instance_id
        elif self.task_model and self.task_model.project:
            # Fallback: try to get project info from task
            session_instance_id = None  # Will be set by session later

        log_entry = LogEntry(
            session_id=self.session_id,
            session_instance_id=session_instance_id,
            task_id=self.task_id,
            level=level,
            message=message,
            log_metadata=metadata,
        )
        self.db.add(log_entry)
        # Only commit if explicitly requested (for performance)
        if commit:
            self.db.commit()
        return log_entry

    async def start_session(self, task_description: str) -> str:
        """
        Start a session for the given task description

        Args:
            task_description: Description of the task to execute

        Returns:
            OpenClaw session key
        """
        return await self.create_session(task_description)

    async def stop_session(self) -> None:
        """
        Stop the OpenClaw session gracefully

        Raises:
            OpenClawSessionError: If stop fails
        """
        try:
            self._log_entry("INFO", "Stopping OpenClaw session")

            # Cleanup session resources
            await self.cleanup()

            self._log_entry("INFO", "OpenClaw session stopped")

        except Exception as e:
            error_msg = f"Failed to stop session: {str(e)}"
            self._log_entry("ERROR", error_msg)
            raise OpenClawSessionError(error_msg)

    async def pause_session(self) -> None:
        """
        Pause the OpenClaw session with full checkpoint save

        Saves current execution state including:
        - Session context (task description, project info)
        - Orchestration workflow state (PLANNING, EXECUTING, etc.)
        - Current step index and results from completed steps
        - Tool execution history

        Raises:
            OpenClawSessionError: If pause fails
        """
        try:
            self._log_entry("INFO", "Pausing OpenClaw session with checkpoint")

            # Create checkpoint service instance
            checkpoint_service = CheckpointService(self.db)

            # Get current session context
            context_data = await self.get_session_context()
            task_context = context_data.get("task") or {}
            if self.task_model:
                context_data["task_id"] = self.task_model.id
                context_data["task_description"] = (
                    self.task_model.description or self.task_model.title
                )
                context_data["task_subfolder"] = getattr(
                    self.task_model, "task_subfolder", None
                )
                if self.task_model.project:
                    context_data["project_name"] = self.task_model.project.name
                    if self.task_model.project.workspace_path:
                        from app.services.workspace.project_isolation_service import (
                            resolve_project_workspace_path,
                        )

                        workspace_path = str(
                            resolve_project_workspace_path(
                                self.task_model.project.workspace_path,
                                self.task_model.project.name,
                            )
                        )
                        context_data["workspace_path_override"] = workspace_path
                        if context_data.get(
                            "task_subfolder"
                        ) and not should_execute_in_canonical_project_root(
                            self.task_model,
                            getattr(self.task_model, "execution_profile", None),
                            getattr(self.task_model, "title", None),
                            getattr(self.task_model, "description", None),
                        ):
                            context_data["project_dir_override"] = str(
                                Path(workspace_path) / context_data["task_subfolder"]
                            )
                        else:
                            context_data["project_dir_override"] = workspace_path
            elif task_context:
                context_data["task_id"] = task_context.get("id")
                context_data["task_description"] = task_context.get(
                    "description"
                ) or task_context.get("title")

            # Save orchestration state if available
            orchestration_state = {}
            if hasattr(self, "_orchestration_state"):
                orchestration_state = self._orchestration_state

            # Get step results from execution history
            step_results = []
            try:
                tool_service = ToolTrackingService(self.db)
                executions = tool_service.get_execution_history(
                    session_id=self.session_id, limit=50
                )

                for exec_item in executions:
                    step_results.append(
                        {
                            "step_type": "tool_execution",
                            "tool_name": exec_item.tool_name,
                            "parameters": (
                                json.loads(exec_item.parameters)
                                if isinstance(exec_item.parameters, str)
                                else exec_item.parameters
                            ),
                            "result": exec_item.result,
                            "success": exec_item.success,
                            "executed_at": (
                                exec_item.executed_at.isoformat()
                                if exec_item.executed_at
                                else None
                            ),
                        }
                    )
            except Exception as exc:
                logger.debug("[OPENCLAW] Ignoring tool tracking restore error: %s", exc)

            # Find current step index (last executed step)
            current_step_index = len(step_results) - 1 if step_results else 0

            # Save checkpoint with detailed state
            checkpoint_name = f"paused_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"

            checkpoint_result = checkpoint_service.save_checkpoint(
                session_id=self.session_id,
                checkpoint_name=checkpoint_name,
                context_data=context_data,
                orchestration_state=orchestration_state,
                current_step_index=current_step_index,
                step_results=step_results,
            )

            # Log checkpoint details
            self._log_entry(
                "INFO",
                f"Checkpoint saved: {checkpoint_result['path']} (name: {checkpoint_name})",
            )

            # Terminate the OpenClaw process gracefully
            await self.cleanup()

            self._log_entry(
                "INFO", f"OpenClaw session paused - Checkpoint saved: {checkpoint_name}"
            )

        except Exception as e:
            error_msg = f"Failed to pause session: {str(e)}"
            self._log_entry("ERROR", error_msg)
            raise OpenClawSessionError(error_msg)

    async def resume_session(self, checkpoint_name: Optional[str] = None) -> str:
        """
        Resume a paused session from a checkpoint

        Args:
            checkpoint_name: Specific checkpoint to restore (optional - uses latest if not specified)

        Returns:
            OpenClaw session key for resumed execution

        Raises:
            OpenClawSessionError: If resume fails
        """
        try:
            self._log_entry("INFO", "Resuming OpenClaw session from checkpoint")

            # Create checkpoint service instance
            checkpoint_service = CheckpointService(self.db)

            # Load checkpoint data
            checkpoint_data = checkpoint_service.load_checkpoint(
                session_id=self.session_id, checkpoint_name=checkpoint_name
            )

            # Restore context
            context_data = checkpoint_data.get("context", {})
            orchestration_state = checkpoint_data.get("orchestration_state", {})
            step_results = checkpoint_data.get("step_results", [])
            current_step_index = checkpoint_data.get("current_step_index", 0)

            # Reconstruct task description from context (if available)
            task_description = (
                context_data.get("task_description")
                or context_data.get("description")
                or "Resumed session"
            )

            # Create new OpenClaw session with restored context
            # Include information about resumed execution in the prompt
            resume_context = {
                **context_data,
                "resumed_from_checkpoint": True,
                "checkpoint_name": checkpoint_data.get("checkpoint_name"),
                "completed_steps_count": len(step_results),
                "last_step_index": current_step_index,
                "previous_orchestration_state": orchestration_state.get("status", ""),
                "previous_plan_summary": (
                    json.dumps(orchestration_state.get("plan", []))[:500]
                    if orchestration_state.get("plan")
                    else None
                ),
            }

            # Create OpenClaw session
            session_key = await self.create_session(
                task_description=task_description, context=resume_context
            )

            # Log successful resume with checkpoint info
            self._log_entry(
                "INFO",
                f"OpenClaw session resumed from checkpoint: {checkpoint_data.get('checkpoint_name')}",
            )
            self._log_entry(
                "INFO",
                f"Restored state - Completed steps: {len(step_results)}, Current step index: {current_step_index}, Previous status: {orchestration_state.get('status', 'unknown')}",
            )

            return session_key

        except CheckpointError as e:
            error_msg = f"No valid checkpoint found to resume from: {str(e)}"
            self._log_entry("ERROR", error_msg)
            raise OpenClawSessionError(error_msg)
        except Exception as e:
            error_msg = f"Failed to resume session: {str(e)}"
            self._log_entry("ERROR", error_msg)
            raise OpenClawSessionError(error_msg)

    async def cleanup(self) -> None:
        """Clean up session resources"""
        try:
            self._log_entry("INFO", "Cleaning up OpenClaw session")

            # Terminate any running processes
            if self.process:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()

            self._log_entry("INFO", "Session cleanup complete")

        except Exception as e:
            logger.error(f"Cleanup failed: {str(e)}")
