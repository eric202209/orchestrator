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
from datetime import datetime
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
from app.services.project_isolation_service import (
    ProjectIsolationService,
    resolve_project_workspace_path,
)
from app.services.permission_service import PermissionApprovalService
from app.services.performance_optimizations import (
    optimize_prompt,
    compress_context,
    perf_tracker,
)
from app.services.checkpoint_service import CheckpointService, CheckpointError
from app.services.tool_tracking_service import ToolTrackingService

logger = logging.getLogger(__name__)


class OpenClawSessionError(Exception):
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
        self.openclaw_session_key: Optional[str] = None
        self.process: Optional[subprocess.Popen] = None
        # Initialize checkpoint service
        from app.services.checkpoint_service import CheckpointService

        self.checkpoint_service = CheckpointService(db)

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

            if self.task_model and self.task_model.task_subfolder:
                return str((project_workspace / self.task_model.task_subfolder).resolve())

            return str(project_workspace.resolve())
        except Exception as exc:
            self._log_entry(
                "WARN",
                f"[OPENCLAW] Failed to resolve execution cwd, falling back to default: {exc}",
            )
            return None

    async def create_openclaw_session(
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
            # Log session creation
            self._log_entry(
                "INFO", f"Creating OpenClaw session for task: {task_description[:100]}"
            )

            # Use the main OpenClaw session that already exists
            self.openclaw_session_key = "agent:main:main"

            self._log_entry(
                "INFO", f"✅ OpenClaw session set to: {self.openclaw_session_key}"
            )

            return self.openclaw_session_key

        except Exception as e:
            error_msg = f"Failed to create OpenClaw session: {str(e)}"
            self._log_entry("ERROR", error_msg)
            raise OpenClawSessionError(error_msg)

    async def execute_task(
        self, prompt: str, timeout_seconds: int = 300, log_callback: callable = None
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
                    "[OPENCLAW] Request completed successfully; awaiting orchestration validation",
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

            return result

        except Exception as e:
            error_msg = f"Task execution failed: {str(e)}"
            self._log_entry("ERROR", error_msg)

            raise OpenClawSessionError(error_msg)

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

            # Return False to indicate permission is pending
            return False

        except Exception as e:
            self._log_entry(
                "ERROR", f"Permission check failed: {str(e)}. Allowing operation..."
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
            if self.session_model and self.session_model.project_id:
                try:
                    isolation_service = ProjectIsolationService(self.db)
                    safety_prompt = isolation_service.get_safety_prompt(
                        self.session_model.project_id
                    )
                    prompt = f"{safety_prompt}\n\n{prompt}"
                    # OPTIMIZATION: Only log safety prompt injection once
                    if not hasattr(self, "_safety_prompt_injected"):
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
                # OPTIMIZATION: Compress project context
                project_context = (
                    compress_context(
                        {
                            "description": (
                                self.session_model.description
                                if self.session_model
                                else ""
                            )
                        }
                    ).get("description", "")[:2000]
                    if self.session_model
                    else ""
                )

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

            if self.task_model and self.task_model.task_subfolder:
                orchestration_state._task_subfolder_override = (
                    self.task_model.task_subfolder
                )

            # Phase 1: PLANNING (OPTIMIZED)
            orchestration_state.status = OrchestrationStatus.PLANNING
            self._log_entry("INFO", "[ORCHESTRATION] PLANNING phase")

            # OPTIMIZATION: Compress project context in planning prompt
            planning_prompt = PromptTemplates.build_planning_prompt(
                task_description=prompt,
                project_context=project_context[:1500] if project_context else "",
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
                        if isinstance(output_data, dict) and "payloads" in output_data:
                            payloads = output_data.get("payloads", [])
                            if isinstance(payloads, list) and len(payloads) > 0:
                                # Get the text from first payload
                                first_payload = payloads[0]
                                if isinstance(first_payload, dict):
                                    output_text = first_payload.get("text", output_text)
                    except json.JSONDecodeError:
                        pass  # Not OpenClaw format, use as-is

                # Strip Markdown code fences if present
                if isinstance(output_text, str):
                    import re

                    # Remove ```json or ``` wrappers
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
                    # Fallback to single step
                    orchestration_state.plan = [
                        {
                            "step_number": 1,
                            "description": prompt,
                            "commands": [prompt],
                            "verification": None,
                            "rollback": None,
                            "expected_files": [],
                        }
                    ]
                    self._log_entry(
                        "INFO", "[ORCHESTRATION] Using fallback single-step plan"
                    )
            except json.JSONDecodeError:
                orchestration_state.plan = [
                    {
                        "step_number": 1,
                        "description": prompt,
                        "commands": [prompt],
                        "verification": None,
                        "rollback": None,
                        "expected_files": [],
                    }
                ]
                self._log_entry(
                    "INFO", "[ORCHESTRATION] Using fallback single-step plan"
                )

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
                "orchestration_state": {
                    "status": orchestration_state.status.value,
                    "plan_length": len(orchestration_state.plan),
                    "steps_completed": len(orchestration_state.execution_results),
                    "debug_attempts": len(orchestration_state.debug_attempts),
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
                    project_context=(
                        self.session_model.description if self.session_model else ""
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
                        "timestamp": datetime.utcnow().isoformat(),
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
                    output_text = json.dumps(output_data)
            else:
                output_text = json.dumps(output_data)

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
                            "timestamp": datetime.utcnow().isoformat(),
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
                            "timestamp": datetime.utcnow().isoformat(),
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
                            "timestamp": datetime.utcnow().isoformat(),
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
                            "timestamp": datetime.utcnow().isoformat(),
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

        # OPTIMIZATION: Single session ID generation
        task_id_str = str(self.task_id or self.session_id)
        new_session_id = f"orchestrator-task-{task_id_str}-{int(time.time())}"

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
            except Exception:
                pass

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
                "timestamp": datetime.utcnow().isoformat(),
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
        }

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
        return await self.create_openclaw_session(task_description)

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
            except Exception:
                pass  # Ignore tool tracking errors

            # Find current step index (last executed step)
            current_step_index = len(step_results) - 1 if step_results else 0

            # Save checkpoint with detailed state
            checkpoint_name = f"paused_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

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
            session_key = await self.create_openclaw_session(
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
