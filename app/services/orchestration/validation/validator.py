"""Rule-first orchestration validation helpers."""

from __future__ import annotations

import ast
import json
import re
import shlex
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.models import TaskCheckpoint
from ..policy import apply_validation_policy
from ..types import ValidationVerdict


SOURCE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx"}
DOC_NAMES = {"readme.md", "notes.md", "summary.md"}
ROOT_LEVEL_EXPECTED_DIRS = {
    "src",
    "tests",
    "test",
    "fixtures",
    "config",
    "docs",
    "scripts",
    "lib",
    "app",
    "spec",
    "frontend",
    "backend",
    "assets",
    "public",
    "static",
    "images",
    "img",
    "css",
    "js",
    ".github",
}
NESTED_PROJECT_STRUCTURAL_DIRS = {
    "src",
    "app",
    "public",
    "static",
    "assets",
    "frontend",
    "backend",
    "tests",
    "test",
    "docs",
    "scripts",
    "lib",
    "spec",
}
WORKFLOW_PHASE_ORDER = {
    "fullstack_scaffold": [
        "create_frontend_skeleton",
        "create_backend_skeleton",
        "wire_api_config",
        "verify_dev_startup",
    ]
}


class ValidatorService:
    """Deterministic plan and completion validation."""

    @staticmethod
    def _ordered_reasons(
        *,
        warnings: List[str],
        repairable: List[str],
        rejected: List[str],
    ) -> List[str]:
        """Return reasons in severity-first order for stable operator feedback."""

        return rejected + repairable + warnings

    @staticmethod
    def _select_status(
        *,
        warnings: List[str],
        repairable: List[str],
        rejected: List[str],
        severity: str = "standard",
        stage: str = "",
    ) -> str:
        if rejected:
            status = "rejected"
        elif repairable:
            status = "repair_required"
        elif warnings:
            status = "warning"
        else:
            status = "accepted"
        return apply_validation_policy(status, severity=severity, stage=stage)

    @staticmethod
    def validate_plan_schema(plan: Any) -> Dict[str, Any]:
        """Validate the structural schema of a plan independently of heuristics."""

        errors: List[str] = []
        details: Dict[str, Any] = {}
        if not isinstance(plan, list):
            return {
                "valid": False,
                "errors": ["Plan payload must be a list of step objects"],
                "details": {"received_type": type(plan).__name__},
            }

        non_dict_steps: List[int] = []
        invalid_step_numbers: List[int] = []
        invalid_descriptions: List[int] = []
        invalid_commands: List[int] = []
        invalid_expected_files: List[int] = []

        for index, step in enumerate(plan, start=1):
            if not isinstance(step, dict):
                non_dict_steps.append(index)
                continue
            if not isinstance(step.get("step_number"), int):
                invalid_step_numbers.append(index)
            if not isinstance(step.get("description", ""), str):
                invalid_descriptions.append(index)
            commands = step.get("commands", [])
            if not isinstance(commands, list) or any(
                not isinstance(command, str) for command in commands
            ):
                invalid_commands.append(index)
            expected_files = step.get("expected_files", [])
            if expected_files is not None and (
                not isinstance(expected_files, list)
                or any(not isinstance(path, str) for path in expected_files)
            ):
                invalid_expected_files.append(index)

        if non_dict_steps:
            errors.append("Plan contains non-object steps")
            details["non_dict_steps"] = non_dict_steps
        if invalid_step_numbers:
            errors.append("Plan steps must define integer step_number values")
            details["invalid_step_number_steps"] = invalid_step_numbers
        if invalid_descriptions:
            errors.append("Plan step descriptions must be strings")
            details["invalid_description_steps"] = invalid_descriptions
        if invalid_commands:
            errors.append("Plan step commands must be arrays of strings")
            details["invalid_commands_steps"] = invalid_commands
        if invalid_expected_files:
            errors.append("Plan expected_files must be arrays of strings")
            details["invalid_expected_files_steps"] = invalid_expected_files

        return {"valid": not errors, "errors": errors, "details": details}

    @classmethod
    def validate_reasoning_artifact(
        cls,
        artifact: Any,
        *,
        plan: Optional[List[Dict[str, Any]]] = None,
        validation_severity: str = "standard",
    ) -> ValidationVerdict:
        warnings: List[str] = []
        repairable: List[str] = []
        rejected: List[str] = []
        details: Dict[str, Any] = {}

        if not isinstance(artifact, dict):
            return ValidationVerdict(
                stage="reasoning_artifact",
                status=apply_validation_policy(
                    "rejected",
                    severity=validation_severity,
                    stage="reasoning_artifact",
                ),
                profile="control_plane",
                reasons=["Reasoning artifact must be a JSON object"],
                details={"received_type": type(artifact).__name__},
                confidence="high",
            )

        intent = str(artifact.get("intent") or "").strip()
        workspace_facts = artifact.get("workspace_facts")
        planned_actions = artifact.get("planned_actions")
        verification_plan = artifact.get("verification_plan")

        if not intent:
            rejected.append("Reasoning artifact must include a non-empty intent")
        elif len(intent) < 12:
            warnings.append("Reasoning artifact intent is unusually short")

        for field_name, value in (
            ("workspace_facts", workspace_facts),
            ("planned_actions", planned_actions),
            ("verification_plan", verification_plan),
        ):
            if not isinstance(value, list):
                rejected.append(f"Reasoning artifact {field_name} must be an array")
                continue
            cleaned_items = [
                str(item or "").strip() for item in value if str(item or "").strip()
            ]
            details[f"{field_name}_count"] = len(cleaned_items)
            if not cleaned_items:
                repairable.append(
                    f"Reasoning artifact {field_name} must contain at least one entry"
                )
            elif len(cleaned_items) > 12:
                warnings.append(
                    f"Reasoning artifact {field_name} is longer than needed for checkpoint inspection"
                )

        plan_count = len(plan or [])
        action_count = details.get("planned_actions_count", 0)
        if plan_count and action_count and action_count < min(plan_count, 2):
            repairable.append(
                "Reasoning artifact planned_actions does not cover enough planned steps"
            )

        status = cls._select_status(
            warnings=warnings,
            repairable=repairable,
            rejected=rejected,
            severity=validation_severity,
            stage="reasoning_artifact",
        )
        confidence = "high"
        if repairable:
            confidence = "medium"
        elif warnings:
            confidence = "low"

        return ValidationVerdict(
            stage="reasoning_artifact",
            status=status,
            profile="control_plane",
            reasons=cls._ordered_reasons(
                warnings=warnings,
                repairable=repairable,
                rejected=rejected,
            ),
            details=details,
            confidence=confidence,
        )

    @staticmethod
    def infer_validation_profile(
        task_prompt: str,
        execution_profile: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> str:
        combined = " ".join(
            [task_prompt or "", title or "", description or "", execution_profile or ""]
        ).lower()
        implementation_markers = (
            "set up",
            "setup",
            "build",
            "create",
            "implement",
            "frontend",
            "backend",
            "react",
            "vite",
            "node",
            "node.js",
            "fastapi",
            "flask",
            "django",
        )
        if any(marker in combined for marker in implementation_markers):
            return "implementation"

        if execution_profile in {"review_only", "test_only"} or any(
            marker in combined
            for marker in ("verify", "verification", "review", "audit", "refine", "qa")
        ):
            return "verification"
        if any(
            marker in combined
            for marker in (
                "inspect",
                "analysis",
                "analyze",
                "architecture",
                "inventory",
                "current project structure",
                "current project architecture",
            )
        ):
            return "verification"
        if any(marker in combined for marker in ("integration", "end-to-end", "e2e")):
            return "integration"
        if any(
            marker in combined
            for marker in ("scaffold", "skeleton", "boilerplate", "initialize only")
        ):
            return "scaffold"
        return "implementation"

    @staticmethod
    def _verification_is_weak(command: Optional[str]) -> bool:
        text = str(command or "").strip().lower()
        if not text:
            return True
        meaningful_markers = (
            "pytest",
            "python -m",
            "node -e",
            "node ",
            "npm test",
            "pnpm test",
            "cargo test",
            "go test",
            "python ",
            "uv run",
        )
        if any(marker in text for marker in meaningful_markers):
            return False
        weak_markers = ("test -f", "test -d", "grep -q", "ls ", "echo ", "cat ")
        return any(marker in text for marker in weak_markers)

    @staticmethod
    def _normalize_failure_signature_parts(reasons: List[str]) -> List[str]:
        normalized: List[str] = []
        for reason in reasons:
            text = re.sub(r"\s+", " ", str(reason or "").strip().lower())
            if text:
                normalized.append(text)
        return sorted(set(normalized))

    @classmethod
    def build_failure_signature(cls, reasons: List[str]) -> str:
        parts = cls._normalize_failure_signature_parts(reasons)
        return " | ".join(parts[:8])

    @staticmethod
    def _workspace_materialization_summary(project_dir: Path) -> Dict[str, int]:
        file_count = 0
        source_file_count = 0
        config_file_count = 0
        scaffold_only_count = 0

        config_names = {
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "requirements.txt",
            "pyproject.toml",
            "tsconfig.json",
            "vite.config.ts",
            "vite.config.js",
            "jest.config.js",
            "vitest.config.ts",
            ".gitignore",
            ".env.example",
        }
        scaffold_only_names = {"package.json", "requirements.txt", "pyproject.toml"}

        for path in project_dir.rglob("*"):
            if not path.is_file():
                continue
            relative_name = path.name.lower()
            file_count += 1
            if path.suffix.lower() in SOURCE_EXTENSIONS:
                source_file_count += 1
            if relative_name in config_names:
                config_file_count += 1
            if relative_name in scaffold_only_names:
                scaffold_only_count += 1

        return {
            "file_count": file_count,
            "source_file_count": source_file_count,
            "config_file_count": config_file_count,
            "scaffold_only_count": scaffold_only_count,
        }

    @staticmethod
    def _plan_contains_stack_conflict(
        plan: List[Dict[str, Any]], task_prompt: str
    ) -> bool:
        lowered_task = (task_prompt or "").lower()
        if any(
            marker in lowered_task
            for marker in ("python", "node", "javascript", "typescript")
        ):
            return False

        seen_python = False
        seen_node = False
        for step in plan:
            text = " ".join(
                [step.get("description", "")]
                + [str(command or "") for command in step.get("commands", []) or []]
            ).lower()
            if any(
                token in text
                for token in ("requirements.txt", "python ", ".py", "pip ", "pytest")
            ):
                seen_python = True
            if any(
                token in text
                for token in ("package.json", "npm ", "pnpm ", "node ", ".js", ".ts")
            ):
                seen_node = True
        return seen_python and seen_node

    @staticmethod
    def _plan_contains_placeholder_intent(plan: List[Dict[str, Any]]) -> bool:
        placeholder_markers = (
            "pass",
            "todo",
            "notimplemented",
            "placeholder",
            "stub",
        )
        for step in plan:
            for command in step.get("commands", []) or []:
                lowered = str(command or "").lower()
                if any(marker in lowered for marker in placeholder_markers):
                    return True
        return False

    @staticmethod
    def _task_allows_multiple_stacks(
        task_prompt: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        combined = " ".join([task_prompt or "", title or "", description or ""]).lower()
        explicit_pairs = (
            ("python", "javascript"),
            ("python", "node"),
            ("python", "typescript"),
            ("django", "react"),
            ("flask", "react"),
            ("fastapi", "react"),
            ("backend", "frontend"),
        )
        if any(
            left in combined and right in combined for left, right in explicit_pairs
        ):
            return True
        return any(
            marker in combined
            for marker in ("polyglot", "multi-language", "full stack", "full-stack")
        )

    @staticmethod
    def _infer_stack_from_plan(plan: List[Dict[str, Any]]) -> Optional[str]:
        seen_python = False
        seen_node = False
        for step in plan:
            text = " ".join(
                [
                    str(step.get("description") or ""),
                    str(step.get("verification") or ""),
                ]
                + [str(command or "") for command in step.get("commands", []) or []]
                + [str(path or "") for path in step.get("expected_files", []) or []]
            ).lower()
            if any(
                token in text
                for token in (
                    "requirements.txt",
                    "python ",
                    ".py",
                    "pip ",
                    "pytest",
                    "pyproject.toml",
                )
            ):
                seen_python = True
            if any(
                token in text
                for token in (
                    "package.json",
                    "npm ",
                    "pnpm ",
                    "node ",
                    ".js",
                    ".ts",
                    "tsconfig.json",
                )
            ):
                seen_node = True
        if seen_python and seen_node:
            return "mixed"
        if seen_node:
            return "node"
        if seen_python:
            return "python"
        return None

    @staticmethod
    def _plan_contains_brittle_commands(
        extracted_plan: Optional[List[Dict[str, Any]]], output_text: str = ""
    ) -> bool:
        if not extracted_plan:
            return False

        heredoc_count = 0
        for step in extracted_plan:
            commands = step.get("commands", [])
            if not isinstance(commands, list):
                return True
            for command in commands:
                raw_command = str(command or "")
                lowered = raw_command.lower()
                if "cat >" in lowered and "<< 'eof'" in lowered:
                    heredoc_count += 1
                if "cat >" in lowered and "<< eof" in lowered:
                    heredoc_count += 1
                if re.search(r"mkdir\s+-p\s+[^|;&\n]+\s*(?:&&|\|)\s*cat\s+>", lowered):
                    return True
                if raw_command.count("\n") > 25:
                    return True
                if len(raw_command) > 1200:
                    return True

        if heredoc_count >= 2:
            return True

        lowered_output = (output_text or "").lower()
        if lowered_output.count("cat >") >= 2 and "```json" in lowered_output:
            return True

        return False

    @staticmethod
    def _is_non_runnable_command(command: str) -> bool:
        text = str(command or "").strip()
        lowered = text.lower()
        if not text:
            return True
        if lowered.startswith("write "):
            return True
        if lowered.startswith("edit "):
            return True
        if lowered.startswith("verify "):
            return True
        if lowered.startswith("check ") and "test " not in lowered:
            return True
        if lowered.startswith("ensure "):
            return True
        if lowered.startswith("confirm "):
            return True
        return False

    @staticmethod
    def _uses_background_process(command: str) -> bool:
        text = str(command or "").strip().lower()
        if not text:
            return False
        # Only check the first line for shell background operator (&).
        # Heredoc bodies start on line 2+, so bare & in HTML content (e.g.
        # "Flowers & Seasons") cannot trigger a false positive.
        first_line = text.split("\n")[0]
        if re.search(r"(?<![&])&(\s|$)", first_line):
            return True
        return any(
            marker in text
            for marker in (
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
        )

    @classmethod
    def _plan_contains_background_processes(
        cls, plan: List[Dict[str, Any]]
    ) -> List[int]:
        bad_steps: List[int] = []
        for step in plan:
            for command in step.get("commands", []) or []:
                if cls._uses_background_process(str(command or "")):
                    bad_steps.append(step.get("step_number"))
                    break
        return [step for step in bad_steps if step is not None]

    @classmethod
    def _plan_contains_non_runnable_commands(
        cls, plan: List[Dict[str, Any]]
    ) -> List[int]:
        bad_steps: List[int] = []
        for step in plan:
            for command in step.get("commands", []) or []:
                if cls._is_non_runnable_command(str(command or "")):
                    bad_steps.append(step.get("step_number"))
                    break
        return [step for step in bad_steps if step is not None]

    @staticmethod
    def _plan_missing_required_fields(
        plan: List[Dict[str, Any]]
    ) -> Dict[str, List[int]]:
        missing_description: List[int] = []
        missing_commands: List[int] = []

        for index, step in enumerate(plan, start=1):
            step_number = step.get("step_number", index)
            if not str(step.get("description") or "").strip():
                missing_description.append(step_number)

            commands = step.get("commands", [])
            if not isinstance(commands, list) or not any(
                str(command or "").strip() for command in commands
            ):
                missing_commands.append(step_number)

        return {
            "missing_description_steps": missing_description,
            "missing_commands_steps": missing_commands,
        }

    @staticmethod
    def _plan_has_invalid_step_sequence(plan: List[Dict[str, Any]]) -> bool:
        step_numbers = [step.get("step_number") for step in plan]
        if not all(isinstance(step_number, int) for step_number in step_numbers):
            return True
        return step_numbers != list(range(1, len(plan) + 1))

    @staticmethod
    def _plan_contains_unsafe_paths(plan: List[Dict[str, Any]]) -> List[str]:
        invalid_paths: List[str] = []
        for step in plan:
            for path_value in step.get("expected_files", []) or []:
                raw_path = str(path_value or "").strip()
                if not raw_path:
                    continue
                candidate = Path(raw_path)
                if candidate.is_absolute() or ".." in candidate.parts:
                    invalid_paths.append(raw_path)
        return invalid_paths[:20]

    @staticmethod
    def _plan_contains_unsafe_command_paths(
        plan: List[Dict[str, Any]]
    ) -> Dict[int, List[str]]:
        """Detect command paths that violate the task-workspace contract."""

        findings: Dict[int, List[str]] = {}
        traversal_pattern = re.compile(
            r"(?<![\w./-])\.\.(?:/[A-Za-z0-9._@:+-]+)+(?:/)?"
        )
        absolute_path_pattern = re.compile(
            r"^/[A-Za-z0-9._@:+-]+(?:/[A-Za-z0-9._@:+-]+)*/*$"
        )
        allowed_absolute_tokens = {
            "/dev/null",
            "/dev/stdout",
            "/dev/stderr",
            "/dev/stdin",
        }

        for index, step in enumerate(plan, start=1):
            step_number = step.get("step_number", index)
            fragments: List[str] = []
            step_text_parts = [
                str(step.get("verification") or ""),
                str(step.get("rollback") or ""),
            ]
            step_text_parts.extend(
                str(command or "") for command in step.get("commands", []) or []
            )

            for text in step_text_parts:
                for match in traversal_pattern.finditer(text):
                    fragment = match.group(0)
                    if fragment not in fragments:
                        fragments.append(fragment)
                try:
                    tokens = shlex.split(text, posix=True)
                except ValueError:
                    tokens = []
                for token in tokens:
                    if token in allowed_absolute_tokens:
                        continue
                    if absolute_path_pattern.fullmatch(token):
                        if token not in fragments:
                            fragments.append(token)

            if fragments:
                findings[int(step_number)] = fragments[:6]

        return findings

    @staticmethod
    def _plan_nests_task_workspace(
        plan: List[Dict[str, Any]], project_dir: Optional[Path]
    ) -> List[int]:
        if not project_dir:
            return []
        nested_prefix = f"{project_dir.name}/"
        bad_steps: List[int] = []
        for step in plan:
            step_text_parts = [
                str(step.get("verification") or ""),
                str(step.get("rollback") or ""),
            ]
            step_text_parts.extend(
                str(command or "") for command in step.get("commands", []) or []
            )
            step_text_parts.extend(
                str(path or "") for path in step.get("expected_files", []) or []
            )
            combined = "\n".join(step_text_parts)
            if nested_prefix in combined:
                bad_steps.append(step.get("step_number"))
        return [step for step in bad_steps if step is not None]

    @staticmethod
    def _plan_creates_nested_project_root(
        plan: List[Dict[str, Any]], project_dir: Optional[Path] = None
    ) -> List[int]:
        """Detect plans that recreate a whole project under one new top-level folder.

        We only want to flag plans that appear to put the *entire deliverable*
        under a new nested root like ``my-app/...`` inside the current workspace.
        Normal static-site and asset layouts such as ``index.html`` plus
        ``assets/...`` should not be treated as nested-project bugs.
        """

        # Dirs that appear in project_dir path are legitimate prefixes in expected_files
        allowed_from_project = set()
        if project_dir:
            try:
                allowed_from_project = {p for p in project_dir.parts if p and p != "/"}
            except Exception:
                pass

        def looks_like_nested_project_scaffold(
            root_name: str, paths: List[str]
        ) -> bool:
            root_level_files = [
                path_text for path_text in paths if len(Path(path_text).parts) == 2
            ]
            second_level_dirs = {
                Path(path_text).parts[1]
                for path_text in paths
                if len(Path(path_text).parts) > 2
            }

            if root_level_files:
                return True

            structural_dirs = second_level_dirs.intersection(
                NESTED_PROJECT_STRUCTURAL_DIRS
            )
            if len(structural_dirs) >= 2:
                return True

            return False

        bad_steps: List[int] = []
        for step in plan:
            expected_files = [
                str(path or "").strip()
                for path in (step.get("expected_files", []) or [])
                if str(path or "").strip()
            ]
            if len(expected_files) < 3:
                continue

            root_level_files = [
                path_text
                for path_text in expected_files
                if len(Path(path_text).parts) == 1
            ]
            top_levels = {
                Path(path_text).parts[0]
                for path_text in expected_files
                if len(Path(path_text).parts) > 1
            }
            suspicious = [
                top
                for top in sorted(top_levels)
                if top not in allowed_from_project and not top.startswith(".")
            ]
            # Only treat this as a nested-project root when the plan appears to
            # put all materialized files under a single new folder and does not
            # also create root-level deliverables like index.html or package.json.
            if len(suspicious) == 1 and not root_level_files:
                nested_root = suspicious[0]
                nested_root_files = [
                    path_text
                    for path_text in expected_files
                    if Path(path_text).parts[0] == nested_root
                ]
                if not looks_like_nested_project_scaffold(
                    nested_root, nested_root_files
                ):
                    continue
                bad_steps.append(step.get("step_number"))
        return [step for step in bad_steps if step is not None]

    @staticmethod
    def _verification_plan_missing_workspace_files(
        plan: List[Dict[str, Any]], project_dir: Optional[Path]
    ) -> List[str]:
        """Return expected source files in verification plans that do not exist yet."""

        if not project_dir or not project_dir.exists():
            return []

        missing: List[str] = []
        seen: set[str] = set()
        for path_text in ValidatorService._core_expected_files(plan):
            candidate = (project_dir / path_text).resolve()
            if candidate.exists():
                continue
            if path_text in seen:
                continue
            seen.add(path_text)
            missing.append(path_text)
        return missing

    @staticmethod
    def _plan_contains_duplicated_path_roots(
        plan: List[Dict[str, Any]]
    ) -> Dict[int, List[str]]:
        """Detect repeated root segments like frontend/src/frontend/src in plan text."""

        duplicate_pattern = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/\1(?:/|$)")
        findings: Dict[int, List[str]] = {}

        for index, step in enumerate(plan, start=1):
            step_number = step.get("step_number", index)
            step_text_parts = [
                str(step.get("verification") or ""),
                str(step.get("rollback") or ""),
            ]
            step_text_parts.extend(
                str(command or "") for command in step.get("commands", []) or []
            )
            step_text_parts.extend(
                str(path or "") for path in step.get("expected_files", []) or []
            )

            fragments: List[str] = []
            for text in step_text_parts:
                for match in duplicate_pattern.finditer(text):
                    fragment = match.group(0).rstrip("/")
                    if fragment not in fragments:
                        fragments.append(fragment)
            if fragments:
                findings[int(step_number)] = fragments[:6]

        return findings

    @staticmethod
    def _infer_workflow_phase_for_step(
        step: Dict[str, Any], workflow_profile: Optional[str]
    ) -> Optional[str]:
        if workflow_profile != "fullstack_scaffold":
            return None

        text = " ".join(
            [
                str(step.get("description") or ""),
                str(step.get("verification") or ""),
                str(step.get("rollback") or ""),
            ]
            + [str(command or "") for command in step.get("commands", []) or []]
            + [str(path or "") for path in step.get("expected_files", []) or []]
        ).lower()
        has_frontend_markers = any(
            marker in text
            for marker in (
                "frontend",
                "react",
                "vite",
                "src/main.tsx",
                "src/app.tsx",
                "package.json",
                "tsconfig.json",
                "npm install",
            )
        )
        has_backend_markers = any(
            marker in text
            for marker in (
                "fastapi",
                "app/main.py",
                "app/config.py",
                "requirements.txt",
                "pip install",
                "pytest",
                "backend",
                ".venv/bin/python",
            )
        )

        if any(
            marker in text
            for marker in (
                "wire api config",
                "proxy",
                "cors",
                "vite.config",
                "api/client",
                "localhost:8080",
                "localhost:3000",
                ".env",
                "api config",
            )
        ):
            return "wire_api_config"

        if has_frontend_markers and not any(
            marker in text
            for marker in ("eslint", "vitest", "smoke check", "dev-ready")
        ):
            return "create_frontend_skeleton"

        if has_backend_markers and not any(
            marker in text
            for marker in ("smoke check", "dev-ready", "health", "routes", "cors")
        ):
            return "create_backend_skeleton"

        if any(
            marker in text
            for marker in (
                "dev-ready",
                "smoke check",
                "health",
                "routes",
                "lint",
                "vitest",
                "eslint",
                "type-check",
                "tsc --noemit",
                "build",
                "verify_dev_startup",
            )
        ):
            return "verify_dev_startup"

        if has_frontend_markers:
            return "create_frontend_skeleton"
        if has_backend_markers:
            return "create_backend_skeleton"

        return None

    @classmethod
    def _workflow_phase_order_violations(
        cls,
        plan: List[Dict[str, Any]],
        workflow_profile: Optional[str],
    ) -> Dict[str, Any]:
        phase_order = WORKFLOW_PHASE_ORDER.get(workflow_profile or "")
        if not phase_order:
            return {}

        phase_positions = {phase: idx for idx, phase in enumerate(phase_order)}
        seen_sequence: List[Dict[str, Any]] = []
        last_position = -1
        violating_steps: List[int] = []

        for index, step in enumerate(plan, start=1):
            phase = cls._infer_workflow_phase_for_step(step, workflow_profile)
            if not phase:
                continue
            step_number = int(step.get("step_number", index))
            position = phase_positions[phase]
            seen_sequence.append({"step_number": step_number, "phase": phase})
            if position < last_position:
                violating_steps.append(step_number)
            else:
                last_position = position

        missing_phases = [
            phase
            for phase in phase_order
            if phase not in {entry["phase"] for entry in seen_sequence}
        ]
        return {
            "phase_sequence": seen_sequence,
            "violating_steps": violating_steps,
            "missing_phases": missing_phases,
        }

    @classmethod
    def validate_plan(
        cls,
        plan: List[Dict[str, Any]],
        *,
        output_text: str,
        task_prompt: str,
        execution_profile: str,
        project_dir: Optional[Path] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        validation_severity: str = "standard",
        workflow_profile: Optional[str] = None,
    ) -> ValidationVerdict:
        profile = cls.infer_validation_profile(
            task_prompt, execution_profile, title=title, description=description
        )
        warnings: List[str] = []
        repairable: List[str] = []
        rejected: List[str] = []
        details: Dict[str, Any] = {"plan_length": len(plan)}
        schema_validation = cls.validate_plan_schema(plan)
        details["plan_schema"] = schema_validation
        if not schema_validation["valid"]:
            rejected.extend(schema_validation["errors"])
            details.update(schema_validation["details"])

        if cls._plan_contains_brittle_commands(plan, output_text):
            repairable.append(
                "Plan contains brittle heredoc-heavy or malformed commands"
            )

        if cls._plan_has_invalid_step_sequence(plan):
            rejected.append(
                "Plan step numbers must be consecutive integers starting at 1"
            )

        missing_fields = cls._plan_missing_required_fields(plan)
        if missing_fields["missing_description_steps"]:
            rejected.append(
                "Plan contains steps with empty descriptions "
                f"(steps: {missing_fields['missing_description_steps'][:5]})"
            )
            details["missing_description_steps"] = missing_fields[
                "missing_description_steps"
            ]
        if missing_fields["missing_commands_steps"]:
            rejected.append(
                "Plan contains steps without runnable commands "
                f"(steps: {missing_fields['missing_commands_steps'][:5]})"
            )
            details["missing_commands_steps"] = missing_fields["missing_commands_steps"]

        unsafe_paths = cls._plan_contains_unsafe_paths(plan)
        if unsafe_paths:
            rejected.append(
                "Plan references unsafe expected file paths outside the workspace root"
            )
            details["unsafe_expected_files"] = unsafe_paths

        unsafe_command_paths = cls._plan_contains_unsafe_command_paths(plan)
        if unsafe_command_paths:
            bad_steps = sorted(unsafe_command_paths.keys())
            rejected.append(
                "Plan commands reference parent-directory paths outside the task workspace "
                f"(steps: {bad_steps[:5]})"
            )
            details["unsafe_command_paths"] = unsafe_command_paths

        non_runnable_steps = cls._plan_contains_non_runnable_commands(plan)
        if non_runnable_steps:
            repairable.append(
                "Plan contains non-runnable pseudo-commands such as `edit` or prose instructions "
                f"(steps: {non_runnable_steps[:5]})"
            )
            details["non_runnable_steps"] = non_runnable_steps

        background_process_steps = cls._plan_contains_background_processes(plan)
        if background_process_steps:
            repairable.append(
                "Plan contains background processes or long-running dev servers "
                f"(steps: {background_process_steps[:5]})"
            )
            details["background_process_steps"] = background_process_steps

        nested_workspace_steps = cls._plan_nests_task_workspace(plan, project_dir)
        if nested_workspace_steps:
            repairable.append(
                "Plan incorrectly recreates the current task workspace as a nested folder "
                f"(steps: {nested_workspace_steps[:5]})"
            )
            details["nested_workspace_steps"] = nested_workspace_steps

        nested_project_root_steps = cls._plan_creates_nested_project_root(
            plan, project_dir
        )
        if nested_project_root_steps:
            repairable.append(
                "Plan appears to generate the deliverable inside a new nested project folder "
                f"instead of the task workspace root (steps: {nested_project_root_steps[:5]})"
            )
            details["nested_project_root_steps"] = nested_project_root_steps

        duplicated_root_paths = cls._plan_contains_duplicated_path_roots(plan)
        if duplicated_root_paths:
            bad_steps = sorted(duplicated_root_paths.keys())
            repairable.append(
                "Plan repeats workspace root segments inside commands or expected files "
                f"(steps: {bad_steps[:5]})"
            )
            details["duplicated_root_paths"] = duplicated_root_paths

        workflow_phase_check = cls._workflow_phase_order_violations(
            plan, workflow_profile
        )
        if workflow_phase_check:
            details["workflow_phase_sequence"] = workflow_phase_check["phase_sequence"]
            if workflow_phase_check["violating_steps"]:
                repairable.append(
                    "Plan violates required workflow phase order "
                    f"for {workflow_profile} (steps: {workflow_phase_check['violating_steps'][:5]})"
                )
                details["workflow_phase_violations"] = workflow_phase_check[
                    "violating_steps"
                ]
            if workflow_phase_check["missing_phases"]:
                warnings.append(
                    "Plan does not clearly cover every required workflow phase "
                    f"for {workflow_profile} (missing: {workflow_phase_check['missing_phases'][:4]})"
                )
                details["missing_workflow_phases"] = workflow_phase_check[
                    "missing_phases"
                ]

        if profile == "implementation":
            weak_verification_steps = [
                step.get("step_number")
                for step in plan
                if cls._verification_is_weak(step.get("verification"))
            ]
            if weak_verification_steps:
                warnings.append(
                    "Plan uses weak verification for implementation-heavy work "
                    f"(steps: {weak_verification_steps[:5]})"
                )
                details["weak_verification_steps"] = weak_verification_steps

            if cls._plan_contains_placeholder_intent(plan):
                rejected.append(
                    "Plan appears to generate placeholder or stub implementations"
                )
        elif profile == "verification":
            missing_workspace_files = cls._verification_plan_missing_workspace_files(
                plan, project_dir
            )
            if missing_workspace_files:
                repairable.append(
                    "Verification/review plan references source files that do not exist in the current workspace "
                    f"(files: {missing_workspace_files[:5]})"
                )
                details["missing_workspace_expected_files"] = missing_workspace_files[
                    :20
                ]

        if cls._plan_contains_stack_conflict(plan, task_prompt):
            repairable.append(
                "Plan mixes inconsistent implementation stacks for one task"
            )
            details["stack_conflict"] = True

        return ValidationVerdict(
            stage="plan",
            status=cls._select_status(
                warnings=warnings,
                repairable=repairable,
                rejected=rejected,
                severity=validation_severity,
                stage="plan",
            ),
            profile=profile,
            reasons=cls._ordered_reasons(
                warnings=warnings, repairable=repairable, rejected=rejected
            ),
            details=details,
        )

    @staticmethod
    def _iter_candidate_files(
        project_dir: Path, file_paths: Iterable[str]
    ) -> List[Path]:
        candidates: List[Path] = []
        for raw_path in file_paths:
            relative = str(raw_path or "").strip().rstrip("/")
            if not relative:
                continue
            candidate = (project_dir / relative).resolve()
            if candidate.exists() and candidate.is_file():
                candidates.append(candidate)
        return candidates

    @staticmethod
    def _find_nested_expected_file_matches(
        project_dir: Path, file_paths: Iterable[str]
    ) -> Dict[str, List[str]]:
        """Look one project-folder level deeper for misplaced generated files."""

        nested_matches: Dict[str, List[str]] = {}
        expected_top_levels = {
            Path(str(raw_path or "").strip().rstrip("/")).parts[0]
            for raw_path in file_paths
            if str(raw_path or "").strip().rstrip("/")
            and len(Path(str(raw_path or "").strip().rstrip("/")).parts) > 1
        }
        top_level_dirs = (
            [
                child
                for child in project_dir.iterdir()
                if child.is_dir()
                and child.name not in ROOT_LEVEL_EXPECTED_DIRS
                and child.name not in expected_top_levels
                and not child.name.startswith(".")
            ]
            if project_dir.exists()
            else []
        )

        for raw_path in file_paths:
            relative = str(raw_path or "").strip().rstrip("/")
            if not relative:
                continue
            for candidate_root in top_level_dirs:
                nested_candidate = (candidate_root / relative).resolve()
                if nested_candidate.exists() and nested_candidate.is_file():
                    nested_matches.setdefault(candidate_root.name, []).append(relative)
        return nested_matches

    @staticmethod
    def _detect_placeholder_content(path: Path) -> List[str]:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            return []

        reasons: List[str] = []
        lowered = content.lower()
        if re.search(r"^\s*pass\s*$", content, flags=re.MULTILINE):
            reasons.append(f"{path.name} still contains `pass` placeholders")
        if "todo" in lowered or "placeholder" in lowered:
            reasons.append(f"{path.name} still contains TODO or placeholder markers")
        if "notimplemented" in lowered or "raise notimplementederror" in lowered:
            reasons.append(f"{path.name} still contains not-implemented markers")
        if "__main__" in content and 'if __name__ == "__main__"' not in content:
            reasons.append(f"{path.name} has a broken Python __main__ entrypoint check")
        if path.suffix == ".py":
            try:
                ast.parse(content)
            except SyntaxError as exc:
                reasons.append(f"{path.name} has Python syntax errors: {exc.msg}")
        return reasons

    @staticmethod
    def _split_content_issue_severity(
        reasons: List[str],
    ) -> tuple[List[str], List[str]]:
        repairable: List[str] = []
        rejected: List[str] = []
        for reason in reasons:
            lowered = reason.lower()
            if any(
                marker in lowered
                for marker in (
                    "`pass` placeholders",
                    "not-implemented markers",
                    "syntax errors",
                    "broken python __main__",
                )
            ):
                rejected.append(reason)
            elif "todo or placeholder markers" in lowered:
                repairable.append(reason)
            else:
                rejected.append(reason)
        return repairable, rejected

    @staticmethod
    def _core_expected_files(plan: List[Dict[str, Any]]) -> List[str]:
        files: List[str] = []
        seen = set()
        for step in plan:
            for raw_path in step.get("expected_files", []) or []:
                path_text = str(raw_path or "").strip()
                if (
                    not path_text
                    or path_text.endswith("/")
                    or path_text.lower() in DOC_NAMES
                ):
                    continue
                if Path(path_text).suffix.lower() not in SOURCE_EXTENSIONS:
                    continue
                if path_text not in seen:
                    seen.add(path_text)
                    files.append(path_text)
        return files

    @classmethod
    def assess_plan_workspace_compatibility(
        cls,
        *,
        project_dir: Path,
        plan: List[Dict[str, Any]],
        completed_step_count: int = 0,
    ) -> Dict[str, Any]:
        """Check whether a saved plan's completed portion still matches the current workspace."""

        scoped_plan = (
            plan[:completed_step_count]
            if completed_step_count and completed_step_count > 0
            else plan
        )
        expected_core_files = cls._core_expected_files(scoped_plan)
        candidate_files = cls._iter_candidate_files(project_dir, expected_core_files)
        nested_matches = cls._find_nested_expected_file_matches(
            project_dir, expected_core_files
        )

        project_dir = project_dir.resolve()
        workspace_source_files = [
            path
            for path in project_dir.rglob("*")
            if path.is_file()
            and path.suffix.lower() in SOURCE_EXTENSIONS
            and not any(
                part in {"node_modules", "__pycache__", ".openclaw"}
                for part in path.relative_to(project_dir).parts
            )
        ]
        nested_match_count = sum(len(matches) for matches in nested_matches.values())
        expected_count = len(expected_core_files)
        matched_count = len(candidate_files)
        compatible = not (
            workspace_source_files
            and expected_count > 0
            and matched_count == 0
            and nested_match_count == 0
        )

        return {
            "compatible": compatible,
            "completed_step_count": completed_step_count,
            "expected_core_count": expected_count,
            "matched_core_count": matched_count,
            "nested_match_count": nested_match_count,
            "workspace_source_count": len(workspace_source_files),
            "expected_core_files": expected_core_files[:20],
            "matched_core_files": [
                str(path.relative_to(project_dir)) for path in candidate_files[:20]
            ],
            "nested_matches": {
                key: value[:10] for key, value in nested_matches.items()
            },
        }

    @classmethod
    def validate_step_success(
        cls,
        *,
        project_dir: Path,
        step: Dict[str, Any],
        step_output: str,
        missing_expected_files: List[str],
        tool_failures: List[str],
        validation_profile: str,
        reported_changed_files: Optional[List[str]] = None,
        relaxed_mode: bool = False,
        validation_severity: str = "standard",
    ) -> ValidationVerdict:
        warnings: List[str] = []
        repairable: List[str] = []
        rejected: List[str] = []
        details: Dict[str, Any] = {}

        if missing_expected_files:
            repairable.append(
                f"Expected files are missing: {', '.join(missing_expected_files[:6])}"
            )
            details["missing_expected_files"] = missing_expected_files[:20]

        if tool_failures:
            repairable.append(
                "Task logs contain tool failures during the successful step window"
            )
            details["tool_failures"] = tool_failures[:10]

        if (
            not relaxed_mode
            and validation_profile == "implementation"
            and cls._verification_is_weak(step.get("verification"))
        ):
            warnings.append(
                "Step verification is too weak for implementation-heavy work"
            )

        candidate_files = cls._iter_candidate_files(
            project_dir,
            step.get("expected_files", []) or [],
        )
        materialized_files = [
            str(path.relative_to(project_dir)) for path in candidate_files
        ]
        reported_changed_files = [
            str(path).strip()
            for path in (reported_changed_files or [])
            if str(path).strip()
        ]
        if reported_changed_files and materialized_files:
            if not set(reported_changed_files) & set(materialized_files):
                repairable.append(
                    "Step reported file changes but none materialized in the expected workspace"
                )
                details["reported_changed_files"] = reported_changed_files[:20]
                details["materialized_files"] = materialized_files[:20]
        placeholder_reasons: List[str] = []
        for candidate in candidate_files:
            placeholder_reasons.extend(cls._detect_placeholder_content(candidate))
        if placeholder_reasons and validation_profile == "implementation":
            repairable_placeholder_reasons, rejected_placeholder_reasons = (
                cls._split_content_issue_severity(placeholder_reasons)
            )
            repairable.extend(repairable_placeholder_reasons[:6])
            rejected.extend(rejected_placeholder_reasons[:6])
            details["placeholder_reasons"] = placeholder_reasons[:20]

        return ValidationVerdict(
            stage="step_completion",
            status=cls._select_status(
                warnings=warnings,
                repairable=repairable,
                rejected=rejected,
                severity=validation_severity,
                stage="step_completion",
            ),
            profile=validation_profile,
            reasons=cls._ordered_reasons(
                warnings=warnings, repairable=repairable, rejected=rejected
            ),
            details=details | {"step_output_preview": step_output[:240]},
        )

    @classmethod
    def validate_task_completion(
        cls,
        *,
        project_dir: Path,
        plan: List[Dict[str, Any]],
        task_prompt: str,
        execution_profile: str,
        workspace_consistency: Optional[Dict[str, Any]] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        relaxed_mode: bool = False,
        completion_evidence: Optional[Dict[str, Any]] = None,
        validation_severity: str = "standard",
    ) -> ValidationVerdict:
        profile = cls.infer_validation_profile(
            task_prompt, execution_profile, title=title, description=description
        )
        expected_core_files = cls._core_expected_files(plan)
        candidate_files = cls._iter_candidate_files(project_dir, expected_core_files)
        nested_matches = cls._find_nested_expected_file_matches(
            project_dir, expected_core_files
        )

        missing_core = [
            path_text
            for path_text in expected_core_files
            if not (project_dir / path_text).resolve().exists()
        ]
        warnings: List[str] = []
        repairable: List[str] = []
        rejected: List[str] = []
        details: Dict[str, Any] = {
            "expected_core_files": expected_core_files[:20],
            "validated_files": [
                str(path.relative_to(project_dir)) for path in candidate_files[:20]
            ],
        }
        workspace_summary = cls._workspace_materialization_summary(project_dir)
        details["workspace_materialization"] = workspace_summary
        completion_evidence = completion_evidence or {}
        reported_changed_files = [
            str(path).strip()
            for path in (completion_evidence.get("reported_changed_files") or [])
            if str(path).strip()
        ]
        contract = {
            "execution_profile": execution_profile,
            "validation_profile": profile,
            "summary_generated": bool(completion_evidence.get("summary_generated")),
            "execution_results_count": int(
                completion_evidence.get("execution_results_count") or 0
            ),
            "requires_source_outputs": profile in {"implementation", "integration"},
        }
        details["completion_contract"] = contract
        if not contract["summary_generated"]:
            rejected.append("Completion contract requires a generated task summary")
        if (
            contract["requires_source_outputs"]
            and contract["execution_results_count"] <= 0
        ):
            rejected.append(
                "Completion contract requires at least one recorded execution result"
            )

        if missing_core:
            repairable.append(
                f"Core implementation files are missing: {', '.join(missing_core[:6])}"
            )
            details["missing_core_files"] = missing_core[:20]

        if reported_changed_files and candidate_files:
            materialized_files = [
                str(path.relative_to(project_dir)) for path in candidate_files
            ]
            if not set(reported_changed_files) & set(materialized_files):
                repairable.append(
                    "Completion evidence reported changed files, but none materialized in the canonical workspace"
                )
                details["reported_changed_files"] = reported_changed_files[:20]
                details["materialized_files"] = materialized_files[:20]

        if nested_matches:
            details["nested_expected_file_matches"] = {
                key: value[:10] for key, value in nested_matches.items()
            }
            dominant_root = max(
                nested_matches.items(),
                key=lambda item: len(item[1]),
                default=(None, []),
            )[0]
            if dominant_root:
                if relaxed_mode:
                    warnings.append(
                        "Implementation appears to have been generated inside nested folder "
                        f"`{dominant_root}/` instead of the task workspace root"
                    )
                else:
                    repairable.append(
                        "Implementation appears to have been generated inside nested folder "
                        f"`{dominant_root}/` instead of the task workspace root"
                    )

        placeholder_reasons: List[str] = []
        for candidate in candidate_files:
            placeholder_reasons.extend(cls._detect_placeholder_content(candidate))
        if placeholder_reasons and profile == "implementation":
            repairable_placeholder_reasons, rejected_placeholder_reasons = (
                cls._split_content_issue_severity(placeholder_reasons)
            )
            repairable.extend(repairable_placeholder_reasons[:10])
            rejected.extend(rejected_placeholder_reasons[:10])
            details["placeholder_reasons"] = placeholder_reasons[:20]

        if profile == "implementation" and not candidate_files:
            if nested_matches:
                target = warnings if relaxed_mode else repairable
                target.append(
                    "No core implementation files were found at the workspace root, but nested generated files were detected"
                )
            else:
                rejected.append("No core implementation source files were produced")

        if profile == "implementation":
            if workspace_summary["file_count"] <= 0:
                rejected.append("Workspace is empty after completion")
            elif (
                workspace_summary["source_file_count"] <= 0
                and workspace_summary["config_file_count"] > 0
            ):
                rejected.append(
                    "Workspace contains only framework/config scaffolding without any implementation source files"
                )

        workspace_consistency = workspace_consistency or {}
        plan_stack = cls._infer_stack_from_plan(plan)
        allows_multiple_stacks = cls._task_allows_multiple_stacks(
            task_prompt, title=title, description=description
        )
        details["workspace_consistency"] = workspace_consistency

        if profile == "implementation":
            if workspace_consistency.get("nested_duplicate_dirs"):
                target = warnings if relaxed_mode else repairable
                target.append(
                    "Workspace contains nested duplicate implementation directories: "
                    + ", ".join(
                        workspace_consistency.get("nested_duplicate_dirs", [])[:4]
                    )
                )
            if workspace_consistency.get("mixed_stack") and not allows_multiple_stacks:
                if plan_stack in {"node", "python"}:
                    target = warnings if relaxed_mode else repairable
                    target.append(
                        "Workspace mixes Python and Node/JS artifacts even though the accepted plan targets a single "
                        f"{plan_stack} stack"
                    )
                else:
                    target = warnings if relaxed_mode else repairable
                    target.append(
                        "Workspace contains mixed Python and Node/JS implementation artifacts for one task"
                    )

        failure_signature = cls.build_failure_signature(
            rejected + repairable + warnings
        )
        if failure_signature:
            details["failure_signature"] = failure_signature

        return ValidationVerdict(
            stage="task_completion",
            status=cls._select_status(
                warnings=warnings,
                repairable=repairable,
                rejected=rejected,
                severity=validation_severity,
                stage="task_completion",
            ),
            profile=profile,
            reasons=cls._ordered_reasons(
                warnings=warnings, repairable=repairable, rejected=rejected
            ),
            details=details,
        )

    @staticmethod
    def validate_baseline_publish(
        *,
        validation_profile: str,
        baseline_path: str,
        baseline_file_count: int,
        missing_task_expected_files: List[str],
        missing_prior_expected_files: List[Dict[str, Any]],
        consistency_issues: Optional[List[str]] = None,
        consistency_details: Optional[Dict[str, Any]] = None,
        relaxed_mode: bool = False,
        validation_severity: str = "standard",
    ) -> ValidationVerdict:
        warnings: List[str] = []
        repairable: List[str] = []
        rejected: List[str] = []
        details: Dict[str, Any] = {
            "baseline_path": baseline_path,
            "baseline_file_count": baseline_file_count,
        }

        if baseline_file_count <= 0:
            repairable.append("Canonical baseline is empty after publish")

        if missing_task_expected_files:
            repairable.append(
                "Published baseline is missing current task files: "
                + ", ".join(missing_task_expected_files[:6])
            )
            details["missing_task_expected_files"] = missing_task_expected_files[:20]

        if missing_prior_expected_files:
            repairable.append(
                "Canonical baseline is missing previously completed task files"
            )
            details["missing_prior_expected_files"] = missing_prior_expected_files[:20]
        if consistency_issues:
            target = warnings if relaxed_mode else repairable
            target.extend(consistency_issues[:4])
            details["consistency_issues"] = consistency_issues[:10]
        if consistency_details:
            details["consistency"] = consistency_details

        return ValidationVerdict(
            stage="baseline_publish",
            status=ValidatorService._select_status(
                warnings=warnings,
                repairable=repairable,
                rejected=rejected,
                severity=validation_severity,
                stage="baseline_publish",
            ),
            profile=validation_profile,
            reasons=ValidatorService._ordered_reasons(
                warnings=warnings, repairable=repairable, rejected=rejected
            ),
            details=details,
        )

    @staticmethod
    def persist_validation_result(
        db: Session,
        *,
        task_id: int,
        session_id: Optional[int],
        stage: str,
        verdict: ValidationVerdict,
        step_number: Optional[int] = None,
    ) -> None:
        db.add(
            TaskCheckpoint(
                task_id=task_id,
                session_id=session_id,
                checkpoint_type=f"validation_{stage}",
                step_number=step_number,
                description=f"{stage}:{verdict.status}",
                state_snapshot=json.dumps(verdict.to_dict()),
            )
        )
