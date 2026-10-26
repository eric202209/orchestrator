"""Provider-free Phase 40E-2 fallback and stale-repair contract regressions."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.services.orchestration.planning import repair_prompts
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.semantic_target_inventory import (
    build_semantic_target_inventory,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)


def _stale_plan(path: str, old: str = "stale source") -> str:
    return json.dumps(
        [
            {
                "step_number": 1,
                "description": "Update the existing module",
                "commands": [],
                "verification": "python3 -m pytest -q",
                "expected_files": [path],
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": path,
                        "old": old,
                        "new": "current replacement",
                    }
                ],
            }
        ]
    )


def _stale_reasons(path: str, old: str = "stale source") -> list[str]:
    return [
        f"stale_replace_ops_steps: replace_in_file old text not found in {path}",
        f"step 1 replace_in_file old text not found in {path}: {old}",
    ]


def _compact_stale(
    *,
    path: str,
    task: str,
    malformed_output: str,
    rejection_reasons: list[str],
    project_dir: Path | None,
    source_materialization=None,
    guidance_block: str = "",
) -> str:
    return PlannerService.build_compact_planning_repair_prompt(
        malformed_output=malformed_output,
        rejection_reasons=rejection_reasons,
        task_description=task,
        project_dir=project_dir,
        source_materialization=source_materialization,
        guidance_block=guidance_block,
    )


def test_minimal_fallbacks_keep_task_tail_source_edit_and_execution_boundaries(
    tmp_path: Path,
):
    path = "src/sample.py"
    source = "def run(value):\n    return value + 1\n\nneedle()\n"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text(source, encoding="utf-8")
    task = (
        "Inspect the existing module and make the requested localized change. "
        + "Preserve public behavior and existing tests. " * 28
        + "Do not change test expectations. Edit only under src/."
    )
    materialization = materialize_planner_source_context(
        tmp_path, task_description=task, expected_paths=[path]
    )

    for builder in (
        PlannerService.build_minimal_planning_prompt,
        PlannerService.build_ultra_minimal_planning_prompt,
    ):
        prompt = builder(
            task,
            tmp_path,
            source_materialization=materialization,
        )
        prompt = PlannerService.include_execution_capability_contract(
            prompt,
            runtime_service=SimpleNamespace(),
            project_dir=tmp_path,
        )

        assert "Do not change test expectations. Edit only under src/." in prompt
        assert source.strip() in prompt
        assert "For a localized existing-file change" in prompt
        assert "## EXECUTION CAPABILITY CONTRACT" in prompt
        assert "APA" in prompt
        assert "GR2" in prompt


def test_stale_exact_match_repair_uses_current_unique_grounding(tmp_path: Path):
    path = "src/sample.py"
    source = "def run(value):\n    return value + 1\n"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text(source, encoding="utf-8")
    task = "Change run() to add two while preserving the module."
    materialization = materialize_planner_source_context(
        tmp_path, task_description=task, expected_paths=[path]
    )
    source_block = materialization.to_prompt_block(provider_safe=True)
    prompt = _compact_stale(
        path=path,
        task=task,
        malformed_output=_stale_plan(path),
        rejection_reasons=_stale_reasons(path),
        project_dir=tmp_path,
        source_materialization=materialization,
        guidance_block=source_block,
    )

    assert "Never use rejected stale text or invent source" in prompt
    assert "exact current source" in prompt
    assert "only when it occurs uniquely in complete current same-path source" in prompt
    assert source.strip() in prompt
    assert (
        "Existing-file write_file is allowed only for a task-required full rewrite"
        in prompt
    )
    assert "Use a write_file op for" not in prompt


def test_ambiguous_exact_match_guidance_requires_unique_current_text(tmp_path: Path):
    path = "src/sample.py"
    duplicate = "    return value + 1\n"
    source = f"def first(value):\n{duplicate}\ndef second(value):\n{duplicate}"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text(source, encoding="utf-8")
    task = "Change first() only."
    materialization = materialize_planner_source_context(
        tmp_path, task_description=task, expected_paths=[path]
    )
    prompt = _compact_stale(
        path=path,
        task=task,
        malformed_output=_stale_plan(path, duplicate),
        rejection_reasons=_stale_reasons(path, duplicate),
        project_dir=tmp_path,
        source_materialization=materialization,
        guidance_block=materialization.to_prompt_block(provider_safe=True),
    )

    assert "only when it occurs uniquely in complete current same-path source" in prompt
    inventory = build_semantic_target_inventory(materialization)
    if inventory.handles:
        assert any(
            f"target_id: {handle.target_id}" in prompt for handle in inventory.handles
        )


def test_missing_source_disallows_precise_replace_and_whole_file_reconstruction():
    path = "src/missing.py"
    prompt = _compact_stale(
        path=path,
        task="Edit the existing module.",
        malformed_output=_stale_plan(path),
        rejection_reasons=_stale_reasons(path),
        project_dir=None,
    )

    assert "No grounded replace is available" in prompt
    assert "Do not write_file to this existing target" in prompt
    assert "partial excerpt cannot ground a whole-file rewrite" in prompt


def test_semantic_target_is_preferred_when_visible(tmp_path: Path):
    path = "src/sample.py"
    source = "def run(value):\n    return value + 1\n\nneedle()\n"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text(source, encoding="utf-8")
    task = "Replace the exact call `needle()` in src/sample.py."
    materialization = materialize_planner_source_context(
        tmp_path, task_description=task, expected_paths=[path]
    )
    inventory = build_semantic_target_inventory(materialization)
    assert inventory.handles
    prompt = _compact_stale(
        path=path,
        task=task,
        malformed_output=_stale_plan(path),
        rejection_reasons=_stale_reasons(path),
        project_dir=tmp_path,
        source_materialization=materialization,
        guidance_block=materialization.to_prompt_block(provider_safe=True),
    )

    assert "For a localized edit, prefer a listed target_id" in prompt
    assert any(
        f"target_id: {handle.target_id}" in prompt for handle in inventory.handles
    )


def test_partial_existing_excerpt_never_authorizes_whole_file_rewrite(
    tmp_path: Path,
):
    path = "src/large.py"
    source = "\n".join(f"line_{index} = {index}" for index in range(500))
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text(source, encoding="utf-8")
    task = "Update one localized line in src/large.py."
    materialization = materialize_planner_source_context(
        tmp_path, task_description=task, expected_paths=[path]
    )
    record = materialization.file_map()[path]
    assert record.truncated
    prompt = _compact_stale(
        path=path,
        task=task,
        malformed_output=_stale_plan(path),
        rejection_reasons=_stale_reasons(path),
        project_dir=tmp_path,
        source_materialization=materialization,
        guidance_block=materialization.to_prompt_block(provider_safe=True),
    )

    assert "Do not write_file to this existing target" in prompt
    assert "partial excerpt cannot ground a whole-file rewrite" in prompt


def test_complete_grounding_permits_task_required_whole_file_rewrite(tmp_path: Path):
    path = "src/sample.py"
    source = "def run(value):\n    return value + 1\n"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text(source, encoding="utf-8")
    task = "Rewrite the complete module to change run() and preserve its API."
    materialization = materialize_planner_source_context(
        tmp_path, task_description=task, expected_paths=[path]
    )
    prompt = _compact_stale(
        path=path,
        task=task,
        malformed_output=_stale_plan(path),
        rejection_reasons=_stale_reasons(path),
        project_dir=tmp_path,
        source_materialization=materialization,
        guidance_block=materialization.to_prompt_block(provider_safe=True),
    )

    assert "complete grounded source and replacement" in prompt
    assert (
        "Existing-file write_file is allowed only for a task-required full rewrite"
        in prompt
    )


def test_full_and_compact_stale_repair_paths_share_grounding_contract(tmp_path: Path):
    path = "src/sample.py"
    source = "def run(value):\n    return value + 1\n"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text(source, encoding="utf-8")
    task = "Make a localized source change in the existing module."
    malformed_output = _stale_plan(path)
    reasons = _stale_reasons(path)
    materialization = materialize_planner_source_context(
        tmp_path, task_description=task, expected_paths=[path]
    )
    full = PlannerService.build_planning_repair_prompt_with_metadata(
        task_description=task,
        malformed_output=malformed_output,
        project_dir=tmp_path,
        rejection_reasons=reasons,
    ).prompt
    compact = _compact_stale(
        path=path,
        task=task,
        malformed_output=malformed_output,
        rejection_reasons=reasons,
        project_dir=tmp_path,
        source_materialization=materialization,
        guidance_block=materialization.to_prompt_block(provider_safe=True),
    )

    for prompt in (full, compact):
        assert "For a localized edit, prefer a listed target_id" in prompt
        assert (
            "only when it occurs uniquely in complete current same-path source"
            in prompt
        )
        assert "Never use rejected stale text or invent source" in prompt
        assert "Use a write_file op for" not in prompt


def test_minimum_budget_stale_envelope_keeps_repair_contract(tmp_path: Path):
    path = "src/sample.py"
    source = "def run(value):\n    return value + 1\n"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text(source, encoding="utf-8")
    task = "Change run() locally and preserve the module."
    malformed_output = _stale_plan(path)
    reasons = _stale_reasons(path)
    materialization = materialize_planner_source_context(
        tmp_path, task_description=task, expected_paths=[path]
    )

    envelope = repair_prompts.build_minimum_safe_stale_replace_repair_envelope(
        task_description=task,
        malformed_output=malformed_output,
        rejection_reasons=reasons,
        prompt_profile="default",
        apply_prompt_profile=None,
        source_materialization=materialization,
    )
    assert envelope is not None
    assert "Never use rejected stale text or invent source" in envelope.prompt
    assert (
        "only when it occurs uniquely in complete current same-path source"
        in envelope.prompt
    )
    assert (
        "Existing-file write_file is allowed only for a task-required full rewrite"
        in envelope.prompt
    )
