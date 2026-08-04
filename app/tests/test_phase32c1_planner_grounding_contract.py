"""Phase 32C planner source-grounding and eager-materialization contract tests."""

import hashlib
import json
from pathlib import Path

from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.repair_arbitration import (
    classify_planning_repair_candidate,
)
from app.services.orchestration.planning.repair_prompts import (
    RequiredRepairSourceEvidenceExceeded,
    build_compact_stale_replace_repair_prompt,
    build_planning_repair_prompt,
)
from app.services.orchestration.planning.source_materialization import (
    SELECTION_HEAD_FALLBACK,
    SELECTION_NEW_FILE,
    SELECTION_TARGET_EXACT,
    SOURCE_STATUS_EXISTING,
    SOURCE_STATUS_MISSING,
    SOURCE_STATUS_NEW,
    SOURCE_STATUS_OMITTED,
    SOURCE_STATUS_UNREADABLE,
    TARGET_HINT_MATCHED,
    TARGET_HINT_NOT_FOUND,
    current_source_version_identity,
    materialize_planner_source_context,
    render_repair_source_materialization,
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


# --- Phase 32D-2 target-aware source region selection ---------------------


ATTEMPT5_TASK = (
    "Add a shared timezone-aware utc_now() helper in app/time_utils.py, "
    "replace the deprecated datetime.utcnow() call in "
    "app/services/workspace/context_service.py with utc_now(), and add focused "
    "regression coverage in app/tests/test_utc_now_helper.py."
)
ATTEMPT5_EXPECTED = [
    "app/services/workspace/context_service.py",
    "app/time_utils.py",
    "app/tests/test_utc_now_helper.py",
]


def _visible_body(record):
    body = record.content
    if record.truncated_before:
        body = body.split("\n", 1)[1]
    if record.truncated_after:
        body = body.rsplit("\n", 1)[0]
    return body


def _attempt5_materialization(**overrides):
    kwargs = {
        "task_description": ATTEMPT5_TASK,
        "expected_paths": ATTEMPT5_EXPECTED,
        "supporting_paths": [],
    }
    kwargs.update(overrides)
    return materialize_planner_source_context(".", **kwargs)


def test_attempt5_head_only_selection_misses_the_target_location():
    source = Path("app/services/workspace/context_service.py").read_text(
        encoding="utf-8"
    )
    head = source.encode("utf-8")[:2000].decode("utf-8", errors="ignore")

    assert "datetime.utcnow()" in source
    assert "datetime.utcnow()" not in head, "head excerpt must not contain the target"


def test_attempt5_target_region_is_materialized_within_bounds():
    materialization = _attempt5_materialization()
    record = materialization.file_map()["app/services/workspace/context_service.py"]
    source = Path("app/services/workspace/context_service.py").read_text(
        encoding="utf-8"
    )
    target_line = source[: source.index("datetime.utcnow()")].count("\n") + 1

    assert record.expected is True
    assert record.priority == "P0"
    assert record.status == SOURCE_STATUS_EXISTING
    assert record.selection_strategy == SELECTION_TARGET_EXACT
    assert record.target_hint == "datetime.utcnow()"
    assert record.target_hint_status == TARGET_HINT_MATCHED
    assert record.target_included is True
    assert "datetime.utcnow()" in record.content
    assert record.start_line <= target_line <= record.end_line
    assert record.included_source_bytes <= 2000
    assert materialization.materialized_source_bytes <= 5000
    assert _visible_body(record) in source
    assert record.full_source_bytes == len(source.encode("utf-8"))
    assert record.version_identity == current_source_version_identity(
        Path("app/services/workspace/context_service.py").resolve()
    )


def test_attempt5_visible_range_matches_the_reported_lines():
    record = _attempt5_materialization().file_map()[
        "app/services/workspace/context_service.py"
    ]
    source = Path("app/services/workspace/context_service.py").read_text(
        encoding="utf-8"
    )
    encoded = source.encode("utf-8")
    lines = source.splitlines(keepends=True)

    assert encoded[record.start_byte : record.end_byte].decode(
        "utf-8"
    ) == _visible_body(record)
    assert _visible_body(record) == "".join(
        lines[record.start_line - 1 : record.end_line]
    )
    assert record.truncated_before is True
    assert record.truncated_after is True


def test_attempt5_expected_target_is_prioritized_over_support_files():
    materialization = _attempt5_materialization(
        supporting_paths=["app/api/v1/router.py", "app/config.py"]
    )
    order = [item.relative_path for item in materialization.files]
    record = materialization.file_map()["app/services/workspace/context_service.py"]

    assert order.index("app/services/workspace/context_service.py") < order.index(
        "app/api/v1/router.py"
    )
    assert order.index("app/services/workspace/context_service.py") < order.index(
        "app/config.py"
    )
    assert record.target_included is True
    assert materialization.materialized_source_bytes <= 5000
    assert all(
        item.content is None or len(item.content.encode("utf-8")) <= 2000
        for item in materialization.files
    )


def test_attempt5_new_files_consume_no_source_bytes():
    materialization = _attempt5_materialization()
    files = materialization.file_map()

    for path in ("app/time_utils.py", "app/tests/test_utc_now_helper.py"):
        record = files[path]
        assert record.status == SOURCE_STATUS_NEW
        assert record.selection_strategy == SELECTION_NEW_FILE
        assert record.included_source_bytes == 0
        assert record.content is None
        assert record.target_included is False
    assert materialization.available


def test_attempt1_target_region_contains_the_legacy_pagination_branch():
    task = (
        "Remove the legacy pagination branch that calls "
        "`query.offset(skip).limit(limit)` in app/api/v1/endpoints/projects.py "
        "and update app/tests/test_pagination_infrastructure.py only."
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
    record = materialization.file_map()["app/api/v1/endpoints/projects.py"]
    source = Path("app/api/v1/endpoints/projects.py").read_text(encoding="utf-8")
    head = source.encode("utf-8")[:2000].decode("utf-8", errors="ignore")

    assert "query.offset(skip).limit(limit)" not in head
    assert record.selection_strategy == SELECTION_TARGET_EXACT
    assert record.target_hint == "query.offset(skip).limit(limit)"
    assert record.target_included is True
    assert "query.offset(skip).limit(limit)" in record.content
    assert "if page is None:" in record.content
    assert record.included_source_bytes <= 2000
    assert materialization.materialized_source_bytes <= 5000


def test_multiple_target_matches_are_deterministic_and_counted(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    filler = "".join(f"# filler line {index}\n" for index in range(200))
    source = (
        filler + "value = legacy_call()\n" + filler + "other = legacy_call()\n" + filler
    )
    (package / "deep.py").write_text(source, encoding="utf-8")
    task = "Replace `legacy_call()` in pkg/deep.py with the new helper."

    first = materialize_planner_source_context(
        tmp_path,
        task_description=task,
        expected_paths=["pkg/deep.py"],
        supporting_paths=[],
    )
    second = materialize_planner_source_context(
        tmp_path,
        task_description=task,
        expected_paths=["pkg/deep.py"],
        supporting_paths=[],
    )
    record = first.file_map()["pkg/deep.py"]

    assert record.target_match_count == 2
    assert record.target_included is True
    assert record.target_match_start == len(
        source[: source.index("legacy_call()")].encode("utf-8")
    )
    assert first.to_metadata() == second.to_metadata()


def test_missing_target_hint_falls_back_without_claiming_grounding(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    source = "".join(f"# filler line {index}\n" for index in range(400))
    (package / "deep.py").write_text(source, encoding="utf-8")
    task = "Replace `absent_symbol()` in pkg/deep.py with the new helper."

    materialization = materialize_planner_source_context(
        tmp_path,
        task_description=task,
        expected_paths=["pkg/deep.py"],
        supporting_paths=[],
    )
    record = materialization.file_map()["pkg/deep.py"]

    assert record.selection_strategy == SELECTION_HEAD_FALLBACK
    assert record.target_hint_status == TARGET_HINT_NOT_FOUND
    assert record.target_hint is None
    assert record.target_included is False
    assert record.start_line == 1
    assert record.truncated_after is True
    assert record.included_source_bytes <= 2000


def test_total_budget_serves_expected_files_before_unrelated_support(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    filler = "".join(f"# filler line {index}\n" for index in range(300))
    (package / "expected.py").write_text(
        filler + "value = target_call()\n" + filler, encoding="utf-8"
    )
    for name in ("support_a.py", "support_b.py", "support_c.py"):
        (package / name).write_text(filler, encoding="utf-8")
    task = "Replace `target_call()` in pkg/expected.py with the new helper."

    materialization = materialize_planner_source_context(
        tmp_path,
        task_description=task,
        expected_paths=["pkg/expected.py"],
        supporting_paths=["pkg/support_a.py", "pkg/support_b.py", "pkg/support_c.py"],
    )
    order = [item.relative_path for item in materialization.files]
    record = materialization.file_map()["pkg/expected.py"]

    assert order[0] == "pkg/expected.py"
    assert record.target_included is True
    assert record.included_source_bytes <= 2000
    assert materialization.materialized_source_bytes <= 5000
    assert materialization.available


def test_target_centered_slicing_does_not_split_multibyte_characters(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    filler = "".join(f"# héllo wörld filler {index}\n" for index in range(200))
    source = filler + "value = target_call()  # ünïcodé\n" + filler
    (package / "deep.py").write_text(source, encoding="utf-8")
    task = "Replace `target_call()` in pkg/deep.py with the new helper."

    record = materialize_planner_source_context(
        tmp_path,
        task_description=task,
        expected_paths=["pkg/deep.py"],
        supporting_paths=[],
    ).file_map()["pkg/deep.py"]

    assert "�" not in record.content
    assert _visible_body(record) in source
    assert "target_call()" in record.content
    assert record.included_source_bytes <= 2000


def test_target_region_grounds_exact_replacement_and_rejects_stale_text(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    filler = "".join(f"# filler line {index}\n" for index in range(300))
    source = filler + "    stamp = datetime.utcnow().isoformat()\n" + filler
    (package / "deep.py").write_text(source, encoding="utf-8")
    task = (
        "Replace the deprecated datetime.utcnow() call in pkg/deep.py with "
        "the shared utc_now() helper."
    )
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description=task,
        expected_paths=["pkg/deep.py"],
        supporting_paths=[],
    )

    grounded = [
        {
            "step_number": 1,
            "description": "Apply the grounded target replacement",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "pkg/deep.py",
                    "old": "    stamp = datetime.utcnow().isoformat()\n",
                    "new": "    stamp = utc_now().isoformat()\n",
                }
            ],
            "verification": "python3 -m py_compile pkg/deep.py",
            "rollback": None,
            "expected_files": ["pkg/deep.py"],
        }
    ]
    stale = json.loads(json.dumps(grounded))
    stale[0]["ops"][0]["old"] = "    stamp = datetime.now().isoformat()\n"

    grounded_issues = PlannerService.find_immediate_repair_step_issues(
        grounded, tmp_path, source_materialization=materialization
    )
    stale_issues = PlannerService.find_immediate_repair_step_issues(
        stale, tmp_path, source_materialization=materialization
    )

    assert grounded_issues.get("stale_replace_ops_steps", []) == []
    assert stale_issues["stale_replace_ops_steps"] == [1]


def test_repair_prompt_receives_the_same_target_region(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    filler = "".join(f"# filler line {index}\n" for index in range(300))
    source = filler + "    stamp = datetime.utcnow().isoformat()\n" + filler
    (package / "deep.py").write_text(source, encoding="utf-8")
    task = (
        "Replace the deprecated datetime.utcnow() call in pkg/deep.py with "
        "the shared utc_now() helper."
    )
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description=task,
        expected_paths=["pkg/deep.py"],
        supporting_paths=[],
    )
    record = materialization.file_map()["pkg/deep.py"]
    malformed = json.dumps(
        _plan(operation="replace_in_file", path="pkg/deep.py", commands=[])
    )

    prompt = build_planning_repair_prompt(
        task_description=task,
        malformed_output=malformed,
        project_dir=tmp_path,
        rejection_reasons=["stale_replace"],
    )

    assert "datetime.utcnow()" in prompt
    assert f"visible_lines: {record.start_line}-{record.end_line}" in prompt
    assert "selection_strategy: target_centered_exact_match" in prompt
    assert "target_hint: datetime.utcnow()" in prompt
    assert "target_included: true" in prompt
    assert "Never reconstruct a whole file from a partial excerpt." in prompt


# Phase 32D-2R1 — compact model-visible source-provenance rendering.
#
# Internal provenance keeps the complete D-2 evidence set; the model-visible
# prompt projection carries only what is needed to use the source safely.

_PROMPT_OMITTED_METADATA_LABELS = (
    "source_length:",
    "included_prompt_length:",
    "visible_bytes:",
    "target_hint_type:",
    "target_hint_authority:",
    "target_hint_status:",
    "target_match_count:",
    "target_match_start:",
    "target_match_end:",
    "truncated_before:",
    "truncated_after:",
    "priority:",
)

_INTERNAL_PROVENANCE_FIELDS = (
    "priority",
    "selection_strategy",
    "full_source_bytes",
    "included_source_bytes",
    "start_byte",
    "end_byte",
    "start_line",
    "end_line",
    "truncated_before",
    "truncated_after",
    "target_hint",
    "target_hint_type",
    "target_hint_authority",
    "target_hint_status",
    "target_match_count",
    "target_match_start",
    "target_match_end",
    "target_included",
    "content_hash",
    "version_identity",
    "status",
    "creation_authorized",
)


def test_internal_provenance_retains_every_target_aware_field():
    materialization = _attempt5_materialization()
    record = materialization.file_map()["app/services/workspace/context_service.py"]

    provenance = record.to_dict()
    for name in _INTERNAL_PROVENANCE_FIELDS:
        assert name in provenance, f"internal provenance lost {name}"

    assert provenance["selection_strategy"] == SELECTION_TARGET_EXACT
    assert provenance["target_hint"] == "datetime.utcnow()"
    assert provenance["target_hint_status"] == TARGET_HINT_MATCHED
    assert provenance["target_match_count"] >= 1
    assert provenance["target_match_start"] is not None
    assert provenance["start_byte"] is not None and provenance["end_byte"] is not None
    assert provenance["target_included"] is True


def test_target_aware_selection_metadata_survives_to_metadata_evidence():
    metadata = _attempt5_materialization().to_metadata()
    entry = next(
        item
        for item in metadata["files"]
        if item["relative_path"] == "app/services/workspace/context_service.py"
    )

    assert metadata["target_materialized_file_count"] == 1
    for name in _INTERNAL_PROVENANCE_FIELDS:
        assert name in entry, f"evidence metadata lost {name}"
    assert "content" not in entry


def test_compact_prompt_rendering_omits_nonessential_metadata():
    block = _attempt5_materialization().to_prompt_block()

    for label in _PROMPT_OMITTED_METADATA_LABELS:
        assert label not in block, f"prompt still renders {label}"


def test_compact_prompt_rendering_retains_required_source_evidence():
    materialization = _attempt5_materialization()
    record = materialization.file_map()["app/services/workspace/context_service.py"]
    block = materialization.to_prompt_block()

    # P0 — the target-containing excerpt itself.
    assert "datetime.utcnow()" in block
    assert _visible_body(record) in block
    # P1 — path and stable source identity.
    assert "### app/services/workspace/context_service.py" in block
    assert f"version_identity: {record.version_identity}" in block
    assert f"content_hash: {record.content_hash}" in block
    # P2 — target hint and visible line range.
    assert "target_hint: datetime.utcnow()" in block
    assert f"visible_lines: {record.start_line}-{record.end_line}" in block
    assert "target_included: true" in block
    # P4/P5 — truncation state and selection strategy.
    assert "truncated: true" in block
    assert f"selection_strategy: {SELECTION_TARGET_EXACT}" in block


def test_compact_prompt_rendering_keeps_new_file_creation_authorization():
    block = _attempt5_materialization().to_prompt_block()

    assert "### app/time_utils.py" in block
    assert "### app/tests/test_utc_now_helper.py" in block
    assert block.count(f"status: {SOURCE_STATUS_NEW}") == 2
    assert block.count("creation_authorized: true") == 2


def test_first_pass_and_repair_prompts_share_one_compact_renderer(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    filler = "".join(f"# filler line {index}\n" for index in range(300))
    (package / "deep.py").write_text(
        filler + "    stamp = datetime.utcnow().isoformat()\n" + filler,
        encoding="utf-8",
    )
    task = (
        "Replace the deprecated datetime.utcnow() call in pkg/deep.py with "
        "the shared utc_now() helper."
    )
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description=task,
        expected_paths=["pkg/deep.py"],
        supporting_paths=[],
    )
    first_pass_block = materialization.to_prompt_block()

    repair_prompt = build_planning_repair_prompt(
        task_description=task,
        malformed_output=json.dumps(
            _plan(operation="replace_in_file", path="pkg/deep.py", commands=[])
        ),
        project_dir=tmp_path,
        rejection_reasons=["stale_replace"],
    )

    # The repair prompt embeds the identical rendering, not a divergent one.
    assert first_pass_block in repair_prompt
    for label in _PROMPT_OMITTED_METADATA_LABELS:
        assert label not in repair_prompt


def test_grounding_invariants_are_stated_once_not_repeated_per_file():
    block = _attempt5_materialization().to_prompt_block()

    invariants = (
        "A future read_file command is not planning-time evidence.",
        "Never reconstruct a whole file from a partial excerpt.",
        "Omitted or truncated source does not authorize fabricated exact replacement.",
        "Each visible region was deliberately selected around the task target; use the visible text.",
    )
    for invariant in invariants:
        assert block.count(invariant) == 1, f"duplicated invariant: {invariant}"


def test_compact_rendering_spends_its_budget_on_source_not_metadata():
    materialization = _attempt5_materialization()
    block = materialization.to_prompt_block()
    excerpt_chars = sum(len(item.content or "") for item in materialization.files)

    # P0 excerpt evidence must never be crowded out by supplementary metadata.
    assert excerpt_chars > 0
    assert len(block) - excerpt_chars < 2000
    assert materialization.materialized_source_bytes <= 5000
    assert (
        materialization.file_map()[
            "app/services/workspace/context_service.py"
        ].included_source_bytes
        <= 2000
    )


def test_repair_projection_compacts_support_before_target_evidence():
    task = (
        "Remove legacy pagination `query.offset(skip).limit(limit)` in "
        "app/api/v1/endpoints/projects.py."
    )
    materialization = materialize_planner_source_context(
        ".",
        task_description=task,
        expected_paths=[
            "app/api/v1/endpoints/projects.py",
            "app/tests/test_pagination_infrastructure.py",
        ],
        supporting_paths=["app/api/v1/router.py", "app/config.py"],
    )
    provenance_before = materialization.to_metadata()
    full = render_repair_source_materialization(
        materialization,
        rejected_paths=["app/api/v1/endpoints/projects.py"],
    )
    compact = render_repair_source_materialization(
        materialization,
        rejected_paths=["app/api/v1/endpoints/projects.py"],
        compaction_level=1,
    )

    assert full == materialization.to_prompt_block()
    assert "query.offset(skip).limit(limit)" in compact
    assert "if page is None:" in compact
    assert "repair_projection: metadata_only" in compact
    assert "version_identity:" in compact
    assert "omission_reason: lower_priority_support_R5" in compact
    assert materialization.to_metadata() == provenance_before


def test_attempt1_repair_projection_fits_without_dropping_target_branch():
    task = (
        "Remove legacy pagination `query.offset(skip).limit(limit)` in "
        "app/api/v1/endpoints/projects.py."
    )
    expected = [
        "app/api/v1/endpoints/projects.py",
        "app/tests/test_pagination_infrastructure.py",
    ]
    materialization = materialize_planner_source_context(
        ".",
        task_description=task,
        expected_paths=expected,
        supporting_paths=["app/api/v1/router.py", "app/config.py"],
    )
    malformed = json.dumps(
        _plan(
            operation="replace_in_file",
            path="app/api/v1/endpoints/projects.py",
            expected_files=expected,
            commands=[],
        )
    )

    prompt = build_compact_stale_replace_repair_prompt(
        task_description=task,
        malformed_output=malformed,
        project_dir=Path("."),
        rejection_reasons=["stale_replace_ops_steps: [1]"],
        source_materialization=materialization,
    )

    assert len(prompt) <= 8000
    assert "query.offset(skip).limit(limit)" in prompt
    assert "if page is None:" in prompt
    assert "version_identity:" in prompt
    assert "visible_lines:" in prompt
    assert "target_included: true" in prompt


def test_rejected_operation_is_r0_without_a_task_hint(tmp_path):
    (tmp_path / "existing.py").write_text(
        "def existing():\n    return 'visible evidence'\n", encoding="utf-8"
    )
    materialization = materialize_planner_source_context(
        tmp_path, task_description="Make a safe change.", expected_paths=["existing.py"]
    )

    projected = render_repair_source_materialization(
        materialization, rejected_paths=["existing.py"], compaction_level=1
    )

    assert "### existing.py" in projected
    assert "repair_projection: full_excerpt" in projected
    assert materialization.file_map()["existing.py"].target_included is False


def test_r2_without_structured_evidence_is_metadata_only_not_head_excerpt(tmp_path):
    (tmp_path / "test_example.py").write_text(
        "# generic heading\n" + "assert useful_symbol()\n" * 120,
        encoding="utf-8",
    )
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description="Update behavior.",
        expected_paths=["test_example.py"],
    )

    projected = render_repair_source_materialization(
        materialization, compaction_level=2
    )

    assert "repair_projection: metadata_only" in projected
    assert "metadata_only_no_repair_evidence" in projected
    assert "# generic heading" not in projected


def test_required_repair_source_overflow_fails_closed_with_diagnostics(tmp_path):
    (tmp_path / "existing.py").write_text(
        "def existing():\n    return 'visible evidence'\n", encoding="utf-8"
    )
    materialization = materialize_planner_source_context(
        tmp_path, task_description="Safe change.", expected_paths=["existing.py"]
    )
    malformed = json.dumps(_plan(operation="replace_in_file", path="existing.py"))

    result = build_compact_stale_replace_repair_prompt(
        task_description="Safe change.",
        malformed_output=malformed,
        project_dir=tmp_path,
        rejection_reasons=["stale_replace_ops_steps: [1]"],
        source_materialization=materialization,
        apply_prompt_profile=lambda prompt, _profile: prompt + ("x" * 8000),
    )

    assert isinstance(result, RequiredRepairSourceEvidenceExceeded)
    assert (
        result.diagnostics["reason"]
        == "required_repair_source_evidence_exceeds_prompt_bound"
    )
    assert result.diagnostics["failure_owner"] == "repair_prompt_projection"
    assert result.diagnostics["required_record_paths"] == ["existing.py"]
    assert result.diagnostics["required_record_priorities"] == ["R0"]
