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
    ]
}
