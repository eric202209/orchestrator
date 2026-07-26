"""Phase 30G — nested-workspace planning/repair effectiveness.

Focused tests for:
  (A) the concrete workspace-prefix prohibition in the live planning prompts
  (B) the nested-workspace repair-guidance builder in repair_prompts.py
  (C) the nested_workspace_violation second-repair policy integration
  (D) repair-attempt observability wiring smoke test
  (E) validator characterization tests for nested-workspace detection,
      including the known false-positive class left unchanged/deferred.
"""

from pathlib import Path

from app.services.orchestration.phases.planning_flow import (
    _PlanningRetryState,
    _build_repair_rejection_reasons,
    _get_targeted_second_repair_reason,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.planning_prompts import (
    build_minimal_planning_prompt,
    build_ultra_minimal_planning_prompt,
)
from app.services.orchestration.planning.repair_prompts import (
    _build_nested_workspace_repair_guidance,
)
from app.services.orchestration.validation.validator import ValidatorService


# ── A. Base planning-prompt guidance ────────────────────────────────────


def test_minimal_planning_prompt_names_workspace_and_forbidden_prefix(tmp_path):
    project_dir = tmp_path / "todo"
    project_dir.mkdir()

    prompt = build_minimal_planning_prompt(
        "Build a todo app",
        project_dir,
    )

    assert 'workspace "todo"' in prompt
    assert '"todo/"' in prompt
    assert "mkdir todo" in prompt
    assert "cd todo" in prompt


def test_ultra_minimal_planning_prompt_names_workspace_and_forbidden_prefix(tmp_path):
    project_dir = tmp_path / "todo"
    project_dir.mkdir()

    prompt = build_ultra_minimal_planning_prompt(
        "Build a todo app",
        project_dir,
    )

    assert 'workspace "todo"' in prompt
    assert '"todo/"' in prompt
    assert "mkdir todo" in prompt
    assert "cd todo" in prompt


def test_planning_prompt_does_not_prohibit_unrelated_child_directories(tmp_path):
    project_dir = tmp_path / "todo"
    project_dir.mkdir()

    prompt = build_minimal_planning_prompt("Build a todo app", project_dir)

    # The rule targets the workspace-name prefix specifically, not every
    # child directory (mkdir src / cd src remain valid examples).
    assert "mkdir src" in prompt
    assert "cd src" in prompt


# ── B. Repair-guidance builder ──────────────────────────────────────────


def test_build_repair_rejection_reasons_names_workspace_and_quotes_offending_text():
    details = {
        "nested_workspace_steps": [1],
        "nested_workspace_name": "todo",
        "nested_workspace_prefix": "todo/",
        "nested_workspace_offending_fragments": {1: ["todo/app.py", "mkdir todo"]},
    }

    reasons = _build_repair_rejection_reasons([], details)

    combined = "\n".join(reasons)
    assert "nested_workspace_violation:" in combined
    assert 'workspace "todo"' in combined
    assert "todo/app.py" in combined
    assert "mkdir todo" in combined
    assert "cd todo" in combined


def test_build_repair_rejection_reasons_names_nested_project_root():
    details = {
        "nested_project_root_steps": [2],
        "nested_project_root_names": {2: "my-app"},
    }

    reasons = _build_repair_rejection_reasons([], details)

    combined = "\n".join(reasons)
    assert "nested_project_root_violation:" in combined
    assert '"my-app"' in combined


def test_nested_workspace_repair_guidance_builder_activates_and_names_prefix():
    rejection_reasons = [
        'nested_workspace_violation: You are already inside workspace "todo"; '
        "do not recreate or enter it. Offending text: step 1: todo/app.py, "
        "mkdir todo. Remove any `mkdir todo` / `cd todo` step and strip the "
        "`todo/` prefix from paths and commands so they are relative to the "
        'workspace root (e.g. "todo/app.py" becomes "app.py"). Preserve all '
        "other valid steps unchanged."
    ]

    guidance = _build_nested_workspace_repair_guidance(rejection_reasons)

    assert 'workspace "todo"' in guidance
    assert '"todo/"' in guidance
    assert "todo/app.py" in guidance
    assert "mkdir todo" in guidance and "cd todo" in guidance
    assert "app.py" in guidance
    assert "Preserve every other valid step" in guidance


def test_nested_workspace_repair_guidance_builder_handles_missing_fragments():
    rejection_reasons = [
        'nested_workspace_violation: You are already inside workspace "app"; '
        "do not recreate or enter it. Offending text: steps [1]. Remove any "
        "`mkdir app` / `cd app` step and strip the `app/` prefix from paths "
        "and commands so they are relative to the workspace root "
        '(e.g. "app/app.py" becomes "app.py"). Preserve all other valid '
        "steps unchanged."
    ]

    guidance = _build_nested_workspace_repair_guidance(rejection_reasons)

    assert 'workspace "app"' in guidance
    assert guidance  # does not crash / returns non-empty guidance


def test_nested_workspace_repair_guidance_builder_does_not_activate_for_unrelated():
    rejection_reasons = ["weak_verification_steps: steps [1] use weak verification"]

    guidance = _build_nested_workspace_repair_guidance(rejection_reasons)

    assert guidance == ""


def test_repair_prompt_includes_nested_workspace_guidance_block(tmp_path):
    project_dir = tmp_path / "todo"
    project_dir.mkdir()

    prompt = PlannerService.build_planning_repair_prompt(
        "Build a todo app",
        malformed_output='[{"step_number":1,"description":"scaffold","commands":['
        '"mkdir todo"],"verification":null,"rollback":null,"expected_files":'
        '["todo/app.py"]}]',
        project_dir=project_dir,
        rejection_reasons=[
            "Plan incorrectly recreates the current task workspace as a nested "
            "folder (steps: [1])",
            'nested_workspace_violation: You are already inside workspace "todo"; '
            "do not recreate or enter it. Offending text: step 1: todo/app.py, "
            "mkdir todo. Remove any `mkdir todo` / `cd todo` step and strip the "
            "`todo/` prefix from paths and commands so they are relative to the "
            'workspace root (e.g. "todo/app.py" becomes "app.py"). Preserve all '
            "other valid steps unchanged.",
        ],
    )

    assert 'workspace "todo"' in prompt
    assert "mkdir todo" in prompt


# ── C. Second-repair policy integration ─────────────────────────────────


def _verdict(details, reasons=None):
    return type("Verdict", (), {"reasons": reasons or [], "details": details})()


def test_nested_workspace_second_repair_fires_when_alone():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    verdict = _verdict(
        {
            "nested_workspace_steps": [1],
            "nested_workspace_name": "todo",
            "nested_workspace_prefix": "todo/",
            "semantic_violation_codes": ["nested_project_folder_command"],
        }
    )

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
    )

    assert reason is not None
    assert reason.issue_key == "nested_workspace_violation"
    assert reason.semantic_violation_code == "nested_project_folder_command"
    assert reason.cap_attribute == "post_repair_nested_workspace_second_repair_used"
    assert reason.cap_used is False
    assert 'workspace "todo"' in reason.rejection_text


def test_nested_workspace_second_repair_blocked_by_incompatible_issue():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    verdict = _verdict(
        {
            "nested_workspace_steps": [1],
            "nested_workspace_name": "todo",
            "test_deletion_ops_steps": [2],
            "semantic_violation_codes": ["nested_project_folder_command"],
        }
    )

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
    )

    assert reason is None


def test_nested_workspace_second_repair_deferred_to_task1_bootstrap_contract():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    verdict = _verdict(
        {
            "nested_workspace_steps": [1],
            "nested_workspace_name": "todo",
            "task1_bootstrap_contract": {"passed": False, "violation_codes": ["x"]},
            "semantic_violation_codes": ["nested_project_folder_command"],
        }
    )

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
    )

    # Task-1 bootstrap contract owns this repair path instead.
    assert reason is not None
    assert reason.issue_key == "task1_bootstrap_contract"


def test_nested_workspace_second_repair_respects_cap():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    retry_state.post_repair_nested_workspace_second_repair_used = True
    verdict = _verdict(
        {
            "nested_project_root_steps": [3],
            "nested_project_root_names": {3: "my-app"},
            "semantic_violation_codes": ["nested_project_folder_command"],
        }
    )

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
    )

    assert reason is not None
    assert reason.cap_used is True


def test_nested_workspace_second_repair_requires_prior_repair():
    retry_state = _PlanningRetryState()
    verdict = _verdict(
        {
            "nested_workspace_steps": [1],
            "nested_workspace_name": "todo",
            "semantic_violation_codes": ["nested_project_folder_command"],
        }
    )

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
    )

    assert reason is None


# ── D. Observability smoke test ─────────────────────────────────────────


def test_record_and_emit_pending_repair_outcome_reports_resolution():
    from app.services.orchestration.phases.planning_support import (
        _record_pending_repair_outcome,
        _emit_repair_outcome_if_pending,
    )

    retry_state = _PlanningRetryState()
    _record_pending_repair_outcome(
        retry_state,
        attempt_number=1,
        original_violation_codes=["nested_project_folder_command"],
        targeted_violation_code="nested_project_folder_command",
    )
    assert retry_state.pending_repair_outcome_attempt_number == 1

    events = []
    ctx = type(
        "Ctx",
        (),
        {
            "session_id": 1,
            "task_id": 2,
            "emit_live": staticmethod(
                lambda level, message, metadata=None: events.append(
                    (level, message, metadata)
                )
            ),
        },
    )()
    resolved_verdict = _verdict({"semantic_violation_codes": []})

    _emit_repair_outcome_if_pending(ctx, retry_state, resolved_verdict)

    assert len(events) == 1
    level, message, metadata = events[0]
    assert level == "INFO"
    assert metadata["repair_attempt_number"] == 1
    assert metadata["targeted_violation_code"] == "nested_project_folder_command"
    assert metadata["target_violation_resolved"] is True
    assert metadata["same_violation_repeated"] is False
    # Pending marker is cleared so the next validation pass does not
    # double-report.
    assert retry_state.pending_repair_outcome_attempt_number == 0


def test_emit_pending_repair_outcome_reports_repeated_violation():
    from app.services.orchestration.phases.planning_support import (
        _record_pending_repair_outcome,
        _emit_repair_outcome_if_pending,
    )

    retry_state = _PlanningRetryState()
    _record_pending_repair_outcome(
        retry_state,
        attempt_number=2,
        original_violation_codes=["nested_project_folder_command"],
        targeted_violation_code="nested_project_folder_command",
    )
    events = []
    ctx = type(
        "Ctx",
        (),
        {
            "session_id": 1,
            "task_id": 2,
            "emit_live": staticmethod(
                lambda level, message, metadata=None: events.append(
                    (level, message, metadata)
                )
            ),
        },
    )()
    repeated_verdict = _verdict(
        {"semantic_violation_codes": ["nested_project_folder_command"]}
    )

    _emit_repair_outcome_if_pending(ctx, retry_state, repeated_verdict)

    level, _message, metadata = events[0]
    assert level == "WARN"
    assert metadata["target_violation_resolved"] is False
    assert metadata["same_violation_repeated"] is True


# ── E. Validator characterization tests ─────────────────────────────────


def _plan_step(**kwargs):
    step = {
        "step_number": 1,
        "description": "step",
        "commands": [],
        "verification": None,
        "rollback": None,
        "expected_files": [],
    }
    step.update(kwargs)
    return [step]


def test_nested_workspace_bare_mkdir_without_slash_not_matched_by_substring_rule(
    tmp_path,
):
    # Phase 30H: root recreation commands are rejected even without a
    # following path segment.
    project_dir = tmp_path / "todo"
    project_dir.mkdir()
    plan = _plan_step(commands=["mkdir todo"])

    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)

    assert steps == [1]


def test_nested_workspace_bare_cd_without_slash_not_matched_by_substring_rule(
    tmp_path,
):
    project_dir = tmp_path / "todo"
    project_dir.mkdir()
    plan = _plan_step(commands=["cd todo && ls"])

    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)

    assert steps == [1]


def test_nested_workspace_single_prefixed_expected_file_is_legitimate_package(
    tmp_path,
):
    # Phase 30J: a single same-name module file under the alias (no root
    # mkdir/cd, no repository marker) is a legitimate package/module target,
    # not duplicate-root scaffold evidence.
    project_dir = tmp_path / "todo"
    project_dir.mkdir()
    plan = _plan_step(expected_files=["todo/app.py"])

    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)

    assert steps == []


def test_nested_workspace_scaffold_creation_detected(tmp_path):
    project_dir = tmp_path / "todo"
    project_dir.mkdir()
    plan = _plan_step(
        commands=["mkdir todo"],
        expected_files=["todo/app.py", "todo/requirements.txt"],
    )

    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)

    assert steps == [1]


def test_nested_workspace_similar_name_does_not_match(tmp_path):
    # "todo" workspace should not false-positive on "todo-cli" paths.
    project_dir = tmp_path / "todo"
    project_dir.mkdir()
    plan = _plan_step(expected_files=["todo-cli/app.py"])

    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)

    assert steps == []


def test_nested_workspace_absolute_path_variant_not_double_counted(tmp_path):
    # Absolute paths are caught by the separate unsafe-path rule, not this one.
    project_dir = tmp_path / "todo"
    project_dir.mkdir()
    plan = _plan_step(commands=["cat /etc/passwd"])

    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)

    assert steps == []


def test_nested_workspace_traversal_variant_not_flagged_by_this_rule(tmp_path):
    project_dir = tmp_path / "todo"
    project_dir.mkdir()
    plan = _plan_step(commands=["cat ../todo/secret"])

    # Phase 30J: a read-only command referencing "todo/" with nothing
    # materialized under it carries no duplicate-root-scaffold evidence, so
    # this rule no longer fires here. The traversal itself is still rejected
    # separately by _plan_contains_unsafe_command_paths (unsafe ../ segments).
    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)

    assert steps == []


def test_nested_workspace_existing_in_place_directory_is_allowed(tmp_path):
    project_dir = tmp_path / "app"
    project_dir.mkdir()
    (project_dir / "app").mkdir()  # existing in-place directory named "app"
    plan = _plan_step(expected_files=["app/routes.py"])

    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)

    assert steps == []


def test_nested_workspace_offending_fragments_quote_exact_text(tmp_path):
    project_dir = tmp_path / "todo"
    project_dir.mkdir()
    plan = [
        {
            "step_number": 1,
            "commands": ["mkdir todo", "cd todo"],
            "verification": None,
            "rollback": None,
            "expected_files": ["todo/app.py"],
        }
    ]

    fragments = ValidatorService._plan_nested_workspace_offending_fragments(
        plan, project_dir
    )

    assert 1 in fragments
    assert "mkdir todo" in fragments[1]
    assert "cd todo" in fragments[1]
    assert "todo/app.py" in fragments[1]


def test_validate_plan_populates_nested_workspace_structured_details(tmp_path):
    project_dir = tmp_path / "todo"
    project_dir.mkdir()
    plan = [
        {
            "step_number": 1,
            "description": "scaffold",
            "commands": ["mkdir todo"],
            "verification": None,
            "rollback": None,
            "expected_files": ["todo/app.py"],
        },
        {
            "step_number": 2,
            "description": "verify",
            "commands": ["python -m pytest"],
            "verification": "python -m pytest",
            "rollback": None,
            "expected_files": [],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="",
        task_prompt="Build a todo app",
        execution_profile="full_lifecycle",
        project_dir=project_dir,
    )

    assert verdict.details.get("nested_workspace_name") == "todo"
    assert verdict.details.get("nested_workspace_prefix") == "todo/"
    assert 1 in verdict.details.get("nested_workspace_offending_fragments", {})
    assert "nested_project_folder_command" in verdict.details.get(
        "semantic_violation_codes", []
    )
