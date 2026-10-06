"""PHASE36-MAINT-OAD — OBS-1 / OBS-2 boundary adjudication.

OBS-1: a restoration request ("restore", "re-enable", "make ... again", ...) is
mutation intent like "fix"; appending "verify it" must not turn it into a
read-only verification task.  Verify-only, inspect-only and ambiguous wording
keep their current profile.

OBS-2 (no production change): an existing target whose materialization record
was omitted by the source budget fails closed, and removing that record would
not let PGRA acquire it, because acquisition is charged to the same budget.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import app.services.orchestration.validation.validator as validator_module
from app.services.orchestration.phases.post_plan_source_grounding import (
    POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
    ground_post_plan_source_materialization,
)
from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_OMITTED,
    materialize_planner_source_context,
)
from app.services.orchestration.validation.validator import ValidatorService

# Exact DQE C7 wording (docs corpus.json, case C7).
C7_TASK = (
    'Orchestration task runs never start: the Celery worker logs "Received '
    "unregistered task of type 'app.tasks.worker.execute_orchestration_task'\" "
    "and discards the message. Make the worker register the orchestration "
    "execution task again and verify it."
)
TARGET = "app/jobs/cleanup.py"
OLD = "INTERVAL_SECONDS = 600\n"


def _profile(text: str, execution_profile: str = "full_lifecycle") -> str:
    return ValidatorService.infer_validation_profile(text, execution_profile)


def _plan() -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Apply the requested change",
            "commands": [],
            "verification": f"python -m py_compile {TARGET}",
            "rollback": None,
            "expected_files": [TARGET],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": TARGET,
                    "old": OLD,
                    "new": "INTERVAL_SECONDS = 300\n",
                }
            ],
        }
    ]


def _admit(tmp_path: Path, task: str, **kwargs):
    root = tmp_path / "root"
    (root / "app/jobs").mkdir(parents=True)
    (root / "app/__init__.py").write_text("")
    (root / "app/jobs/__init__.py").write_text("")
    (root / TARGET).write_text(OLD + "\n\ndef run():\n    return INTERVAL_SECONDS\n")
    materialization = materialize_planner_source_context(
        root, expected_paths=[TARGET], workspace_identity=str(root), source_cache={}
    )
    plan = _plan()
    grounded = ground_post_plan_source_materialization(
        plan,
        project_dir=root,
        source_materialization=materialization,
        workspace_identity=str(root),
    )
    assert grounded.ok
    return ValidatorService().validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=task,
        execution_profile="full_lifecycle",
        project_dir=root,
        source_materialization=grounded.materialization,
        **kwargs,
    )


# --- OBS-1 -----------------------------------------------------------------


def test_c7_restore_style_task_is_mutation_capable(tmp_path):
    assert _profile(C7_TASK) == "implementation"
    assert _admit(tmp_path, C7_TASK).accepted


@pytest.mark.parametrize(
    "request_text",
    [
        "Restore the cache cleanup job so it runs on schedule.",
        "Re-enable the cache cleanup job.",
        "Reenable the cache cleanup job.",
        "Reinstate the cache cleanup job.",
        "Prevent the cache cleanup job from deleting active entries.",
        "Make the cache cleanup job work again.",
        "Fix the cache cleanup job so it runs on schedule.",
        "Update the cache cleanup job so it skips locked entries.",
    ],
)
def test_appending_verify_does_not_erase_mutation_intent(request_text):
    assert _profile(request_text) == "implementation"
    assert _profile(f"{request_text} Verify it.") == "implementation"


def test_explicit_fix_prefix_remains_accepted(tmp_path):
    assert _profile(f"Fix: {C7_TASK}") == "implementation"
    assert _admit(tmp_path, f"Fix: {C7_TASK}").accepted


@pytest.mark.parametrize(
    "task",
    [
        "Verify the cache cleanup job runs on schedule.",
        "Inspect the cache cleanup job and report its schedule.",
        "Verify the restore script for the cache cleanup job completes.",
        "Run the cache cleanup job tests again and verify they pass.",
        "Verify the cache cleanup job runs again after a restart.",
        "Review the cache cleanup job and its restore path.",
    ],
)
def test_verify_and_inspect_only_tasks_stay_mutation_forbidden(tmp_path, task):
    assert _profile(task) == "verification"
    verdict = _admit(tmp_path, task)
    assert not verdict.accepted
    assert verdict.verdict.details["verification_profile_mutated_source_assets"] == [
        TARGET
    ]


@pytest.mark.parametrize(
    "task",
    [
        "Ensure the cache cleanup job is registered. Verify it.",
        "Ensure the cache cleanup job runs on schedule again. Verify it.",
        "Make sure the cache cleanup job runs again after a restart and verify it.",
        "Make certain the cache cleanup job runs again and verify it.",
        "Look into the cache cleanup job. Verify it.",
        "Correct behavior is an hourly run. Verify it.",
    ],
)
def test_ambiguous_wording_keeps_read_only_profile(task):
    assert _profile(task) == "verification"


def test_restoration_vocabulary_only_counts_as_clause_initial_imperative():
    assert _profile("Verify app.restore works.") == "verification"
    assert _profile("Verify the job; restore it if broken.") == "implementation"


def test_restoration_intent_does_not_override_explicit_read_only_modes(tmp_path):
    text = "Restore the cache cleanup job. Verify it."
    assert _profile(text, "review_only") == "verification"
    assert _profile(text, "test_only") == "verification"
    assert not _admit(tmp_path, text, workflow_stage="review").accepted


def test_restoration_vocabulary_is_project_agnostic():
    pattern = validator_module._RESTORATIVE_MUTATION_INTENT_RE.pattern
    for term in ("worker", "celery", "register", "router", "permission"):
        assert term not in pattern
    generic = C7_TASK.replace("the worker register", "the scheduler load")
    assert _profile(generic) == _profile(C7_TASK) == "implementation"


# --- OBS-2 -----------------------------------------------------------------


def _budget_exhausted(tmp_path: Path):
    root = tmp_path / "root"
    (root / "app").mkdir(parents=True)
    (root / "app/__init__.py").write_text("")
    filler = "".join(
        f"VALUE_{i:03d} = {i}  # bounded filler line\n" for i in range(120)
    )
    for index in range(4):
        (root / f"app/aa_big{index}.py").write_text(filler)
    (root / "app/zz_target.py").write_text("# header\n" + OLD)
    paths = [f"app/aa_big{index}.py" for index in range(4)] + ["app/zz_target.py"]
    materialization = materialize_planner_source_context(
        root, supporting_paths=paths, workspace_identity=str(root), source_cache={}
    )
    return root, materialization


def _ground(root: Path, materialization, path: str):
    plan = _plan()
    plan[0]["ops"][0]["path"] = path
    plan[0]["expected_files"] = [path]
    return ground_post_plan_source_materialization(
        plan,
        project_dir=root,
        source_materialization=materialization,
        workspace_identity=str(root),
    )


def test_budget_omitted_target_fails_closed_without_repair_routing(tmp_path):
    root, materialization = _budget_exhausted(tmp_path)
    record = materialization.file_map()["app/zz_target.py"]
    assert record.status == SOURCE_STATUS_OMITTED
    assert record.omission_reason == "maximum_total_source_bytes"

    result = _ground(root, materialization, "app/zz_target.py")

    assert result.failure_code == POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE
    assert result.failure_class is None
    assert result.grounded_paths == ()


def test_omitted_as_missing_would_not_reach_pgra_under_the_same_budget(tmp_path):
    root, materialization = _budget_exhausted(tmp_path)
    without_record = dataclasses.replace(
        materialization,
        files=tuple(
            item
            for item in materialization.files
            if item.relative_path != "app/zz_target.py"
        ),
    )

    result = _ground(root, without_record, "app/zz_target.py")

    assert result.failure_code == POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE
    assert result.failure_detail == (
        "grounding would exceed the existing total source-byte bound"
    )
    assert result.grounded_paths == ()
