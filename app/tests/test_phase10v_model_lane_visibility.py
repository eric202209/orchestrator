"""Phase 10V: Model-lane routing and operator recovery UX tests."""

from __future__ import annotations

from pathlib import Path

from app.services.orchestration.phases.planning_support import (
    _extract_stale_old_text_from_plan,
    _model_lane_limitation_for_invalid_planning_commands,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.normalization import (
    normalize_stale_replace_ops_to_small_file_writes,
)
from app.services.orchestration.planning.repair_prompts import (
    PLANNING_REPAIR_PROMPT_MAX_CHARS,
)
from app.services.session.session_inspection_service import (
    _RECOVERY_ACTION_MAP,
    _extract_model_lane_limitation,
    _stronger_lane_summary,
    _stronger_lane_available,
)
from app.config import settings


# --- V1: Operator visibility ---


def test_recovery_action_map_has_model_lane_limitation_entry():
    assert "model_lane_limitation" in _RECOVERY_ACTION_MAP
    actions = _RECOVERY_ACTION_MAP["model_lane_limitation"]
    action_names = [a["action"] for a in actions]
    assert "diagnostics" in action_names
    assert "retry_stronger_lane" in action_names
    assert "rollback" in action_names


def test_model_lane_limitation_action_requires_stronger_lane_flag():
    actions = _RECOVERY_ACTION_MAP["model_lane_limitation"]
    retry = next(a for a in actions if a["action"] == "retry_stronger_lane")
    assert retry.get("requires_stronger_lane") is True


def test_review_manually_is_primary_in_model_lane_action_map():
    actions = _RECOVERY_ACTION_MAP["model_lane_limitation"]
    primary = next(a for a in actions if a["variant"] == "primary")
    assert primary["action"] == "diagnostics"


def test_extract_model_lane_limitation_from_error_string():
    error = (
        "Planning repair still produced invalid commands: stale_replace_ops_steps=['2']; "
        "model_lane_limitation=repeated_stale_exact_patch_after_capsule; runtime_rewrite_added=false"
    )
    result = _extract_model_lane_limitation(error)
    assert result == "repeated_stale_exact_patch_after_capsule"


def test_extract_model_lane_limitation_from_bucket_marker():
    error = "model_lane_repeated_stale_exact_patch was triggered after bounded repair"
    result = _extract_model_lane_limitation(error)
    assert result == "repeated_stale_exact_patch_after_capsule"


def test_extract_model_lane_limitation_returns_none_for_unrelated_error():
    error = "Planning failed: json_parse_failed: unexpected token"
    result = _extract_model_lane_limitation(error)
    assert result is None


def test_extract_model_lane_limitation_handles_empty():
    assert _extract_model_lane_limitation(None) is None
    assert _extract_model_lane_limitation("") is None


# --- V2: Capability lane labeling ---


def test_model_lane_limitation_marker_produced_for_stale_replace_issue():
    result = _model_lane_limitation_for_invalid_planning_commands(
        {"stale_replace_ops_steps": [2]}
    )
    assert result is not None
    assert result["model_lane_limitation"] == "repeated_stale_exact_patch_after_capsule"
    assert result["failure_cause_bucket"] == "model_lane_repeated_stale_exact_patch"


def test_model_lane_limitation_marker_not_produced_for_non_stale_issues():
    result = _model_lane_limitation_for_invalid_planning_commands(
        {"non_runnable_steps": [1]}
    )
    assert result is None


def test_stronger_lane_available_returns_false_by_default():
    # Without AGENT_SECONDARY_BACKEND configured, should return False
    assert _stronger_lane_available() is False


def test_stronger_lane_summary_represents_unavailable_lane(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_SECONDARY_BACKEND", None)

    summary = _stronger_lane_summary()

    assert summary["configured"] is False
    assert summary["available"] is False
    assert summary["label"] == "unavailable"
    assert summary["capability_traits"]["configured_available"] is False


def test_stronger_lane_summary_rejects_unknown_backend(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_SECONDARY_BACKEND", "unknown_backend")

    summary = _stronger_lane_summary()

    assert summary["configured"] is True
    assert summary["available"] is False
    assert summary["label"] == "unsupported"


# --- V4: No source mutation before safe stop ---


def test_stale_replace_normalization_does_not_mutate_multi_function_file(
    tmp_path: Path,
):
    """normalize_stale_replace_ops_to_small_file_writes must not write
    when the target file has multiple functions (guards prevent conversion)."""
    target = tmp_path / "calculator.py"
    original_content = (
        "def calculate_totals(entries):\n"
        "    return sum(e['amount'] for e in entries)\n"
        "\n"
        "def calculate_refunds(entries):\n"
        "    return sum(e.get('refund', 0) for e in entries)\n"
        "\n"
        "def net_balance(entries):\n"
        "    return calculate_totals(entries) - calculate_refunds(entries)\n"
    )
    target.write_text(original_content, encoding="utf-8")

    plan = [
        {
            "step_number": 2,
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "calculator.py",
                    "old": "reimbursable_total -= refund_amount",
                    "new": "reimbursable_total = max(0, reimbursable_total - refund_amount)",
                }
            ],
        }
    ]

    normalized, details = normalize_stale_replace_ops_to_small_file_writes(
        plan, project_dir=tmp_path
    )

    # The file must not have been converted (multiple functions → guards prevent it)
    assert details["changed"] is False
    assert target.read_text(encoding="utf-8") == original_content
    # The op must still be replace_in_file (not converted to write_file)
    op = normalized[0]["ops"][0]
    assert op["op"] == "replace_in_file"


def test_stale_replace_normalization_does_not_mutate_when_old_text_absent_from_large_file(
    tmp_path: Path,
):
    """Normalization must not write to files exceeding the line-count guard."""
    target = tmp_path / "ledger.py"
    lines = ["def func_{}(): pass\n".format(i) for i in range(90)]
    target.write_text("".join(lines), encoding="utf-8")

    plan = [
        {
            "step_number": 1,
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "ledger.py",
                    "old": "reimbursable_total -= refund_amount",
                    "new": "reimbursable_total = max(0, reimbursable_total - refund_amount)",
                }
            ],
        }
    ]

    _, details = normalize_stale_replace_ops_to_small_file_writes(
        plan, project_dir=tmp_path
    )
    assert details["changed"] is False


# --- V4: Prompt size budget ---


def test_repair_prompt_with_capsule_stays_within_budget(tmp_path: Path):
    """Repair prompt including capsule and model-lane stop context must stay below 6000 chars."""
    (tmp_path / "src" / "ledger_app").mkdir(parents=True)
    for i in range(50):
        (tmp_path / "src" / "ledger_app" / f"m{i}.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='ledger'\n")

    prompt = PlannerService.build_planning_repair_prompt(
        task_description="Fix the ledger calculator to handle refunds correctly.",
        malformed_output=(
            '[{"step_number":2,"ops":[{"op":"replace_in_file","path":"src/ledger_app/calculator.py",'
            '"old":"reimbursable_total -= refund_amount",'
            '"new":"reimbursable_total = max(0, reimbursable_total - refund_amount)"}]}]'
        ),
        project_dir=tmp_path,
        rejection_reasons=[
            "replace_in_file old text not found in src/ledger_app/calculator.py",
            "stale_replace_ops_steps: use identifiers from current file excerpt",
        ],
    )

    assert len(prompt) <= PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert "PROJECT STRUCTURE CAPSULE" in prompt
    assert "`replace_in_file` is only for exact old text" in prompt


# --- V3: Rerun payload evidence ---


def test_extract_stale_old_text_returns_matching_ops():
    plan = [
        {
            "step_number": 2,
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/calculator.py",
                    "old": "reimbursable_total -= refund_amount",
                    "new": "reimbursable_total = max(0, reimbursable_total - refund_amount)",
                }
            ],
        },
        {
            "step_number": 3,
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/other.py",
                    "old": "other_old_text",
                    "new": "other_new_text",
                }
            ],
        },
    ]
    result = _extract_stale_old_text_from_plan(plan, stale_step_numbers=[2])
    assert result == ["reimbursable_total -= refund_amount"]


def test_extract_stale_old_text_ignores_non_stale_steps():
    plan = [
        {
            "step_number": 1,
            "ops": [{"op": "run_command", "command": "pytest"}],
        },
        {
            "step_number": 2,
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/calc.py",
                    "old": "stale text",
                    "new": "new text",
                }
            ],
        },
    ]
    result = _extract_stale_old_text_from_plan(plan, stale_step_numbers=[1])
    assert result == []


def test_extract_stale_old_text_returns_empty_for_none_inputs():
    assert _extract_stale_old_text_from_plan(None, None) == []
    assert _extract_stale_old_text_from_plan([], [2]) == []
    assert (
        _extract_stale_old_text_from_plan([{"step_number": 2, "ops": []}], None) == []
    )
