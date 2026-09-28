"""PHASE36-MAINT-GR4 — long-file Grounding continuation adjudication.

After GR3, the REENTRY-4 shape materializes only the head of a ~5164-byte
``router.py`` (``head_fallback_no_target``), while the permission registration
sits near byte 3230.  GR4 found no Product defect: legacy discovery is one
bounded turn by contract, the same ``read_file`` observation (<= 4096 bytes) is
rendered to Planning as advisory evidence and covers that region, narrow edits
are version-fenced against the complete current file, and whole-file
replacement still requires complete Planning-visible source (GR2).  These cases
pin that adjudicated behavior with the real discovery, materialization,
prompt, and validator functions.  No provider is called.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.planning import source_materialization as sm
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.read_only_discovery import (
    DISCOVERY_ADMISSION_REQUIRED,
    MAX_FILE_BYTES,
    MAX_FILE_LINES,
    DiscoveryContractError,
    assess_discovery_admission,
    execute_discovery_request,
    materialize_observation_source_context,
    parse_discovery_request,
    render_discovery_observation,
    run_discovery_stage,
)
from app.services.orchestration.planning.source_materialization import (
    HINT_AUTHORITY_TASK_DESCRIPTION,
    SELECTION_FULL_FILE,
    SELECTION_HEAD_FALLBACK,
    SELECTION_TARGET_EXACT,
    SELECTION_TARGET_WITH_STRUCTURAL_HEAD,
    extract_source_target_hints,
    materialize_planner_source_context,
)
from app.services.orchestration.validation.validator import ValidatorService

TARGET = "app/api/v1/router.py"
GR2_CODE = "existing_file_rewrite_requires_complete_planning_source"
TASK = (
    "Authenticated clients should be able to GET /api/v1/permissions/pending, "
    "and the doubled /api/v1/permissions/permissions/pending route should not "
    "be exposed. Repair the externally observable behavior while preserving "
    "the existing permission semantics, then verify the public contract."
)
PERMISSION_BLOCK = (
    "# Permission Approval\napi_router.include_router(\n    permissions.router,\n"
    '    prefix="/permissions",\n    tags=["permissions"],\n'
    "    dependencies=[Depends(get_current_active_user)],\n)\n\n"
)
NARROW_OLD = '    permissions.router,\n    prefix="/permissions",\n'
VERIFY = "python -c \"import ast; ast.parse(open('app/api/v1/router.py').read())\""


def _area(i: int) -> str:
    return (
        f"# Area {i}\napi_router.include_router(\n    area_{i}.router,\n"
        f'    prefix="/area{i}",\n    tags=["area{i}"],\n'
        "    dependencies=[Depends(get_current_active_user)],\n)\n\n"
    )


def _router(before: int, after: int) -> str:
    return (
        '"""API router."""\n\nfrom fastapi import APIRouter, Depends\n\n'
        "from app.api.v1.endpoints import permissions\n\n"
        "api_router = APIRouter()\n\n\n"
        '@api_router.get("/health")\nasync def health_check():\n'
        "    return health_payload()\n\n\n"
        + "".join(_area(i) for i in range(before))
        + PERMISSION_BLOCK
        + "".join(_area(i) for i in range(before, before + after))
    )


# REENTRY-4 shape: ~5164 bytes, registration near byte 3230, inside the
# bounded read_file observation but outside the 2000-byte Planning window.
ROUTER = _router(20, 11)
# Same shape with the registration beyond the read_file observation bound.
DEEP_ROUTER = _router(29, 2)


def _workspace(tmp_path: Path, content: str = ROUTER) -> Path:
    root = tmp_path.resolve()
    (root / "app/api/v1").mkdir(parents=True)
    (root / TARGET).write_text(content, encoding="utf-8")
    return root


def _read_observation(root: Path):
    request = parse_discovery_request(f'{{"action":"read_file","path":"{TARGET}"}}')
    return execute_discovery_request(root, request)


def _search_observation(root: Path, query: str):
    request = parse_discovery_request(
        json.dumps({"action": "search_text", "query": query, "paths": [TARGET]})
    )
    return execute_discovery_request(root, request)


def _observed(root: Path, observation, task: str = TASK):
    return materialize_observation_source_context(
        project_dir=root,
        prompt=task,
        planner_contract=None,
        observation=observation,
        materialize=materialize_planner_source_context,
        source_cache={},
    )


def _record(materialization):
    records = [item for item in materialization.files if item.relative_path == TARGET]
    assert len(records) == 1, [item.relative_path for item in materialization.files]
    return records[0]


def _validate(root: Path, op: dict, materialization, task: str = TASK):
    plan = [
        {
            "step_number": 1,
            "description": "Apply the route repair.",
            "commands": [],
            "verification": VERIFY,
            "rollback": None,
            "expected_files": [TARGET],
            "ops": [op],
        }
    ]
    return ValidatorService().validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=task,
        execution_profile="full_lifecycle",
        project_dir=root,
        source_materialization=materialization,
    )


def _narrow(old: str = NARROW_OLD) -> dict:
    return {
        "op": "replace_in_file",
        "path": TARGET,
        "old": old,
        "new": old.replace('prefix="/permissions"', 'prefix=""'),
    }


def _whole_write() -> dict:
    return {"op": "write_file", "path": TARGET, "content": "api_router = None\n"}


def _assert_gr2_rejected(verdict) -> None:
    assert not verdict.accepted
    assert any(GR2_CODE in str(item) for item in verdict.reasons), verdict.reasons


def test_fixture_matches_reentry4_shape():
    encoded = ROUTER.encode("utf-8")
    start = encoded.find(PERMISSION_BLOCK.encode("utf-8"))
    assert 5000 <= len(encoded) <= 5400
    assert 3000 <= start and start + len(PERMISSION_BLOCK) < MAX_FILE_BYTES
    assert ROUTER[: start + len(PERMISSION_BLOCK)].count("\n") < MAX_FILE_LINES
    assert DEEP_ROUTER.encode("utf-8").find(PERMISSION_BLOCK.encode()) > MAX_FILE_BYTES
    assert extract_source_target_hints(TASK) == ()


def test_r1_g0_head_window_misses_region_and_claims_no_target(tmp_path):
    root = _workspace(tmp_path)
    initial = materialize_planner_source_context(
        root, task_description=TASK, supporting_paths=()
    )
    admission = assess_discovery_admission(
        prompt=TASK, planner_contract=None, materialization=initial
    )
    assert admission.status == DISCOVERY_ADMISSION_REQUIRED

    record = _record(_observed(root, _read_observation(root)))

    assert record.selection_strategy == SELECTION_HEAD_FALLBACK
    assert (record.start_byte, record.truncated) == (0, True)
    assert record.end_byte < sm.MAX_SOURCE_CONTENT_PER_FILE_CHARS
    assert record.target_hint is None and record.target_included is False
    assert "permissions.router" not in (record.content or "")


def test_g0_read_file_observation_is_planning_visible_advisory_evidence(tmp_path):
    root = _workspace(tmp_path)
    observation = _read_observation(root)
    materialization = _observed(root, observation)

    prompt = PlannerService.build_minimal_planning_prompt(
        TASK,
        root,
        source_materialization=materialization,
        read_only_observation=observation,
        workspace_has_existing_files=True,
    )

    assert observation.truncated is True
    assert len(observation.content.encode("utf-8")) <= MAX_FILE_BYTES
    assert "permissions.router" not in materialization.to_prompt_block(
        provider_safe=True
    )
    assert "permissions.router" in render_discovery_observation(observation)
    assert "## READ-ONLY OBSERVATION" in prompt
    assert "truncated: true" in prompt
    assert PERMISSION_BLOCK.strip() in prompt


def test_g1_legacy_discovery_has_exactly_one_turn():
    ctx = SimpleNamespace(read_only_discovery_completed=True, runtime_service=object())

    with pytest.raises(DiscoveryContractError, match="discovery_turn_already_used"):
        run_discovery_stage(
            ctx=ctx,
            planning_timeout_seconds=1,
            extract_structured_text=str,
            planner_service=None,
            emit_phase_event=lambda *args, **kwargs: None,
        )


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep unavailable")
def test_r3_r8_task_term_search_hits_gain_no_target_authority(tmp_path):
    root = _workspace(tmp_path)
    observation = _search_observation(root, "permissions")
    block_line = ROUTER[: ROUTER.find("    permissions.router")].count("\n") + 1

    record = _record(_observed(root, observation))

    assert len(observation.hits) > 1
    assert block_line in {hit.line_number for hit in observation.hits}
    assert record.selection_strategy == SELECTION_HEAD_FALLBACK
    assert record.target_hint is None
    assert record.target_hint_authority != HINT_AUTHORITY_TASK_DESCRIPTION
    assert "permissions.router" in render_discovery_observation(observation)


def test_r4_budgets_are_unchanged(tmp_path):
    root = _workspace(tmp_path)

    materialization = _observed(root, _read_observation(root))
    record = _record(materialization)

    assert (sm.MAX_RELEVANT_FILES, sm.MAX_SOURCE_CONTENT_PER_FILE_CHARS) == (25, 2000)
    assert sm.MAX_SOURCE_CONTENT_TOTAL_CHARS == 5000
    assert MAX_FILE_BYTES == 4096
    assert record.included_source_bytes <= sm.MAX_SOURCE_CONTENT_PER_FILE_CHARS + 32
    assert len(record.spans) == 1


def test_narrow_edit_outside_window_is_version_fenced_against_current_file(tmp_path):
    root = _workspace(tmp_path)
    materialization = _observed(root, _read_observation(root))

    verdict = _validate(root, _narrow(), materialization)

    assert verdict.accepted, verdict.reasons
    assert (root / TARGET).read_text(encoding="utf-8") == ROUTER


def test_r5_region_beyond_every_bound_fails_closed(tmp_path):
    root = _workspace(tmp_path, DEEP_ROUTER)
    observation = _read_observation(root)
    materialization = _observed(root, observation)

    assert "permissions.router" not in render_discovery_observation(observation)
    assert "permissions.router" not in (_record(materialization).content or "")
    _assert_gr2_rejected(_validate(root, _whole_write(), materialization))
    guessed = _validate(
        root, _narrow('    permissions.router,\n    prefix="/perm",\n'), materialization
    )
    assert not guessed.accepted
    assert (root / TARGET).read_text(encoding="utf-8") == DEEP_ROUTER


def test_r6_short_file_is_fully_materialized_once(tmp_path):
    short = '"""API router."""\n\napi_router = APIRouter()\n' + PERMISSION_BLOCK
    root = _workspace(tmp_path, short)

    record = _record(_observed(root, _read_observation(root)))

    assert record.selection_strategy == SELECTION_FULL_FILE
    assert record.truncated is False and len(record.spans) == 1


def test_r7_explicit_task_target_still_centers_the_window(tmp_path):
    root = _workspace(tmp_path)
    task = f"{TASK} The registration uses `permissions.router` in {TARGET}."

    record = _record(_observed(root, _read_observation(root), task=task))

    assert record.selection_strategy == SELECTION_TARGET_EXACT
    assert record.target_hint_authority == HINT_AUTHORITY_TASK_DESCRIPTION
    assert record.target_included is True
    assert "permissions.router" in record.content


def test_r10_read_file_body_supplies_no_hints_or_paths(tmp_path):
    root = _workspace(tmp_path)

    materialization = _observed(root, _read_observation(root))

    assert [item.relative_path for item in materialization.files] == [TARGET]
    assert not any(
        item.expected or item.creation_authorized for item in materialization.files
    )
    assert _record(materialization).target_hint is None


def test_r11_partial_head_window_plus_advisory_observation_is_not_full_file(tmp_path):
    root = _workspace(tmp_path)
    materialization = _observed(root, _read_observation(root))

    _assert_gr2_rejected(_validate(root, _whole_write(), materialization))


def test_r9_r11_two_disjoint_spans_are_not_complete_file_evidence(tmp_path):
    root = _workspace(tmp_path)
    task = f"{TASK} Keep the imports; the registration uses `permissions.router` in {TARGET}."

    materialization = materialize_planner_source_context(
        root, task_description=task, supporting_paths=()
    )
    record = _record(materialization)

    assert record.selection_strategy == SELECTION_TARGET_WITH_STRUCTURAL_HEAD
    head, primary = record.spans
    assert head.start_byte == 0 and head.end_byte < primary.start_byte
    assert primary.end_byte < record.full_source_bytes
    assert record.truncated is True
    assert record.included_source_bytes < record.full_source_bytes
    _assert_gr2_rejected(_validate(root, _whole_write(), materialization, task=task))


def test_r12_post_plan_record_cannot_authorize_whole_file_write(tmp_path):
    short = '"""API router."""\n\napi_router = APIRouter()\n' + PERMISSION_BLOCK
    root = _workspace(tmp_path, short)
    materialization = _observed(root, _read_observation(root))
    assert _record(materialization).truncated is False
    post_plan = replace(
        materialization,
        files=tuple(
            replace(item, planning_visible=False) for item in materialization.files
        ),
    )

    _assert_gr2_rejected(_validate(root, _whole_write(), post_plan))
