"""Runtime rule ownership registry.

This module is a lookup table only. It is not imported at runtime.
It exists so that any change to normalization.py or validator rules
can be cross-referenced against declared ownership, scope, and exit conditions.

Architectural rule:
    The runtime should be dumb about content and smart about structure.

A rule belongs in ``core_invariant`` if it enforces a structural boundary
regardless of workload (path safety, workspace escape, lifecycle transitions).

A rule belongs in ``workload_contract`` if it is reusable across projects of
the same task family, has a negative test proving it does not fire for other
families, and has a declared exit condition.

A rule belongs in ``knowledge_guidance`` if it encodes workload-specific
experience that should live in knowledge or planner prompts, not runtime code.

A rule must be labeled ``deprecated_artifact`` if it was written for a single
benchmark or project, passes current tests but should not expand, and has a
concrete exit condition describing how to remove it.

Rules labeled ``deprecated_artifact`` must have ``allowed_to_expand = False``.
No exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class RuntimeRule:
    rule_id: str
    owner_layer: Literal[
        "core_invariant",
        "workload_contract",
        "knowledge_guidance",
        "deprecated_artifact",
    ]
    scope: str
    source_location: str
    negative_tests: list[str]
    exit_condition: str
    allowed_to_expand: bool = field(default=False)


RULE_REGISTRY: dict[str, RuntimeRule] = {
    r.rule_id: r
    for r in [
        # ------------------------------------------------------------------ #
        # deprecated_artifact — do not expand, mark for future removal       #
        # ------------------------------------------------------------------ #
        RuntimeRule(
            rule_id="static_site_path_rewrite",
            owner_layer="deprecated_artifact",
            scope=(
                "Static-site workloads where the workspace root already has "
                "index.html + css/style.css and the planner drifts toward a "
                "React/Vite stack (src/index.js, npm run build)."
            ),
            source_location="app/services/orchestration/planning/normalization.py:656 (normalize_existing_static_site_plan)",
            negative_tests=[
                "test_static_site_normalization_does_not_fire_for_backend_task",
                "test_static_site_normalization_does_not_fire_for_review_task",
                "test_phase10r_mixed_workload_gate.py::test_static_site_normalizer_not_triggered_for_backend_bug_fix",
            ],
            exit_condition=(
                "Replace with a scoped workload_contract for the plain-static-site "
                "task family. Remove once planner-side path-drift handling is "
                "reliable enough that the runtime rewrite is unnecessary. "
                "Do not add new path mappings or asset-detection heuristics."
            ),
            allowed_to_expand=False,
        ),
        RuntimeRule(
            rule_id="static_site_validation_fallback",
            owner_layer="deprecated_artifact",
            scope=(
                "workflow_stage == 'validate' AND workspace has a "
                "public/<name>/index.html + css/style.css directory shape. "
                "Originally written for the Garden/status-site benchmark."
            ),
            source_location="app/services/orchestration/phases/planning_flow.py:168 (_static_site_validation_fallback_plan)",
            negative_tests=[
                "test_static_validation_fallback_does_not_fire_outside_validate_stage",
                "test_static_validation_fallback_does_not_fire_for_backend_workspace",
            ],
            exit_condition=(
                "Remove when status-site verification is moved to a workload "
                "contract or knowledge-guided verification prompt. "
                "Do not add new needle patterns (API, Queue, Knowledge, skip link, "
                "alt text) or new directory shape matches."
            ),
            allowed_to_expand=False,
        ),
        # ------------------------------------------------------------------ #
        # workload_contract — reusable across matching task families         #
        # ------------------------------------------------------------------ #
        RuntimeRule(
            rule_id="file_target_path_correction",
            owner_layer="workload_contract",
            scope=(
                "Any workload where the plan references a file that does not "
                "exist in the workspace but has exactly one unique basename "
                "match among existing workspace files (root drift correction)."
            ),
            source_location="app/services/orchestration/planning/normalization.py:375 (normalize_existing_file_target_plan)",
            negative_tests=[
                "test_existing_file_target_normalization_ignores_ambiguous_matches",
                "test_existing_file_target_normalization_maps_missing_basename_to_unique_path",
                "test_existing_file_target_normalization_maps_nested_root_drift_to_src_path",
            ],
            exit_condition=(
                "Stable; may remain until planner-side path resolution is "
                "reliable enough to not produce root-drifted file targets. "
                "Do not extend scope to multi-file or directory matching."
            ),
            allowed_to_expand=False,
        ),
        RuntimeRule(
            rule_id="stale_replace_small_file_fallback",
            owner_layer="workload_contract",
            scope=(
                "Single-function modules of <=80 lines where the planner emits "
                "a replace_in_file op but the exact old text is absent from the "
                "current file content. Only fires when the replacement snippet "
                "contains the same single function name as the current file."
            ),
            source_location="app/services/orchestration/planning/normalization.py:568 (normalize_stale_replace_ops_to_small_file_writes)",
            negative_tests=[
                "test_stale_replace_fallback_does_not_fire_for_large_files",
                "test_stale_replace_fallback_does_not_fire_for_multi_function_modules",
                "test_stale_replace_fallback_does_not_fire_when_old_text_present",
            ],
            exit_condition=(
                "Deprecate when planner-side stale-patch repair is reliable "
                "enough that a runtime safety net is no longer needed. "
                "Do not extend the line-count threshold or multi-function cases."
            ),
            allowed_to_expand=False,
        ),
        RuntimeRule(
            rule_id="static_site_contract_completion",
            owner_layer="workload_contract",
            scope=(
                "Plans that contain html/css/svg write ops, or whose task prompt "
                "mentions 'html', 'css', 'svg', or 'static site'. Fills missing "
                "parent-dir mkdir ops, expected_files entries, and verification "
                "commands for static-site shaped plans."
            ),
            source_location="app/services/orchestration/planning/normalization.py:842 (complete_repaired_plan_contract)",
            negative_tests=[
                "test_contract_completion_does_not_fire_for_backend_task",
                "test_contract_completion_does_not_fire_for_review_only_task",
                "test_phase10r_mixed_workload_gate.py::test_backend_task_plan_contract_not_completed_as_static_site",
            ],
            exit_condition=(
                "Split into per-family contract modules when a second non-static-site "
                "task family needs contract completion. Do not add new file type "
                "triggers or content inference beyond the current html/css/svg shape."
            ),
            allowed_to_expand=False,
        ),
    ]
}
