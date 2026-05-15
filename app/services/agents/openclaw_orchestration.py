"""Multi-step OpenClaw orchestration methods.

These functions are assigned onto OpenClawSessionService to keep the public
method surface stable while keeping openclaw_service.py focused on lifecycle
and subprocess integration.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from app.config import settings
from app.models import Project
from app.services.orchestration.task_rules import (
    should_execute_in_canonical_project_root,
)
from app.services.prompt_templates import (
    OrchestrationState,
    OrchestrationStatus,
    PromptTemplates,
    StepResult,
)
from app.services.performance_optimizations import compress_context
from app.services.task_service import TaskService
from app.services.workspace.project_isolation_service import (
    ProjectIsolationService,
    resolve_project_workspace_path,
)


def _openclaw_session_error_type():
    from app.services.agents.openclaw_service import OpenClawSessionError

    return OpenClawSessionError


def _openclaw_session_error(message: str):
    return _openclaw_session_error_type()(message)


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
        raise _openclaw_session_error(
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
                    self._log_entry("INFO", "Project isolation safety prompt injected")
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
                    else (self.session_model.name if self.session_model else "Unknown")
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

        if self.task_model and self.task_model.steps and not orchestration_state.plan:
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
                    or getattr(self.session_model, "default_execution_profile", None)
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
                raise _openclaw_session_error(planning_error)

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
                                first_payload = payloads[0]
                                if isinstance(first_payload, dict):
                                    output_text = first_payload.get("text", output_text)
                    except json.JSONDecodeError as exc:
                        self._log_entry(
                            "DEBUG",
                            "[PLANNING] Output was not a payload envelope; "
                            f"using raw output: {exc}",
                        )

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
                    raise _openclaw_session_error(
                        "Planning output was not a valid step list"
                    )
            except json.JSONDecodeError:
                self._log_entry(
                    "WARN",
                    "[ORCHESTRATION] Planning output was not valid JSON; aborting instead of executing raw prompt",
                )
                raise _openclaw_session_error("Planning output was not valid JSON")

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
                raise _openclaw_session_error(
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
            self._log_entry("ERROR", f"[ORCHESTRATION] Summary failed: {summary_error}")
            raise _openclaw_session_error(summary_error)

        self._log_entry(
            "INFO", f"[ORCHESTRATION] Summary result type: {type(summary_result)}"
        )
        if isinstance(summary_result, str):
            self._log_entry(
                "ERROR",
                f"[ORCHESTRATION] Summary result is string, not dict! Content: {summary_result[:200]}",
            )
            raise _openclaw_session_error(
                f"Summary result is not a dict: {type(summary_result)}"
            )

        return {
            "status": "completed",
            "mode": "orchestration",
            "output": summary_result.get("output", "Task completed"),
            "backend": self.backend_descriptor.name,
            "model_family": settings.AGENT_MODEL,
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
        if isinstance(e, _openclaw_session_error_type()):
            raise
        raise _openclaw_session_error(f"Orchestration failed: {str(e)}")


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
                    or getattr(self.session_model, "default_execution_profile", None)
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

        except Exception as e:
            if not isinstance(e, _openclaw_session_error_type()):
                raise
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
    except json.JSONDecodeError as exc:
        self._log_entry(
            "WARN",
            "[PLAN_REVISION] Failed to parse revised plan JSON; keeping original plan: "
            f"{exc}",
        )

    return orchestration_state.plan
