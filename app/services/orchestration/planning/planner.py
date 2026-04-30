"""Planner-stage helpers for orchestration."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..policy import (
    MINIMAL_PLANNING_TIMEOUT_SECONDS,
    PLANNING_REPAIR_TIMEOUT_SECONDS,
    ULTRA_MINIMAL_PLANNING_TIMEOUT_SECONDS,
)
from app.services.workspace.path_display import render_workspace_path_for_prompt


class PlannerService:
    """Planning-stage fallback and repair helpers."""

    _NON_RUNNABLE_COMMAND_PREFIXES = (
        "write ",
        "edit ",
        "verify ",
        "check ",
        "ensure ",
        "confirm ",
    )

    _WEAK_VERIFICATION_MARKERS = (
        "test -f",
        "test -d",
        "grep -q",
        "ls ",
        "echo ",
        "cat ",
        "find ",
        "wc -l",
    )

    _STRONG_VERIFICATION_MARKERS = (
        "pytest",
        "python -m",
        "python ",
        "uv run",
        "node -e",
        "node ",
        "npm test",
        "npm run build",
        "pnpm test",
        "pnpm build",
        "yarn test",
        "yarn build",
        "cargo test",
        "go test",
        "javac ",
        "tsc",
    )

    @staticmethod
    def _render_workflow_guidance(
        workflow_profile: str = "default",
        workflow_phases: Optional[List[str]] = None,
        workspace_has_existing_files: bool = False,
    ) -> str:
        phases = workflow_phases or []
        lines: List[str] = []
        if phases:
            lines.append(f"Workflow profile: {workflow_profile}")
            lines.append("Follow this phase order exactly:")
            lines.extend(f"{idx}. {phase}" for idx, phase in enumerate(phases, start=1))
            lines.append("Keep steps grouped inside this sequence. Do not skip ahead.")
        if workspace_has_existing_files:
            lines.append(
                "Workspace already contains implementation files. Extend or verify existing files instead of re-scaffolding from scratch."
            )
        if workflow_profile == "fullstack_scaffold" or (
            "create_frontend_skeleton" in phases and "create_backend_skeleton" in phases
        ):
            lines.append(
                "Keep frontend work under `frontend/` and backend work under `app/` or `backend/` inside this same workspace."
            )
            lines.append(
                "Never use parent-directory traversal like `../backend` and never create sibling project folders."
            )
        return "\n".join(lines)

    @staticmethod
    def select_prompt_profile(
        backend_name: Optional[str],
        model_family: Optional[str],
    ) -> str:
        backend = (backend_name or "").strip().lower()
        model = (model_family or "").strip().lower()
        if backend == "local_openclaw" and ("qwen" in model or model == "local"):
            return "local_qwen_json_array"
        return "default"

    @staticmethod
    def apply_prompt_profile(prompt: str, prompt_profile: str = "default") -> str:
        if prompt_profile != "local_qwen_json_array":
            return prompt

        return (
            f"{prompt.rstrip()}\n\n"
            "Output discipline for this model:\n"
            "11. Return only a JSON array of steps. Do not wrap it in an object.\n"
            "12. Do not include `payloads`, `text`, `finalAssistantVisibleText`, markdown prose, or commentary.\n"
            "13. The first non-whitespace character must be `[` and the last must be `]`.\n"
            "14. Do not describe the file contents outside the JSON fields for each step.\n"
        )

    @staticmethod
    def looks_salvageable_planning_output(output_text: str) -> bool:
        """Heuristic for whether a failed planning response still contains useful plan content."""

        text = (output_text or "").strip()
        if not text:
            return False
        lowered = text.lower()
        planning_markers = (
            '"step_number"',
            '"commands"',
            '"expected_files"',
            '"description"',
            "finalassistantvisibletext",
            "```json",
            "[",
            "{",
        )
        return any(marker in lowered for marker in planning_markers)

    @staticmethod
    def should_retry_with_minimal_prompt(
        planning_result: Dict[str, Any], output_text: str = ""
    ) -> bool:
        error_text = (planning_result.get("error") or "").lower()
        combined_text = f"{error_text}\n{(output_text or '').lower()}"
        retry_markers = (
            "context window exceeded",
            "request timed out before a response was generated",
            "timed out",
            "timeout",
        )
        return any(marker in combined_text for marker in retry_markers)

    @staticmethod
    def should_start_with_minimal_prompt(
        task_prompt: str,
        project_context: str,
    ) -> bool:
        combined = f"{task_prompt or ''}\n{project_context or ''}"
        lowered_context = (project_context or "").lower()
        lowered_task = (task_prompt or "").lower()
        implementation_markers = (
            "set up",
            "setup",
            "build",
            "create",
            "implement",
            "frontend",
            "backend",
            "fastapi",
            "node.js",
            "react",
            "vite",
            "clean architecture",
        )
        dense_context_markers = (
            "hydrated baseline sources available directly in this workspace",
            "canonical baseline available",
            "earlier ordered tasks already completed and can be reused",
            "promoted workspaces already accepted into the project baseline",
        )
        compact_task_markers = (
            "regression test",
            "test suite",
            "integration test",
            "spec file",
            "unit test",
            "inspection",
            "analyze",
            "review",
        )
        task_looks_implementation_heavy = any(
            marker in lowered_task for marker in implementation_markers
        )
        return (
            len(combined) > 8000
            or len(project_context or "") > 3500
            or any(marker in lowered_context for marker in dense_context_markers)
            or (
                any(marker in lowered_task for marker in compact_task_markers)
                and not task_looks_implementation_heavy
            )
        )

    @staticmethod
    def _uses_background_process(command: str) -> bool:
        text = str(command or "").strip().lower()
        if not text:
            return False
        if re.search(r"(^|[^&])&(?=[^&]|$)", text):
            return True
        background_markers = (
            "nohup ",
            " disown",
            "tail -f",
            "npm run dev",
            "pnpm dev",
            "yarn dev",
            "vite dev",
            "next dev",
            "webpack serve",
        )
        return any(marker in text for marker in background_markers)

    @staticmethod
    def _command_is_placeholder_only(command: str) -> bool:
        text = str(command or "").strip().lower()
        if not text:
            return True
        placeholder_patterns = (
            r"^mkdir(?:\s|$)",
            r"^install\s+-d(?:\s|$)",
            r"^touch(?:\s|$)",
            r"^truncate\s+-s\s+0(?:\s|$)",
            r"^cp\s+/dev/null(?:\s|$)",
            r"^:\s*>\s*",
            r"^true$",
        )
        if any(re.match(pattern, text) for pattern in placeholder_patterns):
            return True
        empty_write_patterns = (
            r"^echo\s+(['\"]?\s*['\"]?)\s*(>|>>)\s+",
            r"^printf\s+(['\"]?\s*['\"]?)\s*(>|>>)\s+",
        )
        return any(re.match(pattern, text) for pattern in empty_write_patterns)

    @staticmethod
    def _step_is_implementation_heavy(step: Dict[str, Any]) -> bool:
        expected_files = [
            str(path or "").strip()
            for path in (step.get("expected_files", []) or [])
            if str(path or "").strip()
        ]
        if any(not path.endswith("/") for path in expected_files):
            return True

        combined = " ".join(
            [
                str(step.get("description") or ""),
                str(step.get("verification") or ""),
            ]
            + [str(command or "") for command in step.get("commands", []) or []]
        ).lower()
        implementation_markers = (
            "create",
            "implement",
            "build",
            "update",
            "modify",
            "wire",
            "scaffold",
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".html",
            ".css",
        )
        inspection_markers = (
            "inspect",
            "review",
            "analyze",
            "inventory",
            "audit",
            "list files",
        )
        return any(marker in combined for marker in implementation_markers) and not any(
            marker in combined for marker in inspection_markers
        )

    @classmethod
    def _verification_is_weak(cls, command: Optional[str]) -> bool:
        text = str(command or "").strip().lower()
        if not text:
            return True
        if any(marker in text for marker in cls._STRONG_VERIFICATION_MARKERS):
            return False
        return any(marker in text for marker in cls._WEAK_VERIFICATION_MARKERS)

    @staticmethod
    def find_immediate_repair_step_issues(
        plan: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, List[int]]:
        issues: Dict[str, List[int]] = {
            "non_runnable_steps": [],
            "background_process_steps": [],
            "placeholder_only_steps": [],
            "weak_verification_steps": [],
        }
        for index, step in enumerate(plan or [], start=1):
            step_number = int(step.get("step_number") or index)
            commands = step.get("commands", []) or []
            expected_files = step.get("expected_files", []) or []
            for command in commands:
                rendered = str(command or "").strip()
                lowered = rendered.lower()
                if lowered.startswith(PlannerService._NON_RUNNABLE_COMMAND_PREFIXES):
                    issues["non_runnable_steps"].append(step_number)
                    break
                if PlannerService._uses_background_process(rendered):
                    issues["background_process_steps"].append(step_number)
                    break
            if expected_files and PlannerService._step_is_implementation_heavy(step):
                if commands and all(
                    PlannerService._command_is_placeholder_only(command)
                    for command in commands
                ):
                    issues["placeholder_only_steps"].append(step_number)
                if PlannerService._verification_is_weak(step.get("verification")):
                    issues["weak_verification_steps"].append(step_number)
        return {key: sorted(set(value)) for key, value in issues.items() if value}

    @staticmethod
    def build_minimal_planning_prompt(
        task_description: str,
        project_dir: Path,
        prompt_profile: str = "default",
        workflow_profile: str = "default",
        workflow_phases: Optional[List[str]] = None,
        workspace_has_existing_files: bool = False,
    ) -> str:
        concise_task = " ".join((task_description or "").split())[:1200]
        display_project_dir = render_workspace_path_for_prompt(project_dir)
        workflow_guidance = PlannerService._render_workflow_guidance(
            workflow_profile=workflow_profile,
            workflow_phases=workflow_phases,
            workspace_has_existing_files=workspace_has_existing_files,
        )
        prompt = f"""Produce a JSON-only execution plan for this software task. Do not implement anything.

Task:
{concise_task}

Workflow:
{workflow_guidance or "No explicit workflow phases. Use the smallest valid sequential plan."}

Rules:
1. Assume working directory is {display_project_dir}
2. Use relative paths only in shell commands and expected_files
3. If a step will later need file-read or file-write tools, keep the planned path relative; the executor will expand it to an absolute path under {display_project_dir}
4. Do not use absolute paths, .., or ~
5. Return 3 to 6 small sequential steps
6. Each step must include: step_number, description, commands, verification, rollback, expected_files
7. `commands` must be an array of strings
8. `verification` must be a single shell string or null
9. `rollback` must be a single shell string or null
10. expected_files must be relative file paths or []
11. Do not use `cat > file <<EOF`, heredocs, or multi-line inline file creation in planning output
12. Do not join separate shell commands with commas
13. Do not use background processes, `&`, `nohup`, `disown`, or long-running dev servers
14. Prefer one-shot verification commands like imports, builds, tests, grep, or short health checks
15. Prefer package-manager/editor-friendly commands and one-file-at-a-time edits
16. Output JSON array only
17. If the workspace already has files, start by inspecting or extending them before re-scaffolding
18. For implementation steps that list expected_files, at least one command must materially write or edit file contents; do not use touch-only or placeholder-only steps
19. For implementation-heavy steps, verification must prove behavior or content, not only file existence
20. Prefer an inspect -> edit -> verify sequence grounded in the current workspace
"""
        return PlannerService.apply_prompt_profile(prompt, prompt_profile)

    @staticmethod
    def build_ultra_minimal_planning_prompt(
        task_description: str,
        project_dir: Path,
        prompt_profile: str = "default",
        workflow_profile: str = "default",
        workflow_phases: Optional[List[str]] = None,
        workspace_has_existing_files: bool = False,
    ) -> str:
        concise_task = " ".join((task_description or "").split())[:700]
        display_project_dir = render_workspace_path_for_prompt(project_dir)
        workflow_guidance = PlannerService._render_workflow_guidance(
            workflow_profile=workflow_profile,
            workflow_phases=workflow_phases,
            workspace_has_existing_files=workspace_has_existing_files,
        )
        prompt = f"""Return JSON array only. No prose.

Task:
{concise_task}

Working directory: {display_project_dir}
Workflow:
{workflow_guidance or "No explicit workflow phases."}

Requirements:
1. 2 to 5 steps only
2. Use short relative shell commands only, and keep expected_files relative
3. If a step will later use file-read or file-write tools, keep that path relative in the plan; execution will expand it under {display_project_dir}
4. No heredocs, no long inline source dumps, no absolute paths, no .., no ~
5. Each step must contain exactly these keys:
   step_number, description, commands, verification, rollback, expected_files
6. `verification` and `rollback` must each be one shell string or null
7. No background processes or long-running servers
8. Keep each command short and machine-runnable
9. If the workspace already has files, inspect or extend them before re-scaffolding
10. For implementation steps with expected_files, include at least one command that writes real file content, not just mkdir/touch
11. For implementation-heavy steps, use verification stronger than file-existence checks
"""
        return PlannerService.apply_prompt_profile(prompt, prompt_profile)

    @staticmethod
    def _looks_like_timeout_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "timed out" in message or "timeout" in message

    @staticmethod
    def build_planning_repair_prompt(
        task_description: str,
        malformed_output: str,
        project_dir: Path,
        rejection_reasons: Optional[List[str]] = None,
        prompt_profile: str = "default",
        workflow_profile: str = "default",
        workflow_phases: Optional[List[str]] = None,
        workspace_has_existing_files: bool = False,
    ) -> str:
        concise_task = " ".join((task_description or "").split())[:2000]
        broken_output = (malformed_output or "")[:8000]
        display_project_dir = render_workspace_path_for_prompt(project_dir)
        workflow_guidance = PlannerService._render_workflow_guidance(
            workflow_profile=workflow_profile,
            workflow_phases=workflow_phases,
            workspace_has_existing_files=workspace_has_existing_files,
        )
        structured_feedback = ""
        if rejection_reasons:
            reason_lines = "\n".join(
                f"- {reason[:300]}" for reason in rejection_reasons[:8]
            )
            structured_feedback = (
                "\nPrevious validator rejection reasons:\n"
                f"{reason_lines}\n"
                "You must address every rejection reason in the repaired plan.\n"
            )
        prompt = f"""Repair this malformed planning output into valid machine-runnable JSON. Return JSON array only.

Task:
{concise_task}

Working directory:
{display_project_dir}

Workflow:
{workflow_guidance or "No explicit workflow phases."}

Malformed planning output:
{broken_output}
{structured_feedback}

Rules:
1. Return a JSON array only
2. Keep 3 to 8 sequential steps
3. Each step must include: step_number, description, commands, verification, rollback, expected_files
4. `commands` must be an array of strings
5. `verification` must be one shell string or null
6. `rollback` must be one shell string or null
7. Use relative paths only in shell commands and expected_files
8. If a step will later use file-read or file-write tools, keep that path relative here; execution will expand it under {display_project_dir}
9. Do not use absolute paths, .., or ~
10. Do not use heredocs, `cat > file <<EOF`, or multi-line inline file dumps in the repaired plan
11. Do not join separate shell commands with commas
12. Do not use background processes, `&`, `nohup`, `disown`, or long-running dev servers
13. Prefer short setup/edit commands over dumping full source files in planning output
14. If the malformed output contains oversized inline file content, replace it with smaller setup/edit commands that preserve the same step intent
15. expected_files must be a JSON array of relative file paths
16. Never repeat workspace root segments inside a path, such as `frontend/src/frontend/src` or `backend/src/backend/src`
17. Paths must be rooted exactly once from the canonical project workspace
18. If the workspace already has files, inspect or extend them before re-scaffolding
19. For implementation steps with expected_files, include at least one command that writes or edits real file contents
20. Do not return placeholder-only steps that only mkdir, touch, or create empty files
21. For implementation-heavy steps, verification must prove behavior or content, not only file existence
"""
        return PlannerService.apply_prompt_profile(prompt, prompt_profile)

    @classmethod
    def retry_with_minimal_prompt(
        cls,
        runtime_service: Any,
        task_description: str,
        project_dir: Path,
        timeout_seconds: int,
        logger: logging.Logger,
        emit_live: Any,
        reason: str,
        rejection_reasons: Optional[List[str]] = None,
        prompt_profile: str = "default",
        workflow_profile: str = "default",
        workflow_phases: Optional[List[str]] = None,
        workspace_has_existing_files: bool = False,
    ) -> Dict[str, Any]:
        logger.warning(
            "[ORCHESTRATION] Planning output was not machine-parseable; "
            f"retrying with minimal prompt ({reason})"
        )
        minimal_timeout = min(timeout_seconds, MINIMAL_PLANNING_TIMEOUT_SECONDS)
        emit_live(
            "WARN",
            (
                "[ORCHESTRATION] Planning output needed a strict JSON retry; "
                f"starting minimal prompt attempt (timeout: {minimal_timeout}s)"
            ),
            metadata={
                "phase": "planning",
                "retry": "minimal_prompt",
                "reason": reason[:240],
                "timeout_seconds": minimal_timeout,
            },
        )
        emit_live(
            "INFO",
            (
                "[ORCHESTRATION] Planning attempt 2 is now running with the minimal "
                f"prompt (timeout: {minimal_timeout}s)"
            ),
            metadata={
                "phase": "planning",
                "attempt": 2,
                "strategy": "minimal_prompt",
                "timeout_seconds": minimal_timeout,
            },
        )
        try:
            return asyncio.run(
                runtime_service.execute_task(
                    cls.build_minimal_planning_prompt(
                        task_description,
                        project_dir,
                        prompt_profile=prompt_profile,
                        workflow_profile=workflow_profile,
                        workflow_phases=workflow_phases,
                        workspace_has_existing_files=workspace_has_existing_files,
                    ),
                    timeout_seconds=minimal_timeout,
                )
            )
        except Exception as exc:
            if not cls._looks_like_timeout_error(exc):
                raise
            ultra_minimal_timeout = min(
                timeout_seconds, ULTRA_MINIMAL_PLANNING_TIMEOUT_SECONDS
            )
            logger.warning(
                "[ORCHESTRATION] Minimal planning prompt timed out; retrying with ultra-minimal prompt"
            )
            emit_live(
                "WARN",
                (
                    "[ORCHESTRATION] Minimal planning timed out; retrying with "
                    f"ultra-minimal prompt (timeout: {ultra_minimal_timeout}s)"
                ),
                metadata={
                    "phase": "planning",
                    "retry": "ultra_minimal_prompt",
                    "reason": str(exc)[:240],
                    "timeout_seconds": ultra_minimal_timeout,
                },
            )
            emit_live(
                "INFO",
                (
                    "[ORCHESTRATION] Planning attempt 3 is now running with the "
                    f"ultra-minimal prompt (timeout: {ultra_minimal_timeout}s)"
                ),
                metadata={
                    "phase": "planning",
                    "attempt": 3,
                    "strategy": "ultra_minimal_prompt",
                    "timeout_seconds": ultra_minimal_timeout,
                },
            )
            return asyncio.run(
                runtime_service.execute_task(
                    cls.build_ultra_minimal_planning_prompt(
                        task_description,
                        project_dir,
                        prompt_profile=prompt_profile,
                        workflow_profile=workflow_profile,
                        workflow_phases=workflow_phases,
                        workspace_has_existing_files=workspace_has_existing_files,
                    ),
                    timeout_seconds=ultra_minimal_timeout,
                )
            )

    @classmethod
    def repair_output(
        cls,
        runtime_service: Any,
        task_description: str,
        malformed_output: str,
        project_dir: Path,
        timeout_seconds: int,
        logger: logging.Logger,
        emit_live: Any,
        reason: str,
        rejection_reasons: Optional[List[str]] = None,
        prompt_profile: str = "default",
        workflow_profile: str = "default",
        workflow_phases: Optional[List[str]] = None,
        workspace_has_existing_files: bool = False,
    ) -> Dict[str, Any]:
        logger.warning(
            "[ORCHESTRATION] Planning output was malformed but salvageable; "
            f"attempting repair ({reason})"
        )
        repair_timeout = min(timeout_seconds, PLANNING_REPAIR_TIMEOUT_SECONDS)
        emit_live(
            "WARN",
            (
                "[ORCHESTRATION] Planning output was malformed; attempting one "
                f"repair pass (timeout: {repair_timeout}s)"
            ),
            metadata={
                "phase": "planning",
                "retry": "repair_prompt",
                "reason": reason[:240],
                "timeout_seconds": repair_timeout,
            },
        )
        emit_live(
            "INFO",
            (
                "[ORCHESTRATION] Planning repair attempt is now running "
                f"(timeout: {repair_timeout}s)"
            ),
            metadata={
                "phase": "planning",
                "attempt": "repair",
                "strategy": "repair_prompt",
                "timeout_seconds": repair_timeout,
            },
        )
        try:
            return asyncio.run(
                runtime_service.execute_task(
                    cls.build_planning_repair_prompt(
                        task_description,
                        malformed_output,
                        project_dir,
                        rejection_reasons=rejection_reasons,
                        prompt_profile=prompt_profile,
                        workflow_profile=workflow_profile,
                        workflow_phases=workflow_phases,
                        workspace_has_existing_files=workspace_has_existing_files,
                    ),
                    timeout_seconds=repair_timeout,
                )
            )
        except Exception as exc:
            if not cls._looks_like_timeout_error(exc):
                raise
            ultra_minimal_timeout = min(
                timeout_seconds, ULTRA_MINIMAL_PLANNING_TIMEOUT_SECONDS
            )
            logger.warning(
                "[ORCHESTRATION] Planning repair prompt timed out; retrying with ultra-minimal prompt"
            )
            emit_live(
                "WARN",
                (
                    "[ORCHESTRATION] Planning repair timed out; retrying with "
                    f"ultra-minimal prompt (timeout: {ultra_minimal_timeout}s)"
                ),
                metadata={
                    "phase": "planning",
                    "retry": "ultra_minimal_prompt",
                    "reason": str(exc)[:240],
                    "timeout_seconds": ultra_minimal_timeout,
                },
            )
            emit_live(
                "INFO",
                (
                    "[ORCHESTRATION] Planning attempt after repair is now running "
                    f"with the ultra-minimal prompt (timeout: {ultra_minimal_timeout}s)"
                ),
                metadata={
                    "phase": "planning",
                    "attempt": "repair_fallback",
                    "strategy": "ultra_minimal_prompt",
                    "timeout_seconds": ultra_minimal_timeout,
                },
            )
            return asyncio.run(
                runtime_service.execute_task(
                    cls.build_ultra_minimal_planning_prompt(
                        task_description,
                        project_dir,
                        prompt_profile=prompt_profile,
                        workflow_profile=workflow_profile,
                        workflow_phases=workflow_phases,
                        workspace_has_existing_files=workspace_has_existing_files,
                    ),
                    timeout_seconds=ultra_minimal_timeout,
                )
            )
