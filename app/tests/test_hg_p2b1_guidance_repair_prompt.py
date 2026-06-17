"""Tests for HG-P2b.1 — active guidance constraints in planning repair prompts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.human_guidance_plan_validator import (
    _REPAIR_GUIDANCE_AUTHORITY,
    _REPAIR_GUIDANCE_HEADER,
    render_active_guidance_for_repair,
)
from app.services.orchestration.planning.repair_prompts import (
    build_compact_planning_repair_prompt,
    build_compact_stale_replace_repair_prompt,
    build_planning_repair_prompt_with_metadata,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_STDOUT_GUIDANCE = (
    "## OPERATOR GUIDANCE (mandatory)\n"
    "These Human Guidance rules are mandatory unless a validator/safety rule forbids them.\n"
    "- All runtime output must go to stdout. Never use logging. Use print() for runtime reporting."
)

_MUTABLE_DEFAULT_GUIDANCE = (
    "## OPERATOR GUIDANCE (mandatory)\n"
    "These Human Guidance rules are mandatory unless a validator/safety rule forbids them.\n"
    "- Never use mutable default arguments. Use None and initialize inside the function."
)

_BOTH_GUIDANCE = (
    "## OPERATOR GUIDANCE (mandatory)\n"
    "These Human Guidance rules are mandatory unless a validator/safety rule forbids them.\n"
    "- Never use mutable default arguments. Use None and initialize inside the function.\n"
    "- All runtime output must go to stdout. Never use logging. Use print() for runtime reporting."
)

_BAD_PLAN = (
    '[{"step_number": 1, "ops": [{"op": "write_file", "path": "x.py",'
    ' "content": "import logging\\nlogger = logging.getLogger(__name__)\\ndef f(x=[]):pass"}]}]'
)
_STALE_REASON = [
    "replace_in_file old text not found in src/core.py."
    " Current file excerpt: def validate(): pass"
]


# ---------------------------------------------------------------------------
# render_active_guidance_for_repair
# ---------------------------------------------------------------------------


def _render(entries, *, table_enabled=True, conflict_enabled=True, activation=True):
    """Helper: call render_active_guidance_for_repair with mocked dependencies."""
    settings_mock = MagicMock()
    settings_mock.HUMAN_GUIDANCE_TABLE_ENABLED = table_enabled
    settings_mock.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED = conflict_enabled

    with patch("app.config.settings", settings_mock):
        with patch(
            "app.services.human_guidance_activation_service.check_activation_flag",
            return_value=activation,
        ):
            with patch(
                "app.services.human_guidance_service.collect_active_guidance",
                return_value=entries,
            ):
                return render_active_guidance_for_repair(
                    MagicMock(),
                    project_id=1,
                    session_id=10,
                    task_id=100,
                    user_id=999,
                )


class TestRenderActiveGuidanceForRepair:
    def test_returns_empty_when_table_disabled(self):
        result = _render(
            [{"message": "Do not use mutable defaults."}], table_enabled=False
        )
        assert result == ""

    def test_returns_empty_when_conflict_detection_disabled(self):
        result = _render(
            [{"message": "Do not use mutable defaults."}], conflict_enabled=False
        )
        assert result == ""

    def test_returns_empty_when_no_entries(self):
        result = _render([])
        assert result == ""

    def test_includes_header_and_authority_line(self):
        result = _render([{"message": "Never use mutable default arguments."}])
        assert _REPAIR_GUIDANCE_HEADER in result
        assert _REPAIR_GUIDANCE_AUTHORITY in result

    def test_includes_guidance_messages(self):
        result = _render(
            [
                {"message": "Never use mutable default arguments."},
                {"message": "All runtime output must go to stdout."},
            ]
        )
        assert "Never use mutable default arguments." in result
        assert "All runtime output must go to stdout." in result

    def test_skips_entries_with_empty_message(self):
        result = _render(
            [{"message": ""}, {"message": None}, {"message": "Valid guidance rule."}]
        )
        assert "Valid guidance rule." in result

    def test_returns_empty_when_only_empty_messages(self):
        result = _render([{"message": ""}, {"message": None}])
        assert result == ""

    def test_never_raises_on_exception(self):
        settings_mock = MagicMock()
        settings_mock.HUMAN_GUIDANCE_TABLE_ENABLED = True
        settings_mock.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED = True

        with patch("app.config.settings", settings_mock):
            with patch(
                "app.services.human_guidance_service.collect_active_guidance",
                side_effect=RuntimeError("database is unavailable"),
            ):
                result = render_active_guidance_for_repair(
                    MagicMock(),
                    project_id=1,
                    session_id=10,
                    task_id=100,
                    user_id=999,
                )
        assert result == ""

    def test_truncates_to_max_chars(self):
        long_message = "x" * 700
        result = _render([{"message": long_message}])
        assert len(result) <= 600


# ---------------------------------------------------------------------------
# build_planning_repair_prompt_with_metadata — guidance_block injection
# ---------------------------------------------------------------------------


class TestRepairPromptWithMetadataGuidanceBlock:
    def _build(self, guidance_block: str = "", rejection_reasons=None, tmp_path=None):
        project_dir = tmp_path or Path("/tmp")
        result = build_planning_repair_prompt_with_metadata(
            task_description="Add report_label using print.",
            malformed_output=_BAD_PLAN,
            project_dir=project_dir,
            rejection_reasons=rejection_reasons or ["bootstrap violation"],
            guidance_block=guidance_block,
        )
        return result.prompt

    def test_no_guidance_block_absent_from_prompt(self, tmp_path):
        prompt = self._build(guidance_block="", tmp_path=tmp_path)
        assert _REPAIR_GUIDANCE_HEADER not in prompt

    def test_guidance_block_present_in_prompt(self, tmp_path):
        prompt = self._build(guidance_block=_STDOUT_GUIDANCE, tmp_path=tmp_path)
        assert _REPAIR_GUIDANCE_HEADER in prompt

    def test_guidance_block_appears_before_bad_plan(self, tmp_path):
        prompt = self._build(guidance_block=_STDOUT_GUIDANCE, tmp_path=tmp_path)
        guidance_pos = prompt.index(_REPAIR_GUIDANCE_HEADER)
        bad_pos = prompt.index("Bad:")
        assert guidance_pos < bad_pos

    def test_stdout_guidance_in_bootstrap_repair_prompt(self, tmp_path):
        prompt = self._build(
            guidance_block=_STDOUT_GUIDANCE,
            rejection_reasons=["bootstrap contract failed: src.validtools.core"],
            tmp_path=tmp_path,
        )
        assert "All runtime output must go to stdout" in prompt
        assert _REPAIR_GUIDANCE_AUTHORITY in prompt

    def test_mutable_default_guidance_in_repair_prompt(self, tmp_path):
        prompt = self._build(
            guidance_block=_MUTABLE_DEFAULT_GUIDANCE,
            rejection_reasons=["guidance_violation: mutable_default"],
            tmp_path=tmp_path,
        )
        assert "Never use mutable default arguments" in prompt

    def test_both_guidance_rules_present(self, tmp_path):
        prompt = self._build(guidance_block=_BOTH_GUIDANCE, tmp_path=tmp_path)
        assert "Never use mutable default arguments" in prompt
        assert "All runtime output must go to stdout" in prompt

    def test_empty_guidance_matches_no_guidance_prompt(self, tmp_path):
        p1 = self._build(guidance_block="", tmp_path=tmp_path)
        p2 = self._build(guidance_block="", tmp_path=tmp_path)
        assert p1 == p2


# ---------------------------------------------------------------------------
# build_compact_planning_repair_prompt — guidance_block injection
# ---------------------------------------------------------------------------


class TestCompactRepairPromptGuidanceBlock:
    def test_no_guidance_block_absent(self):
        prompt = build_compact_planning_repair_prompt(
            _BAD_PLAN,
            rejection_reasons=["malformed"],
            guidance_block="",
        )
        assert _REPAIR_GUIDANCE_HEADER not in prompt

    def test_guidance_block_present(self):
        prompt = build_compact_planning_repair_prompt(
            _BAD_PLAN,
            rejection_reasons=["malformed"],
            guidance_block=_STDOUT_GUIDANCE,
        )
        assert _REPAIR_GUIDANCE_HEADER in prompt

    def test_guidance_appears_before_validation_errors(self):
        prompt = build_compact_planning_repair_prompt(
            _BAD_PLAN,
            rejection_reasons=["some error"],
            guidance_block=_STDOUT_GUIDANCE,
        )
        guidance_pos = prompt.index(_REPAIR_GUIDANCE_HEADER)
        error_pos = prompt.index("Validation errors:")
        assert guidance_pos < error_pos

    def test_stdout_guidance_content_included(self):
        prompt = build_compact_planning_repair_prompt(
            _BAD_PLAN,
            rejection_reasons=["bootstrap violation"],
            guidance_block=_STDOUT_GUIDANCE,
        )
        assert "All runtime output must go to stdout" in prompt

    def test_mutable_default_guidance_content_included(self):
        prompt = build_compact_planning_repair_prompt(
            _BAD_PLAN,
            rejection_reasons=["guidance_violation"],
            guidance_block=_MUTABLE_DEFAULT_GUIDANCE,
        )
        assert "Never use mutable default arguments" in prompt


# ---------------------------------------------------------------------------
# build_compact_stale_replace_repair_prompt — guidance_block injection
# ---------------------------------------------------------------------------


class TestStaleReplaceRepairPromptGuidanceBlock:
    def test_no_guidance_block_absent(self, tmp_path):
        prompt = build_compact_stale_replace_repair_prompt(
            task_description="Add report_label.",
            malformed_output=_BAD_PLAN,
            project_dir=tmp_path,
            rejection_reasons=_STALE_REASON,
            guidance_block="",
        )
        assert _REPAIR_GUIDANCE_HEADER not in prompt

    def test_guidance_block_present(self, tmp_path):
        prompt = build_compact_stale_replace_repair_prompt(
            task_description="Add report_label.",
            malformed_output=_BAD_PLAN,
            project_dir=tmp_path,
            rejection_reasons=_STALE_REASON,
            guidance_block=_STDOUT_GUIDANCE,
        )
        assert _REPAIR_GUIDANCE_HEADER in prompt

    def test_guidance_appears_before_validation_errors(self, tmp_path):
        prompt = build_compact_stale_replace_repair_prompt(
            task_description="Add report_label.",
            malformed_output=_BAD_PLAN,
            project_dir=tmp_path,
            rejection_reasons=_STALE_REASON,
            guidance_block=_STDOUT_GUIDANCE,
        )
        guidance_pos = prompt.index(_REPAIR_GUIDANCE_HEADER)
        error_pos = prompt.index("Validation errors:")
        assert guidance_pos < error_pos

    def test_stdout_guidance_content_included(self, tmp_path):
        prompt = build_compact_stale_replace_repair_prompt(
            task_description="Fix report_label.",
            malformed_output=_BAD_PLAN,
            project_dir=tmp_path,
            rejection_reasons=_STALE_REASON,
            guidance_block=_STDOUT_GUIDANCE,
        )
        assert "All runtime output must go to stdout" in prompt


# ---------------------------------------------------------------------------
# collect_repair_guidance_block
# ---------------------------------------------------------------------------


class TestCollectRepairGuidanceBlock:
    def _make_ctx(self, project_id=1, session_id=10, task_id=100, user_id=999):
        ctx = MagicMock()
        ctx.project.id = project_id
        ctx.project.user_id = user_id
        ctx.session_id = session_id
        ctx.task_id = task_id
        ctx.db = MagicMock()
        return ctx

    def test_passes_ctx_fields_to_render(self):
        from app.services.orchestration.phases.planning_guidance_enforcement import (
            collect_repair_guidance_block,
        )

        ctx = self._make_ctx()
        with patch(
            "app.services.human_guidance_plan_validator.render_active_guidance_for_repair",
            return_value="mock_block",
        ) as mock_render:
            result = collect_repair_guidance_block(ctx)

        assert result == "mock_block"
        mock_render.assert_called_once_with(
            ctx.db,
            project_id=ctx.project.id,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            user_id=ctx.project.user_id,
        )

    def test_returns_empty_string_on_render_error(self):
        from app.services.orchestration.phases.planning_guidance_enforcement import (
            collect_repair_guidance_block,
        )

        ctx = self._make_ctx()
        settings_mock = MagicMock()
        settings_mock.HUMAN_GUIDANCE_TABLE_ENABLED = True
        settings_mock.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED = True

        with patch("app.config.settings", settings_mock):
            with patch(
                "app.services.human_guidance_service.collect_active_guidance",
                side_effect=RuntimeError("db error"),
            ):
                result = collect_repair_guidance_block(ctx)

        assert result == ""
