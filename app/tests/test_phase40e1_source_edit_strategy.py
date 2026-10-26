"""Phase 40E-1 source-edit generation strategy regressions.

These tests exercise the deterministic Plan fixtures and the exact Planning
and repair prompts passed to their adapter. No inference provider is used.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from app.services.orchestration.context.assembly import assemble_planning_prompt
from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.prompt_templates import OrchestrationState
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_path_authority_from_verdict,
)
from app.services.orchestration.validation.rules.core_execution_capability import (
    ExecutionTopology,
)
from app.services.orchestration.validation.validator import ValidatorService


def _plan(operation: dict, path: str, *, description: str = "Edit the module."):
    return [
        {
            "step_number": 1,
            "description": description,
            "commands": [],
            "verification": "python -m pytest -q tests",
            "rollback": None,
            "expected_files": [path],
            "ops": [operation],
        }
    ]


def _materialize(root: Path, task: str, paths: list[str], *, create=()):
    return materialize_planner_source_context(
        root,
        task_description=task,
        expected_paths=paths,
        creation_authorized_paths=list(create),
    )


def _validate(
    root: Path,
    task: str,
    plan: list[dict],
    materialization,
    *,
    execution_topology=None,
):
    result = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=task,
        execution_profile="full_lifecycle",
        project_dir=root,
        title=task,
        description=task,
        source_materialization=materialization,
        execution_topology=execution_topology,
    )
    return getattr(result, "verdict", result)


def _planning_context(root: Path, task: str, materialization):
    state = OrchestrationState(
        session_id="phase40e1-test",
        task_description=task,
        project_name="phase40e1-test",
        project_context="Existing Python package with tests.",
        task_id=1,
    )
    state._project_dir_override = str(root)
    return SimpleNamespace(
        db=None,
        prompt=task,
        execution_profile="full_lifecycle",
        workflow_profile="default",
        planning_adaptation_profile="local_qwen_json_array",
        orchestration_state=state,
        planner_source_materialization=materialization,
    )


def test_assembled_source_edit_contract_reaches_planning_adapter(tmp_path: Path):
    path = "sample.py"
    source = "def value():\n    return 1\n"
    (tmp_path / path).write_text(source, encoding="utf-8")
    task = "Change value() in sample.py to return 2."
    materialization = _materialize(tmp_path, task, [path])
    prompt = assemble_planning_prompt(
        _planning_context(tmp_path, task, materialization),
        {"file_count": 1, "source_file_count": 1},
    )
    captured: list[str] = []

    class Runtime:
        _disable_direct_planning = True

        async def execute_task(self, adapter_prompt: str, **_kwargs):
            captured.append(adapter_prompt)
            return {"output": "[]", "diagnostics": {}}

    asyncio.run(
        PlannerService._execute_task_with_planning_lock(
            Runtime(), prompt, timeout_seconds=10
        )
    )

    assert captured == [prompt]
    assert "`write_file` replaces the whole file" in captured[0]
    assert "prefer an available precise `replace_in_file`" in captured[0]
    assert "exact `old` must be unique in supplied same-path source" in captured[0]
    assert "complete GR2-grounded source" in captured[0]
    assert "## CURRENT SOURCE MATERIALIZATION" in captured[0]
    assert source.strip() in captured[0]


def test_localized_edit_preserves_imports_helpers_and_other_public_functions(
    tmp_path: Path,
):
    path = "sample.py"
    source = (
        "from math import ceil\n\n"
        "DEFAULT = 3\n\n"
        "def _scale(value):\n    return value * DEFAULT\n\n"
        "def total(value):\n    return _scale(value)\n\n"
        "def label(value):\n    return f'item:{value}'\n"
    )
    (tmp_path / path).write_text(source, encoding="utf-8")
    task = "Update total() in sample.py to round its result up."
    materialization = _materialize(tmp_path, task, [path])
    old = "def total(value):\n    return _scale(value)\n"
    new = "def total(value):\n    return ceil(_scale(value))\n"
    plan = _plan(
        {"op": "replace_in_file", "path": path, "old": old, "new": new},
        path,
    )

    verdict = _validate(tmp_path, task, plan, materialization)
    assert verdict.accepted, verdict.reasons
    result = ExecutorService.execute_file_ops(
        tmp_path,
        plan[0]["ops"],
        accepted_path_authority=accepted_path_authority_from_verdict(verdict),
    )

    assert result["success"], result
    updated = (tmp_path / path).read_text(encoding="utf-8")
    assert updated == source.replace(old, new)
    assert "from math import ceil" in updated
    assert "def _scale(value):" in updated
    assert "def label(value):" in updated


def test_authorized_new_file_uses_complete_write_and_existing_complete_rewrite_is_valid(
    tmp_path: Path,
):
    new_path = "generated.py"
    create_task = "Create a new generated.py module with a normalize() function."
    create_materialization = _materialize(
        tmp_path, create_task, [new_path], create=[new_path]
    )
    create_plan = _plan(
        {
            "op": "write_file",
            "path": new_path,
            "content": "def normalize(value):\n    return value.strip().lower()\n",
        },
        new_path,
    )
    create_verdict = _validate(
        tmp_path, create_task, create_plan, create_materialization
    )
    assert create_verdict.accepted, create_verdict.reasons

    existing_path = "module.py"
    original = (
        '"""Small module."""\n\n'
        "from typing import Iterable\n\n\n"
        "def _clean(value: str) -> str:\n    return value.strip()\n\n\n"
        "def values(items: Iterable[str]) -> list[str]:\n"
        "    return [_clean(item) for item in items]\n"
    )
    (tmp_path / existing_path).write_text(original, encoding="utf-8")
    rewrite_task = (
        "Rewrite module.py to add a public count() helper while preserving its API."
    )
    rewrite_materialization = _materialize(tmp_path, rewrite_task, [existing_path])
    rewritten = original + "\n\ndef count(items):\n    return len(values(items))\n"
    rewrite_plan = _plan(
        {"op": "write_file", "path": existing_path, "content": rewritten},
        existing_path,
        description="Rewrite and replace the complete module while preserving its API.",
    )
    rewrite_verdict = _validate(
        tmp_path, rewrite_task, rewrite_plan, rewrite_materialization
    )
    assert rewrite_verdict.accepted, rewrite_verdict.reasons


def test_dropped_definition_rejection_reaches_repair_adapter_with_source_guidance(
    tmp_path: Path, monkeypatch
):
    path = "sample.py"
    source = (
        "from typing import Iterable\n\n"
        "def _clean(value):\n    return value.strip()\n\n"
        "def values(items: Iterable[str]):\n"
        "    return [_clean(item) for item in items]\n"
    )
    (tmp_path / path).write_text(source, encoding="utf-8")
    task = "Update values() in sample.py to normalize each item."
    materialization = _materialize(tmp_path, task, [path])
    rejected_plan = _plan(
        {
            "op": "write_file",
            "path": path,
            "content": (
                "def values(items: Iterable[str]):\n"
                "    return [_clean(item).lower() for item in items]\n"
            ),
        },
        path,
    )
    verdict = _validate(tmp_path, task, rejected_plan, materialization)
    assert verdict.status == "repair_required"
    reason = next(
        reason
        for reason in verdict.reasons
        if "python_write_file_drops_referenced_definitions" in reason
    )
    captured: list[str] = []

    async def capture(_runtime, adapter_prompt, _timeout, **_kwargs):
        captured.append(adapter_prompt)
        return {"output": "[]", "diagnostics": {}}

    monkeypatch.setattr(PlannerService, "_invoke_repair_prompt", staticmethod(capture))
    PlannerService.repair_output(
        runtime_service=SimpleNamespace(),
        task_description=task,
        malformed_output=json.dumps(rejected_plan),
        project_dir=tmp_path,
        timeout_seconds=20,
        logger=logging.getLogger("phase40e1-test"),
        emit_live=lambda *_args, **_kwargs: None,
        reason="Plan validation rejected dropped Python definitions.",
        rejection_reasons=[reason],
        workspace_has_existing_files=True,
        source_materialization=materialization,
    )

    assert len(captured) == 1
    repair = captured[0]
    assert task in repair
    assert "python_write_file_drops_referenced_definitions" in repair
    assert "replace that rejected operation with one `replace_in_file`" in repair
    assert "Do not repeat the rejected fragment as `write_file`" in repair
    assert "## CURRENT SOURCE MATERIALIZATION" in repair
    assert source.strip() in repair

    compact_repair = PlannerService.build_compact_planning_repair_prompt(
        malformed_output=json.dumps(rejected_plan),
        rejection_reasons=[reason],
        guidance_block=materialization.to_prompt_block(provider_safe=True),
        task_description=task,
    )
    assert task in compact_repair
    assert "rejected `write_file` replaces the whole module" in compact_repair
    assert (
        "replace that rejected operation with one `replace_in_file`" in compact_repair
    )
    assert source.strip() in compact_repair


def test_exact_match_edits_still_fail_closed_for_stale_and_ambiguous_source(
    tmp_path: Path,
):
    path = "sample.py"
    duplicate = "    return value + 1\n"
    source = "def first(value):\n" + duplicate + "\n\ndef second(value):\n" + duplicate
    (tmp_path / path).write_text(source, encoding="utf-8")
    task = "Change first() in sample.py to add two."
    materialization = _materialize(tmp_path, task, [path])
    ambiguous_plan = _plan(
        {
            "op": "replace_in_file",
            "path": path,
            "old": duplicate,
            "new": "    return value + 2\n",
        },
        path,
    )
    ambiguous_verdict = _validate(tmp_path, task, ambiguous_plan, materialization)
    assert ambiguous_verdict.accepted, ambiguous_verdict.reasons
    execution = ExecutorService.execute_file_ops(
        tmp_path,
        ambiguous_plan[0]["ops"],
        accepted_path_authority=accepted_path_authority_from_verdict(ambiguous_verdict),
    )
    assert not execution["success"]
    assert "ambiguous" in execution["output"].lower()
    assert (tmp_path / path).read_text(encoding="utf-8") == source

    stale_plan = _plan(
        {
            "op": "replace_in_file",
            "path": path,
            "old": "    return value + 9\n",
            "new": "    return value + 2\n",
        },
        path,
    )
    stale_verdict = _validate(tmp_path, task, stale_plan, materialization)
    assert stale_verdict.status == "repair_required"
    assert any("stale_replace" in reason for reason in stale_verdict.reasons)


def test_missing_source_and_out_of_scope_targets_still_fail_closed(tmp_path: Path):
    package = tmp_path / "pkg"
    package.mkdir()
    sample_path = "pkg/sample.py"
    other_path = "pkg/other.py"
    sample_source = "def value():\n    return 1\n"
    other_source = "def other():\n    return 1\n"
    (tmp_path / sample_path).write_text(sample_source, encoding="utf-8")
    (tmp_path / other_path).write_text(other_source, encoding="utf-8")
    task = "Only modify pkg/sample.py. Change value() in pkg/sample.py to return 2."

    missing_materialization = _materialize(
        tmp_path, "Inspect pkg/other.py.", [other_path]
    )
    ungrounded_plan = _plan(
        {
            "op": "replace_in_file",
            "path": sample_path,
            "old": sample_source,
            "new": "def value():\n    return 2\n",
        },
        sample_path,
    )
    ungrounded_verdict = _validate(
        tmp_path, task, ungrounded_plan, missing_materialization
    )
    assert ungrounded_verdict.status == "repair_required"
    assert any(
        "missing_source_materialization" in reason
        for reason in ungrounded_verdict.reasons
    )

    materialization = _materialize(tmp_path, task, [sample_path, other_path])
    out_of_scope_plan = _plan(
        {
            "op": "replace_in_file",
            "path": other_path,
            "old": other_source,
            "new": "def other():\n    return 2\n",
        },
        other_path,
    )
    out_of_scope_verdict = _validate(tmp_path, task, out_of_scope_plan, materialization)
    assert out_of_scope_verdict.status == "repair_required"
    assert out_of_scope_verdict.details["task_scope_violation_paths"] == [other_path]
    assert not out_of_scope_verdict.accepted


def test_source_edit_does_not_expand_command_capability_admission(tmp_path: Path):
    path = "sample.py"
    source = "def value():\n    return 1\n"
    (tmp_path / path).write_text(source, encoding="utf-8")
    task = "Change value() in sample.py to return 2."
    materialization = _materialize(tmp_path, task, [path])
    plan = _plan(
        {
            "op": "replace_in_file",
            "path": path,
            "old": source,
            "new": "def value():\n    return 2\n",
        },
        path,
    )
    plan[0]["commands"] = ["curl https://example.invalid"]

    verdict = _validate(
        tmp_path,
        task,
        plan,
        materialization,
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )
    assert verdict.status == "repair_required"
    assert any("unsupported_step_command" in reason for reason in verdict.reasons)


def test_workflow_c_full_module_write_pattern_remains_admissible(tmp_path: Path):
    """Keep the already demonstrated Workflow C whole-module edit route viable."""
    path = "dataflow/normalize.py"
    package = tmp_path / "dataflow"
    package.mkdir()
    source = (
        '"""Record normalization."""\n\n'
        "from typing import Dict, Iterable, List\n\n\n"
        "def normalize_id(raw_id: str) -> str:\n"
        "    return raw_id.strip().lower()\n\n\n"
        "def normalize_records(records: Iterable[dict]) -> List[dict]:\n"
        "    latest: Dict[str, dict] = {}\n"
        "    for record in records:\n"
        '        key = record["id"]\n'
        "        current = latest.get(key)\n"
        '        if current is None or record["updated_at"] >= current["updated_at"]:\n'
        '            latest[key] = {**record, "id": normalize_id(record["id"])}\n'
        '    return sorted(latest.values(), key=lambda item: item["id"])\n'
    )
    (tmp_path / path).write_text(source, encoding="utf-8")
    task = "Restore Workflow C duplicate customer semantics in dataflow/normalize.py."
    materialization = _materialize(tmp_path, task, [path])
    rewritten = source.replace(
        '        key = record["id"]', '        key = normalize_id(record["id"])'
    )
    plan = _plan(
        {"op": "write_file", "path": path, "content": rewritten},
        path,
        description="Replace the complete grounded normalize module to restore duplicate handling.",
    )

    verdict = _validate(tmp_path, task, plan, materialization)
    assert verdict.accepted, verdict.reasons
