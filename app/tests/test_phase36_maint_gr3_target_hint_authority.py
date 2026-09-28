"""PHASE36-MAINT-GR3 — target-hint authority for observed source.

REENTRY-4 read ``app/api/v1/router.py`` through read-only discovery.  The
observation body was appended to the task description before hint extraction,
so the file's first call literal ``APIRouter()`` became a ``task_description``
hint and centered the Planning window on the wrong region.  These cases drive
the real discovery executor, observation materializer, and target inventory.
"""

from __future__ import annotations

from pathlib import Path

from app.services.orchestration.planning import source_materialization as sm
from app.services.orchestration.planning.read_only_discovery import (
    DiscoveryObservation,
    SearchHit,
    execute_discovery_request,
    materialize_observation_source_context,
    parse_discovery_request,
)
from app.services.orchestration.planning.semantic_target_inventory import (
    build_semantic_target_inventory,
)
from app.services.orchestration.planning.source_materialization import (
    HINT_AUTHORITY_DISCOVERY_OBSERVATION,
    HINT_AUTHORITY_PLANNER_CONTRACT,
    HINT_AUTHORITY_TASK_DESCRIPTION,
    SELECTION_HEAD_FALLBACK,
    SELECTION_TARGET_EXACT,
    extract_source_target_hints,
    materialize_planner_source_context,
    observed_candidate_paths,
)

TARGET = "app/api/v1/router.py"
TASK = (
    "Authenticated clients should be able to GET /api/v1/permissions/pending, "
    "and the doubled /api/v1/permissions/permissions/pending route should not "
    "be exposed. Repair the externally observable behavior while preserving "
    "the existing permission semantics, then verify the public contract."
)
PERMISSION_BLOCK = (
    "api_router.include_router(\n    permissions.router,\n"
    '    prefix="/permissions",\n    tags=["permissions"],\n'
    "    dependencies=permission_guards(),\n)\n"
)
ROUTER = (
    '"""API router."""\n\nfrom fastapi import APIRouter\n\n'
    "from app.api.v1.endpoints import permissions\n\n"
    "api_router = APIRouter()\n\n\n"
    '@api_router.get("/health")\nasync def health_check():\n'
    "    return health_payload()\n\n\n"
    + "".join(
        f"# Area {i}\napi_router.include_router(\n    area_{i}.router,\n"
        f'    prefix="/area{i}",\n    tags=["area{i}"],\n)\n\n'
        for i in range(40)
    )
    + "# Permission Approval\n"
    + PERMISSION_BLOCK
)


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path.resolve()
    (root / "app/api/v1").mkdir(parents=True)
    (root / TARGET).write_text(ROUTER, encoding="utf-8")
    return root


def _read_observation(root: Path) -> DiscoveryObservation:
    request = parse_discovery_request(f'{{"action":"read_file","path":"{TARGET}"}}')
    return execute_discovery_request(root, request)


def _search_observation(*snippets: str, path: str = TARGET) -> DiscoveryObservation:
    return DiscoveryObservation(
        action="search_text",
        status="completed",
        hits=tuple(
            SearchHit(path=path, line_number=index + 1, snippet=snippet)
            for index, snippet in enumerate(snippets)
        ),
    )


def _observed(root: Path, observation, task: str = TASK, planner_contract=None):
    return materialize_observation_source_context(
        project_dir=root,
        prompt=task,
        planner_contract=planner_contract,
        observation=observation,
        materialize=materialize_planner_source_context,
        source_cache={},
    )


def _record(materialization, path: str = TARGET):
    records = [item for item in materialization.files if item.relative_path == path]
    assert len(records) == 1, [item.relative_path for item in materialization.files]
    return records[0]


def _handle_labels(materialization, observation) -> list[str]:
    inventory = build_semantic_target_inventory(
        materialization,
        additional_candidate_paths=observed_candidate_paths(observation),
    )
    return [handle.label for handle in inventory.handles]


def test_fixture_matches_reentry4_shape():
    encoded = ROUTER.encode("utf-8")
    assert len(encoded) > sm.MAX_SOURCE_CONTENT_PER_FILE_CHARS
    assert encoded.find(b"APIRouter()") < encoded.find(PERMISSION_BLOCK.encode())
    assert extract_source_target_hints(TASK) == ()


def test_r1_observed_source_hint_never_gains_task_authority(tmp_path):
    root = _workspace(tmp_path)
    observation = _read_observation(root)

    hints = extract_source_target_hints(TASK, observation_text=observation.content)
    router_hints = [hint for hint in hints if hint.text == "APIRouter()"]
    assert router_hints
    assert {hint.authority for hint in router_hints} == {
        HINT_AUTHORITY_DISCOVERY_OBSERVATION
    }

    record = _record(_observed(root, observation))
    assert record.target_hint_authority != HINT_AUTHORITY_TASK_DESCRIPTION
    assert record.target_hint != "APIRouter()"
    assert record.selection_strategy != SELECTION_TARGET_EXACT


def test_r2_explicit_task_symbol_keeps_task_authority(tmp_path):
    root = _workspace(tmp_path)
    task = f"{TASK} Start from `APIRouter()` in {TARGET}."
    observation = _read_observation(root)

    record = _record(_observed(root, observation, task=task))

    assert record.target_hint == "APIRouter()"
    assert record.target_hint_authority == HINT_AUTHORITY_TASK_DESCRIPTION
    assert record.selection_strategy == SELECTION_TARGET_EXACT
    assert record.target_included is True


def test_r3_early_unrelated_source_call_is_not_task_authoritative(tmp_path):
    root = _workspace(tmp_path)
    source = "configure_logging()\nhealth_payload()\n" + ROUTER

    hints = extract_source_target_hints(TASK, observation_text=source)

    assert hints
    assert all(hint.authority == HINT_AUTHORITY_DISCOVERY_OBSERVATION for hint in hints)
    record = _record(_observed(root, _read_observation(root)))
    assert record.target_hint is None


def test_r4_task_target_outranks_observed_source_noise(tmp_path):
    root = _workspace(tmp_path)
    task = f"{TASK} The registration uses `permissions.router`."
    observation = _search_observation(
        "api_router = APIRouter()",
        "    return health_payload()",
    )

    record = _record(_observed(root, observation, task=task))

    assert record.target_hint == "permissions.router"
    assert record.target_hint_authority == HINT_AUTHORITY_TASK_DESCRIPTION
    assert record.target_included is True


def test_r5_task_path_is_expected_but_observed_paths_gain_no_authority(tmp_path):
    root = _workspace(tmp_path)
    task = f"{TASK} Repair {TARGET}."
    observation = _search_observation(
        '    # create the new cache file: open("app/generated_cache.py", "w")',
        path=TARGET,
    )

    materialization = _observed(root, observation, task=task)

    assert _record(materialization).expected is True
    assert _record(materialization).priority == "P0"
    assert "app/generated_cache.py" not in {
        item.relative_path for item in materialization.files
    }
    assert not any(
        item.creation_authorized for item in materialization.files
    ), materialization.files


def test_r6_planner_contract_hint_keeps_contract_authority(tmp_path):
    root = _workspace(tmp_path)
    contract = {"task_description": f"Inspect `APIRouter()` in {TARGET}."}

    record = _record(
        _observed(root, _read_observation(root), planner_contract=contract)
    )

    assert record.target_hint == "APIRouter()"
    assert record.target_hint_authority == HINT_AUTHORITY_PLANNER_CONTRACT
    assert record.selection_strategy == SELECTION_TARGET_EXACT


def test_r7_materialization_limits_are_unchanged(tmp_path):
    root = _workspace(tmp_path)

    materialization = _observed(root, _read_observation(root))
    record = _record(materialization)

    assert (sm.MAX_RELEVANT_FILES, sm.MAX_SOURCE_CONTENT_PER_FILE_CHARS) == (25, 2000)
    assert sm.MAX_SOURCE_CONTENT_TOTAL_CHARS == 5000
    assert record.truncated is True
    assert record.included_source_bytes <= sm.MAX_SOURCE_CONTENT_PER_FILE_CHARS + 32
    assert materialization.materialized_source_bytes <= 5000


def test_r8_no_task_hint_uses_head_fallback(tmp_path):
    root = _workspace(tmp_path)

    record = _record(_observed(root, _read_observation(root)))

    assert record.selection_strategy == SELECTION_HEAD_FALLBACK
    assert record.target_hint is None
    assert record.target_hint_authority is None
    assert record.start_byte == 0


def test_r9_reentry4_window_no_longer_centers_on_false_task_target(tmp_path):
    root = _workspace(tmp_path)
    observation = _read_observation(root)

    materialization = _observed(root, observation)
    record = _record(materialization)

    assert record.target_match_start is None
    assert not any(
        "APIRouter()" in label for label in _handle_labels(materialization, observation)
    )
    # Authority is corrected; Grounding is still insufficient for this task.
    assert PERMISSION_BLOCK not in (record.content or "")


def test_search_hit_orientation_still_centers_with_observation_authority(tmp_path):
    root = _workspace(tmp_path)
    observation = _search_observation("    dependencies=permission_guards(),")

    record = _record(_observed(root, observation))

    assert record.target_hint == "permission_guards()"
    assert record.target_hint_authority == HINT_AUTHORITY_DISCOVERY_OBSERVATION
    assert record.target_included is True
    assert "permission_guards()" in (record.content or "")
