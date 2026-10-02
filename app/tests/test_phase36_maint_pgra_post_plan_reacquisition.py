"""PHASE36-MAINT-PGRA — bounded post-Plan target reacquisition.

The REENTRY-9 shape is a narrow ``replace_in_file`` Plan whose existing target
was not in the initial bounded materialization.  These tests pin the intended
acquisition seam: a target-local span may be
reacquired for narrow replacement only, while whole-file reconstruction stays
fail-closed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.services.orchestration.phases.post_plan_source_grounding import (
    POST_PLAN_GROUNDING_CAPACITY_EXCEEDED,
    POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
    POST_PLAN_GROUNDING_PROTECTED,
    POST_PLAN_GROUNDING_SYMLINK,
    ground_post_plan_source_materialization,
)
from app.services.orchestration.planning.source_materialization import (
    HINT_AUTHORITY_PLAN_OPERATION,
    materialize_planner_source_context,
)
from app.services.orchestration.planning.source_operation_verification import (
    FAILURE_VERSION_CHANGED,
    SOURCE_EVIDENCE_VISIBLE_IN_SPAN,
    verify_replace_in_file,
)
from app.services.orchestration.validation.validator import ValidatorService


TARGET = "app/api/v1/endpoints/permissions.py"
OLD = '@router.get("/permissions/pending")'
NEW = '@router.get("/pending")'
TASK = "Correct the pending permissions route while preserving all other routes."
FILLER = "".join(
    f"# unrelated permission route {index}\n"
    f"async def route_{index}():\n    return {index}\n\n"
    for index in range(180)
)
SOURCE = (
    "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n"
    + FILLER
    + OLD
    + '\nasync def pending():\n    return {"pending": []}\n'
)


def _workspace(root: Path, source: str = SOURCE) -> Path:
    root = root.resolve()
    (root / "app/api/v1/endpoints").mkdir(parents=True)
    (root / TARGET).write_text(source, encoding="utf-8")
    return root


def _replace_plan(old: str = OLD, new: str = NEW) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Apply the narrow permissions route repair.",
            "commands": [],
            "verification": "python3 -m py_compile app/api/v1/endpoints/permissions.py",
            "rollback": None,
            "expected_files": [TARGET],
            "ops": [{"op": "replace_in_file", "path": TARGET, "old": old, "new": new}],
        }
    ]


def _whole_file_plan() -> list[dict]:
    return [
        {
            **_replace_plan()[0],
            "description": "Rewrite the permissions module.",
            "ops": [{"op": "write_file", "path": TARGET, "content": "x = 1\n"}],
        }
    ]


def _plan_for_target(target: str, *, old: str = OLD, new: str = NEW) -> list[dict]:
    plan = _replace_plan(old=old, new=new)
    plan[0]["expected_files"] = [target]
    plan[0]["ops"][0]["path"] = target
    return plan


def _initial_materialization(root: Path):
    return materialize_planner_source_context(
        root,
        task_description=TASK,
        supporting_paths=(),
    )


def test_reentry9_shape_reacquires_exact_old_span_for_narrow_replace(tmp_path):
    root = _workspace(tmp_path)
    initial = _initial_materialization(root)
    assert TARGET not in initial.file_map()

    result = ground_post_plan_source_materialization(
        _replace_plan(),
        project_dir=root,
        source_materialization=initial,
    )

    assert result.ok, result.to_dict()
    record = result.materialization.file_map()[TARGET]
    assert record.planning_visible is False
    assert record.truncated is True
    assert record.target_included is True
    assert record.target_match_count == 1
    assert record.target_hint_authority == HINT_AUTHORITY_PLAN_OPERATION
    assert OLD in (record.content or "")
    verdict = verify_replace_in_file(result.materialization, TARGET, OLD, root)
    assert verdict.verified, verdict
    assert verdict.visibility == SOURCE_EVIDENCE_VISIBLE_IN_SPAN

    accepted = ValidatorService.validate_plan(
        _replace_plan(),
        output_text=json.dumps(_replace_plan()),
        task_prompt=TASK,
        execution_profile="full_lifecycle",
        project_dir=root,
        source_materialization=result.materialization,
    )
    assert accepted.accepted, accepted.reasons


def test_reacquisition_does_not_authorize_whole_file_rewrite(tmp_path):
    root = _workspace(tmp_path)
    result = ground_post_plan_source_materialization(
        _replace_plan(),
        project_dir=root,
        source_materialization=_initial_materialization(root),
    )
    assert result.ok, result.to_dict()

    verdict = ValidatorService.validate_plan(
        _whole_file_plan(),
        output_text=json.dumps(_whole_file_plan()),
        task_prompt=TASK,
        execution_profile="full_lifecycle",
        project_dir=root,
        source_materialization=result.materialization,
    )
    assert not verdict.accepted
    assert any(
        "existing_file_rewrite_requires_complete_planning_source" in str(reason)
        for reason in verdict.reasons
    )


def test_unavailable_old_fails_closed(tmp_path):
    root = _workspace(tmp_path)
    result = ground_post_plan_source_materialization(
        _replace_plan(old='@router.get("/does-not-exist")'),
        project_dir=root,
        source_materialization=_initial_materialization(root),
    )
    assert not result.ok
    assert result.failure_code == POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE


def test_ambiguous_old_fails_closed(tmp_path):
    root = _workspace(tmp_path, SOURCE + "\n" + OLD + "\n")
    result = ground_post_plan_source_materialization(
        _replace_plan(),
        project_dir=root,
        source_materialization=_initial_materialization(root),
    )
    assert not result.ok
    assert result.failure_code == POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE


def test_stale_version_after_reacquisition_fails_closed(tmp_path):
    root = _workspace(tmp_path)
    result = ground_post_plan_source_materialization(
        _replace_plan(),
        project_dir=root,
        source_materialization=_initial_materialization(root),
    )
    assert result.ok, result.to_dict()

    (root / TARGET).write_text(
        SOURCE.replace("route_0", "route_zero"), encoding="utf-8"
    )
    stat = (root / TARGET).stat()
    os.utime(root / TARGET, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    verdict = verify_replace_in_file(result.materialization, TARGET, OLD, root)
    assert verdict.failure_code == FAILURE_VERSION_CHANGED


def test_complete_expected_file_narrow_replacement_is_unchanged(tmp_path):
    root = _workspace(tmp_path, "router = APIRouter()\n")
    old = "router = APIRouter()"
    initial = materialize_planner_source_context(
        root, task_description=TASK, expected_paths=[TARGET], supporting_paths=()
    )
    result = ground_post_plan_source_materialization(
        _replace_plan(old=old, new="router = APIRouter(prefix='')"),
        project_dir=root,
        source_materialization=initial,
    )
    assert result.ok, result.to_dict()
    assert not result.materialization.file_map()[TARGET].truncated


def test_truncated_expected_file_with_visible_old_remains_narrow_only(tmp_path):
    root = _workspace(tmp_path)
    old = "router = APIRouter()"
    initial = materialize_planner_source_context(
        root, task_description=TASK, expected_paths=[TARGET], supporting_paths=()
    )
    record = initial.file_map()[TARGET]
    assert record.truncated and old in (record.content or "")
    result = ground_post_plan_source_materialization(
        _replace_plan(old=old, new="router = APIRouter(prefix='')"),
        project_dir=root,
        source_materialization=initial,
    )
    assert result.ok, result.to_dict()


def test_truncated_expected_file_with_unavailable_old_fails_at_admission(tmp_path):
    root = _workspace(tmp_path)
    initial = materialize_planner_source_context(
        root, task_description=TASK, expected_paths=[TARGET], supporting_paths=()
    )
    plan = _replace_plan(old='@router.get("/missing")')
    grounding = ground_post_plan_source_materialization(
        plan, project_dir=root, source_materialization=initial
    )
    assert grounding.ok, grounding.to_dict()
    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=TASK,
        execution_profile="full_lifecycle",
        project_dir=root,
        source_materialization=grounding.materialization,
    )
    assert not verdict.accepted


def test_nonexpected_unobserved_large_file_is_authoritative_only_for_narrow_edit(
    tmp_path,
):
    root = _workspace(tmp_path)
    initial = _initial_materialization(root)
    narrow = ground_post_plan_source_materialization(
        _replace_plan(), project_dir=root, source_materialization=initial
    )
    assert narrow.ok, narrow.to_dict()
    whole = ValidatorService.validate_plan(
        _whole_file_plan(),
        output_text=json.dumps(_whole_file_plan()),
        task_prompt=TASK,
        execution_profile="full_lifecycle",
        project_dir=root,
        source_materialization=narrow.materialization,
    )
    assert not whole.accepted


def test_unmaterialized_large_file_whole_rewrite_stays_rejected(tmp_path):
    root = _workspace(tmp_path)
    result = ground_post_plan_source_materialization(
        _whole_file_plan(),
        project_dir=root,
        source_materialization=_initial_materialization(root),
    )
    assert not result.ok
    assert result.failure_code == POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE


def test_protected_and_symlink_targets_never_reacquire_source(tmp_path):
    root = _workspace(tmp_path)
    (root / ".agent").mkdir()
    (root / ".agent/state.json").write_text("{}", encoding="utf-8")
    protected = ground_post_plan_source_materialization(
        _plan_for_target(".agent/state.json"),
        project_dir=root,
        source_materialization=_initial_materialization(root),
    )
    assert protected.failure_code == POST_PLAN_GROUNDING_PROTECTED

    linked = "app/api/v1/endpoints/linked.py"
    (root / linked).symlink_to(root / TARGET)
    symlink = ground_post_plan_source_materialization(
        _plan_for_target(linked),
        project_dir=root,
        source_materialization=_initial_materialization(root),
    )
    assert symlink.failure_code == POST_PLAN_GROUNDING_SYMLINK


def test_new_file_write_does_not_use_existing_source_reacquisition(tmp_path):
    root = _workspace(tmp_path)
    new_path = "app/api/v1/endpoints/new_permissions.py"
    plan = _plan_for_target(new_path, old="unused", new="unused")
    plan[0]["ops"] = [{"op": "write_file", "path": new_path, "content": "x = 1\n"}]
    result = ground_post_plan_source_materialization(
        plan, project_dir=root, source_materialization=_initial_materialization(root)
    )
    assert result.ok, result.to_dict()


def test_multiple_targets_over_acquisition_budget_fail_closed(tmp_path):
    root = _workspace(tmp_path)
    second = "app/api/v1/endpoints/other_permissions.py"
    (root / second).write_text(SOURCE, encoding="utf-8")
    initial = materialize_planner_source_context(
        root,
        task_description=TASK,
        supporting_paths=(),
        maximum_files=1,
    )
    plan = _replace_plan()
    plan[0]["ops"].append(
        {"op": "replace_in_file", "path": second, "old": OLD, "new": NEW}
    )
    plan[0]["expected_files"].append(second)
    result = ground_post_plan_source_materialization(
        plan, project_dir=root, source_materialization=initial
    )
    assert result.failure_code == POST_PLAN_GROUNDING_CAPACITY_EXCEEDED


def test_target_acquisition_byte_budget_exhaustion_fails_closed(tmp_path):
    root = _workspace(tmp_path)
    initial = materialize_planner_source_context(
        root,
        task_description=TASK,
        supporting_paths=(),
        maximum_total_source_bytes=8,
    )
    result = ground_post_plan_source_materialization(
        _replace_plan(), project_dir=root, source_materialization=initial
    )
    assert not result.ok
    assert result.failure_code == POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE


@pytest.mark.parametrize("old", [OLD, OLD + "\n\n"])
def test_replacement_old_is_checked_as_exact_source_text(tmp_path, old):
    root = _workspace(tmp_path)
    result = ground_post_plan_source_materialization(
        _replace_plan(old=old),
        project_dir=root,
        source_materialization=_initial_materialization(root),
    )
    if old == OLD:
        assert result.ok, result.to_dict()
    else:
        assert not result.ok
