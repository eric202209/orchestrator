"""Post-Phase33 — Project.id-keyed control state writes outside the project repo.

The five proven Project.id-threaded control-state families (events,
fingerprints, change-sets, ``state_manager.json``, task-reports) now write to
``<runtime_root>/control/projects/<project_id>/…`` and read that location first,
falling back to the historical ``<project_root>/.agent/…`` tree.

These tests pin the forward cutover: new writes leave the project repository,
historical state stays byte-identical and readable, and nothing is dual-written
or migrated.  ``engineering-context`` is deliberately out of scope and keeps its
repository-identity keying.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.orchestration.execution.runtime import (
    get_state_manager_path,
    get_state_manager_read_path,
)
from app.services.orchestration.state.persistence import (
    _orchestration_event_log_path,
    append_orchestration_event,
    read_legacy_orchestration_events,
    read_orchestration_events,
    read_orchestration_state_snapshots,
    read_session_fingerprint_index,
    write_session_fingerprint_index,
)
from app.services.workspace.control_state_paths import (
    CONTROL_STATE_DIR_NAME,
    FAMILY_CHANGE_SETS,
    FAMILY_ENGINEERING_CONTEXT,
    FAMILY_EVENTS,
    FAMILY_FINGERPRINTS,
    FAMILY_TASK_REPORTS,
    ControlStateLocation,
    control_state_family_dir,
    control_state_of,
    control_state_read_path,
    control_state_root,
    legacy_control_state_family_dir,
    project_control_state_root,
)

PROJECT_ID = 12
RELOCATED_FAMILIES = (
    FAMILY_EVENTS,
    FAMILY_FINGERPRINTS,
    FAMILY_CHANGE_SETS,
    FAMILY_TASK_REPORTS,
)


def _location(tmp_path: Path, *, project_id: int = PROJECT_ID) -> ControlStateLocation:
    """A fully identity-resolved location over a disposable project + runtime root."""
    return ControlStateLocation(
        legacy_root=tmp_path / "projects" / "demo",
        project_id=project_id,
        control_root=project_control_state_root(tmp_path / "runtime", project_id),
    )


def _seed_legacy_state(location: ControlStateLocation) -> dict[str, Path]:
    """Write one historical artifact per family under ``<project>/.agent``."""
    legacy_root = location.legacy_root / CONTROL_STATE_DIR_NAME
    seeded: dict[str, Path] = {}

    events = legacy_root / FAMILY_EVENTS / "session_1_task_2.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(
        json.dumps({"event_id": "legacy-1", "event_type": "phase_started"}) + "\n",
        encoding="utf-8",
    )
    seeded[FAMILY_EVENTS] = events

    fingerprint = legacy_root / FAMILY_FINGERPRINTS / "session_1.json"
    fingerprint.parent.mkdir(parents=True, exist_ok=True)
    fingerprint.write_text(json.dumps({"anomaly_tags": ["legacy"]}), encoding="utf-8")
    seeded[FAMILY_FINGERPRINTS] = fingerprint

    manifest = legacy_root / FAMILY_CHANGE_SETS / "306" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"task_execution_id": 306}), encoding="utf-8")
    seeded[FAMILY_CHANGE_SETS] = manifest

    report = legacy_root / FAMILY_TASK_REPORTS / "task_report_2.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# legacy report\n", encoding="utf-8")
    seeded[FAMILY_TASK_REPORTS] = report

    state_manager = legacy_root / "state_manager.json"
    state_manager.write_text(json.dumps({"status": "legacy"}), encoding="utf-8")
    seeded["state_manager"] = state_manager

    return seeded


def _snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ── destination contract ─────────────────────────────────────────────────────


def test_write_root_is_the_runtime_root_not_the_project_repository(tmp_path):
    location = _location(tmp_path)

    for family in RELOCATED_FAMILIES:
        target = control_state_family_dir(location, family)
        assert target == tmp_path / "runtime" / "control" / "projects" / "12" / family
        assert CONTROL_STATE_DIR_NAME not in target.parts

    assert control_state_root(location) == (
        tmp_path / "runtime" / "control" / "projects" / "12"
    )


def test_durable_control_state_is_not_under_a_disposable_task_sandbox(tmp_path):
    """Task sandboxes live at <runtime_root>/tasks/…; control state must not."""
    control = control_state_root(_location(tmp_path))

    assert "tasks" not in control.relative_to(tmp_path / "runtime").parts


def test_an_unthreaded_location_keeps_the_historical_layout(tmp_path):
    """No project identity means no relocation: pre-cutover behaviour exactly."""
    location = ControlStateLocation(legacy_root=tmp_path / "project")

    assert control_state_family_dir(location, FAMILY_EVENTS) == (
        tmp_path / "project" / CONTROL_STATE_DIR_NAME / FAMILY_EVENTS
    )


def test_engineering_context_is_out_of_scope_and_stays_repository_keyed(tmp_path):
    """It is resolved without a Project.id, so it cannot relocate."""
    from app.services.engineering_context.service import _ContextFileStore

    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    store = _ContextFileStore(repository_root)

    assert store.directory == (
        repository_root.resolve() / CONTROL_STATE_DIR_NAME / FAMILY_ENGINEERING_CONTEXT
    )


# ── portability: identity survives a machine/config move ─────────────────────


def test_same_project_id_resolves_per_machine_without_persisting_a_machine_path():
    machine_a = ControlStateLocation(
        legacy_root=Path("/tmp/machine-a/projects/demo"),
        project_id=PROJECT_ID,
        control_root=project_control_state_root("/tmp/machine-a/runtime", PROJECT_ID),
    )
    machine_b = ControlStateLocation(
        legacy_root=Path("/tmp/machine-b/work/demo"),
        project_id=PROJECT_ID,
        control_root=project_control_state_root("/tmp/machine-b/state", PROJECT_ID),
    )

    # Legacy roots differ per machine.
    assert machine_a.legacy_root != machine_b.legacy_root
    # Identity does not.
    assert machine_a.identity == machine_b.identity == f"project:{PROJECT_ID}"
    # And the relocated path is the same suffix under each machine's runtime root.
    assert control_state_root(machine_a) == Path(
        "/tmp/machine-a/runtime/control/projects/12"
    )
    assert control_state_root(machine_b) == Path(
        "/tmp/machine-b/state/control/projects/12"
    )
    assert control_state_root(machine_a).relative_to(
        "/tmp/machine-a/runtime"
    ) == control_state_root(machine_b).relative_to("/tmp/machine-b/state")


@pytest.mark.parametrize(
    "workspace_path", ["demo-project", "/absolute/workspace/demo-project"]
)
def test_relative_and_absolute_workspace_paths_share_one_control_state(workspace_path):
    location = ControlStateLocation(
        legacy_root=Path(workspace_path),
        project_id=PROJECT_ID,
        control_root=project_control_state_root("/runtime", PROJECT_ID),
    )

    assert control_state_root(location) == Path("/runtime/control/projects/12")


def test_alternate_workspace_root_does_not_change_the_control_state_path():
    runtime_root = "/runtime"
    for workspace_root in ("/ws-a", "/ws-b"):
        location = ControlStateLocation(
            legacy_root=Path(workspace_root) / "demo",
            project_id=PROJECT_ID,
            control_root=project_control_state_root(runtime_root, PROJECT_ID),
        )
        assert control_state_root(location) == Path("/runtime/control/projects/12")


def test_alternate_runtime_root_moves_control_state_with_it():
    a = project_control_state_root("/runtime-a", PROJECT_ID)
    b = project_control_state_root("/runtime-b", PROJECT_ID)

    assert a != b
    assert a.name == b.name == str(PROJECT_ID)


# ── cutover: new writes leave the repository, history stays readable ─────────


def test_new_event_write_lands_on_the_runtime_root_only(tmp_path):
    location = _location(tmp_path)
    _seed_legacy_state(location)
    legacy_before = _snapshot(location.legacy_root)

    append_orchestration_event(
        project_dir=location,
        session_id=1,
        task_id=2,
        event_type="step_started",
    )

    relocated = (
        control_state_family_dir(location, FAMILY_EVENTS) / "session_1_task_2.jsonl"
    )
    assert relocated.exists()
    assert _snapshot(location.legacy_root) == legacy_before


def test_event_history_spans_the_cutover_in_order(tmp_path):
    location = _location(tmp_path)
    _seed_legacy_state(location)

    append_orchestration_event(
        project_dir=location,
        session_id=1,
        task_id=2,
        event_type="step_started",
    )

    events = read_orchestration_events(location, 1, 2)

    # Historical journal first, then the relocated one (a derived
    # health_score_updated event follows every append).
    assert [event["event_type"] for event in events][:2] == [
        "phase_started",
        "step_started",
    ]
    assert events[0]["event_id"] == "legacy-1"
    # Nothing is duplicated: each event_id appears exactly once.
    assert len({event["event_id"] for event in events}) == len(events)


def test_event_history_is_legacy_only_before_any_relocated_write(tmp_path):
    location = _location(tmp_path)
    _seed_legacy_state(location)

    assert [e["event_id"] for e in read_orchestration_events(location, 1, 2)] == [
        "legacy-1"
    ]


def test_legacy_probe_reader_ignores_the_relocated_journal(tmp_path):
    """Candidate-root selection must not be satisfied by identity-keyed events."""
    location = _location(tmp_path)
    append_orchestration_event(
        project_dir=location, session_id=1, task_id=2, event_type="step_started"
    )

    assert read_legacy_orchestration_events(location, 1, 2) == []
    assert read_orchestration_events(location, 1, 2)


def test_relocated_event_lock_file_sits_beside_the_relocated_journal(tmp_path):
    location = _location(tmp_path)
    seeded = _seed_legacy_state(location)
    legacy_lock = seeded[FAMILY_EVENTS].with_suffix(".jsonl.lock")
    legacy_lock.write_text("", encoding="utf-8")
    legacy_lock_before = legacy_lock.stat().st_mtime_ns

    append_orchestration_event(
        project_dir=location, session_id=1, task_id=2, event_type="step_started"
    )

    relocated_lock = (
        control_state_family_dir(location, FAMILY_EVENTS)
        / "session_1_task_2.jsonl.lock"
    )
    assert relocated_lock.exists()
    assert legacy_lock.exists()
    assert legacy_lock.stat().st_mtime_ns == legacy_lock_before


def test_a_lock_file_is_never_read_back_as_an_event(tmp_path):
    location = _location(tmp_path)
    append_orchestration_event(
        project_dir=location, session_id=1, task_id=2, event_type="step_started"
    )

    events = read_orchestration_events(location, 1, 2)

    assert events and all("event_type" in event for event in events)
    assert events[0]["event_type"] == "step_started"


def test_fingerprint_write_relocates_and_read_falls_back_to_history(tmp_path):
    location = _location(tmp_path)
    _seed_legacy_state(location)

    # Historical index still resolves before anything is relocated.
    assert read_session_fingerprint_index(location, 1, max_age_seconds=0) == {
        "anomaly_tags": ["legacy"]
    }

    write_session_fingerprint_index(location, 1, {"anomaly_tags": ["new"]})

    relocated = (
        control_state_family_dir(location, FAMILY_FINGERPRINTS) / "session_1.json"
    )
    assert relocated.exists()
    assert json.loads(
        (
            legacy_control_state_family_dir(location, FAMILY_FINGERPRINTS)
            / "session_1.json"
        ).read_text(encoding="utf-8")
    ) == {"anomaly_tags": ["legacy"]}
    # Latest-value semantics: the relocated index supersedes, never merges.
    assert read_session_fingerprint_index(location, 1, max_age_seconds=0)[
        "anomaly_tags"
    ] == ["new"]


def test_change_set_and_task_report_history_is_still_found(tmp_path):
    location = _location(tmp_path)
    _seed_legacy_state(location)

    historical_change_set = control_state_read_path(location, FAMILY_CHANGE_SETS, "306")
    historical_report = control_state_read_path(
        location, FAMILY_TASK_REPORTS, "task_report_2.md"
    )

    assert historical_change_set == (
        legacy_control_state_family_dir(location, FAMILY_CHANGE_SETS) / "306"
    )
    assert (
        json.loads((historical_change_set / "manifest.json").read_text())[
            "task_execution_id"
        ]
        == 306
    )
    assert historical_report.read_text(encoding="utf-8") == "# legacy report\n"


def test_a_relocated_artifact_supersedes_its_historical_twin(tmp_path):
    location = _location(tmp_path)
    _seed_legacy_state(location)
    relocated_report = (
        control_state_family_dir(location, FAMILY_TASK_REPORTS) / "task_report_2.md"
    )
    relocated_report.parent.mkdir(parents=True, exist_ok=True)
    relocated_report.write_text("# relocated report\n", encoding="utf-8")

    resolved = control_state_read_path(
        location, FAMILY_TASK_REPORTS, "task_report_2.md"
    )

    assert resolved == relocated_report


def test_state_manager_write_relocates_and_read_prefers_it(tmp_path):
    project_root = tmp_path / "projects" / "demo"
    runtime_root = tmp_path / "runtime"
    location = _location(tmp_path)
    _seed_legacy_state(location)

    # Read falls back to the historical snapshot while none is relocated.
    read_path = control_state_read_path(location, "state_manager.json")
    assert read_path == project_root / CONTROL_STATE_DIR_NAME / "state_manager.json"
    assert json.loads(read_path.read_text())["status"] == "legacy"

    relocated = control_state_root(location) / "state_manager.json"
    relocated.parent.mkdir(parents=True, exist_ok=True)
    relocated.write_text(json.dumps({"status": "relocated"}), encoding="utf-8")

    assert control_state_read_path(location, "state_manager.json") == relocated
    assert relocated.parent == runtime_root / "control" / "projects" / "12"


def test_snapshot_index_keeps_counting_across_the_cutover(tmp_path):
    from app.services.orchestration.state.persistence import (
        write_orchestration_state_snapshot,
    )
    from app.services.orchestration.prompt_templates import OrchestrationState

    location = _location(tmp_path)
    legacy_snapshots = (
        legacy_control_state_family_dir(location, FAMILY_EVENTS)
        / "session_1_task_2_state_snapshots.jsonl"
    )
    legacy_snapshots.parent.mkdir(parents=True, exist_ok=True)
    legacy_snapshots.write_text(
        json.dumps({"snapshot_index": 0})
        + "\n"
        + json.dumps({"snapshot_index": 1})
        + "\n",
        encoding="utf-8",
    )

    state = OrchestrationState(
        session_id="1", task_description="d", task_id=2, project_id=PROJECT_ID
    )
    state._control_root_cache = str(location.control_root)
    state._project_dir_override = str(location.legacy_root)

    payload = write_orchestration_state_snapshot(
        project_dir=location.legacy_root,
        session_id=1,
        task_id=2,
        orchestration_state=state,
        trigger="test",
    )

    assert payload["snapshot_index"] == 2
    assert [
        s["snapshot_index"] for s in read_orchestration_state_snapshots(location, 1, 2)
    ] == [
        0,
        1,
        2,
    ]


def test_no_relocated_family_write_mutates_the_project_repository(tmp_path):
    """The gate's core claim, checked per family against a byte+mtime snapshot."""
    location = _location(tmp_path)
    _seed_legacy_state(location)
    before = _snapshot(location.legacy_root)

    append_orchestration_event(
        project_dir=location, session_id=1, task_id=2, event_type="step_started"
    )
    write_session_fingerprint_index(location, 1, {"anomaly_tags": ["new"]})
    for family, name in (
        (FAMILY_CHANGE_SETS, "306/manifest.json"),
        (FAMILY_TASK_REPORTS, "task_report_2.md"),
    ):
        target = control_state_family_dir(location, family) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("relocated", encoding="utf-8")
    state_manager = control_state_root(location) / "state_manager.json"
    state_manager.write_text("{}", encoding="utf-8")

    assert _snapshot(location.legacy_root) == before
    assert (
        not (location.legacy_root / CONTROL_STATE_DIR_NAME)
        .joinpath(FAMILY_EVENTS, "session_1_task_2.jsonl.lock")
        .exists()
    )


def test_relocation_does_not_dual_write(tmp_path):
    location = _location(tmp_path)

    append_orchestration_event(
        project_dir=location, session_id=1, task_id=2, event_type="step_started"
    )

    assert not (location.legacy_root / CONTROL_STATE_DIR_NAME).exists()


# ── failure behaviour and test-double safety ─────────────────────────────────


def test_missing_runtime_root_identity_fails_closed():
    from app.services.workspace.control_state_paths import (
        resolve_project_control_root,
    )

    with pytest.raises(ValueError):
        resolve_project_control_root(None)


def test_a_mock_project_id_can_never_name_a_control_state_directory():
    """A stub database yields mock ids; they must not become real directories."""
    with pytest.raises(TypeError):
        project_control_state_root("/runtime", MagicMock(name="mock.project_id"))
    with pytest.raises(TypeError):
        project_control_state_root("/runtime", "not-an-id")


def test_a_mock_runtime_root_can_never_become_a_control_state_root():
    """A stub database yields a mock runtime-root setting value."""
    with pytest.raises(TypeError):
        project_control_state_root(MagicMock(name="mock.runtime_root"), PROJECT_ID)


def test_a_stub_database_fails_closed_instead_of_writing_a_mock_tree(tmp_path):
    from app.services.workspace.control_state_paths import (
        project_control_state_location,
        resolve_project_control_root,
    )

    stub_db = MagicMock(name="db")

    with pytest.raises(TypeError):
        resolve_project_control_root(MagicMock(name="mock.project_id"), db=stub_db)
    with pytest.raises(TypeError):
        project_control_state_location(
            tmp_path / "project", MagicMock(name="mock.project_id"), db=stub_db
        )

    assert not (Path.cwd() / "MagicMock").exists()


def test_a_magic_mock_root_still_cannot_create_repository_directories(tmp_path):
    from app.services.workspace.control_state_paths import (
        coerce_control_state_location,
    )

    with pytest.raises(TypeError):
        coerce_control_state_location(MagicMock(name="project_dir"))
    with pytest.raises(TypeError):
        control_state_of(MagicMock(name="orchestration_state"))

    assert not (tmp_path / "MagicMock").exists()


def test_a_missing_project_dir_never_falls_back_to_the_process_cwd():
    class _NoProjectDir:
        project_id = PROJECT_ID

    with pytest.raises(TypeError):
        control_state_of(_NoProjectDir())


def test_relocated_control_state_is_never_hydrated_as_project_content():
    from app.services.workspace.workspace_paths import HYDRATION_EXCLUDED_NAMES

    assert CONTROL_STATE_DIR_NAME in HYDRATION_EXCLUDED_NAMES


def test_control_state_dir_was_not_added_to_the_pollution_guard_exceptions():
    """Ownership was fixed by moving the writes, not by tolerating `.agent`."""
    from app.services.orchestration.validation.runtime_pollution_guard import (
        KNOWN_OPENCLAW_RUNTIME_SCAFFOLD_NAMES,
        ORCHESTRATOR_RUNTIME_STATE_NAMES,
    )

    assert CONTROL_STATE_DIR_NAME not in ORCHESTRATOR_RUNTIME_STATE_NAMES
    # The OpenClaw scaffold detection list is unchanged by this gate.
    assert ".openclaw" in KNOWN_OPENCLAW_RUNTIME_SCAFFOLD_NAMES
    assert "SOUL.md" in KNOWN_OPENCLAW_RUNTIME_SCAFFOLD_NAMES


def test_event_log_path_helper_uses_the_relocated_root(tmp_path):
    location = _location(tmp_path)

    assert _orchestration_event_log_path(location, 1, 2) == (
        control_state_family_dir(location, FAMILY_EVENTS) / "session_1_task_2.jsonl"
    )


def test_state_manager_helpers_agree_for_an_unthreaded_root(tmp_path):
    root = tmp_path / "project"

    assert (
        get_state_manager_path(root)
        == get_state_manager_read_path(root)
        == root / CONTROL_STATE_DIR_NAME / "state_manager.json"
    )
