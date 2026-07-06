"""Rule-first orchestration validation helpers."""

from __future__ import annotations

import copy
import re
import shlex
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
    get_mutation_build_intent_markers,
    get_workflow_markers,
    get_workflow_phases,
)
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
from app.services.orchestration.planning.task_bootstrap_contract import (
    BootstrapTaskType,
    build_task1_bootstrap_contract,
    validate_task1_bootstrap_contract,
)
from .rules.contract_placeholders import (
    _plan_contains_placeholder_intent,
    _plan_fake_verification_artifact_steps,
    _plan_materialized_file_targets,
    _plan_placeholder_source_write_ops,
    _step_uses_fake_verification_artifact,
    _write_file_content_has_placeholder_implementation,
)
from .rules.contract_python import (
    _expected_source_files_not_materialized,
    _plan_appends_contextual_python_fragments,
    _plan_physical_src_python_import_details,
    _plan_python_source_syntax_issues,
    _plan_writes_import_time_python_parse_args,
    _plan_writes_obvious_undefined_python_decorators,
    _plan_writes_obvious_undefined_python_test_names,
    _plan_writes_physical_src_python_imports,
    _python_package_root_contract_violation,
)
from .rules.contract_frontend import (
    _frontend_wrong_stack_materializations,
    _infer_stack_from_plan,
    _plan_contains_stack_conflict,
    _plan_static_site_off_root_mutations,
    _plan_writes_obvious_undefined_js_identifiers,
    _task_allows_multiple_stacks,
)
from .rules.contract_commands import (
    _heredoc_target_is_unsafe,
    _plan_command_budget_diagnostics,
    _plan_contains_background_processes,
    _plan_contains_non_runnable_commands,
    _shadow_rule_warnings,
    _single_file_write_heredoc_targets,
    _uses_brittle_python_inline_command,
    _uses_looped_heredoc,
)
from .rules.contract_verification import (
    _command_source_read_targets,
    _plan_missing_verification_steps,
    _verification_is_weak,
    _verification_plan_creates_new_source_assets,
    _verification_plan_missing_workspace_files,
    _verification_plan_mutates_app_source_assets,
)
from .rules.core_schema import (
    _infer_workflow_phase_for_step,
    _plan_failable_review_probe_steps,
    _plan_has_invalid_step_sequence,
    _plan_missing_required_fields,
    _workflow_phase_order_violations,
    validate_plan_schema,
)
from .rules.core_file_ops import (
    _file_op_alias_issue,
    _nested_file_op_issue,
    _plan_empty_replace_old_text_steps,
    _plan_invalid_file_ops_paths,
    _plan_mutating_steps_for_read_only_stage,
    _plan_replace_ops_missing_targets,
    _read_only_stage_allows_report_write,
    _replace_in_file_has_repairable_old_text_issue,
    _step_is_readonly_inspection,
)
from .rules.core_paths import (
    _plan_contains_duplicated_path_roots,
    _plan_contains_unsafe_command_paths,
    _plan_contains_unsafe_paths,
    _plan_creates_nested_project_root,
    _plan_negative_existing_file_checks,
    _plan_nests_task_workspace,
    _resolve_existing_static_site_mentions,
    _source_path_mentions,
    _strip_heredoc_bodies_for_command_scanning,
)

MAX_INITIAL_PLAN_STEPS = 4
MAX_PLANNING_COMMAND_CHARS = 900
READ_ONLY_WORKFLOW_STAGES = {
    "diagnose",
    "plan",
    "review",
    "validate",
    "validation",
    "complete",
}


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

    # core_invariant rule delegates (app/services/orchestration/validation/rules/).
    validate_plan_schema = staticmethod(validate_plan_schema)
    _plan_missing_required_fields = staticmethod(_plan_missing_required_fields)
    _plan_has_invalid_step_sequence = staticmethod(_plan_has_invalid_step_sequence)
    _plan_failable_review_probe_steps = staticmethod(_plan_failable_review_probe_steps)
    _infer_workflow_phase_for_step = staticmethod(_infer_workflow_phase_for_step)
    _workflow_phase_order_violations = staticmethod(_workflow_phase_order_violations)
    _file_op_alias_issue = staticmethod(_file_op_alias_issue)
    _nested_file_op_issue = staticmethod(_nested_file_op_issue)
    _plan_invalid_file_ops_paths = staticmethod(_plan_invalid_file_ops_paths)
    _plan_replace_ops_missing_targets = staticmethod(_plan_replace_ops_missing_targets)
    _replace_in_file_has_repairable_old_text_issue = staticmethod(
        _replace_in_file_has_repairable_old_text_issue
    )
    _plan_empty_replace_old_text_steps = staticmethod(
        _plan_empty_replace_old_text_steps
    )
    _step_is_readonly_inspection = staticmethod(_step_is_readonly_inspection)
    _plan_mutating_steps_for_read_only_stage = staticmethod(
        _plan_mutating_steps_for_read_only_stage
    )
    _read_only_stage_allows_report_write = staticmethod(
        _read_only_stage_allows_report_write
    )
    _plan_contains_unsafe_paths = staticmethod(_plan_contains_unsafe_paths)
    _plan_contains_unsafe_command_paths = staticmethod(
        _plan_contains_unsafe_command_paths
    )
    _strip_heredoc_bodies_for_command_scanning = staticmethod(
        _strip_heredoc_bodies_for_command_scanning
    )
    _plan_nests_task_workspace = staticmethod(_plan_nests_task_workspace)
    _plan_creates_nested_project_root = staticmethod(_plan_creates_nested_project_root)
    _source_path_mentions = staticmethod(_source_path_mentions)
    _resolve_existing_static_site_mentions = staticmethod(
        _resolve_existing_static_site_mentions
    )
    _plan_contains_duplicated_path_roots = staticmethod(
        _plan_contains_duplicated_path_roots
    )
    _plan_negative_existing_file_checks = staticmethod(
        _plan_negative_existing_file_checks
    )

    # workload_contract rule delegates (app/services/orchestration/validation/rules/).
    _plan_contains_placeholder_intent = staticmethod(_plan_contains_placeholder_intent)
    _plan_fake_verification_artifact_steps = staticmethod(
        _plan_fake_verification_artifact_steps
    )
    _plan_materialized_file_targets = staticmethod(_plan_materialized_file_targets)
    _plan_placeholder_source_write_ops = staticmethod(
        _plan_placeholder_source_write_ops
    )
    _step_uses_fake_verification_artifact = staticmethod(
        _step_uses_fake_verification_artifact
    )
    _write_file_content_has_placeholder_implementation = staticmethod(
        _write_file_content_has_placeholder_implementation
    )
    _expected_source_files_not_materialized = staticmethod(
        _expected_source_files_not_materialized
    )
    _plan_appends_contextual_python_fragments = staticmethod(
        _plan_appends_contextual_python_fragments
    )
    _plan_physical_src_python_import_details = staticmethod(
        _plan_physical_src_python_import_details
    )
    _plan_python_source_syntax_issues = staticmethod(_plan_python_source_syntax_issues)
    _plan_writes_import_time_python_parse_args = staticmethod(
        _plan_writes_import_time_python_parse_args
    )
    _plan_writes_obvious_undefined_python_decorators = staticmethod(
        _plan_writes_obvious_undefined_python_decorators
    )
    _plan_writes_obvious_undefined_python_test_names = staticmethod(
        _plan_writes_obvious_undefined_python_test_names
    )
    _plan_writes_physical_src_python_imports = staticmethod(
        _plan_writes_physical_src_python_imports
    )
    _python_package_root_contract_violation = staticmethod(
        _python_package_root_contract_violation
    )
    _frontend_wrong_stack_materializations = staticmethod(
        _frontend_wrong_stack_materializations
    )
    _infer_stack_from_plan = staticmethod(_infer_stack_from_plan)
    _plan_contains_stack_conflict = staticmethod(_plan_contains_stack_conflict)
    _plan_static_site_off_root_mutations = staticmethod(
        _plan_static_site_off_root_mutations
    )
    _plan_writes_obvious_undefined_js_identifiers = staticmethod(
        _plan_writes_obvious_undefined_js_identifiers
    )
    _task_allows_multiple_stacks = staticmethod(_task_allows_multiple_stacks)
    _plan_command_budget_diagnostics = staticmethod(_plan_command_budget_diagnostics)
    _shadow_rule_warnings = staticmethod(_shadow_rule_warnings)
    _plan_contains_background_processes = staticmethod(
        _plan_contains_background_processes
    )
    _plan_contains_non_runnable_commands = staticmethod(
        _plan_contains_non_runnable_commands
    )
    _single_file_write_heredoc_targets = staticmethod(
        _single_file_write_heredoc_targets
    )
    _heredoc_target_is_unsafe = staticmethod(_heredoc_target_is_unsafe)
    _uses_looped_heredoc = staticmethod(_uses_looped_heredoc)
    _uses_brittle_python_inline_command = staticmethod(
        _uses_brittle_python_inline_command
    )
    _verification_is_weak = staticmethod(_verification_is_weak)
    _plan_missing_verification_steps = staticmethod(_plan_missing_verification_steps)
    _verification_plan_missing_workspace_files = staticmethod(
        _verification_plan_missing_workspace_files
    )
    _verification_plan_creates_new_source_assets = staticmethod(
        _verification_plan_creates_new_source_assets
    )
    _verification_plan_mutates_app_source_assets = staticmethod(
        _verification_plan_mutates_app_source_assets
    )
    _command_source_read_targets = staticmethod(_command_source_read_targets)

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
    def _snake_case_rule_id(value: Any) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[^a-z0-9]+", "_", text)
        return text.strip("_")

    @classmethod
    def _validator_rule_ids_from_details(
        cls,
        *,
        stage: str,
        details: Dict[str, Any],
    ) -> List[str]:
        """Return stable source-level validator rule IDs from detector metadata."""

        ids: List[str] = []

        def add(rule_id: Any) -> None:
            normalized = cls._snake_case_rule_id(rule_id)
            if normalized and normalized not in ids:
                ids.append(normalized)

        for code in details.get("semantic_violation_codes") or []:
            add(code)

        validation_evidence = details.get("validation_evidence")
        if isinstance(validation_evidence, dict):
            for code in validation_evidence.get("semantic_violation_codes") or []:
                add(code)

        if isinstance(details.get("plan_schema"), dict) and not details[
            "plan_schema"
        ].get("valid", True):
            add("plan_schema_invalid")

        detail_rule_ids = {
            "received_type": "reasoning_artifact_invalid_type",
            "read_only_stage_mutation_steps": "read_only_stage_mutation",
            "read_only_stage_failable_probe_steps": "read_only_stage_failable_probe",
            "invalid_ops_path_steps": "invalid_ops_path",
            "missing_replace_in_file_targets": "missing_replace_in_file_target",
            "empty_replace_old_text_steps": "empty_replace_old_text",
            "python_source_syntax_invalid": "python_source_syntax_invalid",
            "static_site_off_root_mutations": "static_site_off_root_mutation",
            "fake_verification_artifact_steps": "fake_verification_artifact",
            "expected_source_file_not_materialized": (
                "expected_source_file_not_materialized"
            ),
            "unmaterialized_expected_files": "unmaterialized_expected_files",
            "oversized_command_steps": "oversized_command_length",
            "brittle_command_subcodes": "brittle_command",
            "malformed_shell_quoting_steps": "malformed_shell_quoting",
            "missing_description_steps": "missing_description",
            "missing_commands_steps": "missing_runnable_commands",
            "unsafe_expected_files": "unsafe_expected_file_path",
            "unsafe_command_paths": "unsafe_command_path",
            "non_runnable_steps": "non_runnable_command",
            "background_process_steps": "background_process",
            "nested_workspace_steps": "nested_workspace",
            "nested_project_root_steps": "nested_project_root",
            "duplicated_root_paths": "duplicated_root_path",
            "task1_bootstrap_contract": "task1_bootstrap_contract",
            "negative_existing_file_checks": "negative_existing_file_check",
            "workflow_phase_violations": "workflow_phase_order_violation",
            "missing_workflow_phases": "workflow_phase_missing",
            "missing_materialization_for_implementation": (
                "missing_materialization_for_implementation"
            ),
            "python_package_root_contract": "python_package_root_contract",
            "missing_verification_steps": "missing_verification_command",
            "weak_verification_steps": "weak_verification",
            "placeholder_only_implementation": "placeholder_implementation",
            "frontend_wrong_stack_materializations": "frontend_wrong_stack",
            "undefined_js_identifier_materializations": "undefined_js_identifier",
            "undefined_python_test_name_materializations": (
                "undefined_python_test_name"
            ),
            "undefined_python_decorator_materializations": (
                "undefined_python_decorator"
            ),
            "import_time_parse_args_materializations": "import_time_parse_args",
            "unsafe_python_append_fragments": "unsafe_python_append_fragment",
            "physical_src_import_materializations": "physical_src_import",
            "verification_profile_mutated_source_assets": (
                "verification_mutates_source_assets"
            ),
            "missing_workspace_expected_files": "missing_workspace_expected_file",
            "verification_profile_created_source_assets": (
                "verification_creates_source_assets"
            ),
            "stack_conflict": "stack_conflict",
            "missing_expected_files": "missing_expected_files",
            "tool_failures": "tool_failures",
            "reported_changed_files": "reported_changed_files_not_materialized",
            "placeholder_reasons": "placeholder_content",
            "test_integrity_findings": "test_integrity_finding",
            "missing_task_expected_files": "baseline_missing_task_expected_files",
            "missing_prior_expected_files": "baseline_missing_prior_expected_files",
            "consistency_issues": "baseline_consistency_issue",
            "missing_core_files": "missing_core_files",
            "nested_expected_file_matches": "nested_expected_file_match",
            "workspace_consistency": "workspace_consistency",
            "symbol_verification": "requested_symbol_missing",
        }
        for detail_key, rule_id in detail_rule_ids.items():
            value = details.get(detail_key)
            if value:
                add(rule_id)

        if stage and ids:
            return ids
        return ids

    @classmethod
    def _with_validator_rule_ids(
        cls,
        *,
        stage: str,
        details: Dict[str, Any],
    ) -> Dict[str, Any]:
        rule_ids = cls._validator_rule_ids_from_details(stage=stage, details=details)
        if rule_ids:
            details = dict(details)
            details["validator_rule_ids"] = rule_ids
        return details

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
                details=cls._with_validator_rule_ids(
                    stage="reasoning_artifact",
                    details={"received_type": type(artifact).__name__},
                ),
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
            details=cls._with_validator_rule_ids(
                stage="reasoning_artifact",
                details=details,
            ),
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
    def has_explicit_repair_intent(
        task_prompt: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        combined = " ".join([task_prompt or "", title or "", description or ""])
        return bool(
            re.search(
                r"\b(?:repair|fix|debug|regression|bug|failing|broken)\b",
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

    @classmethod
    def _plan_declared_expected_files(cls, plan: List[Dict[str, Any]]) -> set[str]:
        files: set[str] = set()
        for step in plan:
            for raw_path in step.get("expected_files", []) or []:
                path = str(raw_path or "").strip().rstrip("/").lstrip("./")
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
                "fix",
                "add",
                "write",
                "modify",
                "implement",
                "generate",
                "scaffold",
                "update",
            )
        )

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
        is_first_ordered_task: bool = False,
    ) -> PlanOutcome:
        plan = copy.deepcopy(plan)
        profile = cls.infer_validation_profile(
            task_prompt, execution_profile, title=title, description=description
        )
        workflow_stage_was_provided = workflow_stage is not None
        if workflow_stage is None and execution_profile == "review_only":
            workflow_stage = "review"
        if workflow_stage in READ_ONLY_WORKFLOW_STAGES:
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
        failable_review_probes = cls._plan_failable_review_probe_steps(
            plan, workflow_stage
        )
        if failable_review_probes:
            repairable.append(
                "Review-only plans must not fail execution when an inspected pattern "
                "is absent; absence should be reported as a finding"
            )
            details["read_only_stage_failable_probe_steps"] = failable_review_probes

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

            empty_replace_old_text_steps = cls._plan_empty_replace_old_text_steps(plan)
            if empty_replace_old_text_steps:
                bad_steps = sorted(empty_replace_old_text_steps.keys())
                repairable.append(
                    "`replace_in_file` operations must provide exact non-empty "
                    "`old` text from the current file, or use `write_file` with "
                    "complete grounded file content "
                    f"(empty_replace_old_text_steps: {bad_steps[:5]})"
                )
                details["empty_replace_old_text_steps"] = empty_replace_old_text_steps

            python_source_syntax_issues = cls._plan_python_source_syntax_issues(
                plan,
                Path(project_dir),
            )
            if python_source_syntax_issues:
                files = [
                    str(issue.get("path") or "(missing path)")
                    for issue in python_source_syntax_issues
                ]
                first_issue = python_source_syntax_issues[0]
                location = ""
                if first_issue.get("line") is not None:
                    location = f" line {first_issue.get('line')}"
                    if first_issue.get("offset") is not None:
                        location += f", offset {first_issue.get('offset')}"
                repairable.append(
                    "Plan writes Python source with invalid syntax "
                    "(python_source_syntax_invalid; "
                    f"{files[0]}{location}: {first_issue.get('message')}; "
                    f"files: {files[:5]})"
                )
                details["python_source_syntax_invalid"] = python_source_syntax_issues[
                    :20
                ]

            static_site_off_root_mutations = cls._plan_static_site_off_root_mutations(
                plan,
                Path(project_dir),
                task_prompt,
            )
            if static_site_off_root_mutations:
                repairable.append(
                    "Existing static-site tasks must keep static file edits inside "
                    "the detected static-site root "
                    f"(files: {static_site_off_root_mutations[:5]})"
                )
                details["static_site_off_root_mutations"] = (
                    static_site_off_root_mutations[:20]
                )

        fake_verification_artifact_steps = cls._plan_fake_verification_artifact_steps(
            plan
        )
        if fake_verification_artifact_steps:
            repairable.append(
                "Plan uses invented test output artifacts for verification instead "
                "of relying on pytest/unittest exit codes "
                f"(steps: {fake_verification_artifact_steps[:5]})"
            )
            details["fake_verification_artifact_steps"] = (
                fake_verification_artifact_steps
            )

        declared_expected_files = cls._plan_declared_expected_files(plan)
        materialized_targets = cls._plan_materialized_file_targets(plan)
        existing_expected_files = {
            path
            for path in declared_expected_files
            if project_dir is not None and (Path(project_dir) / path).exists()
        }
        expected_source_file_not_materialized = (
            cls._expected_source_files_not_materialized(
                declared_expected_files=declared_expected_files,
                materialized_targets=materialized_targets,
                existing_expected_files=existing_expected_files,
            )
        )
        if (
            expected_source_file_not_materialized
            and workflow_stage not in READ_ONLY_WORKFLOW_STAGES
        ):
            repairable.append(
                "Plan declares expected source files that do not exist but are not "
                "materialized by file operations "
                "(expected_source_file_not_materialized; files: "
                f"{expected_source_file_not_materialized[:5]})"
            )
            details["expected_source_file_not_materialized"] = (
                expected_source_file_not_materialized[:20]
            )
        unmaterialized_expected_files = sorted(
            declared_expected_files.difference(
                materialized_targets | existing_expected_files
            )
        )
        if (
            declared_expected_files
            and unmaterialized_expected_files
            and workflow_stage not in READ_ONLY_WORKFLOW_STAGES
        ):
            repairable.append(
                "Plan declares expected files without materializing them through "
                "file operations or shell writes"
            )
            details["unmaterialized_expected_files"] = unmaterialized_expected_files[
                :20
            ]

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

        task1_bootstrap_contract = None
        task1_forbidden_path_drift: List[str] = []
        for issue_group in (
            unsafe_paths,
            nested_workspace_steps,
            nested_project_root_steps,
            list(duplicated_root_paths.keys()) if duplicated_root_paths else [],
        ):
            task1_forbidden_path_drift.extend(str(item) for item in issue_group)
        stage_allows_materialization = workflow_stage not in READ_ONLY_WORKFLOW_STAGES
        if (
            is_first_ordered_task
            and profile == "implementation"
            and stage_allows_materialization
        ):
            task1_bootstrap_contract = validate_task1_bootstrap_contract(
                plan=plan,
                task_prompt=" ".join(
                    str(value or "") for value in (title, description, task_prompt)
                ),
                forbidden_path_drift=task1_forbidden_path_drift,
                existing_files={
                    str(path.relative_to(project_dir))
                    for path in project_dir.rglob("*")
                    if path.is_file()
                },
            )
            details["task1_bootstrap_contract"] = task1_bootstrap_contract.to_dict()
            if not task1_bootstrap_contract.passed:
                repairable.append(
                    "Task 1 bootstrap planning contract failed: "
                    + "; ".join(task1_bootstrap_contract.violations[:4])
                )

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

        if profile == "implementation":
            if (
                cls._task_prompt_requires_materialization(
                    task_prompt, title=title, description=description
                )
                and stage_allows_materialization
            ):
                if not materialized_targets:
                    repairable.append(
                        "Implementation task plan does not materialize any source changes"
                    )
                    details["missing_materialization_for_implementation"] = True

            package_root_violation = cls._python_package_root_contract_violation(
                plan,
                project_dir=project_dir,
                task_prompt=task_prompt,
                title=title,
                description=description,
            )
            if package_root_violation:
                repairable.append(
                    "Python implementation plan changes package roots instead of "
                    "editing the existing package imported by tests"
                )
                details["python_package_root_contract"] = package_root_violation

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
                placeholder_source_ops = cls._plan_placeholder_source_write_ops(
                    plan, task_prompt
                )
                if placeholder_source_ops:
                    details["placeholder_source_write_ops"] = placeholder_source_ops[:5]
            frontend_wrong_stack_files = cls._frontend_wrong_stack_materializations(
                plan,
                workflow_profile,
            )
            if frontend_wrong_stack_files:
                repairable.append(
                    "Frontend-only plan materializes non-frontend or extensionless source files "
                    f"(files: {frontend_wrong_stack_files[:5]})"
                )
                details["frontend_wrong_stack_materializations"] = (
                    frontend_wrong_stack_files[:20]
                )
            undefined_js_identifier_files = (
                cls._plan_writes_obvious_undefined_js_identifiers(plan)
            )
            if undefined_js_identifier_files:
                repairable.append(
                    "Plan writes JavaScript/TypeScript functions with obvious "
                    "undefined return identifiers "
                    f"(files: {undefined_js_identifier_files[:5]})"
                )
                details["undefined_js_identifier_materializations"] = (
                    undefined_js_identifier_files[:20]
                )
            undefined_python_test_name_files = (
                cls._plan_writes_obvious_undefined_python_test_names(plan, project_dir)
            )
            if undefined_python_test_name_files:
                repairable.append(
                    "Plan writes Python tests with obvious undefined names "
                    f"(files: {undefined_python_test_name_files[:5]})"
                )
                details["undefined_python_test_name_materializations"] = (
                    undefined_python_test_name_files[:20]
                )
            undefined_python_decorator_files = (
                cls._plan_writes_obvious_undefined_python_decorators(plan, project_dir)
            )
            if undefined_python_decorator_files:
                repairable.append(
                    "Plan writes Python decorators whose root name is undefined "
                    f"(files: {undefined_python_decorator_files[:5]})"
                )
                details["undefined_python_decorator_materializations"] = (
                    undefined_python_decorator_files[:20]
                )
            import_time_parse_args_files = (
                cls._plan_writes_import_time_python_parse_args(plan, project_dir)
            )
            if import_time_parse_args_files:
                repairable.append(
                    "Plan writes Python CLI argument parsing that runs at import time "
                    f"(files: {import_time_parse_args_files[:5]})"
                )
                details["import_time_parse_args_materializations"] = (
                    import_time_parse_args_files[:20]
                )
            unsafe_python_append_files = cls._plan_appends_contextual_python_fragments(
                plan
            )
            if unsafe_python_append_files:
                repairable.append(
                    "Plan uses append_file to add contextual Python control-flow "
                    "fragments that only make sense inside an existing block; use "
                    "context-aware replace_in_file or write_file with complete "
                    "valid file content instead "
                    f"(files: {unsafe_python_append_files[:5]})"
                )
                details["unsafe_python_append_fragments"] = unsafe_python_append_files[
                    :20
                ]
            physical_src_import_files = cls._plan_writes_physical_src_python_imports(
                plan, project_dir
            )
            if physical_src_import_files:
                repairable.append(
                    "Plan writes Python imports using the physical `src.` prefix in "
                    "a src-layout project; use the package import, not the physical "
                    f"src prefix (files: {physical_src_import_files[:5]})"
                )
                details["physical_src_import_materializations"] = (
                    physical_src_import_files[:20]
                )
                details["physical_src_import_details"] = (
                    cls._plan_physical_src_python_import_details(plan, project_dir)[:10]
                )
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
                plan,
                project_dir,
                include_expected_files=(
                    workflow_stage not in READ_ONLY_WORKFLOW_STAGES
                    or not workflow_stage_was_provided
                ),
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
        if details.get("fake_verification_artifact_steps"):
            semantic_violation_codes.append("fake_verification_artifact")
        if details.get("unmaterialized_expected_files"):
            semantic_violation_codes.append("unmaterialized_expected_files")
        if details.get("expected_source_file_not_materialized"):
            semantic_violation_codes.append("expected_source_file_not_materialized")
        if details.get("physical_src_import_materializations"):
            semantic_violation_codes.append("physical_src_import")
        if details.get("empty_replace_old_text_steps"):
            semantic_violation_codes.append("empty_replace_old_text")
        if details.get("unsafe_python_append_fragments"):
            semantic_violation_codes.append("unsafe_python_append_fragment")
        if details.get("python_source_syntax_invalid"):
            semantic_violation_codes.append("python_source_syntax_invalid")
        if task1_bootstrap_contract and task1_bootstrap_contract.violation_codes:
            semantic_violation_codes.extend(task1_bootstrap_contract.violation_codes)
        if semantic_violation_codes:
            details["semantic_violation_codes"] = list(
                dict.fromkeys(semantic_violation_codes)
            )

        details = cls._with_validator_rule_ids(stage="plan", details=details)
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

        integrity_findings = scan_test_file_changes(materialized_files, project_dir)
        if integrity_findings:
            serialized_findings = [
                finding.to_dict() for finding in integrity_findings[:20]
            ]
            details["test_integrity_findings"] = serialized_findings
            for finding in integrity_findings:
                message = finding.message
                if finding.path:
                    message = f"{message} ({finding.path})"
                if finding.severity == "error":
                    repairable.append(message)
                else:
                    warnings.append(message)

        details = cls._with_validator_rule_ids(
            stage="step_completion",
            details=details | {"step_output_preview": step_output[:240]},
        )
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
            details=details,
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
        is_first_ordered_task: bool = False,
    ) -> ValidationVerdict:
        profile = cls.infer_validation_profile(
            task_prompt, execution_profile, title=title, description=description
        )
        if workflow_stage in READ_ONLY_WORKFLOW_STAGES:
            profile = "verification"
        bootstrap_contract = build_task1_bootstrap_contract(
            plan=plan,
            task_prompt=" ".join(
                str(value or "") for value in (title, description, task_prompt)
            ),
            existing_files={
                str(path.relative_to(project_dir))
                for path in project_dir.rglob("*")
                if path.is_file()
            },
        )
        bootstrap_task_type = bootstrap_contract.bootstrap_task_type
        artifact_only_completion = (
            bootstrap_task_type == BootstrapTaskType.ARTIFACT_ONLY
            and profile in {"implementation", "integration"}
        )
        expected_core_files = list(
            dict.fromkeys(
                cls._core_expected_files(plan)
                + cls._source_path_mentions(title, description, task_prompt)
            )
        )
        expected_core_files = cls._resolve_existing_static_site_mentions(
            project_dir,
            expected_core_files,
            title,
            description,
            task_prompt,
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
            "bootstrap_task_type": str(bootstrap_task_type),
            "artifact_only_completion": artifact_only_completion,
        }
        details["completion_contract"] = contract
        details["bootstrap_task_classification"] = {
            "bootstrap_task_type": str(bootstrap_task_type),
            "classification_evidence": dict(bootstrap_contract.classification_evidence),
            "required_artifacts": list(bootstrap_contract.required_artifacts),
            "required_source_files": list(bootstrap_contract.required_source_files),
            "minimum_artifact_evidence": bootstrap_contract.minimum_artifact_evidence,
            "minimum_implementation_evidence": (
                bootstrap_contract.minimum_implementation_evidence
            ),
        }
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
        repair_keyword_match = cls.repair_requires_independent_evidence(
            task_prompt, title=title, description=description
        )
        explicit_repair_intent = cls.has_explicit_repair_intent(
            "", title=title, description=description
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
        added_files = {
            str(path).replace("\\", "/").lstrip("./")
            for path in ((change_set or {}).get("added_files") or [])
        }
        required_bootstrap_files = set(bootstrap_contract.required_source_files) | set(
            bootstrap_contract.required_test_files
        )
        fresh_bootstrap_generated_test_evidence = bool(
            repair_keyword_match
            and not explicit_repair_intent
            and is_first_ordered_task
            and bootstrap_task_type
            in {BootstrapTaskType.SOURCE_CODE, BootstrapTaskType.MIXED}
            and bootstrap_contract.minimum_implementation_evidence
            and bootstrap_contract.required_source_files
            and bootstrap_contract.required_test_files
            and not pre_existing_tests
            and required_bootstrap_files.issubset(added_files)
            and best_command_quality == "regression_test"
        )
        requires_independent_evidence = bool(
            repair_keyword_match and not fresh_bootstrap_generated_test_evidence
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
            "repair_keyword_match": repair_keyword_match,
            "explicit_repair_intent": explicit_repair_intent,
            "is_first_ordered_task": is_first_ordered_task,
            "fresh_bootstrap_generated_test_evidence": (
                fresh_bootstrap_generated_test_evidence
            ),
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
        if (
            artifact_only_completion
            and not bootstrap_contract.minimum_artifact_evidence
        ):
            rejected.append("Artifact completion lacks substantive artifact evidence")
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

        if reported_changed_files:
            materialized_reported_files = [
                cls._normalize_reported_changed_file(path_text)
                for path_text in reported_changed_files
                if (project_dir / cls._normalize_reported_changed_file(path_text))
                .resolve()
                .is_file()
            ]
            details["materialized_reported_files"] = materialized_reported_files[:20]
        else:
            materialized_reported_files = []

        if (
            reported_changed_files
            and candidate_files
            and not materialized_reported_files
        ):
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
            and not artifact_only_completion
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
                and not artifact_only_completion
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

        # 10K-c: Requested symbol completion verification (non-fatal if check crashes)
        try:
            from app.services.orchestration.validation.completion_symbol_check import (
                check_completion_symbol_presence,
            )

            _full_task_text = " ".join(
                str(v or "") for v in (task_prompt, title, description)
            )
            symbol_check = check_completion_symbol_presence(
                task_description=_full_task_text,
                reported_changed_files=reported_changed_files,
                project_dir=project_dir,
                execution_profile=execution_profile,
            )
            details["symbol_verification"] = symbol_check
            if symbol_check["applicable"] and not symbol_check["passed"]:
                rejected.append(
                    "requested_symbol_missing_from_workspace: "
                    + ", ".join(symbol_check["missing"][:8])
                )
        except Exception:
            pass

        failure_signature = cls.build_failure_signature(
            rejected + repairable + warnings
        )
        if failure_signature:
            details["failure_signature"] = failure_signature

        details = cls._with_validator_rule_ids(
            stage="task_completion",
            details=details,
        )
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

        details = ValidatorService._with_validator_rule_ids(
            stage="baseline_publish",
            details=details,
        )
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
