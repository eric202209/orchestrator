"""Phase 30J — package-vs-root intent semantics and repair-outcome integrity.

Focused regression coverage for the two Phase 30I defects:

  (A) legitimate same-name packages/directories were falsely rejected as
      duplicate project roots.
  (B) structured repair-outcome metadata could report a target violation as
      resolved while the final authoritative diagnostics still contained it.
"""

from __future__ import annotations

from app.services.orchestration.phases.planning_support import (
    _PlanningRetryState,
    _emit_repair_outcome_if_pending,
    _record_pending_repair_outcome,
    compute_final_repair_outcome,
    emit_final_repair_outcome_summary,
)
from app.services.orchestration.validation.validator import ValidatorService


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


def _verdict(details, reasons=None):
    return type("Verdict", (), {"reasons": reasons or [], "details": details})()


def _events_ctx():
    events: list[tuple[str, str, dict]] = []
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
    return ctx, events


# ── Package-versus-root intent ──────────────────────────────────────────


def test_new_same_name_python_package_allowed(tmp_path):
    project_dir = tmp_path / "inventory_api"
    project_dir.mkdir()
    plan = _plan_step(
        expected_files=[
            "inventory_api/__init__.py",
            "inventory_api/routes.py",
            "inventory_api/service.py",
        ]
    )
    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)
    assert steps == []


def test_existing_same_name_python_package_allowed(tmp_path):
    project_dir = tmp_path / "inventory_api"
    project_dir.mkdir()
    (project_dir / "inventory_api").mkdir()
    plan = _plan_step(expected_files=["inventory_api/routes.py"])
    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)
    assert steps == []


def test_same_name_package_with_root_level_pyproject_allowed(tmp_path):
    project_dir = tmp_path / "inventory_api"
    project_dir.mkdir()
    plan = _plan_step(
        expected_files=[
            "pyproject.toml",
            "inventory_api/__init__.py",
            "inventory_api/service.py",
        ]
    )
    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)
    assert steps == []


def test_same_name_package_plus_root_level_tests_allowed(tmp_path):
    project_dir = tmp_path / "inventory_api"
    project_dir.mkdir()
    plan = _plan_step(
        expected_files=[
            "tests/test_service.py",
            "inventory_api/__init__.py",
            "inventory_api/service.py",
        ]
    )
    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)
    assert steps == []


def test_duplicate_root_mkdir_rejected(tmp_path):
    project_dir = tmp_path / "inventory_api"
    project_dir.mkdir()
    plan = _plan_step(commands=["mkdir inventory_api"])
    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)
    assert steps == [1]


def test_duplicate_root_cd_rejected(tmp_path):
    project_dir = tmp_path / "inventory_api"
    project_dir.mkdir()
    plan = _plan_step(commands=["cd inventory_api"])
    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)
    assert steps == [1]


def test_alias_prefixed_independent_pyproject_rejected(tmp_path):
    project_dir = tmp_path / "inventory_api"
    project_dir.mkdir()
    plan = _plan_step(
        expected_files=[
            "inventory_api/pyproject.toml",
            "inventory_api/__init__.py",
        ]
    )
    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)
    assert steps == [1]


def test_alias_prefixed_full_scaffold_rejected(tmp_path):
    project_dir = tmp_path / "inventory_api"
    project_dir.mkdir()
    plan = _plan_step(
        expected_files=[
            "inventory_api/src/main.py",
            "inventory_api/tests/test_main.py",
            "inventory_api/README.md",
        ]
    )
    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)
    assert steps == [1]


def test_similar_but_non_identical_alias_allowed(tmp_path):
    project_dir = tmp_path / "inventory_api"
    project_dir.mkdir()
    plan = _plan_step(expected_files=["inventory_api_v2/__init__.py"])
    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)
    assert steps == []


def test_existing_legitimate_package_not_rejected_via_full_validate(tmp_path):
    project_dir = tmp_path / "inventory_api"
    project_dir.mkdir()
    (project_dir / "inventory_api").mkdir()
    (project_dir / "inventory_api" / "__init__.py").write_text("")
    plan = [
        {
            "step_number": 1,
            "description": "Add a route",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "inventory_api/routes.py",
                    "content": "def list_items():\n    return []\n",
                }
            ],
            "verification": 'python -c "import inventory_api.routes"',
            "rollback": None,
            "expected_files": ["inventory_api/routes.py"],
        }
    ]
    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Add a route",
        execution_profile="full_lifecycle",
        project_dir=project_dir,
    )
    assert "nested_project_folder_command" not in (
        verdict.details.get("semantic_violation_codes") or []
    )


def test_new_legitimate_semantic_package_not_rejected_via_full_validate(tmp_path):
    project_dir = tmp_path / "inventory_api_i1"
    project_dir.mkdir()
    plan = [
        {
            "step_number": 1,
            "description": "Create the package",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "inventory_api_i1/__init__.py",
                    "content": "",
                },
                {
                    "op": "write_file",
                    "path": "inventory_api_i1/routes.py",
                    "content": "def list_items():\n    return []\n",
                },
                {
                    "op": "write_file",
                    "path": "inventory_api_i1/service.py",
                    "content": "def get_items():\n    return []\n",
                },
            ],
            "verification": 'python -c "import inventory_api_i1"',
            "rollback": None,
            "expected_files": [
                "inventory_api_i1/__init__.py",
                "inventory_api_i1/routes.py",
                "inventory_api_i1/service.py",
            ],
        }
    ]
    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Create a small Python API package",
        execution_profile="full_lifecycle",
        project_dir=project_dir,
    )
    assert "nested_project_folder_command" not in (
        verdict.details.get("semantic_violation_codes") or []
    )
    assert not verdict.details.get("nested_project_root_steps")


def test_numeric_child_directory_allowed(tmp_path):
    project_dir = tmp_path / "1"
    project_dir.mkdir()
    plan = _plan_step(expected_files=["fixtures/1/sample.json"])
    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)
    assert steps == []


def test_numeric_duplicate_root_scaffold_rejected(tmp_path):
    project_dir = tmp_path / "1"
    project_dir.mkdir()
    plan = _plan_step(commands=["mkdir 1", "cd 1"])
    steps = ValidatorService._plan_nests_task_workspace(plan, project_dir)
    assert steps == [1]


def test_absolute_path_still_rejected(tmp_path):
    project_dir = tmp_path / "inventory_api"
    project_dir.mkdir()
    plan = _plan_step(expected_files=["/etc/passwd"])
    invalid = ValidatorService._plan_contains_unsafe_paths(plan)
    assert invalid == ["/etc/passwd"]


def test_traversal_path_still_rejected(tmp_path):
    project_dir = tmp_path / "inventory_api"
    project_dir.mkdir()
    plan = _plan_step(expected_files=["../outside/secret.py"])
    invalid = ValidatorService._plan_contains_unsafe_paths(plan)
    assert invalid == ["../outside/secret.py"]


def test_genuine_root_recreation_still_produces_actionable_fragments(tmp_path):
    project_dir = tmp_path / "inventory_api"
    project_dir.mkdir()
    plan = _plan_step(
        commands=["mkdir inventory_api"],
        expected_files=["inventory_api/pyproject.toml"],
    )
    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="task",
        execution_profile="full_lifecycle",
        project_dir=project_dir,
    )
    fragments = verdict.details.get("nested_workspace_offending_fragments") or {}
    assert fragments.get(1)


def test_corrected_fragments_strip_prefix_not_legitimate_package_paths(tmp_path):
    from app.services.orchestration.validation.rules.core_paths import (
        _plan_nested_workspace_corrected_fragments,
    )

    fragments = {1: ["mkdir inventory_api", "inventory_api/pyproject.toml"]}
    aliases = {1: "inventory_api"}
    corrected = _plan_nested_workspace_corrected_fragments(fragments, aliases)
    assert corrected[1] == ["remove `mkdir inventory_api`", "pyproject.toml"]


# ── Repair outcome integrity ────────────────────────────────────────────


def test_target_disappears_after_repair_resolved_true():
    retry_state = _PlanningRetryState()
    _record_pending_repair_outcome(
        retry_state,
        attempt_number=1,
        original_violation_codes=["nested_project_folder_command"],
        targeted_violation_code="nested_project_folder_command",
    )
    ctx, events = _events_ctx()
    _emit_repair_outcome_if_pending(
        ctx, retry_state, _verdict({"semantic_violation_codes": []})
    )
    assert events[0][2]["target_violation_resolved"] is True
    assert events[0][2]["same_violation_repeated"] is False


def test_target_remains_after_repair_resolved_false_repeated_true():
    retry_state = _PlanningRetryState()
    _record_pending_repair_outcome(
        retry_state,
        attempt_number=1,
        original_violation_codes=["nested_project_folder_command"],
        targeted_violation_code="nested_project_folder_command",
    )
    ctx, events = _events_ctx()
    _emit_repair_outcome_if_pending(
        ctx,
        retry_state,
        _verdict({"semantic_violation_codes": ["nested_project_folder_command"]}),
    )
    assert events[0][2]["target_violation_resolved"] is False
    assert events[0][2]["same_violation_repeated"] is True


def test_final_diagnostics_retain_target_no_success_claim():
    attempts = [
        {
            "repair_attempt_number": 1,
            "targeted_violation_code": "nested_project_folder_command",
            "pre_repair_violation_codes": ["nested_project_folder_command"],
            "post_repair_violation_codes": [],
            "target_violation_resolved": True,
            "same_violation_repeated": False,
        }
    ]
    summary = compute_final_repair_outcome(
        attempts, final_violation_codes=["nested_project_folder_command"]
    )
    entry = summary["nested_project_folder_command"]
    assert entry["target_final_status"] != "RESOLVED"
    assert entry["target_present_in_final_verdict"] is True


def test_intermediate_verdict_removes_target_but_final_arbitration_retains_it():
    """The exact Phase 30I I1 inconsistency shape, reproduced then fixed.

    Attempt 1 resolves nested_project_folder_command; attempt 2 targets an
    unrelated violation and reintroduces the target. The naive aggregation
    (any() over attempt-time resolved flags, last-attempt post codes) would
    report resolved=true while the final codes still contain the target —
    `compute_final_repair_outcome` must not.
    """

    attempts = [
        {
            "repair_attempt_number": 1,
            "targeted_violation_code": "nested_project_folder_command",
            "pre_repair_violation_codes": ["nested_project_folder_command"],
            "post_repair_violation_codes": [],
            "target_violation_resolved": True,
            "same_violation_repeated": False,
        },
        {
            "repair_attempt_number": 2,
            "targeted_violation_code": "weak_verification",
            "pre_repair_violation_codes": ["weak_verification"],
            "post_repair_violation_codes": ["nested_project_folder_command"],
            "target_violation_resolved": True,
            "same_violation_repeated": False,
        },
    ]
    final_codes = ["nested_project_folder_command"]
    summary = compute_final_repair_outcome(attempts, final_codes)

    nested_entry = summary["nested_project_folder_command"]
    assert nested_entry["target_present_in_final_verdict"] is True
    assert nested_entry["target_final_status"] == "OUTCOME_INCONSISTENT"
    assert nested_entry["repair_outcome_consistent"] is False

    weak_entry = summary["weak_verification"]
    assert weak_entry["target_final_status"] == "RESOLVED"


def test_target_replaced_by_a_different_blocker():
    attempts = [
        {
            "repair_attempt_number": 1,
            "targeted_violation_code": "nested_project_folder_command",
            "pre_repair_violation_codes": ["nested_project_folder_command"],
            "post_repair_violation_codes": ["weak_verification"],
            "target_violation_resolved": True,
            "same_violation_repeated": False,
        }
    ]
    summary = compute_final_repair_outcome(
        attempts, final_violation_codes=["weak_verification"]
    )
    entry = summary["nested_project_folder_command"]
    assert entry["target_final_status"] == "RESOLVED"
    assert entry["target_present_in_final_verdict"] is False


def test_two_repair_attempts_produce_two_distinct_records():
    retry_state = _PlanningRetryState()
    ctx, _events = _events_ctx()
    _record_pending_repair_outcome(
        retry_state,
        attempt_number=1,
        original_violation_codes=["nested_project_folder_command"],
        targeted_violation_code="nested_project_folder_command",
    )
    _emit_repair_outcome_if_pending(
        ctx, retry_state, _verdict({"semantic_violation_codes": ["weak_verification"]})
    )
    _record_pending_repair_outcome(
        retry_state,
        attempt_number=2,
        original_violation_codes=["weak_verification"],
        targeted_violation_code="weak_verification",
    )
    _emit_repair_outcome_if_pending(
        ctx, retry_state, _verdict({"semantic_violation_codes": []})
    )
    assert len(retry_state.repair_attempt_outcomes) == 2
    assert retry_state.repair_attempt_outcomes[0]["repair_attempt_number"] == 1
    assert retry_state.repair_attempt_outcomes[1]["repair_attempt_number"] == 2


def test_attempt_two_does_not_overwrite_attempt_one():
    retry_state = _PlanningRetryState()
    ctx, _events = _events_ctx()
    _record_pending_repair_outcome(
        retry_state,
        attempt_number=1,
        original_violation_codes=["nested_project_folder_command"],
        targeted_violation_code="nested_project_folder_command",
    )
    _emit_repair_outcome_if_pending(
        ctx, retry_state, _verdict({"semantic_violation_codes": []})
    )
    first_record = dict(retry_state.repair_attempt_outcomes[0])
    _record_pending_repair_outcome(
        retry_state,
        attempt_number=2,
        original_violation_codes=["weak_verification"],
        targeted_violation_code="weak_verification",
    )
    _emit_repair_outcome_if_pending(
        ctx,
        retry_state,
        _verdict({"semantic_violation_codes": ["nested_project_folder_command"]}),
    )
    assert retry_state.repair_attempt_outcomes[0] == first_record


def test_pending_outcome_finalized_on_clean_success():
    retry_state = _PlanningRetryState()
    ctx, events = _events_ctx()
    _record_pending_repair_outcome(
        retry_state,
        attempt_number=1,
        original_violation_codes=["nested_project_folder_command"],
        targeted_violation_code="nested_project_folder_command",
    )
    resolved_verdict = _verdict({"semantic_violation_codes": []})
    _emit_repair_outcome_if_pending(ctx, retry_state, resolved_verdict)
    emit_final_repair_outcome_summary(ctx, retry_state, resolved_verdict)
    final_events = [e for e in events if "PLANNING_REPAIR_OUTCOME_FINAL" in e[1]]
    assert len(final_events) == 1
    assert final_events[0][0] == "INFO"
    summary = final_events[0][2]["target_outcomes"]
    assert summary["nested_project_folder_command"]["target_final_status"] == "RESOLVED"


def test_pending_outcome_finalized_on_bounded_terminal_failure():
    retry_state = _PlanningRetryState()
    ctx, events = _events_ctx()
    _record_pending_repair_outcome(
        retry_state,
        attempt_number=1,
        original_violation_codes=["nested_project_folder_command"],
        targeted_violation_code="nested_project_folder_command",
    )
    repeated_verdict = _verdict(
        {"semantic_violation_codes": ["nested_project_folder_command"]}
    )
    _emit_repair_outcome_if_pending(ctx, retry_state, repeated_verdict)
    emit_final_repair_outcome_summary(ctx, retry_state, repeated_verdict)
    final_events = [e for e in events if "PLANNING_REPAIR_OUTCOME_FINAL" in e[1]]
    assert len(final_events) == 1
    assert final_events[0][0] == "WARN"
    summary = final_events[0][2]["target_outcomes"]
    assert (
        summary["nested_project_folder_command"]["target_final_status"]
        == "REPEATED_AND_EXHAUSTED"
    )


def test_pending_outcome_safely_marked_inconsistent_without_authoritative_verdict():
    attempts = [
        {
            "repair_attempt_number": 1,
            "targeted_violation_code": "nested_project_folder_command",
            "pre_repair_violation_codes": ["nested_project_folder_command"],
            "post_repair_violation_codes": [],
            "target_violation_resolved": True,
            "same_violation_repeated": False,
        }
    ]
    # No final_violation_codes available (None coerces to empty) must not
    # crash; it degrades to "resolved" rather than raising.
    summary = compute_final_repair_outcome(attempts, final_violation_codes=None)
    assert summary["nested_project_folder_command"]["target_final_status"] == "RESOLVED"


def test_resolved_and_repeated_cannot_both_be_true():
    retry_state = _PlanningRetryState()
    ctx, events = _events_ctx()
    for codes in (["nested_project_folder_command"], []):
        _record_pending_repair_outcome(
            retry_state,
            attempt_number=1,
            original_violation_codes=["nested_project_folder_command"],
            targeted_violation_code="nested_project_folder_command",
        )
        _emit_repair_outcome_if_pending(
            ctx, retry_state, _verdict({"semantic_violation_codes": codes})
        )
    for _level, _message, metadata in events:
        assert not (
            metadata["target_violation_resolved"]
            and metadata["same_violation_repeated"]
        )


def test_aggregate_target_outcome_matches_individual_records():
    retry_state = _PlanningRetryState()
    ctx, _events = _events_ctx()
    _record_pending_repair_outcome(
        retry_state,
        attempt_number=1,
        original_violation_codes=["nested_project_folder_command"],
        targeted_violation_code="nested_project_folder_command",
    )
    final_verdict = _verdict({"semantic_violation_codes": []})
    _emit_repair_outcome_if_pending(ctx, retry_state, final_verdict)
    summary = compute_final_repair_outcome(
        retry_state.repair_attempt_outcomes,
        final_verdict.details["semantic_violation_codes"],
    )
    record = retry_state.repair_attempt_outcomes[0]
    entry = summary["nested_project_folder_command"]
    assert entry["target_repair_recoveries"] == (
        1 if record["target_violation_resolved"] else 0
    )
    assert entry["target_repair_attempts"] == 1


def test_phase30i_exact_inconsistency_reproduced_then_fixed():
    """Regression guard for the literal Phase 30I evidence shape.

    Reproduces: repair outcome reports target_violation_resolved=true,
    same_violation_repeated=false for nested_project_folder_command, while
    the final diagnostics still contain it (caused by a second,
    differently-targeted repair attempt). Before the Phase 30J fix an
    aggregation based on `any()` over per-attempt resolved flags plus the
    last attempt's post-repair codes would report this as resolved; the
    authoritative aggregate must not.
    """

    attempt_one_resolved = True
    attempt_one_repeated = False
    final_diagnostics_contains_target = True

    attempts = [
        {
            "repair_attempt_number": 1,
            "targeted_violation_code": "nested_project_folder_command",
            "pre_repair_violation_codes": ["nested_project_folder_command"],
            "post_repair_violation_codes": [],
            "target_violation_resolved": attempt_one_resolved,
            "same_violation_repeated": attempt_one_repeated,
        },
        {
            "repair_attempt_number": 2,
            "targeted_violation_code": "weak_verification",
            "pre_repair_violation_codes": ["weak_verification"],
            "post_repair_violation_codes": ["nested_project_folder_command"],
            "target_violation_resolved": True,
            "same_violation_repeated": False,
        },
    ]
    final_codes = (
        ["nested_project_folder_command"] if final_diagnostics_contains_target else []
    )

    summary = compute_final_repair_outcome(attempts, final_codes)
    entry = summary["nested_project_folder_command"]
    assert entry["target_present_in_final_verdict"] is True
    assert entry["target_final_status"] in {
        "OUTCOME_INCONSISTENT",
        "REPEATED_AND_EXHAUSTED",
    }
    assert entry["target_final_status"] != "RESOLVED"
