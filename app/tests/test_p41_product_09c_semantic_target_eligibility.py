"""P41-PRODUCT-09C semantic target eligibility regressions.

A literal named only in a preservation clause must not become a replacement
handle; modification clauses keep existing semantic targeting.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.phases.planning_support import (
    build_context_semantic_target_inventory,
)
from app.services.orchestration.planning.read_only_discovery import (
    DISCOVERY_ADMISSION_SKIPPED,
    assess_discovery_admission,
)
from app.services.orchestration.planning.semantic_target_inventory import (
    SemanticTargetContractError,
    build_semantic_target_inventory,
    normalize_provider_semantic_intents,
)
from app.services.orchestration.planning.source_materialization import (
    extract_source_target_hints,
    materialize_planner_source_context,
)
from app.services.orchestration.prompt_templates import PromptTemplates

SLUG_PATH = "textkit/slug.py"
SLUG_SOURCE = (
    "import re\n\n\ndef slugify(text):\n"
    '    """Return a lowercase, hyphen-separated slug."""\n'
    "    text = text.strip().lower()\n"
    '    return re.sub(r"[^a-z0-9]+", "-", text)\n'
)
# Retained PRODUCT-09 Task wording.
PRESERVATION_TASK = (
    "Fix `textkit/slug.py::slugify` so the returned slug never has leading or "
    "trailing hyphens. Preserve the existing `slugify(text)` interface. Add "
    "regression tests in `tests/test_slug.py`. Ensure the existing test suite "
    "remains passing."
)
MODIFICATION_TASK = (
    "In textkit/slug.py, change the implementation of `slugify(text)`\n"
    "to remove leading and trailing hyphens."
)
MIXED_TASK = (
    "In textkit/slug.py, modify `slugify(text)` to strip edge hyphens, "
    "but preserve its signature."
)
LEGACY_ONLY_CONTRACT = (
    "Semantic target mode is unavailable for this task. Do not emit `target_id`. "
    "The only supported replace shape is `{op,path,old,new}`"
)


def _workspace(root: Path) -> Path:
    (root / "textkit").mkdir()
    (root / "tests").mkdir()
    (root / SLUG_PATH).write_text(SLUG_SOURCE, encoding="utf-8", newline="")
    (root / "tests/test_slug.py").write_text(
        "from textkit.slug import slugify\n", encoding="utf-8"
    )
    return root


def _planning_prompt(root: Path, task: str, materialization) -> str:
    """Compose the prompt the way ``assemble_planning_prompt`` does."""

    return (
        PromptTemplates.build_planning_prompt(
            task_description=task,
            project_dir=str(root),
            source_materialization=materialization,
        )
        + "\n\n"
        + materialization.to_prompt_block(provider_safe=True)
    )


def _handles(root: Path, task: str):
    materialization = materialize_planner_source_context(root, task_description=task)
    return materialization, build_semantic_target_inventory(materialization).handles


def _hint(task: str):
    hints = [h for h in extract_source_target_hints(task) if h.text == "slugify(text)"]
    assert len(hints) == 1
    return hints[0]


# A
def test_preservation_only_hint_issues_no_handle(tmp_path):
    root = _workspace(tmp_path)
    materialization, handles = _handles(root, PRESERVATION_TASK)
    record = next(f for f in materialization.files if f.relative_path == SLUG_PATH)

    assert _hint(PRESERVATION_TASK).preservation_reference is True
    assert handles == ()
    # The literal still locates context; only replacement eligibility is withheld.
    assert record.target_hint == "slugify(text)"
    assert record.target_region_eligibility_reason == "preservation_reference"
    assert "target_id:" not in materialization.to_prompt_block(provider_safe=True)


# B
def test_modification_hint_keeps_semantic_target(tmp_path):
    _, handles = _handles(_workspace(tmp_path), MODIFICATION_TASK)

    assert _hint(MODIFICATION_TASK).preservation_reference is False
    assert [(h.path, h.region_text) for h in handles] == [(SLUG_PATH, "slugify(text)")]


# C
def test_preservation_of_another_aspect_does_not_suppress_modification_target(
    tmp_path,
):
    _, handles = _handles(_workspace(tmp_path), MIXED_TASK)

    assert _hint(MIXED_TASK).preservation_reference is False
    assert [h.region_text for h in handles] == ["slugify(text)"]


def test_and_joined_preservation_of_another_aspect_keeps_target(tmp_path):
    task = (
        "Change `slugify(text)` in textkit/slug.py and preserve focused "
        "coverage in tests/test_slug.py."
    )
    _, handles = _handles(_workspace(tmp_path), task)

    assert _hint(task).preservation_reference is False
    assert [h.region_text for h in handles] == ["slugify(text)"]


def test_modification_literal_outranks_preserved_literal_in_same_file(tmp_path):
    task = (
        "In textkit/slug.py, preserve `slugify(text)`; "
        'replace `text.strip().lower()` with "text.casefold()".'
    )
    _, handles = _handles(_workspace(tmp_path), task)

    assert [h.region_text for h in handles] == ["text.strip().lower()"]


# D
@pytest.mark.parametrize(
    "task",
    [
        "Replace the exact snippet `needle()` in target.txt.",
        "replace target.txt `needle()`",
        "Update `needle()` in target.txt to call the new helper.",
    ],
)
def test_existing_modification_phrasings_remain_eligible(tmp_path, task):
    (tmp_path / "target.txt").write_text("needle()\n", encoding="utf-8")
    materialization = materialize_planner_source_context(
        tmp_path, task_description=task, expected_paths=["target.txt"]
    )
    handles = build_semantic_target_inventory(materialization).handles

    assert [h.region_text for h in handles] == ["needle()"]


# E
def test_no_eligible_handle_keeps_grounded_legacy_contract(tmp_path):
    root = _workspace(tmp_path)
    materialization, _ = _handles(root, PRESERVATION_TASK)
    prompt = _planning_prompt(root, PRESERVATION_TASK, materialization)

    assert LEGACY_ONLY_CONTRACT in prompt
    assert "target_id: tgt_" not in prompt
    assert "Legacy replace_in_file may use exact old/new" in prompt
    legacy = [
        {
            "description": "Fix slugify",
            "commands": [],
            "verification": "python -m pytest -q",
            "expected_files": [SLUG_PATH],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": SLUG_PATH,
                    "old": '    return re.sub(r"[^a-z0-9]+", "-", text)\n',
                    "new": '    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")\n',
                }
            ],
        }
    ]
    converted = normalize_provider_semantic_intents(
        copy.deepcopy(legacy),
        inventory=build_semantic_target_inventory(materialization),
        project_dir=root,
        source_materialization=materialization,
    )
    assert converted == legacy


# F
def test_remaining_handles_disclose_exact_region(tmp_path):
    root = _workspace(tmp_path)
    materialization, handles = _handles(root, MODIFICATION_TASK)
    prompt = materialization.to_prompt_block(provider_safe=True)

    assert len(handles) == 1
    assert f"target_id: {handles[0].target_id}" in prompt
    assert 'target_region_text: "slugify(text)"' in prompt


# G
@pytest.mark.parametrize(
    "task",
    [
        # Preservation and change in one clause: ambiguous, fail closed.
        "In textkit/slug.py, replace `slugify(text)` without changing callers.",
        # Preservation governs the literal's own coordinated clause.
        "In textkit/slug.py, keep `slugify(text)` and change its body.",
        # Same literal preserved in one clause and modified in another.
        "In textkit/slug.py, preserve `slugify(text)`. Fix `slugify(text)` hyphens.",
        "In textkit/slug.py, do not change `slugify(text)`.",
    ],
)
def test_ambiguous_or_preserved_provenance_is_not_promoted(tmp_path, task):
    _, handles = _handles(_workspace(tmp_path), task)

    assert _hint(task).preservation_reference is True
    assert handles == ()


def test_otherwise_ineligible_hint_is_not_promoted_by_modification_clause(tmp_path):
    # A definition is a locator, not a replacement region (Phase 33D-8R).
    _, handles = _handles(
        _workspace(tmp_path), "In textkit/slug.py, modify `def slugify(text)`."
    )
    assert handles == ()


# H
def test_production_inventory_prompt_conversion_and_admission_agree(tmp_path):
    root = _workspace(tmp_path)
    for task, expected_handles in ((PRESERVATION_TASK, 0), (MODIFICATION_TASK, 1)):
        materialization = materialize_planner_source_context(
            root, task_description=task
        )
        ctx = SimpleNamespace(
            planner_source_materialization=materialization, read_only_observation=None
        )
        inventory = build_context_semantic_target_inventory(ctx)
        prompt = _planning_prompt(root, task, materialization)

        assert len(inventory.handles) == expected_handles
        assert prompt.count("target_id: tgt_") == expected_handles
        for handle in inventory.handles:
            assert f"target_id: {handle.target_id}" in prompt
            converted = normalize_provider_semantic_intents(
                [
                    {
                        "ops": [
                            {
                                "op": "replace_in_file",
                                "path": SLUG_PATH,
                                "target_id": handle.target_id,
                                "new": "slugify(value)",
                            }
                        ]
                    }
                ],
                inventory=inventory,
                project_dir=root,
                source_materialization=materialization,
            )
            assert set(converted[0]["ops"][0]) == {"op", "path", "selector", "new"}
        # Suppression does not newly require read-only discovery.
        admission = assess_discovery_admission(
            prompt=task, planner_contract=None, materialization=materialization
        )
        assert admission.status == DISCOVERY_ADMISSION_SKIPPED

    # A target ID for the suppressed region cannot be smuggled into conversion.
    materialization = materialize_planner_source_context(
        root, task_description=PRESERVATION_TASK
    )
    modification_id = (
        build_semantic_target_inventory(
            materialize_planner_source_context(root, task_description=MODIFICATION_TASK)
        )
        .handles[0]
        .target_id
    )
    with pytest.raises(SemanticTargetContractError) as exc_info:
        normalize_provider_semantic_intents(
            [
                {
                    "ops": [
                        {
                            "op": "replace_in_file",
                            "path": SLUG_PATH,
                            "target_id": modification_id,
                            "new": "slugify(value)",
                        }
                    ]
                }
            ],
            inventory=build_semantic_target_inventory(materialization),
            project_dir=root,
            source_materialization=materialization,
        )
    assert exc_info.value.code == "unknown_target_id"
