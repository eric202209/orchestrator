"""LLM Prompt Templates

Standardized prompt templates for OpenClaw LLM interactions.
Templates are designed for AI development agent tasks.

Architecture:
- Orchestration follows a strict state machine:
  PLANNING → EXECUTING (step-by-step) → DEBUGGING (on failure) → PLAN_REVISION (if needed) → DONE
- Key design principles:
  - Planning and execution are always separate LLM calls
  - Execution is step-gated (one step per call, not bulk)
  - Debugging receives full attempt history, not just the latest error
  - Plan revision preserves completed steps and only rewrites remaining ones
  - Every step has a machine-runnable verification command and rollback command
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
from enum import Enum

# ---------------------------------------------------------------------------
# Workspace constants
# ---------------------------------------------------------------------------

#: Root directory where ALL OpenClaw projects live.
#: Resolved once at import time so every part of the codebase agrees on the path.
OPENCLAW_WORKSPACE_ROOT: Path = (
    Path(os.environ.get("OPENCLAW_WORKSPACE", "~/.openclaw/workspace/vault/projects/"))
    .expanduser()
    .resolve()
)


# ---------------------------------------------------------------------------
# Orchestration State
# ---------------------------------------------------------------------------


class OrchestrationStatus(str, Enum):
    """Orchestration phases"""

    PLANNING = "planning"
    EXECUTING = "executing"
    DEBUGGING = "debugging"
    REVISING_PLAN = "revising_plan"
    SUMMARIZING = "summarizing"
    DONE = "done"
    ABORTED = "aborted"


@dataclass
class StepResult:
    """Result of executing a single step"""

    step_number: int
    status: str  # "success" | "failed"
    output: str = ""
    verification_output: str = ""
    files_changed: List[str] = field(default_factory=list)
    error_message: str = ""
    attempt: int = 1


@dataclass
class OrchestrationState:
    """
    Carries all context through the full plan → execute → debug → revise cycle.
    Pass this into every PromptTemplates builder so the LLM always has full history.

    Workspace layout (new architecture):
    ~/.openclaw/workspace/vault/projects/
      <project_name>/                    ← project workspace
        task_{task_id}/                  ← task-specific subfolder
          ...source files...
          .openclaw/
            session_<id>.json            ← session manifest
    """

    session_id: str
    task_description: str
    project_name: str = ""  # e.g. "TalentBridge" (slug, no spaces)
    project_context: str = ""
    task_id: Optional[int] = None  # For generating task subfolder
    plan: List[Dict[str, Any]] = field(default_factory=list)
    current_step_index: int = 0
    execution_results: List[StepResult] = field(default_factory=list)
    debug_attempts: List[Dict[str, Any]] = field(
        default_factory=list
    )  # per-step retry history
    changed_files: List[str] = field(default_factory=list)
    status: OrchestrationStatus = OrchestrationStatus.PLANNING
    abort_reason: str = ""
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Optional: Override workspace path (for database-configured paths)
    _workspace_path_override: Optional[str] = None
    # Optional: Override task subfolder
    _task_subfolder_override: Optional[str] = None

    # ── Workspace paths ──────────────────────────────────────────────────────

    @property
    def workspace_root(self) -> Path:
        """Absolute path to ~/.openclaw/workspace/vault/projects/ (or $OPENCLAW_WORKSPACE)."""
        return OPENCLAW_WORKSPACE_ROOT

    def _slugify(self, text: str) -> str:
        """
        Convert text to a filesystem-safe slug.
        - Lowercase
        - Replace spaces and special chars with hyphens
        - Remove consecutive hyphens
        - Strip leading/trailing hyphens
        """
        import re

        text = text.lower().strip()
        # Replace any non-alphanumeric char (except hyphens) with hyphen
        text = re.sub(r"[^a-z0-9-]", "-", text)
        # Replace multiple hyphens with single hyphen
        text = re.sub(r"-+", "-", text)
        # Strip leading/trailing hyphens
        text = text.strip("-")
        return text or f"session-{self.session_id}"

    @property
    def project_workspace_path(self) -> Path:
        """
        Absolute path to the project's workspace folder.
        Uses override if set, otherwise uses slugified project name.
        """
        if self._workspace_path_override:
            return Path(self._workspace_path_override)

        slug = (
            self._slugify(self.project_name.strip())
            if self.project_name.strip()
            else f"project-{self.session_id}"
        )
        return self.workspace_root / slug

    @property
    def task_subfolder(self) -> str:
        """
        Get the task subfolder name.
        Format: task_{id} or task_{slugified_name}
        """
        if self._task_subfolder_override:
            return self._task_subfolder_override

        if self.task_id:
            return f"task-{self.task_id}"
        elif self.project_name:
            return f"task-{self._slugify(self.project_name)}"
        else:
            return f"task-{self.session_id}"

    @property
    def project_dir(self) -> Path:
        """
        Absolute path to this task's workspace directory.
        Structure: workspace_root / project_workspace / task_subfolder
        """
        return self.project_workspace_path / self.task_subfolder

    @property
    def session_manifest_path(self) -> Path:
        """Path where the session JSON manifest is saved on close."""
        return self.project_dir / ".openclaw" / f"session_{self.session_id}.json"

    # ── convenience helpers ──────────────────────────────────────────────────

    @property
    def completed_steps(self) -> List[Dict[str, Any]]:
        return self.plan[: self.current_step_index]

    @property
    def current_step(self) -> Optional[Dict[str, Any]]:
        if self.current_step_index < len(self.plan):
            return self.plan[self.current_step_index]
        return None

    @property
    def remaining_steps(self) -> List[Dict[str, Any]]:
        return self.plan[self.current_step_index :]

    def record_success(self, result: StepResult) -> None:
        self.execution_results.append(result)
        self.changed_files.extend(result.files_changed)
        self.debug_attempts.clear()  # reset per-step retry buffer
        self.current_step_index += 1

    def record_failure(self, result: StepResult) -> None:
        self.debug_attempts.append(
            {
                "attempt": result.attempt,
                "error": result.error_message,
                "output": result.output,
            }
        )

    def prior_results_summary(self) -> str:
        if not self.execution_results:
            return "No steps completed yet."
        lines = []
        for r in self.execution_results:
            lines.append(
                f"  Step {r.step_number}: {r.status.upper()} — {r.output[:120]}"
                + (f" | files: {', '.join(r.files_changed)}" if r.files_changed else "")
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Token Estimation Helper
# ---------------------------------------------------------------------------


def estimate_token_count(text: str) -> int:
    """
    Rough token count estimate (1 token ≈ 4 chars for English text).
    This is conservative to avoid context window overflow.
    """
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Prompt Templates
# ---------------------------------------------------------------------------


class PromptTemplates:
    """
    Collection of LLM prompt templates for OpenClaw orchestration.

    Phase templates (call in order):
    1. TASK_PLANNING — one call, produces JSON step plan
    2. STEP_EXECUTION — one call per step
    3. DEBUGGING_TASK — one call per failed attempt
    4. PLAN_REVISION — one call when debugging signals fix_type=revise_plan
    5. TASK_SUMMARY — one call at the end

    Supporting templates:
    CODE_IMPLEMENTATION, CODE_REVIEW, GIT_COMMIT, TESTING_STRATEGY,
    DEPLOYMENT_CHECKLIST, SESSION_CONTEXT, ERROR_RECOVERY,
    TOOL_USAGE_GUIDE, STATUS_REPORT

    Note: All prompts are designed to be concise to avoid context window overflow.
    """

    # ── 1. PLANNING (Concise version) ─────────────────────────────────────────

    TASK_PLANNING = """You are an AI development agent orchestrator. Produce a precise, executable plan — do NOT implement yet.

**Task:** {task_description}

**Context:** {project_context}

**Workspace:**
- Root: {workspace_root}
- Project: {project_dir}

**Execution Boundary:**
1. Every command MUST run inside `{project_dir}`
2. Use relative paths only
3. Do NOT use `..`, `~`, or absolute paths
4. Do NOT create sibling project folders under `{workspace_root}`
5. Assume the working directory is already `{project_dir}`

**Requirements:**
1. Create 3-8 sequential steps
2. Each step: atomic, verifiable, rollback-safe
3. Output JSON array with: step_number, description, commands[], verification?, rollback?, expected_files[]
4. Do NOT create documentation files unless the task explicitly asks for them
5. Avoid README files, notes files, summaries, or explanation documents unless required by the task

**Output (JSON ONLY):**
[
  {{
    "step_number": 1,
    "description": "...",
    "commands": ["..."],
    "verification": "..." or null,
    "rollback": "..." or null,
    "expected_files": ["..."]
  }}
]
"""

    # ── 2. STEP EXECUTION (Concise) ───────────────────────────────────────────

    STEP_EXECUTION = """Execute this step.

**Step:** {step_description}

**Working Directory:** {project_dir}

**Commands:**
{step_commands}

**Verify:** {verification_command}
**Rollback:** {rollback_command}
**Expected Files:**
{expected_files}

**Previous:** {completed_steps_summary}

**Context:** {project_context}

**Path Rules:**
1. Run everything from `{project_dir}`
2. Treat all file paths as relative to `{project_dir}`
3. Do NOT use `..`, `~`, or absolute paths
4. Do NOT create files or folders outside `{project_dir}`
5. Do NOT create documentation files unless the task explicitly requires them
6. Avoid README files, notes files, summaries, or explanation documents unless required by the task

**Output:** status, output, verification_output, files_changed, error_message
"""

    # ── 3. DEBUGGING (Concise) ────────────────────────────────────────────────

    DEBUGGING_TASK = """Debug this failed step.

**Failed:** {step_description}

**Error:** {error_message}

**Output:** {command_output}... (truncated)

**Verify Output:** {verification_output}

**Attempt:** {attempt_number}/{max_attempts}

**History:** {prior_debug_attempts}

**Context:** {project_name} @ {workspace_root}
**Working Directory:** {project_dir}

**Path Rules:**
1. Keep every proposed fix inside `{project_dir}`
2. Use relative paths only
3. Do NOT use `..`, `~`, or absolute paths
4. Do NOT suggest creating or modifying files outside `{project_dir}`
5. Do NOT propose documentation files unless the task explicitly requires them
6. Avoid README files, notes files, summaries, or explanation documents unless required by the task

**Output (JSON):**
{{
  "fix_type": "code_fix" | "command_fix" | "revise_plan",
  "analysis": "...",
  "fix": "...",
  "confidence": "HIGH" | "MEDIUM" | "LOW"
}}
"""

    # ── 4. PLAN REVISION (Concise) ────────────────────────────────────────────

    PLAN_REVISION = """Revise plan after failures.

**Original Plan:** {original_plan_truncated}...

**Failed Steps:** {failed_steps}

**Debug Analysis:** {debug_analysis_truncated}...

**Completed (preserve):** {completed_steps}

**Working Directory:** {project_dir}

**Path Rules:**
1. Revised commands MUST stay inside `{project_dir}`
2. Use relative paths only
3. Do NOT use `..`, `~`, or absolute paths
4. Do NOT create sibling folders under `{workspace_root}`
5. Do NOT add documentation-only steps unless the task explicitly requires them
6. Avoid README files, notes files, summaries, or explanation documents unless required by the task

**Output (JSON):**
{{
  "revised_plan": [...],
  "changes_made": ["..."],
  "confidence": "HIGH" | "MEDIUM" | "LOW"
}}
"""

    # ── 5. TASK SUMMARY (Concise) ─────────────────────────────────────────────

    TASK_SUMMARY = """Summarize completed task.

**Task:** {task_description}

**Plan:** {plan_summary}

**Results:** {execution_results_summary}

**Files:** {changed_files}

**Debugs:** {num_debug_attempts}

**Status:** {final_status}

**Output:** Concise summary for human reviewer
"""

    # ── Supporting Templates (Standalone) ────────────────────────────────────

    CODE_IMPLEMENTATION = """You are a senior software engineer. Implement the following requirement.

**Implementation Task:**
{implementation_task}

**Current Context:**
{current_context}

**Files to Modify:**
{files_to_modify}

**Constraints:**
{constraints}

**Requirements:**
1. Write clean, production-ready code.
2. Follow existing code patterns and style.
3. Add appropriate error handling.
4. Comment complex logic.
5. Ensure type safety where the language supports it.
6. Write or update unit tests for changed functionality.

**Output:**
Provide complete code changes with file paths. Briefly explain your approach.
"""

    # ── TASK EXECUTION (Simple single-call mode) ─────────────────────────────────

    TASK_EXECUTION = """You are an AI development agent. Execute this task.

**Task:** {task_description}

**Project Context:** {project_context}
**Recent Activity:** {recent_activity}
**Available Tools:**
{available_tools}

**Instructions:**
1. Complete the task efficiently
2. Use appropriate tools for the job
3. Provide clear output explaining what you did
4. Handle errors gracefully

**Output:** Explain your actions and results
"""

    CODE_REVIEW = """You are a senior code reviewer. Review the following changes.

**Code Changes:**
{code_changes}

**Review Criteria:**
1. Code quality and readability
2. Performance implications
3. Security vulnerabilities
4. Test coverage adequacy
5. Error handling completeness
6. Consistency with codebase patterns

**Output:**
- Overall assessment: APPROVE | REQUEST_CHANGES
- Specific issues (file, line, severity: BLOCKER / MAJOR / MINOR, description)
- Improvement suggestions
- Confidence: HIGH | MEDIUM | LOW
"""

    GIT_COMMIT = """You are a git commit message generator.

**Changes Summary:**
{changes_summary}

**Files Modified:**
{files_modified}

**Requirements:**
1. Use conventional commit format: <type>(<scope>): <subject>
2. Types: feat, fix, chore, docs, style, refactor, test, perf
3. Subject line ≤ 72 chars.
4. Add body paragraph if the change is non-obvious.
5. Reference related issues if applicable.

**Output:**
The commit message only — no extra commentary.
"""

    TESTING_STRATEGY = """You are a test engineer. Design a testing strategy.

**Feature / Component:**
{feature_description}

**Context:**
{test_context}

**Requirements:**
1. Identify test scenarios: happy path, edge cases, error cases.
2. Specify test types: unit, integration, E2E.
3. Define test data and mocks needed.
4. Estimate coverage percentage.
5. Prioritize test cases (P0 / P1 / P2).

**Output:**
A test plan with: scenarios, test cases (description + expected result), data requirements,
execution order, and coverage goals.
"""

    DEPLOYMENT_CHECKLIST = """You are a DevOps engineer. Create a deployment plan.

**Application:** {app_name}
**Changes to Deploy:** {changes_description}
**Target Environment:** {environment}

**Requirements:**
1. Pre-deployment checks
2. Deployment steps with verification
3. Post-deployment smoke tests
4. Rollback procedure
5. Monitoring and alerting checklist

**Output:**
A step-by-step deployment checklist with verification points at each stage.
"""

    SESSION_CONTEXT = """You are an AI development agent. Here is your current session context.

**Session ID:** {session_id}
**Task:** {task_title}
**Status:** {task_status}
**Step:** {current_step} of {total_steps}

**Project Context:**
{project_context}

**Files on Disk:** {files_on_disk}
**Environment Variables:** {env_vars}
**Prior Step Outputs:** {prior_outputs}
**Recent Activity:**
{recent_activity}

**Instructions:**
Continue from the current step. Use context above for full continuity.
If critical information is missing, request it explicitly before proceeding.
"""

    ERROR_RECOVERY = """You encountered an error while executing a task.

**Error:** {error_message}
**Attempt:** {attempt_number} of {max_attempts}
**Failed Operation:** {failed_operation}
**Context:** {error_context}

**Recovery Steps:**
1. Analyze the error.
2. Determine if it's recoverable within {max_attempts} attempts.
3. Propose an alternative approach if needed.
4. Implement a fix or workaround.
5. Resume task execution.

**Output:**
- Error analysis
- Recovery strategy
- Concrete next steps
"""

    TOOL_USAGE_GUIDE = """You need to use a specific tool to accomplish a task.

**Tool:** {tool_name}
**Task:** {task_description}

**Tool Capabilities:**
{tool_capabilities}

**Required Parameters:**
{required_params}

**Optional Parameters:**
{optional_params}

**Examples:**
{examples}

**Output:**
The exact tool invocation with all required parameters. Explain your parameter choices.
"""

    STATUS_REPORT = """Generate a status report for this session.

**Session ID:** {session_id}
**Task:** {task_title}
**Progress:** {progress_percentage}%
**Current Status:** {current_status}

**Completed Steps:**
{completed_items}

**In Progress:**
{in_progress_items}

**Blocked / Failed:**
{blocked_items}

**Upcoming:**
{upcoming_items}

**Output:**
A clear status report covering current state, progress percentage, blockers, and ETA.
"""

    ORCHESTRATOR_MOBILE_ASSISTANT = """You are the OpenClaw assistant for Orchestrator.

Your job is to help the mobile user query Orchestrator status through the local helper script, not by guessing.

Architecture:
- clawmobile talks to OpenClaw
- OpenClaw runs on the GX10 host/container stack
- Orchestrator is a separate backend/frontend service
- To read Orchestrator state, use this helper script:
  `{script_path}`

Rules:
1. When the user asks for orchestrator status, dashboard health, projects, sessions, tasks, or recent activity, call the helper script first.
2. Do not invent live status from memory.
3. If the script returns JSON, summarize it clearly for mobile.
4. If the script fails, explain the failure briefly and mention the likely cause.
5. Keep answers concise and operational.

Command mapping:
- Overall orchestrator health or status:
  `{script_path} dashboard`
- List projects:
  `{script_path} projects`
- Project status:
  `{script_path} project-status <project_id>`
- Recent sessions:
  `{script_path} sessions`
- Sessions for a project:
  `{script_path} sessions <project_id>`
- Session summary:
  `{script_path} session-summary <session_id>`
- Project tasks:
  `{script_path} project-tasks <project_id>`
- Project tasks filtered by status:
  `{script_path} project-tasks <project_id> <status>`

How to respond:
- For dashboard requests, report projects, active/running sessions, task totals, failures, and recent activity.
- For project requests, report project name, active sessions, and task breakdown.
- For session requests, report session name, status, recent logs, and task progress.
- If IDs are missing and needed, first call `projects` or `sessions` to discover them.

Examples:
- User: "What's the status of the orchestrator?"
  Action: run `{script_path} dashboard`
- User: "Show my projects"
  Action: run `{script_path} projects`
- User: "How is session 12 doing?"
  Action: run `{script_path} session-summary 12`
"""

    # ── Class Methods ────────────────────────────────────────────────────────

    @classmethod
    def get_template(cls, template_name: str) -> Optional[str]:
        """Return a raw template string by name."""
        templates = {
            "task_planning": cls.TASK_PLANNING,
            "step_execution": cls.STEP_EXECUTION,
            "debugging": cls.DEBUGGING_TASK,
            "plan_revision": cls.PLAN_REVISION,
            "task_summary": cls.TASK_SUMMARY,
            "code_implementation": cls.CODE_IMPLEMENTATION,
            "code_review": cls.CODE_REVIEW,
            "git_commit": cls.GIT_COMMIT,
            "testing": cls.TESTING_STRATEGY,
            "deployment": cls.DEPLOYMENT_CHECKLIST,
            "session_context": cls.SESSION_CONTEXT,
            "error_recovery": cls.ERROR_RECOVERY,
            "tool_usage": cls.TOOL_USAGE_GUIDE,
            "status_report": cls.STATUS_REPORT,
            "orchestrator_mobile_assistant": cls.ORCHESTRATOR_MOBILE_ASSISTANT,
            "task_execution": cls.TASK_EXECUTION,
        }
        return templates.get(template_name.lower())

    @classmethod
    def render(cls, template_name: str, **context) -> str:
        """Render a template with provided context variables."""
        template = cls.get_template(template_name)
        if not template:
            # Get available templates by trying common names
            common_templates = [
                "task_execution",
                "task_planning",
                "task_debugging",
                "plan_revision",
                "task_summary",
                "tool_call",
                "agent_response",
            ]
            raise ValueError(
                f"Unknown template: '{template_name}'. "
                f"Available: {common_templates}"
            )
        return template.format(**context)

    @classmethod
    def build_planning_prompt(
        cls,
        task_description: str,
        project_context: Optional[str] = None,
        workspace_root: Optional[str] = None,
        project_dir: Optional[str] = None,
    ) -> str:
        """
        Build a prompt for task planning phase.

        Args:
            task_description: The task to plan.
            project_context: Additional context about the project.
            workspace_root: Workspace root path (defaults to OPENCLAW_WORKSPACE_ROOT).
            project_dir: Project directory path.

        Returns:
            Planning prompt string ready for LLM call.
        """
        ws_root = workspace_root or str(OPENCLAW_WORKSPACE_ROOT)

        # Create a slug from project context (remove spaces, special chars)
        import re

        if project_context:
            # Extract project name from description, convert to slug
            project_name = project_context.split()[0]  # Get first word
            # Create slug: lowercase, replace spaces/special chars with hyphens
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", project_name.lower()).strip("-")
        else:
            slug = "project"

        proj_dir = project_dir or f"{ws_root}/{slug}"

        context = {
            "task_description": task_description,
            "project_context": project_context or "No additional context provided.",
            "workspace_root": ws_root,
            "project_dir": proj_dir,
        }

        return cls.render("task_planning", **context)

    @classmethod
    def build_execution_prompt(
        cls,
        step_description: str,
        step_commands: List[str],
        project_dir: Optional[str] = None,
        verification_command: Optional[str] = None,
        rollback_command: Optional[str] = None,
        expected_files: Optional[List[str]] = None,
        completed_steps_summary: Optional[str] = None,
        project_context: Optional[str] = None,
    ) -> str:
        """
        Build a prompt for step execution phase.

        Args:
            step_description: Description of the current step.
            step_commands: Commands to execute for this step.
            verification_command: Command to verify success.
            rollback_command: Command to undo this step.
            expected_files: Files this step will create/modify.
            completed_steps_summary: Summary of completed steps.
            project_context: Additional context.

        Returns:
            Execution prompt string ready for LLM call.
        """
        context = {
            "step_description": step_description,
            "project_dir": project_dir or "Current task workspace",
            "step_commands": "\n".join(f"- {cmd}" for cmd in step_commands),
            "verification_command": verification_command or "None provided",
            "rollback_command": rollback_command or "None provided",
            "expected_files": "\n".join(f"- {f}" for f in (expected_files or []))
            or "None",
            "completed_steps_summary": completed_steps_summary
            or "No steps completed yet.",
            "project_context": project_context or "No additional context.",
        }

        return cls.render("step_execution", **context)

    @classmethod
    def build_debugging_prompt(
        cls,
        step_description: str,
        error_message: str,
        command_output: str,
        verification_output: str,
        attempt_number: int,
        max_attempts: int = 3,
        prior_debug_attempts: Optional[List[Dict]] = None,
        project_name: str = "",
        workspace_root: Optional[str] = None,
        project_dir: Optional[str] = None,
    ) -> str:
        """
        Build a prompt for debugging phase.

        Args:
            step_description: Description of the failed step.
            error_message: The error that occurred.
            command_output: Output from the failed commands.
            verification_output: Output from verification command.
            attempt_number: Current attempt number.
            max_attempts: Maximum allowed attempts.
            prior_debug_attempts: History of previous debug attempts.
            project_name: Name of the project.
            workspace_root: Workspace root path.

        Returns:
            Debugging prompt string ready for LLM call.
        """
        ws_root = workspace_root or str(OPENCLAW_WORKSPACE_ROOT)
        prior_attempts_text = (
            "\n".join(
                f"Attempt {a['attempt']}: {a['error']}"
                for a in (prior_debug_attempts or [])
            )
            or "No prior attempts."
        )

        # Pre-process values that need slicing or conditional logic
        truncated_output = command_output[:2000] if command_output else "No output"
        truncated_verification = (
            verification_output[:200] if verification_output else "None"
        )

        context = {
            "step_description": step_description,
            "error_message": error_message,
            "command_output": truncated_output,
            "verification_output": truncated_verification,
            "attempt_number": attempt_number,
            "max_attempts": max_attempts,
            "prior_debug_attempts": prior_attempts_text,
            "project_name": project_name,
            "workspace_root": ws_root,
            "project_dir": project_dir or "Current task workspace",
        }

        return cls.render("debugging", **context)

    @classmethod
    def build_task_prompt(
        cls,
        task_description: str,
        project_context: Optional[str] = None,
        recent_logs: Optional[List[Dict[str, Any]]] = None,
        available_tools: Optional[List[str]] = None,
    ) -> str:
        """
        Build a prompt for task execution (legacy single-mode method).

        This is kept for backward compatibility but the new orchestration
        workflow uses build_planning_prompt() + build_execution_prompt() + build_debugging_prompt().

        Args:
            task_description: The task to execute.
            project_context: Additional context about the project.
            recent_logs: Recent activity logs (last 5 used).
            available_tools: List of available tool names.

        Returns:
            Prompt string ready for LLM call.
        """
        project_ctx = project_context or "No additional context provided."

        logs_context = ""
        if recent_logs:
            logs_context = "\n\n**Recent Activity:**\n" + "\n".join(
                f"- [{log.get('level', 'INFO')}] {log.get('message', '')}"
                for log in recent_logs[-5:]
            )

        tools_list = available_tools or [
            "File operations",
            "Git operations",
            "Code execution",
            "API calls",
            "Database queries",
        ]

        context = {
            "task_description": task_description,
            "project_context": project_ctx,
            "recent_activity": logs_context,
            "available_tools": "\n".join(f"- {tool}" for tool in tools_list),
        }

        return cls.render("task_execution", **context)

    @classmethod
    def build_plan_revision_prompt(
        cls,
        original_plan: List[Dict[str, Any]],
        failed_steps: List[StepResult],
        debug_analysis: str,
        completed_steps: List[Dict[str, Any]],
        workspace_root: Optional[str] = None,
        project_dir: Optional[str] = None,
    ) -> str:
        """
        Build a prompt for plan revision phase.

        Args:
            original_plan: The original plan before failures.
            failed_steps: Steps that failed during execution.
            debug_analysis: Analysis from debugging phase.
            completed_steps: Steps that completed successfully.

        Returns:
            Plan revision prompt string.
        """
        original_plan_text = json.dumps(original_plan, indent=2)
        failed_steps_text = "\n".join(
            f"Step {s.step_number}: {s.error_message}" for s in failed_steps
        )
        completed_steps_text = "\n".join(
            f"Step {s['step_number']}: {s['description']}" for s in completed_steps
        )

        # Truncate long strings for the template
        truncated_original_plan = (
            original_plan_text[:500] + "..."
            if len(original_plan_text) > 500
            else original_plan_text
        )
        truncated_debug_analysis = (
            debug_analysis[:300] + "..."
            if len(debug_analysis) > 300
            else debug_analysis
        )

        context = {
            "original_plan_truncated": truncated_original_plan,
            "failed_steps": failed_steps_text,
            "debug_analysis_truncated": truncated_debug_analysis,
            "completed_steps": completed_steps_text,
            "workspace_root": workspace_root or str(OPENCLAW_WORKSPACE_ROOT),
            "project_dir": project_dir or "Current task workspace",
        }

        # Use the PLAN_REVISION template
        return cls.render("plan_revision", **context)

    @classmethod
    def build_orchestrator_mobile_assistant_prompt(
        cls, script_path: str = "./scripts/orchestrator-mobile-api.sh"
    ) -> str:
        """Build a copy-paste prompt for OpenClaw mobile orchestration queries."""
        return cls.render(
            "orchestrator_mobile_assistant",
            script_path=script_path,
        )

    @classmethod
    def build_task_summary(
        cls,
        task_description: str,
        plan_summary: str,
        execution_results_summary: str,
        changed_files: List[str],
        num_debug_attempts: int,
        final_status: str,
    ) -> str:
        """
        Build a prompt for task summary phase.

        Args:
            task_description: Original task description.
            plan_summary: Summary of the plan executed.
            execution_results_summary: Results of all steps.
            changed_files: Files that were created/modified.
            num_debug_attempts: Number of debug attempts made.
            final_status: Final task status.

        Returns:
            Task summary prompt string.
        """
        changed_files_text = "\n".join(f"- {f}" for f in changed_files)

        context = {
            "task_description": task_description,
            "plan_summary": plan_summary,
            "execution_results_summary": execution_results_summary,
            "changed_files": changed_files_text,
            "num_debug_attempts": num_debug_attempts,
            "final_status": final_status,
        }

        # Use the TASK_SUMMARY template
        return cls.TASK_SUMMARY.format(**context)
