"""Rule-first orchestration validation helpers."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.models import TaskCheckpoint
from .types import ValidationVerdict


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
    ".github",
}


class ValidatorService:
    """Deterministic plan and completion validation."""

    @staticmethod
    def _select_status(
        *,
        warnings: List[str],
        repairable: List[str],
        rejected: List[str],
    ) -> str:
        if rejected:
            return "rejected"
        if repairable:
            return "repair_required"
        if warnings:
            return "warning"
        return "accepted"

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
                if re.search(r"mkdir\s+-p\s+[^|;&\n]+,cat\s+>", lowered):
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
    def _plan_creates_nested_project_root(plan: List[Dict[str, Any]]) -> List[int]:
        """Detect plans that recreate a whole project under a new top-level folder."""

        bad_steps: List[int] = []
        for step in plan:
            expected_files = [
                str(path or "").strip()
                for path in (step.get("expected_files", []) or [])
                if str(path or "").strip()
            ]
            top_levels = {
                Path(path_text).parts[0]
                for path_text in expected_files
                if len(Path(path_text).parts) > 1
            }
            suspicious = {
                top
                for top in top_levels
                if top not in ROOT_LEVEL_EXPECTED_DIRS and not top.startswith(".")
            }
            if len(suspicious) == 1 and len(expected_files) >= 3:
                bad_steps.append(step.get("step_number"))
        return [step for step in bad_steps if step is not None]

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
    ) -> ValidationVerdict:
        profile = cls.infer_validation_profile(
            task_prompt, execution_profile, title=title, description=description
        )
        warnings: List[str] = []
        repairable: List[str] = []
        rejected: List[str] = []
        details: Dict[str, Any] = {"plan_length": len(plan)}

        if cls._plan_contains_brittle_commands(plan, output_text):
            repairable.append(
                "Plan contains brittle heredoc-heavy or malformed commands"
            )

        non_runnable_steps = cls._plan_contains_non_runnable_commands(plan)
        if non_runnable_steps:
            repairable.append(
                "Plan contains non-runnable pseudo-commands such as `edit` or prose instructions "
                f"(steps: {non_runnable_steps[:5]})"
            )
            details["non_runnable_steps"] = non_runnable_steps

        nested_workspace_steps = cls._plan_nests_task_workspace(plan, project_dir)
        if nested_workspace_steps:
            repairable.append(
                "Plan incorrectly recreates the current task workspace as a nested folder "
                f"(steps: {nested_workspace_steps[:5]})"
            )
            details["nested_workspace_steps"] = nested_workspace_steps

        nested_project_root_steps = cls._plan_creates_nested_project_root(plan)
        if nested_project_root_steps:
            repairable.append(
                "Plan appears to generate the deliverable inside a new nested project folder "
                f"instead of the task workspace root (steps: {nested_project_root_steps[:5]})"
            )
            details["nested_project_root_steps"] = nested_project_root_steps

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

        if cls._plan_contains_stack_conflict(plan, task_prompt):
            repairable.append(
                "Plan mixes inconsistent implementation stacks for one task"
            )
            details["stack_conflict"] = True

        return ValidationVerdict(
            stage="plan",
            status=cls._select_status(
                warnings=warnings, repairable=repairable, rejected=rejected
            ),
            profile=profile,
            reasons=warnings + repairable + rejected,
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
        top_level_dirs = (
            [
                child
                for child in project_dir.iterdir()
                if child.is_dir() and child.name not in ROOT_LEVEL_EXPECTED_DIRS
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
        if "__main__" in content and "if __name__ == __main__" in content:
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
        relaxed_mode: bool = False,
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
                warnings=warnings, repairable=repairable, rejected=rejected
            ),
            profile=validation_profile,
            reasons=warnings + repairable + rejected,
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

        if missing_core:
            repairable.append(
                f"Core implementation files are missing: {', '.join(missing_core[:6])}"
            )
            details["missing_core_files"] = missing_core[:20]

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

        return ValidationVerdict(
            stage="task_completion",
            status=cls._select_status(
                warnings=warnings, repairable=repairable, rejected=rejected
            ),
            profile=profile,
            reasons=warnings + repairable + rejected,
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
                warnings=warnings, repairable=repairable, rejected=rejected
            ),
            profile=validation_profile,
            reasons=warnings + repairable + rejected,
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
