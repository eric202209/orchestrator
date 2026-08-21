"""Provider-free regressions for bounded discovery admission."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.orchestration.planning.read_only_discovery import (
    DISCOVERY_ADMISSION_REQUIRED,
    DISCOVERY_ADMISSION_SKIPPED,
    DiscoveryObservation,
    SearchHit,
    assess_discovery_admission,
    emit_discovery_admission,
    execute_discovery_request,
    materialize_observation_source_context,
    parse_discovery_request,
)
from app.services.orchestration.phases import planning_flow
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.parsing import extract_structured_text
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)


def _materialize(
    root: Path,
    task: str,
    *,
    planner_contract: dict | None = None,
):
    return materialize_planner_source_context(
        root,
        task_description=task,
        planner_contract=planner_contract,
        supporting_paths=(),
    )


def _admission(root: Path, task: str, *, planner_contract: dict | None = None):
    materialization = _materialize(root, task, planner_contract=planner_contract)
    return (
        assess_discovery_admission(
            prompt=task,
            planner_contract=planner_contract,
            materialization=materialization,
        ),
        materialization,
    )


def test_explicit_existing_file_with_current_source_skips_discovery(tmp_path: Path):
    target = tmp_path / "app" / "services" / "worker.py"
    target.parent.mkdir(parents=True)
    target.write_text("def run():\n    return 1\n", encoding="utf-8")

    admission, materialization = _admission(
        tmp_path, "Inspect app/services/worker.py and verify its behavior."
    )

    assert admission.status == DISCOVERY_ADMISSION_SKIPPED
    assert materialization.available
    assert materialization.file_map()["app/services/worker.py"].content


def test_explicit_existing_unique_target_skips_discovery(tmp_path: Path):
    target = tmp_path / "app" / "services" / "worker.py"
    target.parent.mkdir(parents=True)
    target.write_text("def run():\n    return 1\n", encoding="utf-8")

    admission, materialization = _admission(
        tmp_path, "Replace `run()` in app/services/worker.py."
    )

    assert admission.status == DISCOVERY_ADMISSION_SKIPPED
    assert materialization.file_map()["app/services/worker.py"].target_match_count == 1


def test_known_file_with_unresolved_target_requires_discovery(tmp_path: Path):
    target = tmp_path / "app" / "services" / "worker.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def run():\n    return 1\n\ndef run():\n    return 2\n",
        encoding="utf-8",
    )

    admission, materialization = _admission(
        tmp_path, "Replace `run()` in app/services/worker.py."
    )

    assert admission.status == DISCOVERY_ADMISSION_REQUIRED
    assert admission.reason == "semantic_target_is_not_unique"
    assert materialization.file_map()["app/services/worker.py"].target_match_count == 2


def test_authorized_new_file_skips_discovery(tmp_path: Path):
    admission, materialization = _admission(
        tmp_path, "Create app/new_feature.py with the requested helper."
    )

    assert admission.status == DISCOVERY_ADMISSION_SKIPPED
    created = materialization.file_map()["app/new_feature.py"]
    assert created.creation_authorized
    assert created.status == "new_file_authorized_for_creation"


@pytest.mark.parametrize(
    "task, relative_path, source",
    [
        (
            "Verify app/tests/test_worker.py with the existing test command.",
            "app/tests/test_worker.py",
            "def test_worker():\n    assert True\n",
        ),
        (
            "Run the check for app/services/worker.py.",
            "app/services/worker.py",
            "def run():\n    return 1\n",
        ),
    ],
)
def test_explicit_test_or_verification_paths_bypass_discovery(
    tmp_path: Path, task: str, relative_path: str, source: str
):
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")

    admission, materialization = _admission(tmp_path, task)

    assert admission.status == DISCOVERY_ADMISSION_SKIPPED
    assert materialization.file_map()[relative_path].status == (
        "existing_file_with_materialized_source"
    )


def test_no_usable_path_requires_exactly_one_bounded_discovery_shape(
    tmp_path: Path,
):
    admission, materialization = _admission(
        tmp_path, "Fix the timezone scheduling bug in the repository."
    )

    assert admission.status == DISCOVERY_ADMISSION_REQUIRED
    assert admission.reason == "no_explicit_source_or_creation_path"
    assert materialization.files == ()


def test_unsafe_contract_path_is_not_sufficient_grounding(tmp_path: Path):
    admission, _materialization = _admission(
        tmp_path,
        "Update the requested source.",
        planner_contract={"source_paths": ["../outside.py"]},
    )

    assert admission.status == DISCOVERY_ADMISSION_REQUIRED
    assert admission.reason == "expected_source_status_not_grounded"


def test_truncated_current_source_requires_discovery(tmp_path: Path):
    target = tmp_path / "app" / "services" / "large.py"
    target.parent.mkdir(parents=True)
    target.write_text("value = 1\n" * 500, encoding="utf-8")

    admission, materialization = _admission(
        tmp_path, "Inspect app/services/large.py before changing it."
    )

    assert admission.status == DISCOVERY_ADMISSION_REQUIRED
    assert admission.reason == "current_source_is_missing_or_truncated"
    assert materialization.file_map()["app/services/large.py"].truncated


def test_task_218_provider_free_replay_requires_then_materializes_discovery(
    tmp_path: Path,
):
    # The retained Task-218 shape contains no usable path or target before the
    # bounded observation. The repository fixture is the current checkout so
    # the observation remains real, read-only, and provider-free.
    task = "Fix scheduled task timestamp handling."
    initial_admission, initial = _admission(tmp_path, task)
    assert initial_admission.status == DISCOVERY_ADMISSION_REQUIRED
    assert initial.files == ()

    request = parse_discovery_request(
        json.dumps(
            {
                "action": "search_text",
                "query": "scheduled_task_execution",
                "paths": ["app/tasks", "app/tests"],
            }
        )
    )
    repository_root = Path(__file__).resolve().parents[2]
    observation = execute_discovery_request(repository_root, request)
    assert "app/tasks/maintenance.py" in set(observation.materialization_paths())

    materialization = materialize_observation_source_context(
        project_dir=repository_root,
        prompt=task,
        planner_contract=None,
        observation=observation,
        materialize=materialize_planner_source_context,
    )
    maintenance = materialization.file_map()["app/tasks/maintenance.py"]
    assert materialization.available
    assert maintenance.status == "existing_file_with_materialized_source"
    assert maintenance.content and "scheduled_task_execution" in maintenance.content


def test_observation_materialization_reuses_bounded_source_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import app.services.orchestration.planning.source_materialization as source_module

    target = tmp_path / "app" / "worker.py"
    target.parent.mkdir(parents=True)
    target.write_text("def run():\n    return 1\n", encoding="utf-8")
    reads: list[str] = []
    original = source_module._read_source_text

    def tracked_read(path, relative_path, cache):
        if relative_path not in cache:
            reads.append(relative_path)
        return original(path, relative_path, cache)

    monkeypatch.setattr(source_module, "_read_source_text", tracked_read)
    cache: dict[str, str] = {}
    materialize_planner_source_context(
        tmp_path,
        task_description="Inspect app/worker.py.",
        supporting_paths=(),
        source_cache=cache,
    )
    observation = DiscoveryObservation(
        action="read_file",
        status="completed",
        paths=("app/worker.py",),
        content="def run():",
    )
    materialize_observation_source_context(
        project_dir=tmp_path,
        prompt="Inspect app/worker.py.",
        planner_contract=None,
        observation=observation,
        materialize=materialize_planner_source_context,
        source_cache=cache,
    )

    assert reads.count("app/worker.py") == 1


def test_admission_event_distinguishes_skip_without_provider_call():
    events = []
    ctx = SimpleNamespace(
        orchestration_state=SimpleNamespace(),
        emit_live=lambda *args, **kwargs: None,
    )

    emit_discovery_admission(
        ctx=ctx,
        admission=type(
            "Admission",
            (),
            {
                "status": DISCOVERY_ADMISSION_SKIPPED,
                "reason": "explicit_current_source_is_sufficient",
            },
        )(),
        emit_phase_event=lambda *args, **kwargs: events.append(kwargs),
    )

    assert events[0]["details"]["discovery_admission"] == (
        "SKIPPED_SUFFICIENT_GROUNDING"
    )
    assert events[0]["details"]["discovery_turns_used"] == 0
    assert "Running one bounded" not in events[0]["message"]


def test_fresh_planning_bypasses_provider_discovery_when_source_is_sufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import app.services.orchestration.planning.read_only_discovery as discovery_module

    target = tmp_path / "app" / "services" / "worker.py"
    target.parent.mkdir(parents=True)
    target.write_text("def run():\n    return 1\n", encoding="utf-8")
    prompt = "Inspect app/services/worker.py and verify its behavior."
    state = MagicMock(project_dir=tmp_path, project_context="", plan=None)
    state.phase_history = []
    state.validation_history = []
    state.artifact_supplement = None
    task = SimpleNamespace(title="Inspect worker", description=prompt, template_id=None)
    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=SimpleNamespace(instance_id=None),
        project=MagicMock(),
        task=task,
        session_task_link=MagicMock(),
        session_id=1,
        task_id=1,
        prompt=prompt,
        timeout_seconds=120,
        execution_profile="implementation",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=state,
        runtime_service=object(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post33_pl2"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
        task_execution_id=1,
    )
    monkeypatch.setattr(
        planning_flow,
        "append_orchestration_event",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        planning_flow,
        "write_orchestration_state_snapshot",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        planning_flow,
        "emit_phase_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(planning_flow, "_retrieve_knowledge", lambda *a, **k: None)
    monkeypatch.setattr(
        discovery_module,
        "run_discovery_stage",
        lambda **kwargs: pytest.fail("sufficient grounding must bypass discovery"),
    )
    assembled = []

    class _PlanningStopped(Exception):
        pass

    def _assemble(current_ctx, _workspace_review, **_kwargs):
        assembled.append(current_ctx.planner_source_materialization)
        raise _PlanningStopped

    monkeypatch.setattr(planning_flow, "assemble_planning_prompt", _assemble)

    with pytest.raises(_PlanningStopped):
        planning_flow.execute_planning_phase(
            ctx=ctx,
            workspace_review={"has_existing_files": True},
            extract_structured_text=extract_structured_text,
            extract_plan_steps=lambda value: value,
            looks_like_truncated_multistep_plan=lambda text, plan: False,
            normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
            workspace_violation_error_cls=RuntimeError,
        )

    assert len(assembled) == 1
    assert assembled[0].file_map()["app/services/worker.py"].content
    assert ctx.read_only_discovery_completed is False
