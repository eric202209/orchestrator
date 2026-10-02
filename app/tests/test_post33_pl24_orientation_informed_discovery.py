"""Provider-free PL24 orientation-informed bounded discovery controls.

Every test here is provider-free: no planner, executor, or OpenClaw call is
made.  The Task 222 readiness replay emulates the one model discovery action
with a hand-written canonical request and executes it through the shipped
discovery, materialization, and PL16 handle builders.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.services.orchestration.planning.read_only_discovery import (
    MAX_DISCOVERY_PATHS,
    DiscoveryContractError,
    build_discovery_prompt,
    execute_discovery_request,
    materialize_observation_source_context,
    parse_discovery_request,
)
from app.services.orchestration.planning.repository_orientation import (
    ORIENTATION_BYTE_BUDGET,
    ORIENTATION_PATH_LIMIT,
    ORIENTATION_SCOPE_TRACKED,
    ORIENTATION_SCOPE_UNAVAILABLE,
    ORIENTATION_UNAVAILABLE_NOT_GIT,
    derive_repository_orientation,
    orientation_task_literals,
    render_repository_orientation,
)
from app.services.orchestration.planning.semantic_target_inventory import (
    build_semantic_target_inventory,
)
from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_NEW,
    materialize_planner_source_context,
    observed_candidate_paths,
    provider_planning_contract_capabilities,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

TASK_222_TEXT = (
    "Restore zero-percent visibility for failure-only tool metrics\n\n"
    "Our tool usage analytics currently omits a tool from the success-rate "
    "mapping when every recorded invocation fails. Update the existing backend "
    "behavior so every observed tool has a success-rate entry, including 0% "
    "when there are no successes, while preserving execution counts, totals, "
    "and existing successful-tool percentages. Add focused regression coverage "
    "for a failure-only tool and keep the change limited to the existing "
    "analytics path. Verify with the focused test suite."
)
TASK_222_TARGET = "app/services/tasks/tool_tracking.py"

# Frozen retained wordings and ground-truth production paths for the
# cross-task regression matrix.  Ground truth is used for scoring only; it
# never feeds orientation, the query, or any authority decision.
CROSS_TASK_CASES = {
    222: (TASK_222_TEXT, TASK_222_TARGET, False),
    218: (
        "Fix scheduled task timezone handling\n\nFix scheduled task execution "
        "so ISO-8601 timestamps with Z or explicit UTC offsets are compared "
        "consistently, preserving retry-before-work behavior, and add focused "
        "regression coverage. Keep the scope to existing maintenance code and "
        "focused tests; do not change unrelated lifecycle behavior.",
        "app/tasks/maintenance.py",
        False,
    ),
    217: (
        "Preserve structured errors in fallback failure summaries\n\nKeep "
        "complete structured error records in deterministic fallback failure "
        "summaries while continuing to filter raw JSON fragments and provider "
        "diagnostics. Add focused regression coverage for a complete JSON "
        "error record and an incomplete fragment.",
        "app/services/session/replan_service.py",
        False,
    ),
    220: (
        "Fix manual knowledge synchronization failure state\n\nWhen manual "
        "synchronization fails because the underlying domain operation raises "
        "an error, the item must not remain in an in-progress state. Preserve "
        "existing successful synchronization and retry behavior, and add "
        "focused regression coverage for the failure transition.",
        "app/services/knowledge/knowledge_sync_service.py",
        False,
    ),
    214: (
        "Improve source-import context handling for unreadable files\n\nIn "
        "app/services/project/source_imports.py, make _safe_read_text "
        "gracefully return an empty string when a file disappears or cannot be "
        "opened between discovery and reading.",
        "app/services/project/source_imports.py",
        True,
    ),
    179: (
        "Return requested usable session-log count after filtering\n\nCorrect "
        "session-log streaming so the requested log limit applies to usable "
        "records after filtering or suppression. Limit scope to "
        "app/services/observability/log_stream.py and focused tests under "
        "app/tests/test_log_stream_service.py if needed.",
        "app/services/observability/log_stream.py",
        True,
    ),
    181: (
        "Add reusable structured-log metadata normalization for streaming\n\n"
        "NEW production module: app/services/observability/log_metadata.py; "
        "INTEGRATE it in: app/services/observability/log_stream.py. "
        "log_stream.py parses persisted log metadata directly with json.loads "
        "at each streaming site.",
        "app/services/observability/log_stream.py",
        True,
    ),
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


def _write(root: Path, relative: str, body: str = "content\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


@pytest.fixture()
def tracked_project(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "app/services/widget_registry.py")
    _write(tmp_path, "app/tests/test_widget_registry.py")
    _write(tmp_path, "docs/widget-notes.md")
    _git(tmp_path, "add", "-A")
    return tmp_path


# --- orientation helper -----------------------------------------------------


def test_orientation_lists_only_git_tracked_product_paths(tracked_project: Path):
    _write(tracked_project, "app/services/widget_untracked.py")

    orientation = derive_repository_orientation(
        tracked_project, "Fix the widget registry"
    )

    assert orientation.available is True
    assert orientation.scope == ORIENTATION_SCOPE_TRACKED
    assert "app/services/widget_registry.py" in orientation.paths
    assert "app/services/widget_untracked.py" not in orientation.paths
    assert all(not Path(value).is_absolute() for value in orientation.paths)


def test_orientation_is_unavailable_outside_a_git_work_tree(tmp_path: Path):
    _write(tmp_path, "app/services/widget_registry.py")

    orientation = derive_repository_orientation(tmp_path, "Fix the widget registry")

    assert orientation.available is False
    assert orientation.scope == ORIENTATION_SCOPE_UNAVAILABLE
    assert orientation.unavailable_reason == ORIENTATION_UNAVAILABLE_NOT_GIT
    assert orientation.paths == ()
    # No filesystem-crawler fallback: the untracked file is never enumerated.
    assert render_repository_orientation(orientation) == ""
    prompt = build_discovery_prompt("Fix the widget registry", "", orientation)
    assert "REPOSITORY ORIENTATION" not in prompt
    assert "READ-ONLY DISCOVERY ONLY" in prompt


def test_orientation_truncates_deterministically_with_explicit_metadata(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    for index in range(120):
        _write(tmp_path, f"app/services/widget/widget_module_{index:03d}.py")
    _git(tmp_path, "add", "-A")

    first = derive_repository_orientation(tmp_path, "Repair the widget module")
    second = derive_repository_orientation(tmp_path, "Repair the widget module")

    assert first.paths == second.paths
    assert first.truncated is True
    assert first.entries_shown < first.entries_total
    assert first.entries_shown <= ORIENTATION_PATH_LIMIT
    assert first.bytes_used <= ORIENTATION_BYTE_BUDGET
    assert first.paths == tuple(sorted(first.paths))
    assert "truncated=true" in render_repository_orientation(first)


def test_orientation_excludes_protected_and_toolchain_paths(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    excluded = [
        ".agent/widget-state.json",
        ".openclaw/widget.json",
        "venv/lib/widget.py",
        ".venv/lib/widget.py",
        "node_modules/widget/index.js",
        "__pycache__/widget.pyc",
        ".pytest_cache/widget",
        "runtime.json",
    ]
    for relative in excluded:
        _write(tmp_path, relative)
    _write(tmp_path, "app/services/widget_registry.py")
    _git(tmp_path, "add", "-Af")

    orientation = derive_repository_orientation(tmp_path, "Fix the widget registry")

    assert "app/services/widget_registry.py" in orientation.paths
    for relative in excluded:
        assert relative not in orientation.paths


def test_orientation_excludes_symlink_index_entries(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "app/services/widget_registry.py")
    (tmp_path / "app" / "services" / "widget_alias.py").symlink_to(
        tmp_path / "app" / "services" / "widget_registry.py"
    )
    _git(tmp_path, "add", "-A")

    orientation = derive_repository_orientation(tmp_path, "Fix the widget registry")

    assert "app/services/widget_registry.py" in orientation.paths
    assert "app/services/widget_alias.py" not in orientation.paths


def test_orientation_preserves_full_relative_paths_for_duplicate_basenames(
    tmp_path: Path,
):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "app/services/alpha/widget_registry.py")
    _write(tmp_path, "app/services/beta/widget_registry.py")
    _git(tmp_path, "add", "-A")

    orientation = derive_repository_orientation(tmp_path, "Fix the widget registry")

    assert "app/services/alpha/widget_registry.py" in orientation.paths
    assert "app/services/beta/widget_registry.py" in orientation.paths
    assert "widget_registry.py" not in orientation.paths


def test_orientation_is_empty_for_prose_without_tracked_literals(
    tracked_project: Path,
):
    orientation = derive_repository_orientation(
        tracked_project, "Please make the product feel better for our users."
    )

    assert orientation.available is False
    assert orientation.paths == ()
    prompt = build_discovery_prompt(
        "Please make the product feel better.", "", orientation
    )
    assert "REPOSITORY ORIENTATION" not in prompt
    assert "Return exactly one JSON object" in prompt


def test_orientation_uses_no_semantic_expansion(tracked_project: Path):
    literals = orientation_task_literals("Fix the success-rate mapping for tools")

    assert "success" in literals and "rate" in literals
    # Mechanical punctuation splitting only: no stem, plural, or synonym.
    assert "rates" not in literals
    assert "percentage" not in literals


def test_orientation_places_explicit_paths_first_without_granting_authority(
    tracked_project: Path,
):
    orientation = derive_repository_orientation(
        tracked_project,
        "Fix the widget registry",
        explicit_paths=("app/tests/test_widget_registry.py",),
    )

    assert orientation.paths[0] == "app/tests/test_widget_registry.py"
    # Orientation is a rendering surface only; it exposes no authority field.
    for field in ("expected", "creation_authorized", "target_id", "grant"):
        assert not hasattr(orientation, field)


def test_orientation_ignores_explicit_paths_that_are_not_tracked(
    tracked_project: Path,
):
    orientation = derive_repository_orientation(
        tracked_project,
        "Fix the widget registry",
        explicit_paths=("app/services/widget_absent.py",),
    )

    assert "app/services/widget_absent.py" not in orientation.paths


def test_stale_tracked_path_fails_closed_on_discovery_observation(
    tracked_project: Path,
):
    (tracked_project / "docs" / "widget-notes.md").unlink()

    orientation = derive_repository_orientation(tracked_project, "Fix the widget notes")
    assert "docs/widget-notes.md" in orientation.paths

    request = parse_discovery_request(
        json.dumps({"action": "read_file", "path": "docs/widget-notes.md"})
    )
    with pytest.raises(DiscoveryContractError):
        execute_discovery_request(tracked_project, request)


# --- discovery prompt integration -------------------------------------------


def test_discovery_prompt_renders_a_separate_bounded_orientation_block(
    tracked_project: Path,
):
    orientation = derive_repository_orientation(
        tracked_project, "Fix the widget registry"
    )
    long_context = "x" * 4000
    prompt = build_discovery_prompt(
        "Fix the widget registry", long_context, orientation
    )

    assert "REPOSITORY ORIENTATION (FACTS ONLY)" in prompt
    assert "END REPOSITORY ORIENTATION" in prompt
    assert f"scope={ORIENTATION_SCOPE_TRACKED}" in prompt
    assert f"entries_shown={orientation.entries_shown}" in prompt
    assert f"entries_total={orientation.entries_total}" in prompt
    assert f"bytes_used={orientation.bytes_used}" in prompt
    assert f"byte_budget={ORIENTATION_BYTE_BUDGET}" in prompt
    # The block survives a project context far larger than its 700-char cap.
    assert "app/services/widget_registry.py" in prompt
    assert prompt.count("x" * 700) == 1


def test_discovery_prompt_states_orientation_is_advisory_only(tracked_project: Path):
    orientation = derive_repository_orientation(
        tracked_project, "Fix the widget registry"
    )
    prompt = build_discovery_prompt("Fix the widget registry", "", orientation)

    assert "advisory candidates" in prompt
    assert "not authorized for creation or mutation" in prompt
    assert "does not mean it is absent" in prompt
    assert "search_text/read_file/stop behavior is unchanged" in prompt
    assert f"at most {MAX_DISCOVERY_PATHS}" in prompt
    assert "Choose at most one action" in prompt


def test_orientation_does_not_consume_task_or_project_context_budget(
    tracked_project: Path,
):
    orientation = derive_repository_orientation(
        tracked_project, "Fix the widget registry"
    )
    task = "Fix the widget registry. " * 40
    context = "Bounded planning context. " * 40
    without = build_discovery_prompt(task, context)
    with_block = build_discovery_prompt(task, context, orientation)

    assert with_block.startswith(without)
    assert len(with_block) - len(without) == len(
        "\n" + render_repository_orientation(orientation)
    )


# --- Task 222 provider-free readiness replay --------------------------------


def _emulated_informed_discovery(query: str, scopes: tuple[str, ...]):
    request = parse_discovery_request(
        json.dumps({"action": "search_text", "query": query, "paths": list(scopes)})
    )
    return request, execute_discovery_request(REPOSITORY_ROOT, request)


def test_task222_provider_free_orientation_informed_readiness():
    orientation = derive_repository_orientation(REPOSITORY_ROOT, TASK_222_TEXT)

    # 1. the oriented candidate set exposes the factual production path
    assert orientation.available is True
    assert TASK_222_TARGET in orientation.paths

    # 2. the block is in the exact prompt handed to the provider-facing builder
    prompt = build_discovery_prompt(TASK_222_TEXT, "", orientation)
    assert "REPOSITORY ORIENTATION (FACTS ONLY)" in prompt
    assert TASK_222_TARGET in prompt

    # 3. existing scope rules still bound the provider to <= MAX_DISCOVERY_PATHS
    with pytest.raises(DiscoveryContractError):
        parse_discovery_request(
            json.dumps(
                {
                    "action": "search_text",
                    "query": "rate",
                    "paths": list(orientation.paths[: MAX_DISCOVERY_PATHS + 1]),
                }
            )
        )

    # 4. one informed task-literal query against the oriented path materializes
    #    the defective region from current source
    assert "rate" in orientation_task_literals(TASK_222_TEXT)
    _, observation = _emulated_informed_discovery("rate", (TASK_222_TARGET,))
    assert observation.hits
    materialization = materialize_observation_source_context(
        project_dir=REPOSITORY_ROOT,
        prompt=TASK_222_TEXT,
        planner_contract=None,
        observation=observation,
        materialize=materialize_planner_source_context,
        source_cache={},
    )
    records = [
        item for item in materialization.files if item.relative_path == TASK_222_TARGET
    ]
    assert len(records) == 1
    record = records[0]
    assert "success_rates" in (record.content or "")

    # 5. existing target-hint extraction recognizes the region literal
    assert record.target_hint == "success_rates"
    assert record.target_included is True
    assert record.target_match_count == 1

    # 6. existing PL16 inventory emits at least one opaque handle
    candidates = observed_candidate_paths(observation)
    inventory = build_semantic_target_inventory(
        materialization, additional_candidate_paths=candidates
    )
    assert len(inventory.handles) >= 1
    handle = inventory.handles[0]
    assert handle.path == TASK_222_TARGET
    assert handle.target_id.startswith("tgt_")

    # 7. re-derived provider capabilities; an observed path is in the same
    # replace scope for legacy old/new as for semantic handles (PHASE36-MAINT
    # PMAC).  Affordance only: authority is unchanged (8/9/10).
    assert provider_planning_contract_capabilities(
        materialization, additional_candidate_paths=candidates
    ) == (True, True)

    # 8/9/10. visibility grants no authority and creates no new-file route
    assert record.expected is False
    assert record.creation_authorized is False
    assert all(item.status != SOURCE_STATUS_NEW for item in materialization.files)
    assert not any(item.creation_authorized for item in materialization.files)


def test_task222_unselected_oriented_paths_gain_no_authority():
    orientation = derive_repository_orientation(REPOSITORY_ROOT, TASK_222_TEXT)
    unselected = [value for value in orientation.paths if value != TASK_222_TARGET]
    assert unselected

    _, observation = _emulated_informed_discovery("rate", (TASK_222_TARGET,))
    materialization = materialize_observation_source_context(
        project_dir=REPOSITORY_ROOT,
        prompt=TASK_222_TEXT,
        planner_contract=None,
        observation=observation,
        materialize=materialize_planner_source_context,
        source_cache={},
    )
    materialized = {item.relative_path for item in materialization.files}
    assert materialized.isdisjoint(set(unselected))

    inventory = build_semantic_target_inventory(
        materialization,
        additional_candidate_paths=observed_candidate_paths(observation),
    )
    assert {handle.path for handle in inventory.handles}.isdisjoint(set(unselected))


# --- cross-task regression --------------------------------------------------


@pytest.mark.parametrize("task_id", sorted(CROSS_TASK_CASES))
def test_cross_task_orientation_regression(task_id: int):
    task_text, ground_truth, explicit = CROSS_TASK_CASES[task_id]
    explicit_paths = (ground_truth,) if explicit else ()
    orientation = derive_repository_orientation(
        REPOSITORY_ROOT, task_text, explicit_paths=explicit_paths
    )

    assert orientation.entries_shown <= ORIENTATION_PATH_LIMIT
    assert orientation.bytes_used <= ORIENTATION_BYTE_BUDGET
    assert orientation.truncated == (
        orientation.entries_shown < orientation.entries_total
    )

    if explicit:
        # An explicitly named task path stays primary and is never downgraded.
        assert orientation.paths[0] == ground_truth

    # Discovery can still be formed from whatever orientation returned, and the
    # existing scope authority is unchanged.
    scopes = orientation.paths[:MAX_DISCOVERY_PATHS] or ("app",)
    request = parse_discovery_request(
        json.dumps(
            {
                "action": "search_text",
                "query": "def ",
                "paths": list(dict.fromkeys(scopes)),
            }
        )
    )
    assert request.action == "search_text"
    assert len(request.paths) <= MAX_DISCOVERY_PATHS


# --- turn and emission invariants -------------------------------------------


def test_orientation_reaches_the_provider_prompt_and_adds_no_second_turn(
    tracked_project: Path,
):
    from types import SimpleNamespace

    from app.services.orchestration.planning.read_only_discovery import (
        run_discovery_stage,
    )

    seen: dict[str, object] = {}

    class _Planner:
        @staticmethod
        async def _execute_task_with_planning_lock(*args, **kwargs):
            seen["prompt"] = args[1]
            return {"status": "completed", "output": '{"action":"stop"}'}

    events: list[dict] = []

    def _emit(*args, **kwargs):
        events.append(kwargs.get("details") or {})

    ctx = SimpleNamespace(
        read_only_discovery_completed=False,
        runtime_service=object(),
        prompt="Fix the widget registry",
        orchestration_state=SimpleNamespace(
            project_context="", project_dir=tracked_project
        ),
        emit_live=lambda *args, **kwargs: None,
        session_id=1,
        task_id=2,
        task_execution_id=3,
    )
    observation = run_discovery_stage(
        ctx=ctx,
        planning_timeout_seconds=120,
        extract_structured_text=lambda value: str(value),
        planner_service=_Planner,
        emit_phase_event=_emit,
    )

    assert observation.action == "stop"
    assert "REPOSITORY ORIENTATION (FACTS ONLY)" in str(seen["prompt"])
    assert "app/services/widget_registry.py" in str(seen["prompt"])
    assert events and events[0]["max_discovery_turns"] == 1
    assert events[0]["orientation_available"] is True
    assert events[0]["orientation_scope"] == ORIENTATION_SCOPE_TRACKED

    # The single-turn guard is unchanged: a second call is refused.
    with pytest.raises(DiscoveryContractError):
        run_discovery_stage(
            ctx=ctx,
            planning_timeout_seconds=120,
            extract_structured_text=lambda value: str(value),
            planner_service=_Planner,
            emit_phase_event=_emit,
        )
