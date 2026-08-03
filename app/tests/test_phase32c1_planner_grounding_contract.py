"""Phase 32C planner source-grounding and eager-materialization contract tests."""

import hashlib
import json

from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.repair_arbitration import (
    classify_planning_repair_candidate,
)
from app.services.orchestration.planning.repair_prompts import (
    build_planning_repair_prompt,
)
from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_EXISTING,
    SOURCE_STATUS_MISSING,
    SOURCE_STATUS_NEW,
    SOURCE_STATUS_OMITTED,
    SOURCE_STATUS_UNREADABLE,
    materialize_planner_source_context,
)
from app.services.orchestration.validation.validator import ValidatorService


def _plan(*, operation, path, expected_files=None, commands=None):
    return [
        {
            "step_number": 1,
            "description": "Apply the grounded source change",
            "commands": commands or [],
            "ops": (
                [{"op": operation, "path": path, "old": "old", "new": "new"}]
                if operation == "replace_in_file"
                else [
                    {
                        "op": operation,
                        "path": path,
                        "content": "def existing():\n    return 2\n",
                    }
                ]
            ),
            "verification": "python3 -m py_compile existing.py",
            "rollback": None,
            "expected_files": expected_files or [path],
        }
    ]


def _validate(plan, tmp_path, task_prompt="Update existing.py"):
    return ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=task_prompt,
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Phase 32C-1 contract test",
        description=task_prompt,
    )


def test_replace_old_text_must_exist_before_plan_acceptance(tmp_path):
    (tmp_path / "existing.py").write_text(
        "def existing():\n    return 1\n", encoding="utf-8"
    )
    plan = _plan(
        operation="replace_in_file",
        path="existing.py",
        commands=[],
    )

    outcome = _validate(plan, tmp_path)

    assert not outcome.accepted, "stale old_text must be rejected before acceptance"


def test_future_read_step_cannot_ground_later_static_replace(tmp_path):
    (tmp_path / "existing.py").write_text(
        "def existing():\n    return 1\n", encoding="utf-8"
    )
    plan = [
        {
            "step_number": 1,
            "description": "Read the current source",
            "commands": ["{'op': 'read_file', 'path': 'existing.py'}"],
            "verification": "python3 -m py_compile existing.py",
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Apply an exact replacement",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "existing.py",
                    "old": "invented source",
                    "new": "new source",
                }
            ],
            "verification": "python3 -m py_compile existing.py",
            "rollback": None,
            "expected_files": ["existing.py"],
        },
    ]

    issues = PlannerService.find_immediate_repair_step_issues(plan, tmp_path)

    assert issues["stale_replace_ops_steps"] == [2]


def test_missing_source_repair_receives_exact_current_source(tmp_path):
    target = tmp_path / "existing.py"
    target.write_text("def current_source():\n    return 1\n", encoding="utf-8")
    malformed = json.dumps(
        _plan(operation="replace_in_file", path="existing.py", commands=[])
    )

    prompt = build_planning_repair_prompt(
        task_description="Update existing.py",
        malformed_output=malformed,
        project_dir=tmp_path,
        rejection_reasons=["missing_source_materialization"],
    )

    assert "def current_source():" in prompt


def test_repair_without_progressing_evidence_is_not_improvement(tmp_path):
    (tmp_path / "existing.py").write_text(
        "def existing():\n    return 1\n", encoding="utf-8"
    )
    stale_plan = _plan(operation="replace_in_file", path="existing.py", commands=[])

    arbitration = classify_planning_repair_candidate(
        previous_plan=stale_plan,
        repaired_plan=stale_plan,
        project_dir=tmp_path,
        immediate_repair_issues={"stale_replace_ops_steps": [1]},
    )

    assert arbitration["outcome"] != "improved_or_preserved"
    assert "stale_replace" in arbitration["regression_labels"]


def test_existing_file_write_requires_explicit_replace_authorization(tmp_path):
    (tmp_path / "existing.py").write_text(
        "def existing():\n    return 1\n", encoding="utf-8"
    )
    plan = _plan(operation="write_file", path="existing.py", commands=[])

    outcome = _validate(plan, tmp_path)

    assert not outcome.accepted, "existing-file write needs explicit authorization"


def test_expected_files_must_be_materialized_before_validation(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Declare an output without materializing it",
            "commands": [],
            "ops": [],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["new.py"],
        }
    ]

    outcome = _validate(plan, tmp_path, task_prompt="Create new.py")

    assert not outcome.accepted
    assert "unmaterialized_expected_files" in outcome.details


def test_task_scope_remains_authoritative(tmp_path):
    plan = _plan(
        operation="write_file",
        path="outside_scope.py",
        commands=[],
        expected_files=["outside_scope.py"],
    )

    outcome = _validate(
        plan,
        tmp_path,
        task_prompt="Modify only app/allowed.py; do not change other files",
    )

    assert not outcome.accepted, "the validator must enforce the task scope"


def test_grounded_valid_plan_passes_without_repair(tmp_path):
    target = tmp_path / "existing.py"
    target.write_text("def existing():\n    return 1\n", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Apply the exact current-source replacement",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "existing.py",
                    "old": "def existing():\n    return 1\n",
                    "new": "def existing():\n    return 2\n",
                }
            ],
            "verification": "python3 -m py_compile existing.py",
            "rollback": None,
            "expected_files": ["existing.py"],
        }
    ]

    outcome = _validate(plan, tmp_path)

    assert outcome.accepted, outcome.reasons


def test_expected_small_sources_are_fully_materialized_with_provenance(tmp_path):
    target = tmp_path / "existing.py"
    source = "def current_source():\n    return 1\n"
    target.write_text(source, encoding="utf-8")

    materialization = materialize_planner_source_context(
        tmp_path,
        expected_paths=["existing.py"],
        task_description="Update existing.py",
    )
    record = materialization.file_map()["existing.py"]

    assert record.status == SOURCE_STATUS_EXISTING
    assert record.content == source
    assert record.relative_path == "existing.py"
    assert record.workspace_identity == str(tmp_path.resolve())
    assert record.content_hash == hashlib.sha256(source.encode()).hexdigest()
    assert record.version_identity
    assert record.truncated is False
    assert record.source_length_chars == len(source)
    assert record.included_prompt_length == len(source)


def test_expected_path_uses_workspace_safety_and_does_not_follow_escape(tmp_path):
    outside = tmp_path.parent / "phase32-outside-source.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    link = tmp_path / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError:
        return

    materialization = materialize_planner_source_context(
        tmp_path,
        expected_paths=["linked.py"],
        task_description="Update linked.py",
    )
    record = materialization.file_map()["linked.py"]

    assert record.status == SOURCE_STATUS_UNREADABLE
    assert "linked.py" in " ".join(materialization.unavailable_reasons)
    assert record.content is None


def test_missing_expected_file_is_not_silently_treated_as_new(tmp_path):
    materialization = materialize_planner_source_context(
        tmp_path,
        expected_paths=["missing.py"],
        task_description="Update missing.py",
    )
    record = materialization.file_map()["missing.py"]

    assert record.status == SOURCE_STATUS_MISSING
    assert record.creation_authorized is False
    assert not materialization.available
    assert any(
        reason.startswith("missing.py:")
        for reason in materialization.unavailable_reasons
    )


def test_new_expected_file_is_creation_authorized_without_fake_source(tmp_path):
    materialization = materialize_planner_source_context(
        tmp_path,
        expected_paths=["new.py"],
        task_description="Create new.py with the requested helper",
    )
    record = materialization.file_map()["new.py"]

    assert record.status == SOURCE_STATUS_NEW
    assert record.creation_authorized is True
    assert record.content is None
    assert materialization.available

    plan = _plan(operation="write_file", path="new.py", commands=[])
    outcome = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Create new.py with the requested helper",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    assert outcome.accepted, outcome.reasons


def test_binary_expected_file_fails_closed_without_replacement_evidence(tmp_path):
    (tmp_path / "binary.dat").write_bytes(b"\x00\x01binary")

    materialization = materialize_planner_source_context(
        tmp_path,
        expected_paths=["binary.dat"],
        task_description="Update binary.dat",
    )
    record = materialization.file_map()["binary.dat"]

    assert record.status == SOURCE_STATUS_UNREADABLE
    assert record.content is None
    assert not materialization.available


def test_materialized_version_change_rejects_stale_exact_edit(tmp_path):
    target = tmp_path / "existing.py"
    target.write_text("def existing():\n    return 1\n", encoding="utf-8")
    materialization = materialize_planner_source_context(
        tmp_path,
        expected_paths=["existing.py"],
        task_description="Update existing.py",
    )
    target.write_text("def existing():\n    return 2\n", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Apply the exact replacement",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "existing.py",
                    "old": "def existing():\n    return 1\n",
                    "new": "def existing():\n    return 3\n",
                }
            ],
            "verification": "python -m pytest -q",
            "rollback": None,
            "expected_files": ["existing.py"],
        }
    ]

    immediate = PlannerService.find_immediate_repair_step_issues(
        plan, tmp_path, source_materialization=materialization
    )
    outcome = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Update existing.py",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        source_materialization=materialization,
    )

    assert immediate["stale_replace_ops_steps"] == [1]
    assert not outcome.accepted


def test_truncation_is_explicit_and_bound_is_deterministic(tmp_path):
    (tmp_path / "source0.py").write_text("é" * 2000, encoding="utf-8")
    for index in (1, 2):
        (tmp_path / f"source{index}.py").write_text("x" * 2000, encoding="utf-8")

    first = materialize_planner_source_context(
        tmp_path,
        expected_paths=["source0.py", "source1.py", "source2.py"],
        task_description="Update source0.py, source1.py, and source2.py",
    )
    second = materialize_planner_source_context(
        tmp_path,
        expected_paths=["source0.py", "source1.py", "source2.py"],
        task_description="Update source0.py, source1.py, and source2.py",
    )

    assert [item.relative_path for item in first.files] == [
        item.relative_path for item in second.files
    ]
    assert first.to_metadata() == second.to_metadata()
    assert first.materialized_source_bytes <= 5000
    assert all(
        item.content is None or len(item.content.encode("utf-8")) <= 2000
        for item in first.files
    )
    assert any(item.truncated for item in first.files) or any(
        item.status == SOURCE_STATUS_OMITTED for item in first.files
    )


def test_unrelated_repository_files_are_not_loaded(tmp_path):
    (tmp_path / "expected.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "unrelated.py").write_text("unrelated = True\n", encoding="utf-8")

    materialization = materialize_planner_source_context(
        tmp_path,
        expected_paths=["expected.py"],
        task_description="Update expected.py",
    )

    assert [item.relative_path for item in materialization.files] == ["expected.py"]
    assert "unrelated.py" not in materialization.to_prompt_block()


def test_repair_prompt_reuses_exact_source_and_provenance(tmp_path):
    target = tmp_path / "existing.py"
    source = "def current_source():\n    return 1\n"
    target.write_text(source, encoding="utf-8")
    malformed = json.dumps(_plan(operation="replace_in_file", path="existing.py"))

    prompt = build_planning_repair_prompt(
        task_description="Update existing.py",
        malformed_output=malformed,
        project_dir=tmp_path,
        rejection_reasons=["stale_replace", "missing_source_materialization"],
    )

    assert "CURRENT SOURCE MATERIALIZATION" in prompt
    assert source in prompt
    assert hashlib.sha256(source.encode()).hexdigest() in prompt
    assert "future read_file" in prompt


def test_retained_contract_a_materializes_projects_pagination_targets():
    task = (
        "Remove legacy pagination from app/api/v1/endpoints/projects.py and "
        "update app/tests/test_pagination_infrastructure.py only."
    )
    materialization = materialize_planner_source_context(
        ".",
        task_description=task,
        expected_paths=[
            "app/api/v1/endpoints/projects.py",
            "app/tests/test_pagination_infrastructure.py",
        ],
        supporting_paths=[],
    )

    assert materialization.available
    assert [item.relative_path for item in materialization.files] == [
        "app/api/v1/endpoints/projects.py",
        "app/tests/test_pagination_infrastructure.py",
    ]
    assert all(item.status == SOURCE_STATUS_EXISTING for item in materialization.files)
    assert "replace_in_file.old_text" in materialization.to_prompt_block()


def test_retained_contract_b_distinguishes_existing_and_authorized_new_targets():
    task = (
        "Add a shared timezone-aware utc_now() helper in app/time_utils.py, "
        "update app/services/workspace/context_service.py, and add focused "
        "regression coverage in app/tests/test_utc_now_helper.py."
    )
    materialization = materialize_planner_source_context(
        ".",
        task_description=task,
        expected_paths=[
            "app/services/workspace/context_service.py",
            "app/time_utils.py",
            "app/tests/test_utc_now_helper.py",
        ],
        supporting_paths=[],
    )
    statuses = {item.relative_path: item.status for item in materialization.files}

    assert materialization.available
    assert (
        statuses["app/services/workspace/context_service.py"] == SOURCE_STATUS_EXISTING
    )
    assert statuses["app/time_utils.py"] == SOURCE_STATUS_NEW
    assert statuses["app/tests/test_utc_now_helper.py"] == SOURCE_STATUS_NEW
    assert all(
        item.content is None
        for item in materialization.files
        if item.status == SOURCE_STATUS_NEW
    )
