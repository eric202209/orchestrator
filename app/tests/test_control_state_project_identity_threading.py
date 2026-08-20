"""Control-state identity is Project.id, not the project's physical location.

These tests pin the prerequisite for relocating Orchestrator-owned durable
control state out of the project repository: identity must survive a project
moving between workspace roots, while the *current* storage location must stay
byte- and path-compatible with the legacy ``<project_root>/.agent`` layout.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.orchestration.prompt_templates import OrchestrationState
from app.services.orchestration.state.persistence import (
    _orchestration_event_log_path,
    _session_fingerprint_index_path,
    append_orchestration_event,
    read_orchestration_events,
    read_session_fingerprint_index,
    write_session_fingerprint_index,
)
from app.services.orchestration.execution.runtime import get_state_manager_path
from app.services.orchestration.task_rules import get_task_report_path
from app.services.workspace.control_state_paths import (
    CONTROL_STATE_DIR_NAME,
    FAMILY_CHANGE_SETS,
    FAMILY_EVENTS,
    ControlStateLocation,
    coerce_control_state_location,
    control_state_of,
    control_state_family_dir,
    control_state_identity,
    control_state_root,
    project_control_state_root,
)
from app.services.workspace.system_settings import get_effective_runtime_root

pytestmark = pytest.mark.unit

PROJECT_ID = 12


# ── identity is independent of physical location ─────────────────────────────


def test_same_project_id_in_two_workspace_roots_has_one_identity(tmp_path):
    """Project 12 checked out under two different workspace roots is one project."""
    workspace_a = tmp_path / "machine-a" / "workspace-root"
    workspace_b = tmp_path / "machine-b" / "other-workspace-root"
    root_a = workspace_a / "orchestrator"
    root_b = workspace_b / "orchestrator"

    location_a = ControlStateLocation(legacy_root=root_a, project_id=PROJECT_ID)
    location_b = ControlStateLocation(legacy_root=root_b, project_id=PROJECT_ID)

    # Identity is the same...
    assert location_a.identity == location_b.identity == f"project:{PROJECT_ID}"
    # ...even though the current (legacy, un-relocated) storage location differs.
    assert control_state_root(location_a) != control_state_root(location_b)


def test_identity_resolution_is_distinct_from_current_storage_location(tmp_path):
    """The two concepts must not be conflated while relocation has not happened."""
    root = tmp_path / "workspace-root" / "orchestrator"
    location = ControlStateLocation(legacy_root=root, project_id=PROJECT_ID)

    assert control_state_identity(location) == f"project:{PROJECT_ID}"
    assert control_state_root(location) == root / CONTROL_STATE_DIR_NAME


def test_relocated_root_is_keyed_only_by_project_id(db_session, tmp_path, monkeypatch):
    """The relocation target is derived from the runtime root + Project.id only."""
    monkeypatch.setattr(
        "app.services.workspace.system_settings.settings.RUNTIME_ROOT",
        str(tmp_path / "runtime-root"),
    )
    runtime_root = get_effective_runtime_root(db_session)

    target = project_control_state_root(runtime_root, PROJECT_ID)

    assert target == runtime_root / "control" / "projects" / str(PROJECT_ID)
    # No workspace path, machine root, or project name contributes to it.
    assert "orchestrator" not in target.relative_to(runtime_root).as_posix()


def test_relocated_root_requires_identity():
    with pytest.raises(ValueError):
        project_control_state_root("/anything", None)


# ── current storage location is unchanged by this gate ───────────────────────


def test_event_log_path_is_unchanged_legacy_layout(tmp_path):
    root = tmp_path / "project"
    expected = root / ".agent" / "events" / "session_1_task_2.jsonl"

    assert _orchestration_event_log_path(root, 1, 2) == expected
    assert _orchestration_event_log_path(root, 1, 2, project_id=PROJECT_ID) == expected


def test_fingerprint_path_is_unchanged_legacy_layout(tmp_path):
    root = tmp_path / "project"
    expected = root / ".agent" / "fingerprints" / "session_7.json"

    assert _session_fingerprint_index_path(root, 7) == expected
    assert _session_fingerprint_index_path(root, 7, project_id=PROJECT_ID) == expected


def test_change_set_state_manager_and_report_paths_are_unchanged(tmp_path, db_session):
    from app.models import Project, Task

    root = tmp_path / "project"
    assert (
        control_state_family_dir(root, FAMILY_CHANGE_SETS, project_id=PROJECT_ID) / "9"
        == root / ".agent" / "change-sets" / "9"
    )
    # Relocated: identity now resolves the state manager under the runtime root.
    assert get_state_manager_path(root, project_id=PROJECT_ID) == (
        get_effective_runtime_root(db_session)
        / "control"
        / "projects"
        / str(PROJECT_ID)
        / "state_manager.json"
    )

    project = Project(name="Report Project", user_id=1)
    db_session.add(project)
    db_session.commit()
    task = Task(project_id=project.id, title="t", description="d")
    db_session.add(task)
    db_session.commit()

    assert (
        get_task_report_path(root, task, project_id=project.id)
        == root / ".agent" / "task-reports" / f"task_report_{task.id}.md"
    )


# ── identity threading through the event-journal boundary ────────────────────


def test_orchestration_state_carries_project_id_to_control_state(tmp_path):
    state = OrchestrationState(
        session_id="5",
        task_description="d",
        project_name="Orchestrator",
        task_id=3,
        project_id=PROJECT_ID,
    )
    state._project_dir_override = str(tmp_path / "project")

    location = state.control_state_location

    assert isinstance(location, ControlStateLocation)
    assert location.project_id == PROJECT_ID
    assert Path(location) == tmp_path / "project"


def test_append_and_read_round_trip_carries_identity_and_legacy_path(tmp_path):
    state = OrchestrationState(
        session_id="5",
        task_description="d",
        project_name="Orchestrator",
        task_id=3,
        project_id=PROJECT_ID,
    )
    state._project_dir_override = str(tmp_path / "project")

    append_orchestration_event(
        project_dir=state.control_state_location,
        session_id=5,
        task_id=3,
        event_type="phase_started",
        details={"phase": "planning"},
    )

    # Relocated: the write leaves the project repository entirely.
    written = (
        project_control_state_root(get_effective_runtime_root(), PROJECT_ID)
        / "events"
        / "session_5_task_3.jsonl"
    )
    assert written.exists(), "write location must be the runtime control root"
    assert not (tmp_path / "project" / ".agent").exists()

    events = read_orchestration_events(state.control_state_location, 5, 3)
    assert events[0]["event_type"] == "phase_started"

    # A bare path carries no resolved control root, so it stays legacy-scoped
    # and sees only the historical journal — here, nothing.
    assert (
        read_orchestration_events(tmp_path / "project", 5, 3, project_id=PROJECT_ID)
        == []
    )


def test_explicit_project_id_never_overwrites_threaded_identity(tmp_path):
    threaded = ControlStateLocation(legacy_root=tmp_path, project_id=PROJECT_ID)

    assert coerce_control_state_location(threaded, project_id=99).project_id == (
        PROJECT_ID
    )
    assert coerce_control_state_location(tmp_path, project_id=99).project_id == 99


def test_control_state_of_is_total_over_duck_typed_states(tmp_path):
    """Producers must resolve a location even for stand-in state objects.

    Every event emission is wrapped in a best-effort ``try/except``; raising
    here would drop the event silently instead of failing loudly.
    """

    class _StandIn:
        project_dir = tmp_path / "project"

    location = control_state_of(_StandIn())
    assert Path(location) == tmp_path / "project"
    assert location.project_id is None

    real = OrchestrationState(
        session_id="1",
        task_description="d",
        project_name="p",
        task_id=1,
        project_id=PROJECT_ID,
    )
    real._project_dir_override = str(tmp_path / "project")
    assert control_state_of(real).project_id == PROJECT_ID


def test_missing_project_dir_never_resolves_to_the_process_cwd():
    """A state with no project_dir must raise, not write into the CWD.

    Defaulting to "." would put Orchestrator control state in whatever
    directory the worker happens to run in — the exact ownership violation
    this contract exists to prevent. Raising matches the pre-existing
    ``Path(None)`` behavior inside the caller's best-effort ``try/except``.
    """

    class _NoProjectDir:
        project_dir = None

    with pytest.raises(TypeError):
        control_state_of(_NoProjectDir())


def test_dynamic_mock_path_cannot_write_control_state_into_cwd(tmp_path, monkeypatch):
    """An unconfigured mock path must fail closed instead of creating ``MagicMock/``."""
    monkeypatch.chdir(tmp_path)
    state = MagicMock(name="orchestration_state")

    with pytest.raises(TypeError, match="concrete path"):
        append_orchestration_event(
            project_dir=state.control_state_location,
            session_id=1,
            task_id=42,
            event_type="probe",
            details={},
        )

    assert not (tmp_path / "MagicMock").exists()


def test_control_state_of_also_rejects_dynamic_mock_paths(tmp_path, monkeypatch):
    """The second entry point must fail closed the same way.

    ``control_state_of`` reads ``project_dir`` directly when the state carries
    no ``ControlStateLocation``, so it needs the same concrete-path coercion as
    ``coerce_control_state_location`` or a mock slips past the guard.
    """
    monkeypatch.chdir(tmp_path)

    with pytest.raises(TypeError, match="concrete path"):
        control_state_of(MagicMock(name="orchestration_state"))

    assert not (tmp_path / "MagicMock").exists()


def test_identity_is_never_inferred_from_a_path(tmp_path):
    """An unthreaded caller stays unthreaded rather than inventing an identity."""
    location = coerce_control_state_location(tmp_path / "orchestrator")

    assert location.project_id is None
    assert location.identity is None
    assert control_state_family_dir(location, FAMILY_EVENTS) == (
        tmp_path / "orchestrator" / ".agent" / "events"
    )


def test_fingerprint_round_trip_is_identity_threaded_and_path_compatible(tmp_path):
    root = tmp_path / "project"

    write_session_fingerprint_index(
        root, 7, {"anomaly_tags": ["a"]}, project_id=PROJECT_ID
    )

    assert (root / ".agent" / "fingerprints" / "session_7.json").exists()
    loaded = read_session_fingerprint_index(root, 7, project_id=PROJECT_ID)
    assert loaded is not None and loaded["anomaly_tags"] == ["a"]


def test_relocating_a_project_does_not_split_its_control_state_identity(tmp_path):
    """The move that the relocation gate must survive, proven without moving data."""
    before = ControlStateLocation(
        legacy_root=tmp_path / "root-a" / "orchestrator", project_id=PROJECT_ID
    )
    after = ControlStateLocation(
        legacy_root=tmp_path / "root-b" / "orchestrator", project_id=PROJECT_ID
    )

    runtime_root = tmp_path / "runtime"
    assert project_control_state_root(
        runtime_root, before.project_id
    ) == project_control_state_root(runtime_root, after.project_id)
