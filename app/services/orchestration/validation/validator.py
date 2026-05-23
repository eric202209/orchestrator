"""Rule-first orchestration validation helpers."""

from __future__ import annotations

import re
import shlex
import ast
from pathlib import Path
from typing import Any, Dict, List, Optional
from ..policy import apply_validation_policy
from ..types import (
    PlanAccepted,
    PlanOutcome,
    PlanRejected,
    PlanRepairRequired,
    ValidationVerdict,
)

from .persistence import persist_validation_result as _persist_validation_result
from app.services.orchestration.operations.file_ops_contract import (
    normalize_file_op_shape,
    operation_has_file_op_path,
    validate_file_op_shape,
)
from app.services.orchestration.workflow_profiles import (
    get_implementation_intent_markers,
    get_multi_stack_pair_markers,
    get_mutation_build_intent_markers,
    get_workflow_markers,
    get_workflow_phases,
)
from .placeholder_policy import path_allows_placeholder_fixture_content
from .workspace_checks import (
    NESTED_PROJECT_STRUCTURAL_DIRS,
    SOURCE_EXTENSIONS,
    assess_plan_workspace_compatibility as _assess_plan_workspace_compatibility,
    core_expected_files as _core_expected_files,
    detect_placeholder_content as _detect_placeholder_content,
    find_nested_expected_file_matches as _find_nested_expected_file_matches,
    iter_candidate_files as _iter_candidate_files,
    split_content_issue_severity as _split_content_issue_severity,
)
from .workspace_guard import (
    TaskWorkspaceViolationError,
    normalize_path_reference,
)
from .integrity import (
    check_test_preservation,
    classify_verification_command,
    pre_existing_python_test_files,
    scan_test_file_changes,
)

MAX_INITIAL_PLAN_STEPS = 4
MAX_PLANNING_COMMAND_CHARS = 900
PLAN_STRUCTURAL_PLACEHOLDER_MARKER_PATTERN = re.compile(
    r"\b(?:placeholder|stub|notimplemented|notimplementederror)\b|"
    r"\bnot[-_\s]*implemented\b",
    re.IGNORECASE,
)
PLAN_PASS_MARKER_PATTERN = re.compile(r"\bpass\b", re.IGNORECASE)
PLAN_TODO_FIXME_MARKER_PATTERN = re.compile(r"\b(?:todo|fixme)\b", re.IGNORECASE)


class ValidatorService:
    """Deterministic plan and completion validation."""

    _iter_candidate_files = staticmethod(_iter_candidate_files)
    _find_nested_expected_file_matches = staticmethod(
        _find_nested_expected_file_matches
    )
    _detect_placeholder_content = staticmethod(_detect_placeholder_content)
    _split_content_issue_severity = staticmethod(_split_content_issue_severity)
    _core_expected_files = staticmethod(_core_expected_files)
    assess_plan_workspace_compatibility = staticmethod(
        _assess_plan_workspace_compatibility
    )
    persist_validation_result = staticmethod(_persist_validation_result)

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
        invalid_verification: List[int] = []
        invalid_rollback: List[int] = []
        invalid_expected_files: List[int] = []
        invalid_ops: List[int] = []
        missing_required_fields: Dict[int, List[str]] = {}
        extra_fields: Dict[int, List[str]] = {}
        required_fields = {
            "step_number",
            "description",
            "commands",
            "verification",
            "rollback",
            "expected_files",
        }
        allowed_fields = set(required_fields)
        allowed_fields.add("ops")

        for index, step in enumerate(plan, start=1):
            if not isinstance(step, dict):
                non_dict_steps.append(index)
                continue
            missing_fields = sorted(required_fields.difference(step.keys()))
            if missing_fields:
                missing_required_fields[index] = missing_fields
            extras = sorted(set(step.keys()).difference(allowed_fields))
            if extras:
                extra_fields[index] = extras
            if not isinstance(step.get("step_number"), int):
                invalid_step_numbers.append(index)
            if not isinstance(step.get("description", ""), str):
                invalid_descriptions.append(index)
            commands = step.get("commands", [])
            if not isinstance(commands, list) or any(
                not isinstance(command, str) for command in commands
            ):
                invalid_commands.append(index)
            verification = step.get("verification")
            if verification is not None and not isinstance(verification, str):
                invalid_verification.append(index)
            rollback = step.get("rollback")
            if rollback is not None and not isinstance(rollback, str):
                invalid_rollback.append(index)
            expected_files = step.get("expected_files", [])
            if expected_files is not None and (
                not isinstance(expected_files, list)
                or any(not isinstance(path, str) for path in expected_files)
            ):
                invalid_expected_files.append(index)
            ops = step.get("ops", [])
            if ops is not None:
                if not isinstance(ops, list):
                    invalid_ops.append(index)
                else:
                    for operation in ops:
                        if not validate_file_op_shape(operation):
                            invalid_ops.append(index)
                            break

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
        if missing_required_fields:
            errors.append(
                "Plan steps must include step_number, description, commands, verification, rollback, and expected_files"
            )
            details["missing_required_fields"] = missing_required_fields
        if extra_fields:
            errors.append("Plan steps must not include extra keys")
            details["extra_fields"] = extra_fields
        if invalid_verification:
            errors.append("Plan step verification values must be strings or null")
            details["invalid_verification_steps"] = invalid_verification
        if invalid_rollback:
            errors.append("Plan step rollback values must be strings or null")
            details["invalid_rollback_steps"] = invalid_rollback
        if invalid_expected_files:
            errors.append("Plan expected_files must be arrays of strings")
            details["invalid_expected_files_steps"] = invalid_expected_files
        if invalid_ops:
            errors.append(
                "Plan ops must be arrays of supported operation objects with valid string fields"
            )
            details["invalid_ops_steps"] = sorted(set(invalid_ops))

        return {"valid": not errors, "errors": errors, "details": details}

    @staticmethod
    def _plan_invalid_file_ops_paths(
        plan: List[Dict[str, Any]], project_dir: Path
    ) -> List[int]:
        invalid_steps: List[int] = []
        for index, step in enumerate(plan, start=1):
            step_number = step.get("step_number", index)
            for operation in step.get("ops", []) or []:
                try:
                    normalize_path_reference(
                        str(operation.get("path") or ""), project_dir
                    )
                except TaskWorkspaceViolationError:
                    invalid_steps.append(int(step_number))
                    break
        return sorted(set(invalid_steps))

    @staticmethod
    def _plan_replace_ops_missing_targets(
        plan: List[Dict[str, Any]], project_dir: Path
    ) -> Dict[int, List[str]]:
        known_paths = {
            str(path.relative_to(project_dir))
            for path in project_dir.rglob("*")
            if path.is_file()
        }
        missing_by_step: Dict[int, List[str]] = {}

        for index, step in enumerate(plan, start=1):
            step_number = int(step.get("step_number", index))
            for raw_operation in step.get("ops", []) or []:
                if not isinstance(raw_operation, dict):
                    continue
                operation = normalize_file_op_shape(raw_operation)
                op_name = str(operation.get("op") or "")
                raw_path = str(operation.get("path") or "")
                if not raw_path.strip():
                    continue
                try:
                    relative_path = normalize_path_reference(raw_path, project_dir)
                except TaskWorkspaceViolationError:
                    continue
                if relative_path == ".":
                    continue
                if op_name == "replace_in_file" and relative_path not in known_paths:
                    missing_by_step.setdefault(step_number, []).append(relative_path)
                elif op_name in {"write_file", "append_file"}:
                    known_paths.add(relative_path)
                elif op_name == "delete_file":
                    known_paths.discard(relative_path)

        return {
            step: sorted(set(paths)) for step, paths in missing_by_step.items() if paths
        }

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

    @classmethod
    def infer_validation_profile(
        cls,
        task_prompt: str,
        execution_profile: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> str:
        combined = " ".join(
            [task_prompt or "", title or "", description or "", execution_profile or ""]
        ).lower()
        if cls._task_looks_like_mutation_task(
            task_prompt, title=title, description=description
        ):
            return "mutation"
        implementation_markers = get_implementation_intent_markers()
        if execution_profile == "full_lifecycle" and any(
            marker in combined
            for marker in (
                "fix",
                "repair",
                "update",
                "modify",
                "write",
                "change",
                "preserve",
            )
        ):
            return "implementation"
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
            "python3 -m",
            "python3 ",
            "python -m",
            "node -e",
            "node ",
            "npm test",
            "pnpm test",
            "cargo test",
            "go test",
            "python ",
            "uv run",
            "npm run build",
            "pnpm build",
            "yarn build",
            "tsc",
        )
        if any(marker in text for marker in meaningful_markers):
            return False
        weak_command_patterns = (
            r"test\s+-[fds]\b",
            r"grep\s+-q\b",
            r"ls\b",
            r"echo\b",
            r"cat\b",
            r"find\b",
            r"wc\s+-l\b",
        )
        return any(
            re.search(rf"(?:^|[;&|()\n])\s*{pattern}(?:\s|$)", text)
            for pattern in weak_command_patterns
        )

    @staticmethod
    def repair_requires_independent_evidence(
        task_prompt: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        combined = " ".join([task_prompt or "", title or "", description or ""])
        return bool(
            re.search(
                r"\b(?:repair|fix|debug|regression|bug|failure|failing|broken)\b",
                combined,
                re.IGNORECASE,
            )
        )

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
    def _normalize_reported_changed_file(path_text: str) -> str:
        value = str(path_text or "").strip()
        if value.endswith(" (deleted)"):
            value = value[: -len(" (deleted)")].strip()
        return value.lstrip("./")

    @staticmethod
    def _task_looks_like_mutation_task(
        task_prompt: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        text = " ".join(
            str(value or "") for value in (title, description, task_prompt)
        ).lower()
        build_detection_text = re.sub(
            r"\b(?:do not|don't|without)\s+"
            r"(?:create|build|implement|scaffold|add)\b[^.;\n]*",
            " ",
            text,
        )
        mutation_terms = {
            "append",
            "archive",
            "changelog",
            "config",
            "delete",
            "docs",
            "documentation",
            "manifest",
            "metadata",
            "package.json",
            "readme",
            "release notes",
            "remove",
            "replace",
            "version",
        }
        build_terms = set(get_mutation_build_intent_markers())
        has_mutation_term = any(term in text for term in mutation_terms)
        has_build_term = any(term in build_detection_text for term in build_terms)
        return has_mutation_term and not has_build_term

    @classmethod
    def _mutation_expected_files(cls, plan: List[Dict[str, Any]]) -> List[str]:
        files: List[str] = []
        seen = set()

        def add(path_text: Any) -> None:
            normalized = str(path_text or "").strip().rstrip("/").lstrip("./")
            if not normalized or normalized in seen:
                return
            if Path(normalized).suffix.lower() in SOURCE_EXTENSIONS:
                return
            seen.add(normalized)
            files.append(normalized)

        for step in plan:
            for operation in step.get("ops", []) or []:
                if not isinstance(operation, dict):
                    continue
                if str(operation.get("op") or "") in {"delete_file", "mkdir"}:
                    continue
                add(operation.get("path"))
            for raw_path in step.get("expected_files", []) or []:
                add(raw_path)

        return files

    @classmethod
    def _mutation_completion_evidence(
        cls,
        *,
        project_dir: Path,
        plan: List[Dict[str, Any]],
        task_prompt: str,
        reported_changed_files: List[str],
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        expected_files = cls._mutation_expected_files(plan)
        materialized_files = [
            path_text
            for path_text in expected_files
            if (project_dir / path_text).resolve().is_file()
        ]
        normalized_reported = {
            cls._normalize_reported_changed_file(path_text)
            for path_text in reported_changed_files
        }
        matched_reported_files = [
            path_text
            for path_text in materialized_files
            if path_text in normalized_reported
        ]
        mutation_task = cls._task_looks_like_mutation_task(
            task_prompt, title=title, description=description
        )
        supported = bool(
            mutation_task
            and materialized_files
            and (not reported_changed_files or bool(matched_reported_files))
        )
        return {
            "supported": supported,
            "mutation_task": mutation_task,
            "expected_files": expected_files[:20],
            "materialized_files": materialized_files[:20],
            "matched_reported_files": matched_reported_files[:20],
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
            if ValidatorService._step_is_readonly_inspection(step):
                continue
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
                token in text for token in ("package.json", "npm ", "pnpm ", "node ")
            ) or re.search(r"\.(?:js|ts)(?![a-z0-9_])", text):
                seen_node = True
        return seen_python and seen_node

    @staticmethod
    def _plan_contains_placeholder_intent(
        plan: List[Dict[str, Any]], task_prompt: str = ""
    ) -> bool:
        allow_todo_fixme_literals = ValidatorService._task_allows_todo_fixme_literals(
            task_prompt
        )

        for step in plan:
            for operation in step.get("ops", []) or []:
                if not isinstance(operation, dict):
                    continue
                if operation.get("op") != "write_file":
                    continue
                if ValidatorService._write_file_content_has_placeholder_implementation(
                    str(operation.get("path", "")),
                    str(operation.get("content", "")),
                    allow_todo_fixme_literals=allow_todo_fixme_literals,
                ):
                    return True

            for command in step.get("commands", []) or []:
                if ValidatorService._command_writes_placeholder_implementation(
                    str(command or ""),
                    allow_todo_fixme_literals=allow_todo_fixme_literals,
                ):
                    return True
        return False

    @staticmethod
    def _task_allows_todo_fixme_literals(task_prompt: str) -> bool:
        lowered = str(task_prompt or "").lower()
        if not any(marker in lowered for marker in ("todo", "fixme")):
            return False
        return any(
            intent in lowered
            for intent in (
                "report",
                "scan",
                "scanner",
                "generator",
                "detect",
                "extract",
                "list",
                "summar",
            )
        )

    @staticmethod
    def _write_file_content_has_placeholder_implementation(
        path_text: str, content: str, *, allow_todo_fixme_literals: bool = False
    ) -> bool:
        raw = str(content or "")
        if path_allows_placeholder_fixture_content(path_text):
            return False

        if PLAN_STRUCTURAL_PLACEHOLDER_MARKER_PATTERN.search(raw):
            return True
        if not allow_todo_fixme_literals and PLAN_TODO_FIXME_MARKER_PATTERN.search(raw):
            return True
        if not PLAN_PASS_MARKER_PATTERN.search(raw):
            return False

        if Path(str(path_text or "")).suffix.lower() != ".py":
            return True

        try:
            tree = ast.parse(raw)
        except SyntaxError:
            return True

        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list) or len(body) != 1:
                continue
            if isinstance(body[0], ast.Pass) and isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                return True
        return False

    @staticmethod
    def _command_write_targets(command: str) -> List[str]:
        targets = ValidatorService._single_file_write_heredoc_targets(command)
        try:
            tokens = shlex.split(str(command or ""), posix=True)
        except ValueError:
            tokens = str(command or "").split()

        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token in {">", ">>"} and index + 1 < len(tokens):
                targets.append(tokens[index + 1])
                index += 2
                continue
            if token.startswith((">", ">>")) and token not in {">&1", ">&2"}:
                target = token.lstrip(">")
                if target:
                    targets.append(target)
            if token == "tee":
                next_index = index + 1
                while next_index < len(tokens) and tokens[next_index].startswith("-"):
                    next_index += 1
                if next_index < len(tokens):
                    targets.append(tokens[next_index])
            index += 1

        return [target for target in targets if target]

    @staticmethod
    def _command_writes_placeholder_implementation(
        command: str, *, allow_todo_fixme_literals: bool = False
    ) -> bool:
        raw = str(command or "")
        has_marker = (
            PLAN_STRUCTURAL_PLACEHOLDER_MARKER_PATTERN.search(raw)
            or PLAN_PASS_MARKER_PATTERN.search(raw)
            or (
                not allow_todo_fixme_literals
                and PLAN_TODO_FIXME_MARKER_PATTERN.search(raw)
            )
        )
        if not has_marker:
            return False

        targets = ValidatorService._command_write_targets(raw)
        if targets:
            return not all(
                path_allows_placeholder_fixture_content(target) for target in targets
            )

        return False

    @classmethod
    def _plan_declared_expected_files(cls, plan: List[Dict[str, Any]]) -> set[str]:
        files: set[str] = set()
        for step in plan:
            for raw_path in step.get("expected_files", []) or []:
                path = str(raw_path or "").strip().rstrip("/").lstrip("./")
                if path:
                    files.add(path)
        return files

    @classmethod
    def _plan_materialized_file_targets(cls, plan: List[Dict[str, Any]]) -> set[str]:
        files: set[str] = set()
        for step in plan:
            for operation in step.get("ops", []) or []:
                if not isinstance(operation, dict):
                    continue
                if str(operation.get("op") or "") in {"write_file", "append_file"}:
                    path = (
                        str(operation.get("path") or "")
                        .strip()
                        .rstrip("/")
                        .lstrip("./")
                    )
                    if path:
                        files.add(path)
            top_level_op = str(
                step.get("op") or step.get("step") or step.get("type") or ""
            ).strip()
            if top_level_op in {"create_file", "write_file", "write", "append_file"}:
                path = (
                    str(step.get("path") or step.get("file") or "")
                    .strip()
                    .rstrip("/")
                    .lstrip("./")
                )
                if path:
                    files.add(path)
            for command in step.get("commands", []) or []:
                for target in cls._command_write_targets(str(command or "")):
                    path = str(target or "").strip().rstrip("/").lstrip("./")
                    if path:
                        files.add(path)
        return files

    @staticmethod
    def _task_prompt_requires_materialization(
        task_prompt: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        combined = " ".join(
            str(value or "") for value in (task_prompt, title, description)
        ).lower()
        return any(
            marker in combined
            for marker in (
                "create",
                "build",
                "add",
                "write",
                "implement",
                "generate",
                "scaffold",
            )
        )

    @staticmethod
    def _task_allows_multiple_stacks(
        task_prompt: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        combined = " ".join([task_prompt or "", title or "", description or ""]).lower()
        explicit_pairs = get_multi_stack_pair_markers()
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
                    "tsconfig.json",
                )
            ) or re.search(r"\.(?:js|ts)(?![a-z0-9_])", text):
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
        diagnostics = ValidatorService._plan_command_budget_diagnostics(
            extracted_plan, output_text
        )
        return bool(diagnostics.get("has_brittle_commands"))

    @classmethod
    def _plan_command_budget_diagnostics(
        cls, extracted_plan: Optional[List[Dict[str, Any]]], output_text: str = ""
    ) -> Dict[str, Any]:
        if not extracted_plan:
            return {
                "step_count": 0,
                "max_command_length": 0,
                "heredoc_command_count": 0,
                "command_total_chars": 0,
                "oversized_command_steps": [],
                "has_brittle_commands": False,
                "brittle_command_subcodes": [],
                "brittle_command_step_details": {},
                "brittle_command_step_command_lengths": {},
            }

        heredoc_count = 0
        max_command_length = 0
        command_total_chars = 0
        oversized_command_steps: List[int] = []
        has_brittle_commands = False
        plan_subcodes: set = set()
        step_subcodes: Dict[int, List[str]] = {}
        step_command_lengths: Dict[int, List[int]] = {}

        def _flag(step_num, code: str) -> None:
            nonlocal has_brittle_commands
            has_brittle_commands = True
            plan_subcodes.add(code)
            if step_num is not None:
                step_subcodes.setdefault(int(step_num), []).append(code)

        for step in extracted_plan:
            commands = step.get("commands", [])
            if not isinstance(commands, list):
                _flag(step.get("step_number"), "non_list_commands")
                continue
            step_number = step.get("step_number")
            for command in commands:
                raw_command = str(command or "")
                lowered = raw_command.lower()
                command_length = len(raw_command)
                write_heredoc_targets = (
                    ValidatorService._single_file_write_heredoc_targets(raw_command)
                )
                command_total_chars += command_length
                max_command_length = max(max_command_length, command_length)
                if ValidatorService._uses_brittle_python_inline_command(raw_command):
                    _flag(step_number, "brittle_inline_python")
                heredoc_count += len(write_heredoc_targets)
                if "<<" in lowered:
                    if not write_heredoc_targets:
                        _flag(step_number, "disallowed_heredoc_shape")
                    if len(write_heredoc_targets) > 1:
                        _flag(step_number, "multiple_heredoc_in_command")
                    if ValidatorService._uses_looped_heredoc(raw_command):
                        _flag(step_number, "looped_heredoc")
                    if any(
                        ValidatorService._heredoc_target_is_unsafe(target)
                        for target in write_heredoc_targets
                    ):
                        _flag(step_number, "unsafe_heredoc_target")
                if raw_command.count("\n") > 25:
                    _flag(step_number, "too_many_lines")
                if command_length > MAX_PLANNING_COMMAND_CHARS:
                    _flag(step_number, "oversized_command_length")
                    if step_number is not None:
                        normalized_step_number = int(step_number)
                        oversized_command_steps.append(normalized_step_number)
                        step_command_lengths.setdefault(
                            normalized_step_number, []
                        ).append(command_length)

        if heredoc_count >= 2:
            has_brittle_commands = True
            plan_subcodes.add("multiple_heredoc_across_plan")

        lowered_output = (output_text or "").lower()
        if lowered_output.count("cat >") >= 2 and "```json" in lowered_output:
            has_brittle_commands = True
            plan_subcodes.add("markdown_wrapped_heredoc")

        return {
            "step_count": len(extracted_plan),
            "max_command_length": max_command_length,
            "heredoc_command_count": heredoc_count,
            "command_total_chars": command_total_chars,
            "oversized_command_steps": sorted(set(oversized_command_steps)),
            "has_brittle_commands": has_brittle_commands,
            "brittle_command_subcodes": sorted(plan_subcodes),
            "brittle_command_step_details": {
                k: sorted(set(v)) for k, v in step_subcodes.items()
            },
            "brittle_command_step_command_lengths": {
                k: sorted(set(v)) for k, v in step_command_lengths.items()
            },
            "malformed_shell_quoting_steps": cls._plan_malformed_shell_quoting_steps(
                extracted_plan
            ),
        }

    @staticmethod
    def _shadow_rule_warnings(
        command_budget: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Report downgrade candidates without changing validation status."""

        subcodes = set(command_budget.get("brittle_command_subcodes") or [])
        step_details = command_budget.get("brittle_command_step_details") or {}
        warnings: List[Dict[str, Any]] = []

        def _steps_for(codes: set[str]) -> List[int]:
            steps: List[int] = []
            for raw_step, raw_codes in step_details.items():
                if codes.intersection(set(raw_codes or [])):
                    try:
                        steps.append(int(raw_step))
                    except (TypeError, ValueError):
                        continue
            return sorted(set(steps))

        heredoc_codes = {
            "disallowed_heredoc_shape",
            "multiple_heredoc_in_command",
            "looped_heredoc",
            "unsafe_heredoc_target",
            "multiple_heredoc_across_plan",
            "markdown_wrapped_heredoc",
        }
        if subcodes.intersection(heredoc_codes):
            warnings.append(
                {
                    "rule_id": "model_behavior.heredoc_guidance",
                    "category": "model_behavior_patch",
                    "current_owner": "validator.command_budget_diagnostics",
                    "current_behavior": "repair_required",
                    "shadow_candidate": True,
                    "proposed_shadow_behavior": "warning_after_live_evidence",
                    "fallback_detectors": [
                        "structured_ops_contract",
                        "workspace_guard",
                        "completion_verification",
                    ],
                    "subcodes": sorted(subcodes.intersection(heredoc_codes)),
                    "steps": _steps_for(heredoc_codes),
                }
            )

        if "oversized_command_length" in subcodes:
            warnings.append(
                {
                    "rule_id": "model_behavior.command_length_prompt_patch",
                    "category": "model_behavior_patch",
                    "current_owner": "validator.command_budget_diagnostics",
                    "current_behavior": "repair_required",
                    "shadow_candidate": True,
                    "proposed_shadow_behavior": "warning_for_non_file_writing_commands",
                    "fallback_detectors": [
                        "structured_ops_contract",
                        "completion_verification",
                    ],
                    "subcodes": ["oversized_command_length"],
                    "steps": command_budget.get("oversized_command_steps") or [],
                }
            )

        malformed_shell_quoting_steps = (
            command_budget.get("malformed_shell_quoting_steps") or []
        )
        printf_or_shell_codes = {"brittle_inline_python", "too_many_lines"}
        if malformed_shell_quoting_steps or subcodes.intersection(
            printf_or_shell_codes
        ):
            warnings.append(
                {
                    "rule_id": "model_behavior.shell_quoting_patch",
                    "category": "model_behavior_patch",
                    "current_owner": "validator.command_budget_diagnostics",
                    "current_behavior": "repair_required",
                    "shadow_candidate": True,
                    "proposed_shadow_behavior": "shell_fallback_warning",
                    "fallback_detectors": [
                        "structured_ops_contract",
                        "executor_command_preflight",
                        "completion_verification",
                    ],
                    "subcodes": sorted(subcodes.intersection(printf_or_shell_codes)),
                    "steps": sorted(set(malformed_shell_quoting_steps)),
                }
            )

        return warnings

    @staticmethod
    def _command_has_malformed_shell_quoting(command: str) -> bool:
        raw = str(command or "")
        if "\\'" in raw and re.search(r"\bprintf\s+'", raw):
            return True
        try:
            shlex.split(raw, posix=True)
        except ValueError:
            return True
        return False

    @classmethod
    def _plan_malformed_shell_quoting_steps(
        cls, plan: List[Dict[str, Any]]
    ) -> List[int]:
        bad_steps: List[int] = []
        for index, step in enumerate(plan, start=1):
            step_number = step.get("step_number", index)
            step_text_parts = [
                str(step.get("verification") or ""),
                str(step.get("rollback") or ""),
            ]
            step_text_parts.extend(
                str(command or "") for command in step.get("commands", []) or []
            )
            if any(
                cls._command_has_malformed_shell_quoting(text)
                for text in step_text_parts
            ):
                bad_steps.append(int(step_number))
        return sorted(set(bad_steps))

    @staticmethod
    def _single_file_write_heredoc_targets(command: str) -> List[str]:
        """Return targets for bounded `cat > file <<EOF` write heredocs."""

        target_pattern = re.compile(
            r"(?:^|[\n;&|]\s*)"
            r"(?:mkdir\s+-p\s+[^\n;&|]+\s*&&\s*)?"
            r"cat\s+>\s*(?P<target>'[^']+'|\"[^\"]+\"|[^\s<;&|]+)"
            r"\s*<<\s*['\"]?[A-Za-z_][A-Za-z0-9_]*['\"]?",
            re.IGNORECASE,
        )
        targets: List[str] = []
        for match in target_pattern.finditer(str(command or "")):
            target = match.group("target").strip().strip("'\"")
            if target:
                targets.append(target)
        return targets

    @staticmethod
    def _uses_looped_heredoc(command: str) -> bool:
        first_line = str(command or "").split("\n", 1)[0].lower()
        return bool(re.search(r"\b(for|while)\b.*\bdo\b.*cat\s+>", first_line))

    @staticmethod
    def _heredoc_target_is_unsafe(target: str) -> bool:
        path_text = str(target or "").strip()
        if not path_text:
            return True
        candidate = Path(path_text)
        return (
            candidate.is_absolute() or "~" in candidate.parts or ".." in candidate.parts
        )

    @staticmethod
    def _uses_brittle_python_inline_command(command: str) -> bool:
        raw = str(command or "").strip()
        lowered = raw.lower()
        if "python -c" not in lowered and "python3 -c" not in lowered:
            return False
        if "stdin.read(" in lowered and not re.search(r"(^|[^<>])\|([^|]|$)|<", raw):
            return True

        quote_chars = raw.count('"') + raw.count("'")
        has_nested_python_content = any(
            marker in raw
            for marker in (
                "f'",
                'f"',
                "json.dumps(",
                "assert ",
                "{",
                "}",
            )
        )
        return quote_chars >= 4 and has_nested_python_content

    @staticmethod
    def _is_non_runnable_command(command: str) -> bool:
        text = str(command or "").strip()
        lowered = text.lower()
        if not text:
            return True
        if re.match(
            r"^\{\s*(?:\\?\"\\?|')?(?:ops|op|command|cmd)(?:\\?\"\\?|')?\s*:", text
        ):
            return True
        non_runnable_prefixes = (
            "write ",
            "edit ",
            "create files",
            "create file",
            "set up ",
            "setup ",
            "implement ",
            "add component",
            "update component",
            "verify ",
        )
        if lowered.startswith(non_runnable_prefixes):
            return True
        if lowered.startswith("check ") and "test " not in lowered:
            return True
        if lowered.startswith("ensure "):
            return True
        if lowered.startswith("confirm "):
            return True
        if re.match(
            r"^(create|build|make)\s+(the\s+)?(app|page|site|ui|component)\b", lowered
        ):
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
    def _step_is_readonly_inspection(step: Dict[str, Any]) -> bool:
        ops = step.get("ops") or []
        if isinstance(ops, list) and any(operation_has_file_op_path(op) for op in ops):
            return False
        commands = [
            str(command or "").strip()
            for command in (step.get("commands", []) or [])
            if str(command or "").strip()
        ]
        if not commands:
            return False
        readonly_prefixes = (
            "ls",
            "cat",
            "pwd",
            "find",
            "rg",
            "grep",
            "wc",
            "head",
            "tail",
            "sed -n",
        )
        if not all(command.startswith(readonly_prefixes) for command in commands):
            return False
        description = str(step.get("description") or "").lower()
        inspection_markers = (
            "inspect",
            "review",
            "analyze",
            "inventory",
            "audit",
            "list",
            "current workspace",
            "current project",
        )
        return any(marker in description for marker in inspection_markers)

    @staticmethod
    def _plan_missing_verification_steps(plan: List[Dict[str, Any]]) -> List[int]:
        missing_steps: List[int] = []
        for index, step in enumerate(plan, start=1):
            step_number = step.get("step_number", index)
            if ValidatorService._step_is_readonly_inspection(step):
                continue
            if not str(step.get("verification") or "").strip():
                missing_steps.append(step_number)
        return [step for step in missing_steps if step is not None]

    @staticmethod
    def _plan_missing_required_fields(
        plan: List[Dict[str, Any]],
    ) -> Dict[str, List[int]]:
        missing_description: List[int] = []
        missing_commands: List[int] = []

        for index, step in enumerate(plan, start=1):
            step_number = step.get("step_number", index)
            if not str(step.get("description") or "").strip():
                missing_description.append(step_number)

            commands = step.get("commands", [])
            ops = step.get("ops", [])
            has_file_ops = isinstance(ops, list) and any(
                operation_has_file_op_path(operation) for operation in ops
            )
            if not isinstance(commands, list) or (
                not any(str(command or "").strip() for command in commands)
                and not has_file_ops
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
        plan: List[Dict[str, Any]],
    ) -> Dict[int, List[str]]:
        """Detect command paths that violate the task-workspace contract."""

        findings: Dict[int, List[str]] = {}
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
                text = ValidatorService._strip_heredoc_bodies_for_command_scanning(text)
                try:
                    tokens = shlex.split(text, posix=True)
                except ValueError:
                    tokens = []
                for token_index, token in enumerate(tokens):
                    previous = tokens[token_index - 1] if token_index >= 1 else ""
                    command_name = Path(tokens[0]).name if tokens else ""
                    if previous in {"-c", "-e"} and command_name in {
                        "python",
                        "python3",
                        "node",
                    }:
                        continue
                    if token in allowed_absolute_tokens:
                        continue
                    if token.startswith("../") or "/../" in token:
                        if token not in fragments:
                            fragments.append(token)
                        continue
                    if absolute_path_pattern.fullmatch(token):
                        if token not in fragments:
                            fragments.append(token)

            if fragments:
                findings[int(step_number)] = fragments[:6]

        return findings

    @staticmethod
    def _strip_heredoc_bodies_for_command_scanning(command: str) -> str:
        """Keep shell syntax visible while hiding heredoc payload text.

        Path-safety checks should inspect the command and heredoc target, not file
        content such as CSS `url('../images/foo.svg')` written by the heredoc.
        """

        lines = str(command or "").splitlines()
        if not lines:
            return ""

        visible: List[str] = []
        delimiter: Optional[str] = None
        heredoc_pattern = re.compile(
            r"<<-?\s*(?:'(?P<single>[A-Za-z_][A-Za-z0-9_]*)'"
            r'|"(?P<double>[A-Za-z_][A-Za-z0-9_]*)"'
            r"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
        )

        for line in lines:
            stripped = line.strip()
            if delimiter is not None:
                if stripped == delimiter:
                    delimiter = None
                continue

            visible.append(line)
            match = heredoc_pattern.search(line)
            if match:
                delimiter = (
                    match.group("single")
                    or match.group("double")
                    or match.group("bare")
                )

        return "\n".join(visible)

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

        project_root = Path(project_dir)
        known_paths = {
            path.relative_to(project_root).as_posix()
            for path in project_root.rglob("*")
            if path.is_file()
        }
        missing: List[str] = []
        seen: set[str] = set()
        for step in plan:
            for raw_operation in step.get("ops", []) or []:
                if not isinstance(raw_operation, dict):
                    continue
                operation = normalize_file_op_shape(raw_operation)
                op_name = str(operation.get("op") or "")
                raw_path = str(operation.get("path") or "")
                if not raw_path.strip():
                    continue
                try:
                    relative_path = normalize_path_reference(raw_path, project_root)
                except TaskWorkspaceViolationError:
                    continue
                if relative_path == ".":
                    continue
                if op_name in {"write_file", "append_file"}:
                    known_paths.add(relative_path)
                elif op_name == "delete_file":
                    known_paths.discard(relative_path)

            step_source_paths = ValidatorService._core_expected_files([step])
            for command in step.get("commands", []) or []:
                step_source_paths.extend(
                    ValidatorService._command_source_read_targets(str(command or ""))
                )
            verification = str(step.get("verification") or "")
            if verification:
                step_source_paths.extend(
                    ValidatorService._command_source_read_targets(verification)
                )

            for path_text in step_source_paths:
                try:
                    relative_path = normalize_path_reference(path_text, project_root)
                except TaskWorkspaceViolationError:
                    relative_path = path_text
                if relative_path in known_paths:
                    continue
                candidate = (project_root / relative_path).resolve()
                if candidate.exists():
                    known_paths.add(relative_path)
                    continue
                if relative_path in seen:
                    continue
                seen.add(relative_path)
                missing.append(relative_path)
        return missing

    @staticmethod
    def _verification_plan_creates_new_source_assets(
        plan: List[Dict[str, Any]], project_dir: Optional[Path]
    ) -> List[str]:
        """Return app/source assets a verification plan tries to create from scratch."""

        if not project_dir or not project_dir.exists():
            return []

        blocked_extensions = {
            ".css",
            ".html",
            ".jsx",
            ".py",
            ".scss",
            ".svg",
            ".ts",
            ".tsx",
        }
        project_root = Path(project_dir)
        created: List[str] = []
        seen: set[str] = set()
        for step in plan:
            for raw_operation in step.get("ops", []) or []:
                if not isinstance(raw_operation, dict):
                    continue
                operation = normalize_file_op_shape(raw_operation)
                op_name = str(operation.get("op") or "")
                if op_name not in {"write_file", "append_file"}:
                    continue
                raw_path = str(operation.get("path") or "")
                if not raw_path.strip():
                    continue
                try:
                    relative_path = normalize_path_reference(raw_path, project_root)
                except TaskWorkspaceViolationError:
                    continue
                path = Path(relative_path)
                if path.suffix.lower() not in blocked_extensions:
                    continue
                if path.name.lower().startswith(("verify", "check")):
                    continue
                if path.parts and path.parts[0] in {"test", "tests", "spec"}:
                    continue
                if (project_root / relative_path).exists():
                    continue
                if relative_path not in seen:
                    seen.add(relative_path)
                    created.append(relative_path)
        return created

    @staticmethod
    def _verification_plan_mutates_app_source_assets(
        plan: List[Dict[str, Any]], project_dir: Optional[Path]
    ) -> List[str]:
        """Return app/source assets mutated by a verification-only plan."""

        if not project_dir or not project_dir.exists():
            return []

        blocked_extensions = {
            ".css",
            ".html",
            ".jsx",
            ".py",
            ".scss",
            ".svg",
            ".ts",
            ".tsx",
        }
        project_root = Path(project_dir)
        mutated: List[str] = []
        seen: set[str] = set()
        for step in plan:
            for raw_operation in step.get("ops", []) or []:
                if not isinstance(raw_operation, dict):
                    continue
                operation = normalize_file_op_shape(raw_operation)
                op_name = str(operation.get("op") or "")
                if op_name not in {"write_file", "append_file", "replace_in_file"}:
                    continue
                raw_path = str(operation.get("path") or "")
                if not raw_path.strip():
                    continue
                try:
                    relative_path = normalize_path_reference(raw_path, project_root)
                except TaskWorkspaceViolationError:
                    continue
                path = Path(relative_path)
                if path.suffix.lower() not in blocked_extensions:
                    continue
                if path.name.lower().startswith(("verify", "check")):
                    continue
                if path.parts and path.parts[0] in {"test", "tests", "spec"}:
                    continue
                if relative_path not in seen:
                    seen.add(relative_path)
                    mutated.append(relative_path)
        return mutated

    @staticmethod
    def _command_source_read_targets(command: str) -> List[str]:
        """Extract likely source-file reads from shell or inline Node commands."""

        raw = str(command or "")
        targets: List[str] = []

        for match in re.finditer(
            r"\b(?:readFileSync|existsSync|statSync|lstatSync)\(\s*['\"]([^'\"]+)['\"]",
            raw,
        ):
            targets.append(match.group(1))

        try:
            tokens = shlex.split(raw, posix=True)
        except ValueError:
            tokens = raw.split()

        if tokens:
            command_name = Path(tokens[0]).name
            if command_name in {"cat", "head", "tail", "less"}:
                for token in tokens[1:]:
                    if token in {"|", "||", "&&", ";"}:
                        break
                    if token.startswith("-") or token.startswith((">", "2>")):
                        continue
                    targets.append(token)
            elif command_name in {"node", "python", "python3"} and len(tokens) > 1:
                script = tokens[1]
                if script not in {"-e", "-c"}:
                    targets.append(script)

        filtered: List[str] = []
        seen: set[str] = set()
        for target in targets:
            path_text = str(target or "").strip()
            if (
                not path_text
                or path_text in {".", ".."}
                or path_text.startswith(("-", "$", "http://", "https://"))
                or any(char in path_text for char in "*?[]{}")
            ):
                continue
            if Path(path_text).suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            if path_text not in seen:
                seen.add(path_text)
                filtered.append(path_text)
        return filtered

    @staticmethod
    def _source_path_mentions(*values: Any) -> List[str]:
        """Extract explicit relative source paths from task text."""

        extensions = "|".join(re.escape(ext.lstrip(".")) for ext in SOURCE_EXTENSIONS)
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_.~/-])"
            rf"([A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+\.({extensions}))"
            rf"(?![A-Za-z0-9_.-])",
            re.IGNORECASE,
        )
        files: List[str] = []
        seen: set[str] = set()
        for value in values:
            for match in pattern.finditer(str(value or "")):
                path_text = match.group(1).replace("\\", "/").strip().lstrip("./")
                if (
                    not path_text
                    or path_text.startswith(("/", "../", "~"))
                    or "/../" in path_text
                ):
                    continue
                if Path(path_text).suffix.lower() not in SOURCE_EXTENSIONS:
                    continue
                if path_text not in seen:
                    seen.add(path_text)
                    files.append(path_text)
        return files

    @staticmethod
    def _plan_contains_duplicated_path_roots(
        plan: List[Dict[str, Any]],
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
    def _plan_negative_existing_file_checks(
        plan: List[Dict[str, Any]],
        project_dir: Optional[Path],
    ) -> Dict[int, List[str]]:
        """Detect negative existence preconditions for files this task creates."""

        if project_dir is None:
            return {}

        expected_targets = {
            str(path or "").strip().lstrip("./")
            for step in plan
            for path in (step.get("expected_files", []) or [])
            if str(path or "").strip()
        }
        for step in plan:
            for operation in step.get("ops", []) or []:
                if not isinstance(operation, dict):
                    continue
                if str(operation.get("op") or "").strip() not in {
                    "write_file",
                    "append_file",
                    "replace_in_file",
                }:
                    continue
                path_text = str(operation.get("path") or "").strip().lstrip("./")
                if path_text:
                    expected_targets.add(path_text)

        findings: Dict[int, List[str]] = {}
        negative_patterns = (
            re.compile(r"\btest\s+!\s+-[efs]\s+(?P<path>[^\s;&|]+)"),
            re.compile(r"\[\s+!\s+-[efs]\s+(?P<path>[^\]\s;&|]+)\s+\]"),
        )
        for index, step in enumerate(plan, start=1):
            step_number = int(step.get("step_number", index))
            commands = [
                str(command or "") for command in step.get("commands", []) or []
            ]
            if step.get("verification"):
                commands.append(str(step.get("verification") or ""))
            for command in commands:
                for pattern in negative_patterns:
                    for match in pattern.finditer(command):
                        path_text = (
                            match.group("path").strip().strip("'\"").lstrip("./")
                        )
                        if path_text not in expected_targets:
                            continue
                        if (Path(project_dir) / path_text).exists():
                            findings.setdefault(step_number, []).append(path_text)

        return {step: sorted(set(paths)) for step, paths in findings.items()}

    @staticmethod
    def _plan_mutating_steps_for_read_only_stage(
        plan: List[Dict[str, Any]], workflow_stage: Optional[str]
    ) -> List[int]:
        if workflow_stage not in {"diagnose", "plan", "review", "validate", "complete"}:
            return []

        mutating_ops = {
            "write_file",
            "append_file",
            "replace_in_file",
            "create_file",
            "mkdir",
            "delete_file",
        }
        mutating_command_patterns = (
            re.compile(r"(^|[;&|]\s*)(mkdir|touch|cp|mv|rm)\b"),
            re.compile(r"\bsed\s+-i\b"),
            re.compile(r">\s*[^&\s]"),
            re.compile(r"\btee\s+"),
        )
        findings: List[int] = []
        for index, step in enumerate(plan, start=1):
            step_number = int(step.get("step_number", index))
            if any(
                isinstance(operation, dict)
                and str(operation.get("op") or "").strip() in mutating_ops
                for operation in (step.get("ops") or [])
            ):
                findings.append(step_number)
                continue
            commands = [
                str(command or "") for command in step.get("commands", []) or []
            ]
            if any(
                pattern.search(command)
                for command in commands
                for pattern in mutating_command_patterns
            ):
                findings.append(step_number)
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
        marker_groups = get_workflow_markers(workflow_profile)
        frontend_markers = marker_groups.get("frontend") or []
        backend_markers = marker_groups.get("backend") or []
        wire_api_config_markers = marker_groups.get("wire_api_config") or []
        verify_dev_startup_markers = marker_groups.get("verify_dev_startup") or []
        frontend_exclusions = marker_groups.get("frontend_skeleton_exclusions") or []
        backend_exclusions = marker_groups.get("backend_skeleton_exclusions") or []

        has_frontend_markers = any(marker in text for marker in frontend_markers)
        has_backend_markers = any(marker in text for marker in backend_markers)

        if any(marker in text for marker in wire_api_config_markers):
            return "wire_api_config"

        if has_frontend_markers and not any(
            marker in text for marker in frontend_exclusions
        ):
            return "create_frontend_skeleton"

        if has_backend_markers and not any(
            marker in text for marker in backend_exclusions
        ):
            return "create_backend_skeleton"

        if any(marker in text for marker in verify_dev_startup_markers):
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
        phase_order = get_workflow_phases(workflow_profile or "")
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
        workflow_stage: Optional[str] = None,
    ) -> PlanOutcome:
        profile = cls.infer_validation_profile(
            task_prompt, execution_profile, title=title, description=description
        )
        if workflow_stage in {"diagnose", "plan", "review", "validate", "complete"}:
            profile = "verification"
        warnings: List[str] = []
        repairable: List[str] = []
        rejected: List[str] = []
        details: Dict[str, Any] = {"plan_length": len(plan)}
        schema_validation = cls.validate_plan_schema(plan)
        details["plan_schema"] = schema_validation
        if not schema_validation["valid"]:
            rejected.extend(schema_validation["errors"])
            details.update(schema_validation["details"])

        read_only_stage_mutations = cls._plan_mutating_steps_for_read_only_stage(
            plan, workflow_stage
        )
        if read_only_stage_mutations:
            repairable.append(
                f"Workflow stage '{workflow_stage}' must not mutate files or directories"
            )
            details["read_only_stage_mutation_steps"] = read_only_stage_mutations

        if project_dir is not None:
            invalid_ops_path_steps = cls._plan_invalid_file_ops_paths(
                plan, Path(project_dir)
            )
            if invalid_ops_path_steps:
                rejected.append(
                    "Plan write_file operations must stay inside the task workspace; "
                    "other file operations must stay inside the task workspace "
                    f"(steps: {invalid_ops_path_steps[:5]})"
                )
                details["invalid_ops_path_steps"] = invalid_ops_path_steps

            missing_replace_targets = cls._plan_replace_ops_missing_targets(
                plan, Path(project_dir)
            )
            if missing_replace_targets:
                bad_steps = sorted(missing_replace_targets.keys())
                repairable.append(
                    "`replace_in_file` operations must target files that already "
                    "exist in the current workspace or were created by an earlier "
                    f"plan step (steps: {bad_steps[:5]})"
                )
                details["missing_replace_in_file_targets"] = missing_replace_targets

        command_budget = cls._plan_command_budget_diagnostics(plan, output_text)
        details["step_count"] = command_budget["step_count"]
        details["max_command_length"] = command_budget["max_command_length"]
        details["heredoc_command_count"] = command_budget["heredoc_command_count"]
        details["command_total_chars"] = command_budget["command_total_chars"]
        shadow_warnings = cls._shadow_rule_warnings(command_budget)
        if shadow_warnings:
            details["shadow_warnings"] = shadow_warnings
        if command_budget.get("oversized_command_steps"):
            details["oversized_command_steps"] = command_budget[
                "oversized_command_steps"
            ]
        malformed_shell_quoting_steps = (
            command_budget.get("malformed_shell_quoting_steps") or []
        )
        if malformed_shell_quoting_steps:
            details["malformed_shell_quoting_steps"] = malformed_shell_quoting_steps

        if len(plan) > MAX_INITIAL_PLAN_STEPS:
            repairable.append(
                f"Plan contains too many steps for the initial planning budget "
                f"(max: {MAX_INITIAL_PLAN_STEPS}, actual: {len(plan)})"
            )
            details["max_steps"] = MAX_INITIAL_PLAN_STEPS

        if command_budget.get("has_brittle_commands"):
            repairable.append(
                "Plan contains brittle heredoc-heavy or malformed commands"
            )
            brittle_subcodes = command_budget.get("brittle_command_subcodes") or []
            if brittle_subcodes:
                details["brittle_command_subcodes"] = brittle_subcodes
            brittle_step_details = (
                command_budget.get("brittle_command_step_details") or {}
            )
            if brittle_step_details:
                details["brittle_command_step_details"] = brittle_step_details
            brittle_step_lengths = (
                command_budget.get("brittle_command_step_command_lengths") or {}
            )
            if brittle_step_lengths:
                details["brittle_command_step_command_lengths"] = brittle_step_lengths
        if malformed_shell_quoting_steps:
            repairable.append(
                "Plan contains malformed shell quoting in runnable commands "
                f"(steps: {malformed_shell_quoting_steps[:5]})"
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

        negative_existing_checks = cls._plan_negative_existing_file_checks(
            plan, project_dir
        )
        if negative_existing_checks:
            bad_steps = sorted(negative_existing_checks.keys())
            repairable.append(
                "Plan checks that expected output files do not exist even though "
                "they are already present in the workspace "
                f"(steps: {bad_steps[:5]})"
            )
            details["negative_existing_file_checks"] = negative_existing_checks

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

        stage_allows_materialization = workflow_stage not in {
            "diagnose",
            "plan",
            "review",
            "validate",
            "complete",
        }
        if profile == "implementation":
            if (
                cls._task_prompt_requires_materialization(
                    task_prompt, title=title, description=description
                )
                and stage_allows_materialization
            ):
                declared_expected_files = cls._plan_declared_expected_files(plan)
                materialized_targets = cls._plan_materialized_file_targets(plan)
                existing_expected_files = {
                    path
                    for path in declared_expected_files
                    if project_dir is not None and (Path(project_dir) / path).exists()
                }
                unmaterialized_expected_files = sorted(
                    declared_expected_files.difference(
                        materialized_targets | existing_expected_files
                    )
                )
                if declared_expected_files and unmaterialized_expected_files:
                    repairable.append(
                        "Plan declares expected files without materializing them "
                        "through file operations or shell writes"
                    )
                    details["unmaterialized_expected_files"] = (
                        unmaterialized_expected_files[:20]
                    )

            missing_verification_steps = cls._plan_missing_verification_steps(plan)
            if missing_verification_steps:
                repairable.append(
                    "Plan is missing verification commands for implementation-heavy work "
                    f"(steps: {missing_verification_steps[:5]})"
                )
                details["missing_verification_steps"] = missing_verification_steps

            weak_verification_steps = [
                step.get("step_number")
                for step in plan
                if step.get("step_number") not in missing_verification_steps
                and not cls._step_is_readonly_inspection(step)
                and cls._verification_is_weak(step.get("verification"))
            ]
            if weak_verification_steps:
                repairable.append(
                    "Plan uses weak verification for implementation-heavy work "
                    f"(steps: {weak_verification_steps[:5]})"
                )
                details["weak_verification_steps"] = weak_verification_steps
                details["verification_command_quality"] = [
                    {
                        "step_number": step.get("step_number"),
                        "command_quality": classify_verification_command(
                            step.get("verification")
                        ),
                    }
                    for step in plan
                    if step.get("step_number") in weak_verification_steps
                ]

            if cls._plan_contains_placeholder_intent(plan, task_prompt):
                repairable.append(
                    "Plan appears to generate placeholder or stub implementations"
                )
                details["placeholder_only_implementation"] = True
        elif profile == "verification":
            mutated_source_assets = cls._verification_plan_mutates_app_source_assets(
                plan, project_dir
            )
            if mutated_source_assets:
                repairable.append(
                    "Verification/review plan mutates app source assets instead "
                    "of only verifying the current workspace "
                    f"(files: {mutated_source_assets[:5]})"
                )
                details["verification_profile_mutated_source_assets"] = (
                    mutated_source_assets[:20]
                )
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
            created_source_assets = cls._verification_plan_creates_new_source_assets(
                plan, project_dir
            )
            if created_source_assets:
                repairable.append(
                    "Verification/review plan creates new app source assets instead "
                    "of verifying the current workspace "
                    f"(files: {created_source_assets[:5]})"
                )
                details["verification_profile_created_source_assets"] = (
                    created_source_assets[:20]
                )

        if len(plan) > 1 and not schema_validation.get("errors"):
            _first = plan[0]
            _first_ops = _first.get("ops") or []
            _first_cmds = _first.get("commands") or []
            _has_first_write = any(
                (op.get("op") or "")
                in ("write_file", "create_file", "append_file", "mkdir")
                for op in _first_ops
            )
            if not _has_first_write and _first_cmds:
                _existence_re = re.compile(r"test\s+-[fds]\s+(\S+)")
                _checked = {
                    Path(m.group(1)).name
                    for cmd in _first_cmds
                    for m in _existence_re.finditer(cmd)
                }
                if _checked:
                    for _j in range(1, len(plan)):
                        _later_ops = plan[_j].get("ops") or []
                        _created = {
                            Path(op.get("path") or "").name
                            for op in _later_ops
                            if (op.get("op") or "") in ("write_file", "create_file")
                        }
                        if _created & _checked:
                            plan[0], plan[_j] = plan[_j], plan[0]
                            for _k, _s in enumerate(plan):
                                _s["step_number"] = _k + 1
                            warnings.append(
                                f"Plan step order corrected: moved file creation "
                                f"before existence check for "
                                f"{sorted(_created & _checked)}"
                            )
                            details["step_order_corrected"] = sorted(
                                _created & _checked
                            )
                            break

        if cls._plan_contains_stack_conflict(plan, task_prompt):
            repairable.append(
                "Plan mixes inconsistent implementation stacks for one task"
            )
            details["stack_conflict"] = True

        semantic_violation_codes: List[str] = []
        if non_runnable_steps:
            semantic_violation_codes.append("non_runnable_command")
        if nested_workspace_steps or nested_project_root_steps:
            semantic_violation_codes.append("nested_project_folder_command")
        if details.get("missing_verification_steps"):
            semantic_violation_codes.append("missing_verification_command")
        if details.get("weak_verification_steps"):
            semantic_violation_codes.append("weak_verification")
            weak_quality_values = {
                str(entry.get("command_quality") or "")
                for entry in details.get("verification_command_quality", [])
            }
            if "insufficient" in weak_quality_values:
                semantic_violation_codes.append("command_quality_insufficient")
            if "smoke_only" in weak_quality_values:
                semantic_violation_codes.append("command_quality_smoke_only")
        if details.get("malformed_shell_quoting_steps"):
            semantic_violation_codes.append("malformed_shell_quoting")
        if details.get("verification_profile_mutated_source_assets"):
            semantic_violation_codes.append("verification_mutates_source_assets")
        if semantic_violation_codes:
            details["semantic_violation_codes"] = semantic_violation_codes

        verdict = ValidationVerdict(
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
        if verdict.rejected:
            return PlanRejected(verdict=verdict)
        if verdict.repairable:
            return PlanRepairRequired(verdict=verdict)
        return PlanAccepted(verdict=verdict)

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
        delete_targets = {
            str(op.get("path", "")).strip().lstrip("./")
            for op in (step.get("ops") or [])
            if isinstance(op, dict)
            and str(op.get("op", "")).strip() == "delete_file"
            and str(op.get("path", "")).strip()
        }
        reported_changed_file_set = {
            str(path).strip().lstrip("./") for path in reported_changed_files
        }
        materialized_file_set = {
            str(path).strip().lstrip("./") for path in materialized_files
        }
        delete_materialized_files = {
            path
            for path in reported_changed_file_set
            if path in delete_targets and not (project_dir / path).exists()
        }
        if reported_changed_files and materialized_files:
            if not (
                (reported_changed_file_set & materialized_file_set)
                | delete_materialized_files
            ):
                repairable.append(
                    "Step reported file changes but none materialized in the expected workspace"
                )
                details["reported_changed_files"] = reported_changed_files[:20]
                details["materialized_files"] = materialized_files[:20]
                if delete_targets:
                    details["delete_targets"] = sorted(delete_targets)[:20]
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
        workflow_stage: Optional[str] = None,
    ) -> ValidationVerdict:
        profile = cls.infer_validation_profile(
            task_prompt, execution_profile, title=title, description=description
        )
        if workflow_stage in {"diagnose", "plan", "review", "validate", "complete"}:
            profile = "verification"
        expected_core_files = list(
            dict.fromkeys(
                cls._core_expected_files(plan)
                + cls._source_path_mentions(title, description, task_prompt)
            )
        )
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
        mutation_completion = cls._mutation_completion_evidence(
            project_dir=project_dir,
            plan=plan,
            task_prompt=task_prompt,
            reported_changed_files=reported_changed_files,
            title=title,
            description=description,
        )
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
        details["mutation_completion"] = mutation_completion
        command_quality_rank = {
            "missing": 0,
            "insufficient": 1,
            "smoke_only": 2,
            "behavioral": 3,
            "regression_test": 4,
        }
        command_quality_by_step: List[Dict[str, Any]] = []
        for step in plan or []:
            command = str(step.get("verification") or "").strip()
            quality = classify_verification_command(command)
            command_quality_by_step.append(
                {
                    "step_number": step.get("step_number"),
                    "command": command,
                    "command_quality": quality,
                }
            )
        completion_verification_command = str(
            completion_evidence.get("completion_verification_command")
            or completion_evidence.get("verification_command")
            or ""
        ).strip()
        if completion_verification_command:
            command_quality_by_step.append(
                {
                    "step_number": None,
                    "source": "completion_verification",
                    "command": completion_verification_command,
                    "command_quality": classify_verification_command(
                        completion_verification_command
                    ),
                }
            )
        best_command_quality = max(
            (entry["command_quality"] for entry in command_quality_by_step),
            key=lambda quality: command_quality_rank.get(str(quality), 0),
            default="missing",
        )
        requires_independent_evidence = cls.repair_requires_independent_evidence(
            task_prompt, title=title, description=description
        )
        integrity_findings = scan_test_file_changes(
            reported_changed_files,
            project_dir,
        )
        change_set = completion_evidence.get("change_set")
        if isinstance(change_set, dict):
            integrity_findings.extend(check_test_preservation(change_set, project_dir))
        else:
            change_set = None
        pre_existing_tests = pre_existing_python_test_files(project_dir, change_set)
        behavior_baseline = completion_evidence.get("behavior_baseline")
        behavior_baseline_passed = bool(
            isinstance(behavior_baseline, dict) and behavior_baseline.get("passed")
        )
        has_independent_regression_test = (
            best_command_quality == "regression_test" and bool(pre_existing_tests)
        )
        integrity_payload = [finding.to_dict() for finding in integrity_findings]
        integrity_blockers = [
            finding
            for finding in integrity_findings
            if finding.severity == "error" and finding.confidence == "high"
        ]
        verification_insufficient = False
        semantic_violation_codes: List[str] = []
        if best_command_quality == "missing":
            semantic_violation_codes.append("command_quality_missing")
        elif best_command_quality == "insufficient":
            semantic_violation_codes.append("command_quality_insufficient")
        elif best_command_quality == "smoke_only":
            semantic_violation_codes.append("command_quality_smoke_only")
        semantic_violation_codes.extend(
            sorted({finding.code for finding in integrity_findings})
        )
        if integrity_blockers:
            semantic_violation_codes.append("test_preservation_violation")
        details["validation_evidence"] = {
            "command_quality": best_command_quality,
            "command_quality_by_step": command_quality_by_step[:20],
            "integrity_findings": integrity_payload[:50],
            "semantic_violation_codes": sorted(set(semantic_violation_codes)),
            "requires_independent_evidence": requires_independent_evidence,
            "pre_existing_test_files": pre_existing_tests[:20],
            "has_independent_regression_test": has_independent_regression_test,
            "behavior_baseline": behavior_baseline,
            "behavior_baseline_passed": behavior_baseline_passed,
            "verification_insufficient": False,
        }
        if not contract["summary_generated"]:
            rejected.append("Completion contract requires a generated task summary")
        if (
            contract["requires_source_outputs"]
            and contract["execution_results_count"] <= 0
        ):
            rejected.append(
                "Completion contract requires at least one recorded execution result"
            )
        if requires_independent_evidence:
            if best_command_quality in {"missing", "insufficient"}:
                verification_insufficient = True
                rejected.append(
                    "Repair task verification is insufficient: no meaningful independent verification command ran"
                )
            elif best_command_quality == "smoke_only":
                verification_insufficient = True
                warnings.append(
                    "Repair task verification is smoke-only; independent behavioral evidence is weak"
                )
            elif (
                best_command_quality == "regression_test"
                and not has_independent_regression_test
                and not behavior_baseline_passed
            ):
                verification_insufficient = True
                rejected.append(
                    "Repair task verification is insufficient: regression tests appear to be newly generated without pre-existing test coverage"
                )
            if integrity_blockers:
                verification_insufficient = True
                for finding in integrity_blockers[:5]:
                    rejected.append(
                        f"Verification integrity blocker: {finding.message}"
                    )
        elif integrity_blockers:
            warnings.extend(
                f"Verification integrity warning: {finding.message}"
                for finding in integrity_blockers[:5]
            )
        details["validation_evidence"][
            "verification_insufficient"
        ] = verification_insufficient

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

        if (
            profile == "implementation"
            and not candidate_files
            and not mutation_completion["supported"]
        ):
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
                and not mutation_completion["supported"]
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
