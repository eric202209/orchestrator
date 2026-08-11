"""Rule-first orchestration validation helpers."""

from __future__ import annotations

import copy
import re
import shlex
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional
from ..policy import apply_validation_policy
from ..types import (
    CandidateFinding,
    PlanAccepted,
    PlanOutcome,
    PlanRejected,
    PlanRepairRequired,
    ValidationVerdict,
)

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
)
from .accepted_path_authority import (
    ACCEPTED_PLAN_STATUSES,
    build_accepted_path_authority,
)
from .path_authority import PathAuthorityError
from .candidate_checks import (
    candidate_authorized_paths,
    candidate_delta_identity,
    validate_candidate_delta,
)
from .integrity import (
    check_test_preservation,
    classify_verification_command,
    pre_existing_python_test_files,
    pre_existing_source_files,
    scan_test_file_changes,
)
from app.services.orchestration.planning.task_bootstrap_contract import (
    BootstrapTaskType,
    build_task1_bootstrap_contract,
    validate_task1_bootstrap_contract,
)
from app.services.orchestration.planning.planner_contract_registry import (
    planner_contract_source_paths,
    planner_contract_test_paths,
)
from app.services.orchestration.planning.repair_faithfulness import (
    extract_required_file_paths,
)
from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_EXISTING,
    SOURCE_STATUS_NEW,
    materialize_planner_source_context,
    materialized_source_file,
)
from app.services.orchestration.planning.source_operation_verification import (
    FAILURE_STALE_OLD_TEXT,
    ResolvedSource,
    resolve_version_fenced_source,
    verify_replace_operation,
)
from app.services.orchestration.planning.workspace_identity import (
    PlannerWorkspaceIdentity,
)
from app.services.workspace.workspace_paths import is_hydration_excluded_path
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
    _plan_nested_project_root_evidence,
    _plan_nested_project_root_names,
    _plan_nested_workspace_aliases,
    _plan_nested_workspace_corrected_fragments,
    _plan_nested_workspace_offending_fragments,
    _plan_nests_task_workspace,
    _resolve_existing_static_site_mentions,
    _source_path_mentions,
    _strip_heredoc_bodies_for_command_scanning,
)

MAX_INITIAL_PLAN_STEPS = 4


def is_orchestration_internal_path(relative_path: str) -> bool:
    """True when a canonical-relative path is an Orchestrator-owned artifact.

    Phase 32J-1R3: change-set capture writes `.agent/change-sets/<id>/…` into
    the canonical root *before* publication preflight runs, so a recursive
    regular-file walk counted internal metadata as product baseline content and
    an empty (or emptied) product baseline passed preflight.  Ownership is
    decided by the same `HYDRATION_EXCLUDED_NAMES` authority that change-set
    capture (`ChangeSetService._path_is_safe_relative`) and canonical baseline
    counting (`count_baseline_files`) already use, so preflight and
    post-promotion validation agree on what a product file is.  This is
    ownership-based, not a hidden-file or ignore rule: `.github/**`,
    `.flake8`, `.env.example` and other legitimate repository dotfiles are not
    orchestration-owned and still count.
    """

    return is_hydration_excluded_path(Path(relative_path))


def _product_baseline_paths(paths: set[str]) -> set[str]:
    return {path for path in paths if not is_orchestration_internal_path(path)}


def _trusted_baseline_inventory(baseline_dir: Path) -> tuple[set[str], set[str]]:
    """Enumerate a pre-existing canonical baseline without resolving targets.

    Phase 33C-1: a baseline entry is *trusted filesystem observation*, not a
    candidate-declared path, so it must not be pushed through the
    filesystem-resolving `normalize_path_reference` primitive.  Doing so made an
    ordinary hydrated toolchain symlink (`venv/bin/python3 ->
    /usr/bin/python3.12`) look like an escaping candidate path and aborted
    publication preflight with `TaskWorkspaceViolationError` before the
    orchestration-internal filter ever ran (Product Attempt 16).

    Ownership is therefore classified *first*, by the same
    `is_orchestration_internal_path` / `HYDRATION_EXCLUDED_NAMES` authority the
    rest of publication already uses, and the entry type is read with `lstat`
    semantics so a symlink is observed as a symlink rather than followed.  No
    allowlist is involved: `venv/`, `node_modules/` and `.agent/` are excluded
    because they are not product content, not because of what they point at.

    Returns `(product_paths, excluded_paths)`; both are canonical relative POSIX
    strings and neither has had any target resolved.
    """

    product_paths: set[str] = set()
    excluded_paths: set[str] = set()
    for path in baseline_dir.rglob("*"):
        relative = path.relative_to(baseline_dir).as_posix()
        excluded = is_orchestration_internal_path(relative)
        try:
            mode = path.lstat().st_mode
        except OSError:
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            continue
        if excluded:
            excluded_paths.add(relative)
        else:
            product_paths.add(relative)
    return product_paths, excluded_paths


def _declared_candidate_relative_path(path_text: str) -> str:
    """Validate a candidate-declared product path lexically, without the filesystem.

    Phase 33C-1: candidate/Change Set path strings are untrusted *declarations*.
    Deciding whether a declaration is acceptable is a syntax question, so it must
    not depend on `.resolve()`, on symlink following, or on the path existing.
    The unsafe declaration classes previously rejected by
    `normalize_path_reference` in this flow (empty, `~`, absolute, `..`
    traversal) stay rejected, and drive-letter declarations — which the resolving
    primitive silently accepted as relative filenames on POSIX — are now rejected
    too.  Orchestration-owned and hydration-excluded roots continue to be filtered
    by `_product_baseline_paths`, the existing ownership authority.

    Phase 33C-2 supersedes this helper with the shared `declare()` primitive in
    `validation/path_authority.py`; it is deliberately private and local to
    publication until then.
    """

    raw = (path_text or "").strip().strip("\"'")
    if not raw:
        raise TaskWorkspaceViolationError("Empty path reference is not allowed")
    if raw.startswith("~"):
        raise TaskWorkspaceViolationError(
            f"Home-directory path is not allowed in task workspace: {raw}"
        )
    if raw.startswith("/") or re.match(r"^[A-Za-z]:([\\/]|$)", raw):
        raise TaskWorkspaceViolationError(
            f"Absolute path is not allowed in task workspace: {raw}"
        )
    segments = [part for part in PurePosixPath(raw).parts if part != "."]
    if any(part == ".." for part in segments):
        raise TaskWorkspaceViolationError(f"Path escapes task workspace: {raw}")
    if not segments:
        raise TaskWorkspaceViolationError("Empty path reference is not allowed")
    return PurePosixPath(*segments).as_posix()


def _declared_candidate_paths(paths: Any) -> set[str]:
    return _product_baseline_paths(
        {_declared_candidate_relative_path(str(path)) for path in paths or []}
    )


MIXED_LANGUAGE_WORKSPACE_ISSUE = (
    "Workspace contains both Python and Node/JS implementation artifacts"
)


def _mixed_language_attribution(
    *,
    canonical_paths: set[str],
    added_paths: set[str],
    modified_paths: set[str],
    projected_paths: set[str],
) -> Dict[str, Any]:
    """Attribute a mixed-stack workspace to the baseline or to the candidate.

    Pre-existing mixed state that the candidate neither introduces nor worsens
    is baseline debt, not a candidate defect.  Deleted paths only ever shrink
    the projected set; they never contribute a candidate stack.
    """

    # Deferred: `phases` imports the execution flow, which imports this module.
    from app.services.orchestration.phases.completion_workspace import (
        _stack_set_for_paths,
    )

    baseline_stacks = _stack_set_for_paths(sorted(canonical_paths))
    candidate_stacks = _stack_set_for_paths(sorted(added_paths | modified_paths))
    projected_stacks = _stack_set_for_paths(sorted(projected_paths))
    baseline_present = len(baseline_stacks) > 1
    projected_present = len(projected_stacks) > 1
    candidate_introduced = projected_present and not baseline_present
    candidate_worsened = projected_present and len(candidate_stacks) > 1
    if not projected_present:
        severity = "resolved"
    elif candidate_introduced or candidate_worsened:
        severity = "repair_required"
    else:
        severity = "warning"
    return {
        "baseline_present": baseline_present,
        "projected_present": projected_present,
        "candidate_introduced": candidate_introduced,
        "candidate_worsened": candidate_worsened,
        "candidate_improved": baseline_present and not projected_present,
        "authority": "baseline_publish_candidate_projection",
        "severity": severity,
        "baseline_stacks": sorted(baseline_stacks),
        "candidate_stacks": sorted(candidate_stacks),
        "projected_stacks": sorted(projected_stacks),
    }


def _expected_file_attribution(
    *,
    expected_files: List[Dict[str, Any]],
    canonical_paths: set[str],
    projected_paths: set[str],
    added_paths: set[str],
    modified_paths: set[str],
    deleted_paths: set[str],
    authority: str,
) -> Dict[str, Any]:
    """Project expected-file obligations without assigning severity to callers."""

    obligations: Dict[str, List[Dict[str, Any]]] = {}
    for expected_file in expected_files:
        path = expected_file.get("path")
        if not path:
            continue
        obligations.setdefault(path, []).append(expected_file)

    paths = []
    for path in sorted(obligations):
        baseline_present = path in canonical_paths
        projected_present = path in projected_paths
        paths.append(
            {
                "path": path,
                "baseline_present": baseline_present,
                "projected_present": projected_present,
                "candidate_added": path in added_paths,
                "candidate_modified": path in modified_paths,
                "candidate_deleted": path in deleted_paths,
                "candidate_introduced": not baseline_present and projected_present,
                "candidate_worsened": baseline_present and not projected_present,
                "candidate_improved": not baseline_present and projected_present,
                "candidate_owned_obligation": baseline_present
                and not projected_present,
                "owners": obligations[path],
            }
        )

    missing_paths = [path for path in paths if not path["projected_present"]]
    worsened_paths = [path for path in missing_paths if path["candidate_worsened"]]
    improved_paths = [path for path in paths if path["candidate_improved"]]
    return {
        "baseline_present": all(path["baseline_present"] for path in paths),
        "projected_present": all(path["projected_present"] for path in paths),
        "candidate_introduced": any(path["candidate_introduced"] for path in paths),
        "candidate_worsened": bool(worsened_paths),
        "candidate_improved": bool(improved_paths),
        "candidate_owned_obligation": bool(worsened_paths),
        "authority": authority,
        "paths": paths,
        "owners": [owner for path in paths for owner in path["owners"]],
        "missing_paths": [path["path"] for path in missing_paths],
    }


def _plan_target_paths(plan: List[Dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for step in plan or []:
        for value in step.get("expected_files") or []:
            normalized = str(value or "").strip().replace("\\", "/").lstrip("./")
            if normalized and normalized not in seen:
                seen.add(normalized)
                paths.append(normalized)
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            normalized = (
                str(operation.get("path") or "").strip().replace("\\", "/").lstrip("./")
            )
            if normalized and normalized not in seen:
                seen.add(normalized)
                paths.append(normalized)
    return paths


def _explicit_task_scope_paths(*values: Any) -> set[str]:
    text = "\n".join(str(value or "") for value in values)
    scope_paths: set[str] = set()
    for match in re.finditer(
        r"\b(?:only|hard\s+scope\s*:)\b(?P<body>.{0,320})",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        body = re.split(
            r"\b(?:may\s+change|may\s+modify|do\s+not)\b",
            match.group("body"),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        scope_paths.update(extract_required_file_paths(body))
    return {path.replace("\\", "/").lstrip("./") for path in scope_paths}


def _existing_write_authorized(
    task_text: str, relative_path: str, operation_description: str = ""
) -> bool:
    authorization_words = (
        r"\b(preserve|replace|rewrite|overwrite|rebuild|entire\s+file|full\s+file)\b"
    )
    if re.search(authorization_words, str(operation_description or ""), re.IGNORECASE):
        return True
    lowered = "\n".join(
        [str(task_text or ""), str(operation_description or "")]
    ).lower()
    target = relative_path.lower()
    start = 0
    while True:
        index = lowered.find(target, start)
        if index < 0:
            return False
        window = lowered[max(0, index - 180) : index + len(target) + 180]
        if re.search(authorization_words, window):
            return True
        start = index + len(target)


def _plan_creation_authorized_paths(
    plan: List[Dict[str, Any]],
) -> set[str]:
    paths: set[str] = set()
    target_paths = set(_plan_target_paths(plan))
    for step in plan or []:
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "").strip() in {
                "write_file",
                "create_file",
                "append_file",
            }:
                path = (
                    str(operation.get("path") or "")
                    .strip()
                    .replace("\\", "/")
                    .lstrip("./")
                )
                if path:
                    paths.add(path)
        for command in step.get("commands") or []:
            rendered = str(command or "")
            if ">" not in rendered and not re.search(r"\b(?:touch|tee)\b", rendered):
                continue
            paths.update(path for path in target_paths if path in rendered)
            paths.update(
                path
                for path in extract_required_file_paths(rendered)
                if path in target_paths
            )
    return paths


def _source_operation_contract_issues(
    plan: List[Dict[str, Any]],
    *,
    task_text: str,
    project_dir: Path,
    source_materialization: Any,
) -> Dict[str, Any]:
    details: Dict[str, Any] = {
        "stale_replace_materialization": [],
        "missing_source_materialization": [],
        "existing_file_write_without_authorization": [],
        "new_file_write_without_creation_authorization": [],
        "source_materialization_unavailable": [],
        "source_operation_verdicts": [],
    }
    unavailable = list(getattr(source_materialization, "unavailable_reasons", ()) or ())
    if unavailable:
        details["source_materialization_unavailable"] = unavailable[:20]

    # The in-plan buffer holds the complete current file whenever the captured
    # version identity still holds, so a byte-perfect edit outside the visible
    # span is verifiable without widening the model-visible prompt.
    current_content: dict[str, str] = {}
    resolved_sources: dict[str, ResolvedSource] = {}
    for item in getattr(source_materialization, "files", ()) or ():
        if getattr(item, "status", None) != SOURCE_STATUS_EXISTING:
            continue
        relative = str(getattr(item, "relative_path", ""))
        resolved = resolve_version_fenced_source(
            source_materialization, relative, project_dir
        )
        resolved_sources[relative] = resolved
        if resolved.failure_code is None and resolved.full_content is not None:
            current_content[relative] = resolved.full_content

    for index, step in enumerate(plan or [], start=1):
        for operation_index, operation in enumerate(step.get("ops") or [], start=1):
            if not isinstance(operation, dict):
                continue
            op_name = str(operation.get("op") or "").strip()
            if op_name not in {"write_file", "append_file", "replace_in_file"}:
                continue
            relative_path = (
                str(operation.get("path") or "").strip().replace("\\", "/").lstrip("./")
            )
            if not relative_path:
                continue
            record = materialized_source_file(source_materialization, relative_path)
            label = f"step {index} op {operation_index} ({relative_path})"
            if op_name == "write_file":
                if record is None:
                    content = operation.get("content")
                    expected_in_step = relative_path in {
                        str(value or "").strip().replace("\\", "/").lstrip("./")
                        for value in step.get("expected_files") or []
                    }
                    if (
                        expected_in_step
                        and isinstance(content, str)
                        and not (project_dir / relative_path).exists()
                    ):
                        current_content[relative_path] = content
                        continue
                    if (
                        (project_dir / relative_path).is_file()
                        and isinstance(content, str)
                        and _existing_write_authorized(
                            task_text,
                            relative_path,
                            step.get("description", ""),
                        )
                    ):
                        current_content[relative_path] = content
                        continue
                    details["new_file_write_without_creation_authorization"].append(
                        label
                    )
                    continue
                if record.status == SOURCE_STATUS_EXISTING:
                    if not _existing_write_authorized(
                        task_text,
                        relative_path,
                        step.get("description", ""),
                    ):
                        details["existing_file_write_without_authorization"].append(
                            label
                        )
                        continue
                elif record.status != SOURCE_STATUS_NEW:
                    details["new_file_write_without_creation_authorization"].append(
                        label
                    )
                    continue
                content = operation.get("content")
                if isinstance(content, str):
                    current_content[relative_path] = content
                continue

            if op_name == "append_file":
                content = operation.get("content")
                if relative_path not in current_content:
                    if record is None or record.status not in {
                        SOURCE_STATUS_EXISTING,
                        SOURCE_STATUS_NEW,
                    }:
                        details["missing_source_materialization"].append(label)
                        continue
                    if record.status == SOURCE_STATUS_NEW:
                        current_content[relative_path] = ""
                if isinstance(content, str):
                    current_content[relative_path] = (
                        current_content.get(relative_path, "") + content
                    )
                continue

            old_text = operation.get("old")
            if old_text is None:
                old_text = operation.get("old_text")
            if not isinstance(old_text, str) or not old_text:
                continue
            content = current_content.get(relative_path)
            verdict = verify_replace_operation(
                resolved_sources.get(relative_path),
                old_text,
                relative_path=relative_path,
                simulated_content=content,
                step_index=index,
                operation_index=operation_index,
            )
            if len(details["source_operation_verdicts"]) < 20:
                details["source_operation_verdicts"].append(verdict.to_dict())
            if verdict.failure_code == FAILURE_STALE_OLD_TEXT:
                details["stale_replace_materialization"].append(label)
            elif verdict.failure_code is not None:
                details["missing_source_materialization"].append(label)
            elif content is not None:
                new_text = operation.get("new")
                if isinstance(new_text, str):
                    current_content[relative_path] = content.replace(
                        old_text, new_text, 1
                    )
    return details


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
    _plan_nested_workspace_offending_fragments = staticmethod(
        _plan_nested_workspace_offending_fragments
    )
    _plan_nested_workspace_aliases = staticmethod(_plan_nested_workspace_aliases)
    _plan_nested_workspace_corrected_fragments = staticmethod(
        _plan_nested_workspace_corrected_fragments
    )
    _plan_creates_nested_project_root = staticmethod(_plan_creates_nested_project_root)
    _plan_nested_project_root_names = staticmethod(_plan_nested_project_root_names)
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
    def _placeholder_delta_baseline(
        cls,
        candidate: Path,
        project_dir: Path,
        change_set: Optional[Dict[str, Any]],
    ) -> tuple[Optional[str], bool]:
        """Pre-candidate content of ``candidate`` plus whether the delta is known.

        Returns ``(baseline_text, delta_available)``.

        ``delta_available`` is ``True`` when the candidate delta for this file
        is established: the file is candidate-added or absent from the
        pre-candidate snapshot (baseline ``""`` — every line is candidate
        authored), or a readable snapshot copy exists. It is ``False`` when no
        change set, no snapshot root, or no readable baseline copy is
        available. Callers still scan the whole file in that case, but must not
        attribute what they find to the candidate.
        """

        if not change_set:
            return None, False
        relative_text = str(candidate.relative_to(project_dir)).replace("\\", "/")
        added_files = {
            str(path).replace("\\", "/").lstrip("./")
            for path in (change_set.get("added_files") or [])
        }
        if relative_text.lstrip("./") in added_files:
            return "", True
        snapshot_raw = str(change_set.get("snapshot_path") or "").strip()
        if not snapshot_raw:
            return None, False
        snapshot_root = Path(snapshot_raw)
        if not snapshot_root.exists():
            return None, False
        baseline_path = (snapshot_root / relative_text).resolve()
        try:
            if not baseline_path.is_relative_to(snapshot_root.resolve()):
                return None, False
        except ValueError:
            return None, False
        if not baseline_path.is_file():
            return "", True
        try:
            return baseline_path.read_text(encoding="utf-8"), True
        except Exception:
            return None, False

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
    def _planner_contract_unexpected_materialization_paths(
        plan: List[Dict[str, Any]],
        planner_contract: Mapping[str, Any] | None,
    ) -> list[str]:
        """Return materialized paths outside an explicit registered inventory."""

        if not isinstance(planner_contract, Mapping):
            return []
        allowed_paths = set(planner_contract_source_paths(planner_contract)) | set(
            planner_contract_test_paths(planner_contract)
        )
        if not allowed_paths:
            return []
        materialized_paths = _plan_materialized_file_targets(plan)
        return sorted(materialized_paths.difference(allowed_paths))

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
        workspace_identity: PlannerWorkspaceIdentity | None = None,
        planner_contract: Mapping[str, Any] | None = None,
        source_materialization: Any = None,
    ) -> PlanOutcome:
        # The Accepted Path Authority binds the plan the caller holds, which is
        # also what ``_completion_plan_identity`` hashes downstream.  The local
        # deep copy below may be reordered by the step-order correction, and
        # that correction deliberately never escapes this function.
        accepted_plan = plan
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
        if source_materialization is None and project_dir is not None:
            source_materialization = materialize_planner_source_context(
                Path(project_dir),
                task_description=task_prompt,
                expected_paths=_plan_target_paths(plan),
                creation_authorized_paths=_plan_creation_authorized_paths(plan),
            )
        if source_materialization is not None:
            details["source_materialization"] = (
                source_materialization.to_metadata()
                if hasattr(source_materialization, "to_metadata")
                else {}
            )
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
        unexpected_contract_paths = (
            cls._planner_contract_unexpected_materialization_paths(
                plan, planner_contract
            )
        )
        if unexpected_contract_paths:
            repairable.append(
                "Plan materializes paths outside the registered planner contract "
                f"inventory (paths: {unexpected_contract_paths[:8]})"
            )
            details["unexpected_registered_contract_paths"] = unexpected_contract_paths[
                :20
            ]
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

        if project_dir is not None and source_materialization is not None:
            source_contract_issues = _source_operation_contract_issues(
                plan,
                task_text="\n".join(
                    [str(task_prompt or ""), str(title or ""), str(description or "")]
                ),
                project_dir=Path(project_dir),
                source_materialization=source_materialization,
            )
            if (
                source_contract_issues["source_materialization_unavailable"]
                and workflow_stage not in READ_ONLY_WORKFLOW_STAGES
            ):
                repairable.append(
                    "planning_source_materialization_unavailable: expected source "
                    "could not be grounded within the bounded source contract"
                )
                details["planning_source_materialization_unavailable"] = (
                    source_contract_issues["source_materialization_unavailable"]
                )
            if source_contract_issues["stale_replace_materialization"]:
                repairable.append(
                    "stale_replace: replace_in_file.old_text is absent from the "
                    "materialized current source/version"
                )
                details["stale_replace_materialization"] = source_contract_issues[
                    "stale_replace_materialization"
                ]
            structured_operation_findings = [
                {
                    "step_number": verdict.get("step_index"),
                    "operation_index": verdict.get("operation_index"),
                    "relative_path": verdict.get("path"),
                    "failure_code": verdict.get("failure_code"),
                    "visibility": verdict.get("visibility"),
                    "visible_text_verified": verdict.get(
                        "present_in_visible_span", False
                    ),
                    "full_file_same_version_verified": verdict.get(
                        "present_in_full_file_same_version", False
                    ),
                    "source_version_identity": verdict.get("recorded_version_identity"),
                }
                for verdict in source_contract_issues["source_operation_verdicts"]
                if verdict.get("failure_code")
            ]
            if structured_operation_findings:
                details["source_operation_findings"] = structured_operation_findings
            if source_contract_issues["missing_source_materialization"]:
                repairable.append(
                    "missing_source_materialization: exact file content was not "
                    "materialized for a source-dependent operation"
                )
                details["missing_source_materialization"] = source_contract_issues[
                    "missing_source_materialization"
                ]
            if source_contract_issues["existing_file_write_without_authorization"]:
                repairable.append(
                    "existing_file_write_requires_explicit_replace_authorization"
                )
                details["existing_file_write_without_authorization"] = (
                    source_contract_issues["existing_file_write_without_authorization"]
                )
            if source_contract_issues["new_file_write_without_creation_authorization"]:
                repairable.append(
                    "new_file_creation_not_authorized: write_file may create only "
                    "a classified new expected file"
                )
                details["new_file_write_without_creation_authorization"] = (
                    source_contract_issues[
                        "new_file_write_without_creation_authorization"
                    ]
                )

        scope_paths = _explicit_task_scope_paths(task_prompt, title, description)
        if scope_paths:
            out_of_scope = sorted(
                path for path in _plan_target_paths(plan) if path not in scope_paths
            )
            if out_of_scope:
                repairable.append(
                    "task scope violation: plan targets files outside the explicit "
                    f"scope ({out_of_scope[:8]})"
                )
                details["task_scope_violation_paths"] = out_of_scope[:20]

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

        nested_workspace_steps = cls._plan_nests_task_workspace(
            plan, project_dir, workspace_identity
        )
        if nested_workspace_steps:
            repairable.append(
                "Plan incorrectly recreates the current task workspace as a nested folder "
                f"(steps: {nested_workspace_steps[:5]})"
            )
            details["nested_workspace_steps"] = nested_workspace_steps
            if project_dir is not None:
                offending_fragments = cls._plan_nested_workspace_offending_fragments(
                    plan, project_dir, workspace_identity
                )
                offending_aliases = cls._plan_nested_workspace_aliases(
                    plan, project_dir, workspace_identity
                )
                details["nested_workspace_name"] = next(
                    iter(offending_aliases.values()), Path(project_dir).name
                )
                details["nested_workspace_prefix"] = (
                    f'{details["nested_workspace_name"]}/'
                )
                details["nested_workspace_offending_fragments"] = offending_fragments
                if workspace_identity is not None:
                    corrected_fragments = (
                        cls._plan_nested_workspace_corrected_fragments(
                            offending_fragments, offending_aliases
                        )
                    )
                    details.update(
                        {
                            "physical_runtime_basename": workspace_identity.physical_runtime_basename,
                            "logical_project_name": workspace_identity.logical_project_name,
                            "display_project_path": workspace_identity.display_project_path,
                            "offending_root_alias": next(
                                iter(offending_aliases.values()),
                                workspace_identity.physical_runtime_basename,
                            ),
                            "offending_fragments": offending_fragments,
                            "corrected_fragments": corrected_fragments,
                            "violation_kind": "duplicate_root_alias",
                        }
                    )

        nested_project_root_steps = cls._plan_creates_nested_project_root(
            plan, project_dir
        )
        if nested_project_root_steps:
            repairable.append(
                "Plan appears to generate the deliverable inside a new nested project folder "
                f"instead of the task workspace root (steps: {nested_project_root_steps[:5]})"
            )
            details["nested_project_root_steps"] = nested_project_root_steps
            root_names = cls._plan_nested_project_root_names(
                plan, nested_project_root_steps
            )
            details["nested_project_root_names"] = root_names
            details["violation_kind"] = "duplicate_project_root_scaffold"
            details["offending_root_alias"] = next(iter(root_names.values()), None)
            details["root_intent_evidence"] = _plan_nested_project_root_evidence(
                plan, nested_project_root_steps, root_names
            )

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
                planner_contract=planner_contract,
                require_registered_contract=is_first_ordered_task,
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

        status = cls._select_status(
            warnings=warnings,
            repairable=repairable,
            rejected=rejected,
            severity=validation_severity,
            stage="plan",
        )
        # Phase 33C-3: the Plan requests scope; the validator grants it.  The
        # authority is minted only once the plan is authoritative enough to
        # enter Execution, and is frozen as evidence beside — never instead of —
        # the source-materialization record it was derived from.
        if status in ACCEPTED_PLAN_STATUSES:
            if source_materialization is None:
                details["accepted_path_authority_unavailable"] = (
                    "source_materialization_absent"
                )
            else:
                try:
                    authority, undeclarable_paths = build_accepted_path_authority(
                        plan=accepted_plan,
                        source_materialization=source_materialization,
                        task_explicit_scope_paths=scope_paths,
                        creation_requested_paths=_plan_creation_authorized_paths(
                            accepted_plan
                        ),
                    )
                except PathAuthorityError as exc:
                    # A contradictory grant set is not downgraded to a warning
                    # and is never silently omitted: an accepted plan without a
                    # valid authority is not a valid accepted plan.
                    rejected.append(
                        "accepted_path_authority_construction_failed: " f"{exc.code}"
                    )
                    details["accepted_path_authority_error"] = {
                        "code": exc.code,
                        "message": str(exc),
                    }
                    status = cls._select_status(
                        warnings=warnings,
                        repairable=repairable,
                        rejected=rejected,
                        severity=validation_severity,
                        stage="plan",
                    )
                else:
                    details["accepted_path_authority"] = authority.to_dict()
                    if undeclarable_paths:
                        details["accepted_path_authority_undeclarable_paths"] = list(
                            undeclarable_paths[:20]
                        )
        details = cls._with_validator_rule_ids(stage="plan", details=details)
        verdict = ValidationVerdict(
            stage="plan",
            status=status,
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
        completion_evidence = completion_evidence or {}
        if completion_evidence.get("candidate_delta_required") and not isinstance(
            completion_evidence.get("change_set"), dict
        ):
            finding = CandidateFinding(
                rule_id="candidate_delta_unavailable",
                source="change_set",
                category="infrastructure",
                severity="error",
                attribution="unknown",
                repairable=False,
                message="Trustworthy candidate delta is unavailable",
                evidence={"change_set_present": False},
            )
            return ValidationVerdict(
                stage="task_completion",
                status="unknown",
                profile=profile,
                reasons=[finding.message],
                details={
                    "validated_files": [],
                    "validator_rule_ids": [finding.rule_id],
                    "evidence_failure": True,
                },
                findings=[finding],
            )
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
            details["candidate_authorized_paths"] = list(
                candidate_authorized_paths(project_dir, change_set)
            )
        else:
            change_set = None
        pre_existing_tests = pre_existing_python_test_files(project_dir, change_set)
        pre_existing_sources = pre_existing_source_files(project_dir, change_set)
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
            # Explicit repair intent normally demands independent pre-existing
            # evidence — unless the workspace contained no source at all, in
            # which case the demanded evidence cannot exist: the first-task
            # bootstrap materialized both the implementation and its tests
            # from scratch, and those are the only obtainable evidence. Any
            # pre-existing source file keeps the strict requirement.
            and (not explicit_repair_intent or not pre_existing_sources)
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
            "pre_existing_source_files": pre_existing_sources[:20],
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
        unattributable_placeholder_reasons: List[str] = []
        for candidate in candidate_files:
            baseline_text, delta_available = cls._placeholder_delta_baseline(
                candidate, project_dir, change_set
            )
            file_reasons = cls._detect_placeholder_content(
                candidate, baseline_text=baseline_text
            )
            placeholder_reasons.extend(file_reasons)
            if not delta_available:
                # Only the TODO/placeholder marker rule is delta-scoped; the
                # remaining rules are whole-file by construction and already
                # fail closed as rejections.
                unattributable_placeholder_reasons.extend(
                    reason
                    for reason in file_reasons
                    if "todo or placeholder markers" in reason.lower()
                )
        if placeholder_reasons and profile == "implementation":
            repairable_placeholder_reasons, rejected_placeholder_reasons = (
                cls._split_content_issue_severity(placeholder_reasons)
            )
            # A placeholder marker may only become a candidate repair objective
            # when the candidate delta proves the candidate wrote it. Without
            # that evidence the reason still fails the gate, but it is rejected
            # rather than offered to Candidate Repair as candidate-introduced.
            unattributable = set(unattributable_placeholder_reasons)
            repairable.extend(
                [
                    reason
                    for reason in repairable_placeholder_reasons
                    if reason not in unattributable
                ][:10]
            )
            rejected.extend(
                (
                    rejected_placeholder_reasons
                    + [
                        reason
                        for reason in repairable_placeholder_reasons
                        if reason in unattributable
                    ]
                )[:10]
            )
            details["placeholder_reasons"] = placeholder_reasons[:20]
            if unattributable:
                details["unattributable_placeholder_reasons"] = sorted(unattributable)[
                    :20
                ]

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

        candidate_findings: List[CandidateFinding] = []
        structured_rule_ids = cls._validator_rule_ids_from_details(
            stage="task_completion", details=details
        )
        unattributable_messages = set(
            details.get("unattributable_placeholder_reasons") or []
        )
        for index, message in enumerate(rejected):
            unattributable_message = message in unattributable_messages
            candidate_findings.append(
                CandidateFinding(
                    rule_id=(
                        structured_rule_ids[index]
                        if index < len(structured_rule_ids)
                        else f"candidate_contract_rejected_{index + 1}"
                    ),
                    source="task_contract",
                    category="task_contract",
                    severity="error",
                    attribution=(
                        "unknown" if unattributable_message else "candidate_introduced"
                    ),
                    repairable=False,
                    message=message,
                    evidence=(
                        {"delta_evidence": "unavailable"}
                        if unattributable_message
                        else {}
                    ),
                )
            )
        for index, message in enumerate(repairable):
            candidate_findings.append(
                CandidateFinding(
                    rule_id=(
                        structured_rule_ids[index]
                        if index < len(structured_rule_ids)
                        else f"candidate_contract_repairable_{index + 1}"
                    ),
                    source="task_contract",
                    category="task_contract",
                    severity="error",
                    attribution="candidate_introduced",
                    repairable=True,
                    message=message,
                )
            )
        for index, message in enumerate(warnings):
            candidate_findings.append(
                CandidateFinding(
                    rule_id=f"candidate_warning_{index + 1}",
                    source="task_contract",
                    category="task_contract",
                    severity="warning",
                    attribution="unknown",
                    repairable=False,
                    message=message,
                )
            )
        candidate_identity = None
        if completion_evidence.get("run_candidate_checks") and change_set is not None:
            candidate_identity = candidate_delta_identity(
                change_set, project_dir=project_dir
            )
            candidate_checks = validate_candidate_delta(
                project_dir=project_dir,
                change_set=change_set,
                plan=plan,
                task_prompt=task_prompt,
                include_static_checks=bool(
                    completion_evidence.get("include_static_checks", True)
                ),
                allow_broad_fallback=bool(
                    completion_evidence.get("allow_broad_verification_fallback", False)
                ),
            )
            candidate_findings.extend(candidate_checks.findings)
            details["focused_test_selection"] = {
                "command": candidate_checks.selection.command,
                "source": candidate_checks.selection.source,
                "paths": list(candidate_checks.selection.paths),
                "fallback": candidate_checks.selection.fallback,
            }
            details["candidate_commands"] = list(candidate_checks.commands_run)
            details["test_findings"] = [
                finding.to_dict()
                for finding in candidate_checks.findings
                if finding.category == "test"
            ]
            details["static_findings"] = [
                finding.to_dict()
                for finding in candidate_checks.findings
                if finding.category == "static"
            ]
            for finding in candidate_checks.findings:
                if finding.severity != "error":
                    warnings.append(finding.message)
                elif finding.repairable:
                    repairable.append(finding.message)
                else:
                    rejected.append(finding.message)

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
            findings=candidate_findings,
            candidate_identity=candidate_identity,
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
        candidate_change_set: Optional[Dict[str, Any]] = None,
        prior_expected_files: Optional[List[Dict[str, Any]]] = None,
        current_expected_files: Optional[List[str]] = None,
    ) -> ValidationVerdict:
        warnings: List[str] = []
        repairable: List[str] = []
        rejected: List[str] = []
        details: Dict[str, Any] = {
            "baseline_path": baseline_path,
            "baseline_file_count": baseline_file_count,
        }

        baseline_dir = Path(baseline_path).resolve()
        # Phase 33C-1: two trust classes, two primitives.  Pre-existing baseline
        # entries are trusted observation (classified before any target is
        # followed); Change Set entries are untrusted declarations (validated
        # lexically, never resolved).
        canonical_paths, excluded_baseline_paths = _trusted_baseline_inventory(
            baseline_dir
        )
        added_paths = _declared_candidate_paths(
            (candidate_change_set or {}).get("added_files")
        )
        modified_paths = _declared_candidate_paths(
            (candidate_change_set or {}).get("modified_files")
        )
        deleted_paths = _declared_candidate_paths(
            (candidate_change_set or {}).get("deleted_files")
        )
        projected_paths = canonical_paths | added_paths | modified_paths
        projected_paths.difference_update(deleted_paths)
        projected_file_count = len(projected_paths)
        if candidate_change_set is not None:
            details["preflight_candidate_projection"] = {
                "mode": "candidate_aware",
                "canonical_paths": sorted(canonical_paths)[:20],
                "canonical_raw_paths": sorted(
                    canonical_paths | excluded_baseline_paths
                )[:20],
                "orchestration_internal_paths": sorted(excluded_baseline_paths)[:20],
                "added_paths": sorted(added_paths)[:20],
                "modified_paths": sorted(modified_paths)[:20],
                "deleted_paths": sorted(deleted_paths)[:20],
                "projected_paths": sorted(projected_paths)[:20],
                "projected_file_count": projected_file_count,
            }

        if (candidate_change_set is not None and not projected_file_count) or (
            candidate_change_set is None and baseline_file_count <= 0
        ):
            repairable.append("Canonical baseline is empty after publish")

        current_expected_attribution = _expected_file_attribution(
            expected_files=[
                {"path": path}
                for path in (current_expected_files or missing_task_expected_files)
            ],
            canonical_paths=canonical_paths,
            projected_paths=projected_paths,
            added_paths=added_paths,
            modified_paths=modified_paths,
            deleted_paths=deleted_paths,
            authority="baseline_publish_candidate_projection",
        )
        if current_expected_attribution["missing_paths"]:
            repairable.append(
                "Published baseline is missing current task files: "
                + ", ".join(current_expected_attribution["missing_paths"][:6])
            )
            details["missing_task_expected_files"] = current_expected_attribution[
                "missing_paths"
            ][:20]
        if current_expected_files or missing_task_expected_files:
            current_expected_attribution["severity"] = (
                "repair_required"
                if current_expected_attribution["missing_paths"]
                else "resolved"
            )
            details.setdefault("baseline_condition_attribution", {})[
                "missing_current_task_expected_files"
            ] = current_expected_attribution

        prior_expected_attribution = _expected_file_attribution(
            expected_files=prior_expected_files or missing_prior_expected_files,
            canonical_paths=canonical_paths,
            projected_paths=projected_paths,
            added_paths=added_paths,
            modified_paths=modified_paths,
            deleted_paths=deleted_paths,
            authority="task_service_prior_expected_files",
        )
        if prior_expected_attribution["missing_paths"]:
            if prior_expected_attribution["candidate_worsened"]:
                repairable.append(
                    "Canonical baseline is missing previously completed task files"
                )
                prior_expected_attribution["severity"] = "repair_required"
            else:
                warnings.append(
                    "Canonical baseline is missing previously completed task files"
                )
                prior_expected_attribution["severity"] = "warning"
            details["missing_prior_expected_files"] = missing_prior_expected_files[:20]
        elif prior_expected_files or missing_prior_expected_files:
            prior_expected_attribution["severity"] = "resolved"
        if prior_expected_files or missing_prior_expected_files:
            details.setdefault("baseline_condition_attribution", {})[
                "missing_prior_expected_files"
            ] = prior_expected_attribution
        if consistency_issues:
            mixed_attribution = _mixed_language_attribution(
                canonical_paths=canonical_paths,
                added_paths=added_paths,
                modified_paths=modified_paths,
                projected_paths=projected_paths,
            )
            target = warnings if relaxed_mode else repairable
            for issue in consistency_issues[:4]:
                if issue != MIXED_LANGUAGE_WORKSPACE_ISSUE:
                    target.append(issue)
                elif mixed_attribution["severity"] == "repair_required":
                    target.append(issue)
                elif mixed_attribution["severity"] == "warning":
                    warnings.append(issue)
            details["consistency_issues"] = consistency_issues[:10]
            if MIXED_LANGUAGE_WORKSPACE_ISSUE in consistency_issues:
                details.setdefault("baseline_condition_attribution", {})[
                    "mixed_language_workspace"
                ] = mixed_attribution
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
