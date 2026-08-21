"""Provider-free POST33-PL3 minimum Plan contract replays."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.services.orchestration.planning.plan_sanitizer import (
    sanitize_common_plan_issues,
)
from app.services.orchestration.planning.semantic_target_inventory import (
    SemanticTargetContractError,
    build_semantic_target_inventory,
    normalize_provider_semantic_intents,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.validation.parsing import extract_plan_steps
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.validation.workspace_guard import normalize_plan


PATH = "target.py"
SOURCE = "def target(value: int) -> int:\n    return value\n\nresult = target()\n"
TASK = f"Replace the exact call `target()` in {PATH}."


def _materialize(root: Path, *, source: str = SOURCE, task: str = TASK):
    (root / PATH).write_text(source, encoding="utf-8")
    return materialize_planner_source_context(
        root,
        task_description=task,
        expected_paths=[PATH],
    )


def _write_plan(*, compact: bool, path: str = "notes.py") -> list[dict]:
    step = {
        "description": "Create the requested file",
        "commands": [],
        "verification": f"python -m py_compile {path}",
        "expected_files": [path],
        "ops": [
            {
                "op": "write_file",
                "path": path,
                "content": "value = 1\n",
            }
        ],
    }
    if not compact:
        step = {
            "step_number": 1,
            **step,
            "rollback": f"rm -f {path}",
        }
    return [step]


def _semantic_plan(target_id: str, *, compact: bool) -> list[dict]:
    step = {
        "description": "Replace the selected source region",
        "commands": [],
        "verification": f"python -m py_compile {PATH}",
        "expected_files": [PATH],
        "ops": [
            {
                "op": "replace_in_file",
                "path": PATH,
                "target_id": target_id,
                "new": "other()",
            }
        ],
    }
    if not compact:
        step = {"step_number": 1, **step, "rollback": None}
    return [step]


def _canonicalize(plan: list[dict], task: str = "") -> list[dict]:
    return sanitize_common_plan_issues(plan, task_prompt=task)


def test_compact_provider_step_normalizes_to_existing_canonical_shape(tmp_path):
    compact = _write_plan(compact=True)

    assert extract_plan_steps(compact) == compact
    canonical = _canonicalize(compact, "Create notes.py")

    assert canonical[0]["step_number"] == 1
    assert canonical[0]["rollback"] is None
    assert set(canonical[0]) == {
        "step_number",
        "description",
        "commands",
        "verification",
        "rollback",
        "expected_files",
        "ops",
    }

    (tmp_path / "notes.py").write_text("value = 1\n", encoding="utf-8")
    stored = normalize_plan(compact, tmp_path, logging.getLogger("post33-pl3"))
    assert stored[0]["step_number"] == 1
    assert stored[0]["rollback"] is None


def test_old_plan_format_remains_accepted_and_authoritative(tmp_path):
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description="Create notes.py",
        expected_paths=["notes.py"],
        creation_authorized_paths=["notes.py"],
    )
    old_plan = _canonicalize(_write_plan(compact=False), "Create notes.py")
    verdict = ValidatorService.validate_plan(
        old_plan,
        output_text=json.dumps(old_plan),
        task_prompt="Create notes.py",
        execution_profile="implementation",
        project_dir=tmp_path,
        source_materialization=materialization,
    )

    assert verdict.accepted, verdict.reasons
    assert old_plan[0]["step_number"] == 1
    assert old_plan[0]["rollback"] == "rm -f notes.py"


def test_array_order_wins_over_provider_numbering_without_changing_execution_order():
    plan = [
        {
            "step_number": 99,
            "description": "Create the prerequisite",
            "commands": [],
            "verification": "test -f one.txt",
            "rollback": None,
            "expected_files": ["one.txt"],
            "ops": [{"op": "write_file", "path": "one.txt", "content": "1\n"}],
        },
        {
            "step_number": 4,
            "description": "Append the dependent value",
            "commands": [],
            "verification": "test -f one.txt",
            "rollback": None,
            "expected_files": ["one.txt"],
            "ops": [{"op": "append_file", "path": "one.txt", "content": "2\n"}],
        },
    ]

    canonical = _canonicalize(plan, "Create one.txt and append the dependent value")

    assert [step["step_number"] for step in canonical] == [1, 2]
    assert [step["ops"][0]["op"] for step in canonical] == [
        "write_file",
        "append_file",
    ]


def test_typed_ops_derive_expected_files_but_shell_artifacts_do_not():
    typed = _canonicalize(
        [
            {
                "description": "Write the typed artifact",
                "commands": [],
                "verification": "python -m py_compile artifact.py",
                "expected_files": [],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "artifact.py",
                        "content": "value = 1\n",
                    }
                ],
            }
        ],
        "Create artifact.py",
    )
    shell = _canonicalize(
        [
            {
                "description": "Run the project generator",
                "commands": ["python -m generator"],
                "verification": "python -m generator --check",
                "expected_files": [],
            }
        ],
        "Run the project generator",
    )

    assert typed[0]["expected_files"] == ["artifact.py"]
    assert shell[0]["expected_files"] == []


def test_exact_semantic_target_replay_has_no_model_old_bytes(tmp_path):
    materialization = _materialize(tmp_path)
    inventory = build_semantic_target_inventory(materialization)
    assert len(inventory.handles) == 1
    target_id = inventory.handles[0].target_id

    current = normalize_provider_semantic_intents(
        _semantic_plan(target_id, compact=False),
        inventory=inventory,
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    proposed = normalize_provider_semantic_intents(
        _semantic_plan(target_id, compact=True),
        inventory=inventory,
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    current = _canonicalize(current, TASK)
    proposed = _canonicalize(proposed, TASK)

    assert proposed == current
    assert set(proposed[0]["ops"][0]) == {"op", "path", "selector", "new"}
    assert "old" not in json.dumps(proposed)


def test_ambiguous_target_cannot_be_derived_from_compact_or_current_contract(tmp_path):
    materialization = _materialize(
        tmp_path,
        source=SOURCE + "\n" + SOURCE,
    )
    assert build_semantic_target_inventory(materialization).handles == ()

    plan = _semantic_plan("tgt_unissued", compact=True)
    with pytest.raises(SemanticTargetContractError) as exc_info:
        normalize_provider_semantic_intents(
            plan,
            inventory=build_semantic_target_inventory(materialization),
            project_dir=tmp_path,
            source_materialization=materialization,
        )

    assert exc_info.value.code == "unknown_target_id"


def test_wrong_target_remains_a_path_semantic_failure(tmp_path):
    correct = "app/tasks/maintenance.py"
    wrong = "app/celery_app.py"
    (tmp_path / correct).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / wrong).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / correct).write_text("def scheduled_task_execution():\n    pass\n")
    (tmp_path / wrong).write_text("def unrelated():\n    pass\n")
    task = f"Only {correct} may be modified. Fix scheduled_task_execution there."
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description=task,
        expected_paths=[correct, wrong],
    )
    plan = _canonicalize(
        [
            {
                "description": "Modify the selected function",
                "commands": [],
                "verification": f"python -m py_compile {wrong}",
                "expected_files": [wrong],
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": wrong,
                        "old": "def unrelated():\n    pass\n",
                        "new": "def unrelated():\n    return 1\n",
                    }
                ],
            }
        ],
        task,
    )
    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=task,
        execution_profile="implementation",
        project_dir=tmp_path,
        source_materialization=materialization,
    )

    assert not verdict.accepted
    assert any("task scope violation" in reason for reason in verdict.reasons)


def test_planning_prompt_subtracts_only_proven_positional_and_rollback_fields(tmp_path):
    from app.services.orchestration.planning.planning_prompts import (
        build_minimal_planning_prompt,
    )

    prompt = build_minimal_planning_prompt(
        "Create notes.py",
        project_dir=tmp_path,
        workspace_has_existing_files=True,
    )

    assert "optional keys are step_number, rollback, and ops" in prompt
    assert "Array order is authoritative" in prompt
    assert "workspace snapshots own restoration" in prompt
    assert "expected_files" in prompt
    assert "commands" in prompt and "verification" in prompt
    assert (
        "Each step must include these required keys, optional ops, and no other keys"
        not in prompt
    )
