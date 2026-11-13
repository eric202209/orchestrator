"""P41-PRODUCT-09B semantic target region disclosure regressions.

The fixture mirrors the retained PRODUCT-09 slug workspace.  Target IDs are
always issued by the production inventory because they are version-fenced.
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
import logging
from pathlib import Path

import pytest

from app.services.orchestration.phases import planning_flow
from app.services.orchestration.phases.planning_verification import (
    _strengthen_weak_expected_file_verifications,
)
from app.services.orchestration.planning.normalization import (
    normalize_blank_line_divergent_replace_anchors,
    normalize_existing_file_target_plan,
    normalize_stale_replace_ops_to_small_file_writes,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.prompt_contracts import (
    render_operation_choice_contract,
)
from app.services.orchestration.planning.semantic_target_inventory import (
    build_semantic_target_inventory,
    normalize_provider_semantic_intents,
)
from app.services.orchestration.planning.source_materialization import (
    HINT_TYPE_QUOTED_SNIPPET,
    SourceTargetHint,
    materialize_planner_source_context,
    render_repair_source_materialization,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.validation.workspace_guard import (
    TaskOperationContractViolation,
    normalize_plan,
)

SLUG_PATH = "textkit/slug.py"
SLUG_SOURCE = (
    "import re\n\n\ndef slugify(text):\n"
    '    """Return a lowercase, hyphen-separated slug."""\n'
    "    text = text.strip().lower()\n"
    '    return re.sub(r"[^a-z0-9]+", "-", text)\n'
)
TEST_SOURCE = (
    "from textkit.slug import slugify\n\n\ndef test_slugify_spaces():\n"
    '    assert slugify("Hello World") == "hello-world"\n'
)
# PRODUCT-09C: the retained PRODUCT-09 wording names `slugify(text)` only in a
# preservation clause and no longer issues a handle.  These disclosure tests use
# a modification clause that selects the same `slugify(text)` region.
TASK = (
    "Fix `textkit/slug.py::slugify` so the returned slug never has leading or "
    "trailing hyphens. Change `slugify(text)` as needed. Add regression tests in "
    "`tests/test_slug.py`. Ensure the existing test suite passes."
)
# Shape of the retained PRODUCT-09 whole-file `new` payload.
WHOLE_FILE_NEW = (
    "import re\n\n\ndef slugify(text):\n"
    '    """Return a lowercase, hyphen-separated slug."""\n'
    "    text = text.strip().lower()\n"
    '    slug = re.sub(r"[^a-z0-9]+", "-", text)\n'
    '    return slug.strip("-")'
)
APPENDED_TESTS = (
    "\n\ndef test_slugify_edge_hyphens():\n"
    '    assert slugify("---Hello---World---") == "hello-world"\n'
)
REGION_RULE = (
    "`new` replaces only target_region_text (JSON string), not the file or function"
)
DECLINE_RULE = "for changes outside it, use another grounded operation."


def _workspace(root: Path, slug_source: str = SLUG_SOURCE) -> Path:
    (root / "textkit").mkdir()
    (root / "tests").mkdir()
    (root / SLUG_PATH).write_text(slug_source, encoding="utf-8", newline="")
    (root / "tests/test_slug.py").write_text(TEST_SOURCE, encoding="utf-8")
    return root


def _materialize(root: Path, task: str = TASK, **kwargs):
    return materialize_planner_source_context(root, task_description=task, **kwargs)


def _plan(first_op: dict) -> list[dict]:
    return [
        {
            "description": "Fix slugify to strip leading/trailing hyphens",
            "ops": [first_op],
            "commands": [],
            "verification": "python -m pytest tests/test_slug.py -q",
            "expected_files": [SLUG_PATH],
        },
        {
            "description": "Add regression tests for edge cases",
            "ops": [
                {
                    "op": "append_file",
                    "path": "tests/test_slug.py",
                    "content": APPENDED_TESTS,
                }
            ],
            "commands": [],
            "verification": "python -m pytest tests/test_slug.py -q",
            "expected_files": ["tests/test_slug.py"],
        },
        {
            "description": "Run full test suite",
            "commands": ["python -m pytest tests/ -q"],
            "verification": "python -m pytest tests/ -q",
            "expected_files": [],
        },
    ]


def _production_normalization(plan, root: Path, materialization, *, convert=True):
    """Production planning_flow order; ``convert=False`` reproduces PRODUCT-09."""

    if convert:
        plan = normalize_provider_semantic_intents(
            plan,
            inventory=build_semantic_target_inventory(materialization),
            project_dir=root,
            source_materialization=materialization,
        )
    plan = PlannerService.sanitize_common_plan_issues(plan, task_prompt=TASK)
    plan = _strengthen_weak_expected_file_verifications(plan)
    plan, _ = normalize_existing_file_target_plan(plan, project_dir=str(root))
    plan, _ = normalize_blank_line_divergent_replace_anchors(
        plan, project_dir=str(root), source_materialization=materialization
    )
    plan, _ = normalize_stale_replace_ops_to_small_file_writes(
        plan, project_dir=str(root)
    )
    return normalize_plan(plan, root, logging.getLogger(__name__))


def _validate(plan, root: Path, materialization):
    return ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=TASK,
        execution_profile="full_lifecycle",
        project_dir=root,
        validation_severity="standard",
        source_materialization=materialization,
    )


def _handle(materialization):
    handles = build_semantic_target_inventory(materialization).handles
    assert len(handles) == 1
    return handles[0]


# Test A
def test_prompt_discloses_exact_slugify_target_region(tmp_path):
    materialization = _materialize(_workspace(tmp_path))
    handle = _handle(materialization)
    prompt = materialization.to_prompt_block(provider_safe=True)

    assert handle.region_text == "slugify(text)"
    assert f"target_id: {handle.target_id}" in prompt
    assert 'target_region_text: "slugify(text)"' in prompt
    # Disclosure never exposes selector internals or filesystem identities.
    record = materialization.files[0]
    assert record.version_identity not in prompt
    for forbidden in ("start_byte", "end_byte", "selected_region_sha256"):
        assert forbidden not in prompt


# Test B
def test_prompt_states_new_replaces_only_the_disclosed_region(tmp_path):
    prompt = _materialize(_workspace(tmp_path)).to_prompt_block(provider_safe=True)

    assert REGION_RULE in prompt
    assert DECLINE_RULE in prompt
    contract = render_operation_choice_contract()
    assert "only when the change lies entirely within its `target_region_text`" in (
        contract
    )


def test_repair_projection_discloses_the_same_region(tmp_path):
    materialization = _materialize(_workspace(tmp_path))
    repair = render_repair_source_materialization(
        materialization, provider_safe=True, rejected_paths=(), compaction_level=1
    )

    assert 'target_region_text: "slugify(text)"' in repair
    assert "replaces only its target_region_text" in repair


# Test C
def test_full_file_context_is_not_presented_as_the_replacement_region(tmp_path):
    materialization = _materialize(_workspace(tmp_path))
    prompt = materialization.to_prompt_block(provider_safe=True)

    assert materialization.files[0].selection_strategy == "full_file"
    assert "Materialization context: full_file" not in prompt
    assert "Shown content (full_file) is context, not the replacement region." in (
        prompt
    )
    assert SLUG_SOURCE in prompt
    assert _handle(materialization).region_text != SLUG_SOURCE


# Test D
def test_disclosed_region_preserves_whitespace_and_newlines(tmp_path):
    single = tmp_path / "single"
    single.mkdir()
    spaced = 'VALUE = {"a":  1,\t"b": 2}'
    _workspace(single, slug_source=f"{spaced}\n")
    materialization = _materialize(
        single, task=f"In textkit/slug.py replace `{spaced}` with a frozen mapping."
    )
    assert _handle(materialization).region_text == spaced
    assert f"target_region_text: {json.dumps(spaced)}" in (
        materialization.to_prompt_block(provider_safe=True)
    )

    multi = tmp_path / "multi"
    multi.mkdir()
    region = "CONFIG = dict(\n    a=1,"
    _workspace(multi, slug_source=f"{region}\n    b=2,\n)\n")
    materialization = _materialize(
        multi,
        task="Change CONFIG in textkit/slug.py",
        additional_target_hints=[
            SourceTargetHint(
                text=region,
                hint_type=HINT_TYPE_QUOTED_SNIPPET,
                authority="task_description",
                target_path=SLUG_PATH,
            )
        ],
    )
    assert _handle(materialization).region_text == region
    prompt = materialization.to_prompt_block(provider_safe=True)
    assert 'target_region_text: "CONFIG = dict(\\n    a=1,"' in prompt


def test_region_that_cannot_be_constructed_is_not_disclosed(tmp_path):
    root = _workspace(tmp_path)
    materialization = _materialize(root)
    # Source changes after materialization: the selector cannot be fenced, so
    # no handle (and no possibly stale region text) is issued.
    (root / SLUG_PATH).write_text(SLUG_SOURCE + "# changed\n", encoding="utf-8")

    assert build_semantic_target_inventory(materialization).handles == ()
    assert "target_region_text" not in materialization.to_prompt_block(
        provider_safe=True
    )


# Test E
def test_semantic_conversion_runs_before_sanitization_in_planning_flow():
    tree = ast.parse(inspect.getsource(planning_flow))
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = {}
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None
                )
                if name in {
                    "normalize_provider_semantic_intents",
                    "sanitize_common_plan_issues",
                }:
                    calls.setdefault(name, node.lineno)
        if len(calls) == 2:
            assert (
                calls["normalize_provider_semantic_intents"]
                < calls["sanitize_common_plan_issues"]
            )
            return
    pytest.fail("planning_flow has no function calling both normalization stages")


def test_retained_operation_shape_keeps_selector_through_production_order(tmp_path):
    root = _workspace(tmp_path)
    materialization = _materialize(root)
    target_id = _handle(materialization).target_id
    raw_plan = _plan(
        {
            "op": "replace_in_file",
            "path": SLUG_PATH,
            "target_id": target_id,
            "new": "replacement content",
        }
    )

    normalized = _production_normalization(
        copy.deepcopy(raw_plan), root, materialization
    )
    op = normalized[0]["ops"][0]
    assert set(op) == {"op", "path", "selector", "new"}
    assert (op["selector"]["start_byte"], op["selector"]["end_byte"]) == (16, 29)

    # Omitting conversion (the PRODUCT-09 harness path) loses target_id and
    # produces the misleading missing-`old` failure.
    with pytest.raises(TaskOperationContractViolation, match="got raw keys"):
        _production_normalization(
            copy.deepcopy(raw_plan), root, materialization, convert=False
        )


# Test F
def test_whole_file_payload_into_slugify_region_is_still_rejected(tmp_path):
    root = _workspace(tmp_path)
    materialization = _materialize(root)
    plan = _production_normalization(
        _plan(
            {
                "op": "replace_in_file",
                "path": SLUG_PATH,
                "target_id": _handle(materialization).target_id,
                "new": WHOLE_FILE_NEW,
            }
        ),
        root,
        materialization,
    )

    outcome = _validate(plan, root, materialization)
    assert not outcome.accepted
    assert any("python_source_syntax_invalid" in r for r in outcome.reasons)


# Section 7 eligibility limitation (not solved here)
def test_disclosed_region_excludes_the_required_return_line_change(tmp_path):
    materialization = _materialize(_workspace(tmp_path))
    region = _handle(materialization).region_text

    assert "return" not in region
    assert 'return re.sub(r"[^a-z0-9]+", "-", text)' in SLUG_SOURCE
    # The contract lets the Planner decline the handle for a grounded legacy edit.
    assert DECLINE_RULE in materialization.to_prompt_block(provider_safe=True)
    assert "`{op,path,old,new}`" in render_operation_choice_contract()


# Test G
def test_legacy_old_new_operation_remains_valid(tmp_path):
    root = _workspace(tmp_path)
    materialization = _materialize(root)
    plan = _production_normalization(
        _plan(
            {
                "op": "replace_in_file",
                "path": SLUG_PATH,
                "old": '    return re.sub(r"[^a-z0-9]+", "-", text)\n',
                "new": (
                    '    slug = re.sub(r"[^a-z0-9]+", "-", text)\n'
                    '    return slug.strip("-")\n'
                ),
            }
        ),
        root,
        materialization,
    )

    assert set(plan[0]["ops"][0]) == {"op", "path", "old", "new"}
    outcome = _validate(plan, root, materialization)
    assert outcome.accepted, outcome.reasons
