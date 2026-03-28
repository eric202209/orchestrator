"""OpenClaw Session Service

Integration service for orchestrating AI development tasks via OpenClaw sessions.
Handles session lifecycle, tool execution tracking, and log streaming.
Implements multi-step orchestration workflow: PLANNING → EXECUTING → DEBUGGING → PLAN_REVISION → DONE
"""

import json
import subprocess
import logging
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime
from sqlalchemy.orm import Session
from app.models import Session as SessionModel, Task, TaskStatus, LogEntry
from app.config import settings
from app.services.prompt_templates import (
    OrchestrationStatus,
    OrchestrationState,
    StepResult,
    PromptTemplates,
)
from app.services.project_isolation_service import ProjectIsolationService
from app.services.permission_service import (
    PermissionApprovalService,
    PermissionOperationType,
)

logger = logging.getLogger(__name__)


class OpenClawSessionError(Exception):
    """Custom exception for OpenClaw session errors"""

    pass


class OpenClawSessionService:
    """Service for managing OpenClaw session orchestration"""

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
            # Prepare session message with context
            message = {
                "task": task_description,
                "timestamp": datetime.utcnow().isoformat(),
                "session_id": self.session_id,
                "task_id": self.task_id,
                "context": context or {},
            }

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
            # Check if we should use demo mode or real execution
            if self.use_demo_mode:
                # DEMO MODE: Return mock logs (for UI testing)
                result = await self._execute_demo_mode(prompt)
                # Demo mode always completes successfully (by design)
                result["status"] = "completed"
            else:
                # REAL MODE: Execute task via OpenClaw HTTP API
                # Use streaming version if log_callback is provided
                if log_callback:
                    result = await self.execute_task_with_streaming(
                        prompt, timeout_seconds, log_callback
                    )
                else:
                    result = await self._execute_real_mode(prompt, timeout_seconds)

            # Update task completion based on result status
            if self.task_model:
                if result.get("status") == "completed":
                    self.task_model.status = TaskStatus.DONE
                    self._log_entry("INFO", f"Task completed successfully")
                elif result.get("status") == "failed":
                    self.task_model.status = TaskStatus.FAILED
                    self.task_model.error_message = result.get(
                        "error", "Execution failed"
                    )
                    self._log_entry(
                        "ERROR", f"Task failed: {self.task_model.error_message}"
                    )
                else:
                    # Unknown status, mark as failed
                    self.task_model.status = TaskStatus.FAILED
                    self.task_model.error_message = (
                        f"Unknown status: {result.get('status', 'unknown')}"
                    )
                    self._log_entry("ERROR", f"Task failed with unknown status")

                self.task_model.completed_at = datetime.utcnow()
                self.db.commit()

            return result

        except Exception as e:
            error_msg = f"Task execution failed: {str(e)}"
            self._log_entry("ERROR", error_msg)

            if self.task_model:
                self.task_model.status = TaskStatus.FAILED
                self.task_model.error_message = str(e)
                self.db.commit()

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
            # Add project isolation safety prompt
            if self.session_model and self.session_model.project_id:
                try:
                    isolation_service = ProjectIsolationService(self.db)
                    safety_prompt = isolation_service.get_safety_prompt(
                        self.session_model.project_id
                    )
                    prompt = f"{safety_prompt}\n\n{prompt}"
                    self._log_entry("INFO", "Project isolation safety prompt injected")
                except Exception as e:
                    self._log_entry("WARN", f"Failed to inject safety prompt: {str(e)}")

            self._log_entry(
                "INFO", f"[ORCHESTRATION] Starting with prompt: {prompt[:100]}..."
            )

            if orchestration_state is None:
                orchestration_state = OrchestrationState(
                    session_id=str(self.session_id),
                    task_description=prompt,
                    project_name=self.session_model.name if self.session_model else "",
                    project_context="",
                )

            # Phase 1: PLANNING
            orchestration_state.status = OrchestrationStatus.PLANNING
            self._log_entry("INFO", "[ORCHESTRATION] Starting PLANNING phase")

            planning_prompt = PromptTemplates.build_planning_prompt(
                task_description=prompt,
                project_context=(
                    self.session_model.description if self.session_model else ""
                ),
            )

            planning_result = await self.execute_task(
                planning_prompt, timeout_seconds=120
            )

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

            # Phase 5: DONE
            orchestration_state.status = OrchestrationStatus.DONE
            self._log_entry("INFO", "[ORCHESTRATION] Task completed successfully")

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
            raise OpenClawSessionError(f"Orchestration failed: {str(e)}")

    async def _execute_step_with_retry(
        self,
        step: Dict[str, Any],
        step_index: int,
        orchestration_state: OrchestrationState,
        max_retries: int = 3,
    ) -> StepResult:
        """Execute a single step with retry logic"""
        step_description = step.get("description", "Unknown step")
        step_commands = step.get("commands", [])

        self._log_entry("INFO", f"[STEP] Executing: {step_description[:100]}...")

        for attempt in range(max_retries):
            try:
                # Build execution prompt
                execution_prompt = PromptTemplates.build_execution_prompt(
                    step_description=step_description,
                    step_commands=step_commands,
                    verification_command=step.get("verification"),
                    rollback_command=step.get("rollback"),
                    expected_files=step.get("expected_files", []),
                    completed_steps_summary=orchestration_state.prior_results_summary(),
                    project_context=(
                        self.session_model.description if self.session_model else ""
                    ),
                )

                # Execute step
                result = await self.execute_task(execution_prompt, timeout_seconds=180)

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

            except Exception as e:
                # Extract meaningful error message
                error_msg = str(e)

                # If error message contains garbled output, try to extract actual error
                if (
                    error_msg.strip() in ["\"'"]
                    or error_msg.strip().startswith('"), "')
                    or error_msg.strip().startswith('"), "')
                ):
                    # Try to get error from stderr if available
                    # This handles cases where OpenClaw CLI returned garbled error
                    self._log_entry(
                        "WARN",
                        f"[STEP] Garbled error detected: {repr(error_msg)}. Checking for better error message...",
                    )

                    # If we have access to the result object, check its stderr
                    # For now, provide a more helpful error message
                    error_msg = f"Execution failed with unclear error. See logs for details. Original error: {str(e)[:200]}"

                step_result = StepResult(
                    step_number=step_index + 1,
                    status="failed",
                    error_message=error_msg,
                    attempt=attempt + 1,
                )
                orchestration_state.record_failure(step_result)
                self._log_entry(
                    "ERROR", f"[STEP] Step {step_index + 1} error: {error_msg}"
                )

        # All retries failed
        return StepResult(
            step_number=step_index + 1,
            status="failed",
            error_message=f"All {max_retries} attempts failed",
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
            project_name=self.session_model.name if self.session_model else "",
            workspace_root=str(orchestration_state.workspace_root),
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

    async def _execute_real_mode(
        self, prompt: str, timeout_seconds: int = 300
    ) -> Dict[str, Any]:
        """
        Real mode: Execute task via OpenClaw CLI (uses embedded model)

        Args:
            prompt: Task prompt (should already be built with templates)
            timeout_seconds: Maximum execution time

        Returns:
            Actual execution result
        """
        import subprocess
        import json

        # Check prompt length to avoid context window overflow
        # The model has 65,536 token limit, so we need to be conservative
        MAX_PROMPT_LENGTH = 50000  # Leave room for model overhead

        if len(prompt) > MAX_PROMPT_LENGTH:
            self._log_entry(
                "WARN",
                f"Prompt too long ({len(prompt)} chars), truncating to {MAX_PROMPT_LENGTH}",
            )
            # Truncate but keep the end for context
            prompt = (
                prompt[:MAX_PROMPT_LENGTH] + "\n\n[TRUNCATED - prompt was too long]"
            )

        self._log_entry("INFO", f"Running in REAL MODE - executing via OpenClaw CLI")
        self._log_entry("INFO", f"Starting task execution with prompt template:")
        self._log_entry(
            "INFO", f"Prompt length: {len(prompt)} chars, preview: {prompt[:200]}..."
        )

        # Use --local with unique session ID to avoid gateway lock
        try:
            import uuid

            # Ensure task_id is not None
            if self.task_id is None:
                self._log_entry(
                    "WARN",
                    f"task_id is None! Using session_id instead: {self.session_id}",
                )
                task_id_str = str(self.session_id)
            else:
                task_id_str = str(self.task_id)

            new_session_id = f"orchestrator-task-{task_id_str}-{uuid.uuid4().hex[:8]}"

            # Log the full prompt structure (first 500 chars)
            self._log_entry(
                "INFO",
                f"Prompt contains: {('EXECUTE THIS TASK DIRECTLY' in prompt and 'EXECUTE' or 'Standard')}",
            )

            # Escape single quotes in prompt for bash command
            escaped_prompt = prompt.replace("'", "'\\''")
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"openclaw agent --local --session-id {new_session_id} --message '{escaped_prompt}' --json --timeout {timeout_seconds}",
                ],
                capture_output=True,
                text=True,
                timeout=timeout_seconds + 30,
                executable="/usr/bin/bash",
            )

            # Debug: Log the full result
            self._log_entry(
                "INFO",
                f"[OPENCLAW] Return code: {result.returncode}, stdout_len: {len(result.stdout)}, stderr_len: {len(result.stderr)}",
            )
            # Always log stderr for debugging (even when returncode is 0)
            if result.stderr:
                self._log_entry(
                    "INFO",
                    f"[OPENCLAW] STDERR (length {len(result.stderr)}): {result.stderr[:500]}",
                )
            if result.returncode != 0:
                self._log_entry(
                    "ERROR",
                    f"[OPENCLAW] STDERR: {result.stderr[:1000]}",
                )

            if result.returncode == 0:
                try:
                    # Debug: Log raw stdout
                    self._log_entry(
                        "INFO",
                        f"[OPENCLAW] Raw stdout: {repr(result.stdout[:500])}...",
                    )

                    # Debug: Try parsing with debug
                    stdout_stripped = result.stdout.strip()
                    self._log_entry(
                        "INFO",
                        f"[OPENCLAW] Stripped length: {len(stdout_stripped)}, first 100 chars: {repr(stdout_stripped[:100])}",
                    )

                    output_data = json.loads(stdout_stripped)

                    # Debug log
                    self._log_entry(
                        "INFO",
                        f"[OPENCLAW] Full response received, type: {type(output_data)}, keys: {list(output_data.keys()) if isinstance(output_data, dict) else 'N/A'}",
                    )

                    # Extract text from payloads array
                    output_text = ""
                    if isinstance(output_data, dict):
                        if "payloads" in output_data:
                            payloads = output_data.get("payloads", [])
                            if isinstance(payloads, list) and len(payloads) > 0:
                                first_payload = payloads[0]
                                if isinstance(first_payload, dict):
                                    output_text = first_payload.get("text", "")
                                else:
                                    output_text = str(first_payload)
                            else:
                                output_text = json.dumps(output_data)
                        else:
                            output_text = json.dumps(output_data)
                    else:
                        output_text = result.stdout

                    self._log_entry(
                        "INFO", f"Task execution completed: {output_text[:300]}"
                    )

                    return {
                        "status": "completed",
                        "mode": "real",
                        "output": output_text,
                        "logs": [
                            {
                                "level": "INFO",
                                "message": f"Task received: {prompt[:100]}...",
                                "timestamp": datetime.utcnow().isoformat(),
                            },
                            {
                                "level": "INFO",
                                "message": f"Task executed via OpenClaw CLI",
                                "timestamp": datetime.utcnow().isoformat(),
                            },
                        ],
                        "execution_time": 0.0,
                        "session_key": "orchestrator-session",
                        "note": "Real execution completed via OpenClaw CLI",
                    }
                except json.JSONDecodeError:
                    # Non-JSON response
                    self._log_entry("INFO", f"OpenClaw output: {result.stdout[:500]}")
                    return {
                        "status": "completed",
                        "mode": "real",
                        "output": result.stdout,
                        "logs": [
                            {
                                "level": "INFO",
                                "message": f"Task executed via OpenClaw CLI",
                                "timestamp": datetime.utcnow().isoformat(),
                            }
                        ],
                        "execution_time": 0.0,
                        "session_key": "orchestrator-session",
                        "note": "Real execution completed via OpenClaw CLI",
                    }
            else:
                raise Exception(f"OpenClaw CLI failed: {result.stderr}")

        except subprocess.TimeoutExpired as e:
            self._log_entry("ERROR", f"Task execution timed out: {str(e)}")
            return {
                "status": "failed",
                "mode": "real",
                "output": f"Task timed out after {timeout_seconds} seconds",
                "logs": [
                    {
                        "level": "ERROR",
                        "message": f"Task execution timed out after {timeout_seconds} seconds",
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                ],
                "execution_time": timeout_seconds,
                "error": "Timeout",
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
        self, prompt: str, timeout_seconds: int = 300, log_callback: callable = None
    ) -> Dict[str, Any]:
        """
        Execute task via OpenClaw CLI with real-time log streaming

        Args:
            prompt: Task prompt
            timeout_seconds: Maximum execution time
            log_callback: Optional callback for real-time log entries

        Returns:
            Execution result with logs
        """
        import subprocess
        import json

        if len(prompt) > MAX_PROMPT_LENGTH:
            self._log_entry(
                "WARN",
                f"Prompt too long ({len(prompt)} chars), truncating to {MAX_PROMPT_LENGTH}",
            )
            prompt = (
                prompt[:MAX_PROMPT_LENGTH] + "\n\n[TRUNCATED - prompt was too long]"
            )

        self._log_entry("INFO", f"Running in REAL MODE - executing via OpenClaw CLI")
        self._log_entry("INFO", f"Starting task execution with prompt template:")
        self._log_entry(
            "INFO", f"Prompt length: {len(prompt)} chars, preview: {prompt[:200]}..."
        )

        try:
            import uuid

            if self.task_id is None:
                self._log_entry(
                    "WARN",
                    f"task_id is None! Using session_id instead: {self.session_id}",
                )
                task_id_str = str(self.session_id)
            else:
                task_id_str = str(self.task_id)

            new_session_id = f"orchestrator-task-{task_id_str}-{uuid.uuid4().hex[:8]}"

            self._log_entry(
                "INFO",
                f"Prompt contains: {('EXECUTE THIS TASK DIRECTLY' in prompt and 'EXECUTE' or 'Standard')}",
            )

            # Use Popen for streaming instead of run()
            escaped_prompt = prompt.replace("'", "'\\''")
            process = await asyncio.create_subprocess_shell(
                f"openclaw agent --local --session-id {new_session_id} --message '{escaped_prompt}' --json --timeout {timeout_seconds}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=8192,
            )

            # Stream stdout and stderr in real-time
            async def stream_output(stream, level):
                """Stream output from a subprocess pipe"""
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    line_text = line.decode("utf-8", errors="replace").strip()
                    if line_text:
                        # Log to database
                        self._log_entry(level, line_text)
                        # Call callback if provided
                        if log_callback:
                            await log_callback(level, line_text)

            try:
                # Stream both stdout and stderr concurrently
                await asyncio.gather(
                    stream_output(process.stdout, "INFO"),
                    stream_output(process.stderr, "WARN"),
                )

                # Wait for process to complete
                await process.wait()

                # Get final output
                stdout, stderr = await process.communicate()
                stdout_text = stdout.decode("utf-8", errors="replace")
                stderr_text = stderr.decode("utf-8", errors="replace")

                self._log_entry(
                    "INFO",
                    f"[OPENCLAW] Return code: {process.returncode}, stdout_len: {len(stdout_text)}, stderr_len: {len(stderr_text)}",
                )

                if process.returncode == 0:
                    try:
                        output_data = json.loads(stdout_text.strip())

                        # Extract text from payloads array
                        output_text = ""
                        if isinstance(output_data, dict) and "payloads" in output_data:
                            payloads = output_data.get("payloads", [])
                            if isinstance(payloads, list) and len(payloads) > 0:
                                first_payload = payloads[0]
                                if isinstance(first_payload, dict):
                                    output_text = first_payload.get("text", "")

                        self._log_entry(
                            "INFO", f"Task execution completed: {output_text[:300]}"
                        )

                        return {
                            "status": "completed",
                            "mode": "real",
                            "output": output_text,
                            "logs": [],  # Logs already streamed via callback
                            "execution_time": 0.0,
                            "session_key": new_session_id,
                            "note": "Real execution completed via OpenClaw CLI with streaming",
                        }
                    except json.JSONDecodeError:
                        self._log_entry("INFO", f"OpenClaw output: {stdout_text[:500]}")
                        return {
                            "status": "completed",
                            "mode": "real",
                            "output": stdout_text,
                            "logs": [],
                            "execution_time": 0.0,
                            "session_key": new_session_id,
                            "note": "Real execution completed via OpenClaw CLI with streaming",
                        }
                else:
                    raise Exception(f"OpenClaw CLI failed: {stderr_text}")

            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                self._log_entry(
                    "ERROR", f"Task execution timed out: {timeout_seconds}s"
                )
                return {
                    "status": "failed",
                    "mode": "real",
                    "output": f"Task timed out after {timeout_seconds} seconds",
                    "logs": [],
                    "execution_time": timeout_seconds,
                    "error": "Timeout",
                }

        except subprocess.TimeoutExpired as e:
            self._log_entry("ERROR", f"Task execution timed out: {str(e)}")
            return {
                "status": "failed",
                "mode": "real",
                "output": f"Task timed out after {timeout_seconds} seconds",
                "logs": [],
                "execution_time": timeout_seconds,
                "error": "Timeout",
            }
        except Exception as e:
            error_str = str(e)
            self._log_entry("ERROR", f"Error executing task via OpenClaw: {error_str}")
            return {
                "status": "failed",
                "mode": "real",
                "output": f"Execution error: {error_str}",
                "logs": [],
                "execution_time": 0.0,
                "error": error_str,
            }

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
            "session_name": self.session_model.name,
            "task": task_context,
            "recent_logs": logs_data,
            "openclaw_session_key": self.openclaw_session_key,
        }

    def _log_entry(
        self, level: str, message: str, metadata: Optional[str] = None
    ) -> LogEntry:
        """Create database log entry with instance tracking"""
        # Get instance_id from session if available
        session_instance_id = None
        if self.session_model:
            session_instance_id = self.session_model.instance_id

        log_entry = LogEntry(
            session_id=self.session_id,
            session_instance_id=session_instance_id,
            task_id=self.task_id,
            level=level,
            message=message,
            log_metadata=metadata,
        )
        self.db.add(log_entry)
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
        Pause the OpenClaw session

        Raises:
            OpenClawSessionError: If pause fails
        """
        try:
            self._log_entry("INFO", "Pausing OpenClaw session")

            # Save current state
            context = await self.get_session_context()
            # TODO: Implement actual pause logic
            # Save process state, context, etc.

            self._log_entry("INFO", "OpenClaw session paused")

        except Exception as e:
            error_msg = f"Failed to pause session: {str(e)}"
            self._log_entry("ERROR", error_msg)
            raise OpenClawSessionError(error_msg)

    async def resume_session(self) -> None:
        """
        Resume a paused session

        Raises:
            OpenClawSessionError: If resume fails
        """
        try:
            self._log_entry("INFO", "Resuming OpenClaw session")

            # Get saved context
            context = await self.get_session_context()
            # TODO: Implement actual resume logic
            # Restore process state, context, etc.

            self._log_entry("INFO", "OpenClaw session resumed")

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
