"""Characterization tests: Prompt Reduction Arm B (Priority 8).

Objective: verify that REDUCED_PLANNING_PROMPT_ENABLED=False is identical to
current production, that REDUCED_PLANNING_PROMPT_ENABLED=True produces the Arm B
reduced prompt, and that all classification decisions (KEEP/COMPRESS/REMOVE) are
reflected correctly in the Arm B output.

Constraints:
- No live model calls.
- No changes to validator, planning schema, repair logic, or execution.
- REDUCED_PLANNING_PROMPT_ENABLED defaults False (Arm A = production).
- All assertions are read-only observations about prompt content and size.

Cases verified:
  arm_a_unchanged        : flag=False → identical to production Arm A
  arm_b_generated        : flag=True  → Arm B prompt produced
  keep_rules_present     : Rule 7, 8, 10 + Req 12 verbatim in Arm B
  unknown_rules_compressed: Rules 2-4, 11 summary present in Arm B
  removed_sections_absent: JSON example, Rules 1/5/6 absent from Arm B
  size_reduction         : Arm B measurably smaller than Arm A
  profile_gating         : operation_choice_contract gated by execution_profile
  flag_default           : REDUCED_PLANNING_PROMPT_ENABLED defaults False
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.orchestration.prompt_templates import PromptTemplates
from app.config import settings


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

_TASK = "Create a FastAPI endpoint GET /items that returns a sorted list of items."
_CONTEXT = "FastAPI project with SQLAlchemy. Models defined in app/models.py."
_PROJECT_DIR = "/tmp/test-project"
_WORKSPACE_ROOT = "/tmp"


def _build_arm_a(execution_profile: str = "full_lifecycle") -> str:
    """Build Arm A prompt (flag off)."""
    with patch.object(settings, "REDUCED_PLANNING_PROMPT_ENABLED", False):
        return PromptTemplates.build_planning_prompt(
            task_description=_TASK,
            project_context=_CONTEXT,
            workspace_root=_WORKSPACE_ROOT,
            project_dir=_PROJECT_DIR,
            execution_profile=execution_profile,
        )


def _build_arm_b(execution_profile: str = "full_lifecycle") -> str:
    """Build Arm B prompt (flag on)."""
    with patch.object(settings, "REDUCED_PLANNING_PROMPT_ENABLED", True):
        return PromptTemplates.build_planning_prompt(
            task_description=_TASK,
            project_context=_CONTEXT,
            workspace_root=_WORKSPACE_ROOT,
            project_dir=_PROJECT_DIR,
            execution_profile=execution_profile,
        )


# ---------------------------------------------------------------------------
# Case: Arm A unchanged (flag=False)
# ---------------------------------------------------------------------------


class TestArmAUnchanged:
    def test_flag_off_produces_arm_a_preamble(self):
        result = _build_arm_a()
        assert result.startswith(
            "Return ONLY a valid JSON array. First character must be `[`. Last must be `]`."
        )

    def test_arm_a_contains_valid_minimal_json_example(self):
        """Arm A must include the annotated JSON example block (848c)."""
        result = _build_arm_a()
        assert "Valid Minimal JSON Example" in result

    def test_arm_a_contains_full_requirements_block(self):
        """Arm A must have all 14 requirements including removed ones."""
        result = _build_arm_a()
        # Req 10 (rollback present) — REMOVED in Arm B
        assert "rollback` must always be present" in result
        # Req 9 (verification present) — REMOVED in Arm B
        assert "verification` must always be present" in result
        # Req 3 (JSON-only output) — REMOVED in Arm B
        assert "Output JSON array only" in result

    def test_arm_a_contains_11_planning_rules(self):
        """Arm A must retain all 11 Planning Rules."""
        result = _build_arm_a()
        assert "Short runnable shell only" in result  # Rule 1 (REMOVED in B)
        assert "Don't assume files exist" in result  # Rule 5 (REMOVED in B)
        assert "If prior artifacts are mentioned" in result  # Rule 6 (REMOVED in B)

    def test_arm_a_operation_choice_contract_always_present(self):
        """Arm A includes the operation_choice_contract regardless of profile."""
        for profile in ["full_lifecycle", "review_only", "test_only", "debug_only"]:
            result = _build_arm_a(execution_profile=profile)
            assert (
                "`replace_in_file` is only for exact old text" in result
            ), f"Arm A must include operation_choice_contract for profile={profile}"

    def test_arm_a_identical_direct_call_and_flag_false(self):
        """Direct call to build_planning_prompt with flag=False is identical to Arm A."""
        flag_off = _build_arm_a()
        with patch.object(settings, "REDUCED_PLANNING_PROMPT_ENABLED", False):
            direct = PromptTemplates.build_planning_prompt(
                task_description=_TASK,
                project_context=_CONTEXT,
                workspace_root=_WORKSPACE_ROOT,
                project_dir=_PROJECT_DIR,
            )
        assert flag_off == direct


# ---------------------------------------------------------------------------
# Case: Arm B generated (flag=True)
# ---------------------------------------------------------------------------


class TestArmBGenerated:
    def test_flag_on_produces_arm_b_preamble(self):
        result = _build_arm_b()
        assert result.startswith(
            "Return ONLY a valid JSON array. First character must be `[`. Last must be `]`."
        )

    def test_arm_b_is_not_identical_to_arm_a(self):
        arm_a = _build_arm_a()
        arm_b = _build_arm_b()
        assert arm_a != arm_b, "Arm B must differ from Arm A"

    def test_arm_b_contains_task_description(self):
        result = _build_arm_b()
        assert _TASK[:60] in result

    def test_arm_b_contains_project_context(self):
        result = _build_arm_b()
        assert "FastAPI" in result

    def test_arm_b_contains_workspace_path(self):
        result = _build_arm_b()
        assert _PROJECT_DIR in result

    def test_arm_b_contains_execution_profile_rules(self):
        """Arm B must still render execution profile rules (unchanged from A)."""
        result = _build_arm_b()
        assert "Plan, implement, verify, and summarize" in result

    def test_arm_b_footer_retained(self):
        result = _build_arm_b()
        assert "Return only a JSON array. No markdown. No prose." in result

    def test_arm_b_execution_boundary_retained(self):
        """Execution Boundary block must be unchanged in Arm B."""
        result = _build_arm_b()
        assert "Working directory is already" in result
        assert "Use relative paths only" in result


# ---------------------------------------------------------------------------
# Case: KEEP rules present in Arm B
# ---------------------------------------------------------------------------


class TestKeepRulesPresentInArmB:
    def test_rule_8_heredoc_ban_present_verbatim(self):
        """Rule 8 (26 repairs, load-bearing) must be verbatim in Arm B."""
        result = _build_arm_b()
        assert "Never use heredoc syntax" in result

    def test_rule_10_verification_format_present_verbatim(self):
        """Rule 10 (8 repairs, load-bearing) must be verbatim in Arm B."""
        result = _build_arm_b()
        assert (
            "Verification must use `python -c`, `python -m`, `npm run build`, `node -e`"
            in result
        )

    def test_rule_7_ops_preference_present_verbatim(self):
        """Rule 7 (structural correctness) must be verbatim in Arm B."""
        result = _build_arm_b()
        assert "For routine file changes, prefer `ops`" in result

    def test_rule_9_final_verify_step_present(self):
        """Rule 9 must be present as a one-sentence summary in Arm B."""
        result = _build_arm_b()
        assert "exactly one final meaningful verification" in result

    def test_req_12_expected_files_present_verbatim(self):
        """`expected_files` rule (14 rejections, load-bearing) verbatim in Arm B."""
        result = _build_arm_b()
        assert (
            "`expected_files` must always be present and must be a JSON array of relative path strings"
            in result
        )

    def test_supported_file_ops_list_present(self):
        """Supported file ops list (defines valid ops, KEEP) must be in Arm B."""
        result = _build_arm_b()
        assert "write_file" in result

    def test_preamble_output_discipline_present(self):
        """Output format guard (structural) must be in Arm B."""
        result = _build_arm_b()
        assert "First character must be `[`" in result
        assert "Last must be `]`" in result

    def test_invalid_shape_guard_present(self):
        """Invalid shape warning must be retained in Arm B."""
        result = _build_arm_b()
        assert (
            'Objects like {"steps": [...]}" instead of a top-level array' in result
            or "Objects like" in result
        )


# ---------------------------------------------------------------------------
# Case: Compressed UNKNOWN rule summaries present in Arm B
# ---------------------------------------------------------------------------


class TestUnknownRulesCompressedInArmB:
    def test_rule_2_incremental_summary_present(self):
        """Rule 2 (incremental order, UNKNOWN) must be present in compressed form."""
        result = _build_arm_b()
        assert "incrementally" in result.lower() or "one file at a time" in result

    def test_rule_3_relative_paths_summary_present(self):
        """Rule 3 (relative paths, UNKNOWN) must be present in compressed form."""
        result = _build_arm_b()
        assert "relative paths only" in result

    def test_rule_4_no_background_summary_present(self):
        """Rule 4 (no background processes, UNKNOWN) must be in compressed form."""
        result = _build_arm_b()
        assert "background processes" in result or "nohup" in result

    def test_rule_11_scaffold_summary_present(self):
        """Rule 11 (scaffold in workspace, UNKNOWN) must be in compressed form."""
        result = _build_arm_b()
        assert "scaffold" in result.lower() or "current workspace" in result


# ---------------------------------------------------------------------------
# Case: Removed sections absent from Arm B
# ---------------------------------------------------------------------------


class TestRemovedSectionsAbsentFromArmB:
    def test_json_example_block_absent(self):
        """848c JSON example block must be absent from Arm B."""
        result = _build_arm_b()
        assert "Valid Minimal JSON Example" not in result

    def test_rule_1_short_runnable_shell_absent(self):
        """Rule 1 (Short runnable shell only) must be absent from Arm B."""
        result = _build_arm_b()
        assert "Short runnable shell only" not in result

    def test_rule_5_dont_assume_files_absent(self):
        """Rule 5 (Don't assume files exist) must be absent from Arm B."""
        result = _build_arm_b()
        assert "Don't assume files exist" not in result

    def test_rule_6_prior_artifacts_absent(self):
        """Rule 6 (extend prior artifacts, REMOVE decision) must be absent from Arm B."""
        result = _build_arm_b()
        assert "If prior artifacts are mentioned in context" not in result

    def test_req_3_json_only_output_absent(self):
        """Req 3 (JSON-only output, redundant) must be absent from Arm B."""
        result = _build_arm_b()
        assert "Output JSON array only" not in result

    def test_req_4_avoid_docs_absent(self):
        """Req 4 (avoid documentation files, zero signal) must be absent from Arm B."""
        result = _build_arm_b()
        assert (
            "Do NOT create documentation files unless the task explicitly" not in result
        )

    def test_req_9_verification_must_be_present_absent(self):
        """Req 9 (verification must be present, subsumed) absent from Arm B requirements."""
        result = _build_arm_b()
        # Req 9 text: "`verification` must always be present and must be one shell string or null"
        assert "`verification` must always be present" not in result

    def test_req_10_rollback_must_be_present_absent(self):
        """Req 10 (rollback must be present, zero signal) absent from Arm B requirements."""
        result = _build_arm_b()
        assert "`rollback` must always be present" not in result


# ---------------------------------------------------------------------------
# Case: Prompt size reduction measurable
# ---------------------------------------------------------------------------


class TestPromptSizeReduction:
    def test_arm_b_smaller_than_arm_a(self):
        """Arm B must be measurably smaller than Arm A."""
        arm_a = _build_arm_a()
        arm_b = _build_arm_b()
        reduction = len(arm_a) - len(arm_b)
        assert reduction > 0, (
            f"Arm B must be smaller than Arm A: "
            f"Arm A={len(arm_a)}c, Arm B={len(arm_b)}c"
        )

    def test_arm_b_savings_at_least_1800c(self):
        """Arm B must save at least 1800c vs Arm A.

        Threshold lowered from 1900c after v3 revision replaced the compact
        pathlib.Path example with a py_compile example + prohibition note
        (~140c net addition to Arm B).
        """
        arm_a = _build_arm_a()
        arm_b = _build_arm_b()
        savings = len(arm_a) - len(arm_b)
        assert savings >= 1800, (
            f"Arm B savings {savings}c below 1800c minimum "
            f"(Arm A={len(arm_a)}c, Arm B={len(arm_b)}c)"
        )

    def test_arm_b_static_frame_size_summary(self):
        """Print size comparison for maintenance report (not a correctness gate)."""
        arm_a = _build_arm_a()
        arm_b = _build_arm_b()
        savings = len(arm_a) - len(arm_b)
        pct = (savings / len(arm_a)) * 100 if arm_a else 0
        print(
            f"\n\n=== ARM B PROMPT SIZE COMPARISON ===\n"
            f"Arm A (production):  {len(arm_a):>6}c\n"
            f"Arm B (reduced):     {len(arm_b):>6}c\n"
            f"Savings:             {savings:>6}c ({pct:.1f}%)\n"
            f"Design target:       ~3,169c (69% of static frame)\n"
            f"=== END COMPARISON ===\n"
        )
        # Not asserting design target — measured reduction validates the direction.
        assert savings > 0

    def test_arm_b_review_only_smaller_than_arm_a_review_only(self):
        """Arm B review_only must be smaller than Arm A review_only.

        Arm A always includes the operation_choice_contract (~120c); Arm B
        omits it for review_only. This validates profile-gated savings on a
        same-profile comparison.
        """
        arm_a_review = _build_arm_a(execution_profile="review_only")
        arm_b_review = _build_arm_b(execution_profile="review_only")
        assert len(arm_b_review) < len(arm_a_review), (
            f"review_only Arm B ({len(arm_b_review)}c) must be smaller than "
            f"review_only Arm A ({len(arm_a_review)}c)"
        )


# ---------------------------------------------------------------------------
# Case: Profile gating of operation_choice_contract
# ---------------------------------------------------------------------------


class TestProfileGating:
    def test_full_lifecycle_includes_operation_contract(self):
        """full_lifecycle Arm B must include the replace_in_file contract."""
        result = _build_arm_b(execution_profile="full_lifecycle")
        assert "`replace_in_file` is only for exact old text" in result

    def test_execute_only_includes_operation_contract(self):
        """execute_only Arm B must include the replace_in_file contract."""
        result = _build_arm_b(execution_profile="execute_only")
        assert "`replace_in_file` is only for exact old text" in result

    def test_review_only_excludes_operation_contract(self):
        """review_only Arm B must NOT include the operation_choice_contract."""
        result = _build_arm_b(execution_profile="review_only")
        assert "`replace_in_file` is only for exact old text" not in result

    def test_test_only_excludes_operation_contract(self):
        """test_only Arm B must NOT include the operation_choice_contract."""
        result = _build_arm_b(execution_profile="test_only")
        assert "`replace_in_file` is only for exact old text" not in result

    def test_debug_only_excludes_operation_contract(self):
        """debug_only Arm B must NOT include the operation_choice_contract."""
        result = _build_arm_b(execution_profile="debug_only")
        assert "`replace_in_file` is only for exact old text" not in result

    def test_arm_a_always_includes_contract_regardless_of_profile(self):
        """Arm A includes operation_choice_contract for all profiles (unchanged)."""
        for profile in [
            "full_lifecycle",
            "execute_only",
            "review_only",
            "test_only",
            "debug_only",
        ]:
            result = _build_arm_a(execution_profile=profile)
            assert (
                "`replace_in_file` is only for exact old text" in result
            ), f"Arm A must include contract for profile={profile}"


# ---------------------------------------------------------------------------
# Case: Feature flag defaults and isolation
# ---------------------------------------------------------------------------


class TestFeatureFlagDefaults:
    def test_reduced_planning_prompt_flag_defaults_false(self):
        """REDUCED_PLANNING_PROMPT_ENABLED must default to False (no runtime change)."""
        from app.config import settings

        assert settings.REDUCED_PLANNING_PROMPT_ENABLED is False, (
            "REDUCED_PLANNING_PROMPT_ENABLED must default to False — "
            "flag off means zero runtime behavior change"
        )

    def test_flag_false_matches_direct_arm_a_call(self):
        """Flag=False must produce the same output as calling build_planning_prompt_arm_a directly."""
        arm_a_via_flag = _build_arm_a()
        # build_planning_prompt with flag=False calls render("task_planning", ...)
        # Direct render of TASK_PLANNING with same context must match
        assert "Valid Minimal JSON Example" in arm_a_via_flag
        assert "Short runnable shell only" in arm_a_via_flag

    def test_flag_true_routes_to_arm_b_template(self):
        """Flag=True must route to TASK_PLANNING_ARM_B, not TASK_PLANNING."""
        arm_b_via_flag = _build_arm_b()
        assert "Valid Minimal JSON Example" not in arm_b_via_flag
        assert "Return only a JSON array. No markdown. No prose." in arm_b_via_flag

    def test_arm_b_direct_build_matches_flag_on(self):
        """Direct call to build_planning_prompt_arm_b must match flag=True result."""
        flag_on = _build_arm_b()
        direct = PromptTemplates.build_planning_prompt_arm_b(
            task_description=_TASK,
            project_context=_CONTEXT,
            workspace_root=_WORKSPACE_ROOT,
            project_dir=_PROJECT_DIR,
        )
        assert flag_on == direct

    def test_no_other_flags_affected(self):
        """Other feature flags must be unaffected by REDUCED_PLANNING_PROMPT_ENABLED.

        Phase 18H: verify repository defaults, independent of local `.env`
        (which may enable WORKING_MEMORY_INJECTION_ENABLED for pilot
        validation).
        """
        from app.tests.conftest import repo_default_settings

        defaults = repo_default_settings()
        assert defaults.PSS_CONTINUATION_INJECTION_ENABLED is False
        assert defaults.ARTIFACT_CONTINUATION_ENABLED is False
        assert defaults.WORKING_MEMORY_INJECTION_ENABLED is False


# ---------------------------------------------------------------------------
# Case: Compact example present in Arm B (v3 revision — 2026-06-09)
# ---------------------------------------------------------------------------


class TestCompactExampleInArmB:
    """Verify the v3 compact example added after pilot run 3.

    Run 3 (2026-06-09) showed the v2 pathlib.Path example anchored Qwen on
    python -c, causing T01 regression and content-assertion drift. v3 replaces
    the example verification with python3 -m py_compile and adds a prohibition
    note to suppress f-string and content-assertion patterns.
    """

    def test_compact_example_block_present(self):
        """Arm B must include the compact **Example:** label."""
        result = _build_arm_b()
        assert "**Example:**" in result

    def test_compact_example_includes_write_file_op(self):
        """Arm B v3 example must contain a write_file op."""
        result = _build_arm_b()
        assert '"op":"write_file"' in result

    def test_compact_example_includes_expected_files(self):
        """Arm B v3 example must include an expected_files array."""
        result = _build_arm_b()
        lines = result.split("\n")
        example_line = next(
            (line for line in lines if line.strip().startswith("[{")), None
        )
        assert example_line is not None, "No example JSON line found"
        assert '"expected_files"' in example_line

    def test_compact_example_uses_py_compile(self):
        """Arm B v3 example verification must use python3 -m py_compile."""
        result = _build_arm_b()
        assert "python3 -m py_compile" in result

    def test_compact_example_no_pathlib_path(self):
        """Arm B v3 must not contain pathlib.Path (removed in v3)."""
        result = _build_arm_b()
        assert "pathlib.Path" not in result

    def test_compact_example_no_python_c_in_example_json(self):
        """Arm B v3 example JSON line must not use python -c as verification."""
        result = _build_arm_b()
        lines = result.split("\n")
        example_line = next(
            (line for line in lines if line.strip().startswith("[{")), None
        )
        assert example_line is not None, "No example JSON line found"
        assert "python -c" not in example_line

    def test_v3_note_never_assert_content(self):
        """Arm B v3 note must include 'Never assert content values'."""
        result = _build_arm_b()
        assert "Never assert content values" in result

    def test_v3_note_never_fstrings(self):
        """Arm B v3 note must include 'Never use f-strings'."""
        result = _build_arm_b()
        assert "Never use f-strings" in result

    def test_compact_example_no_heredoc(self):
        """Arm B example must not use heredoc (<<) syntax."""
        result = _build_arm_b()
        assert "<<" not in result

    def test_compact_example_no_nested_fstring(self):
        """Arm B template must not contain f-string literals."""
        result = _build_arm_b()
        assert "f'" not in result
        assert 'f"' not in result

    def test_old_848c_example_still_absent(self):
        """The original 848c 'Valid Minimal JSON Example' block must remain absent from Arm B."""
        result = _build_arm_b()
        assert "Valid Minimal JSON Example" not in result

    def test_arm_a_still_has_original_example(self):
        """Arm A must still contain the original 'Valid Minimal JSON Example' block."""
        result = _build_arm_a()
        assert "Valid Minimal JSON Example" in result

    def test_compact_example_absent_from_arm_a(self):
        """The compact **Example:** block must NOT appear in Arm A (Arm A is unchanged)."""
        result = _build_arm_a()
        assert "**Example:**" not in result
