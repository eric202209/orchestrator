"""HG Hardening Phase 3 — WM purpose semantics tests.

Covers:
  - _is_wm_eligible_entry correctly classifies purpose_targets
  - write_working_memory includes all-purpose guidance (NULL / ["all"])
  - write_working_memory includes planning-purpose guidance
  - write_working_memory includes repair-purpose guidance
  - write_working_memory EXCLUDES execution-only guidance
  - write_working_memory includes mixed execution+planning guidance (not exclusively exec)
  - legacy NULL purpose_targets guidance still included (unchanged behavior)
  - backend/model filtering still respected after the purpose change
  - selection/telemetry rows still recorded for included entries
  - effective_purposes metadata field present on each stored entry
  - existing WM test invariants still hold
  - P3 purpose routing unchanged (planning/execution/repair/validation paths)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    HumanGuidance,
    HumanGuidanceUsage,
    Project,
    Session as SessionModel,
    User,
)
from app.services.human_guidance.service import (
    collect_active_guidance,
    create_guidance,
)
from app.services.orchestration.working_memory import (
    _FILENAME,
    _HUMAN_GUIDANCE_LIMIT,
    _WM_EXCLUDED_SOLE_PURPOSES,
    _is_wm_eligible_entry,
    write_working_memory,
)


# ── fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(email="p3-wm@example.com", hashed_password="x", is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def project(db_session: Session, user: User) -> Project:
    p = Project(name="p3-wm-project", workspace_path=None, user_id=user.id)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def running_session(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="p3-wm-session",
        status="running",
        is_active=True,
        instance_id="p3-wm-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def _make_orch_state(project_dir: str) -> MagicMock:
    state = MagicMock()
    state.project_dir = project_dir
    state.plan = []
    state.changed_files = []
    state.validation_history = []
    state.project_context = ""
    return state


def _make_task(task_id: int = 1) -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.title = "p3 test task"
    t.plan_position = task_id
    return t


def _make_logger() -> MagicMock:
    return MagicMock()


def _write_wm(db, session_id, tmp_path, task_id=1):
    state = _make_orch_state(str(tmp_path))
    state.session_id = session_id
    write_working_memory(
        orchestration_state=state,
        task=_make_task(task_id),
        summary="done",
        logger=_make_logger(),
        db=db,
    )
    return json.loads((tmp_path / ".agent" / _FILENAME).read_text())


# ── 1. _is_wm_eligible_entry ──────────────────────────────────────────────────


class TestIsWmEligibleEntry:
    def test_null_purpose_targets_is_eligible(self):
        assert _is_wm_eligible_entry({"purpose_targets": None}) is True

    def test_empty_list_is_eligible(self):
        assert _is_wm_eligible_entry({"purpose_targets": []}) is True

    def test_all_purpose_is_eligible(self):
        assert _is_wm_eligible_entry({"purpose_targets": ["all"]}) is True

    def test_planning_only_is_eligible(self):
        assert _is_wm_eligible_entry({"purpose_targets": ["planning"]}) is True

    def test_repair_only_is_eligible(self):
        assert _is_wm_eligible_entry({"purpose_targets": ["repair"]}) is True

    def test_validation_only_is_eligible(self):
        assert _is_wm_eligible_entry({"purpose_targets": ["validation"]}) is True

    def test_execution_only_is_not_eligible(self):
        assert _is_wm_eligible_entry({"purpose_targets": ["execution"]}) is False

    def test_execution_plus_planning_is_eligible(self):
        # Mixed: execution AND planning — the entry also applies to planning
        assert (
            _is_wm_eligible_entry({"purpose_targets": ["execution", "planning"]})
            is True
        )

    def test_execution_plus_all_is_eligible(self):
        assert _is_wm_eligible_entry({"purpose_targets": ["execution", "all"]}) is True

    def test_missing_key_is_eligible(self):
        assert _is_wm_eligible_entry({}) is True

    def test_wm_excluded_sole_purposes_constant(self):
        assert "execution" in _WM_EXCLUDED_SOLE_PURPOSES
        assert "planning" not in _WM_EXCLUDED_SOLE_PURPOSES
        assert "all" not in _WM_EXCLUDED_SOLE_PURPOSES


# ── 2. write_working_memory — purpose inclusion/exclusion ─────────────────────


class TestWmPurposeSemantics:
    def test_includes_null_purpose_guidance(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        """Legacy guidance with NULL purpose_targets must remain in WM."""
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Null purpose guidance.",
            priority=50,
            # purpose_targets omitted → NULL → defaults to ["all"]
        )

        data = _write_wm(db_session, running_session.id, tmp_path)
        messages = [g["message"] for g in data["human_guidance"]]
        assert "Null purpose guidance." in messages

    def test_includes_all_purpose_guidance(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="All purpose guidance.",
            priority=50,
            purpose_targets=["all"],
        )

        data = _write_wm(db_session, running_session.id, tmp_path)
        messages = [g["message"] for g in data["human_guidance"]]
        assert "All purpose guidance." in messages

    def test_includes_planning_purpose_guidance(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        """Planning-purpose guidance was previously excluded (bug). Now must be included."""
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Planning-only guidance.",
            priority=50,
            purpose_targets=["planning"],
        )

        data = _write_wm(db_session, running_session.id, tmp_path)
        messages = [g["message"] for g in data["human_guidance"]]
        assert (
            "Planning-only guidance." in messages
        ), "Planning-purpose guidance must appear in WM (planning-visible context)"

    def test_includes_repair_purpose_guidance(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Repair-only guidance.",
            priority=50,
            purpose_targets=["repair"],
        )

        data = _write_wm(db_session, running_session.id, tmp_path)
        messages = [g["message"] for g in data["human_guidance"]]
        assert "Repair-only guidance." in messages

    def test_excludes_execution_only_guidance(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        """Execution-only guidance must NOT appear in WM (would leak into planner)."""
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Execution-only guidance.",
            priority=50,
            purpose_targets=["execution"],
        )

        data = _write_wm(db_session, running_session.id, tmp_path)
        messages = [g["message"] for g in data["human_guidance"]]
        assert (
            "Execution-only guidance." not in messages
        ), "Execution-only guidance must not leak into planning-context WM"

    def test_includes_mixed_execution_and_planning_guidance(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        """Entry tagged ['execution', 'planning'] is not exclusively execution → included."""
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Mixed execution+planning guidance.",
            priority=50,
            purpose_targets=["execution", "planning"],
        )

        data = _write_wm(db_session, running_session.id, tmp_path)
        messages = [g["message"] for g in data["human_guidance"]]
        assert "Mixed execution+planning guidance." in messages

    def test_execution_and_all_purpose_guidance_included(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        """['execution', 'all'] should be included (has "all")."""
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Execution+all guidance.",
            priority=50,
            purpose_targets=["execution", "all"],
        )

        data = _write_wm(db_session, running_session.id, tmp_path)
        messages = [g["message"] for g in data["human_guidance"]]
        assert "Execution+all guidance." in messages


# ── 3. effective_purposes metadata ───────────────────────────────────────────


class TestEffectivePurposesMetadata:
    def test_stored_entries_have_effective_purposes_field(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Guidance with purpose metadata.",
            priority=50,
        )

        data = _write_wm(db_session, running_session.id, tmp_path)
        assert len(data["human_guidance"]) == 1
        g = data["human_guidance"][0]
        assert "effective_purposes" in g
        assert isinstance(g["effective_purposes"], list)
        assert len(g["effective_purposes"]) > 0

    def test_effective_purposes_reflects_planning_target(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Planning guidance.",
            purpose_targets=["planning"],
        )

        data = _write_wm(db_session, running_session.id, tmp_path)
        g = data["human_guidance"][0]
        assert "planning" in g["effective_purposes"]

    def test_null_purpose_entry_has_effective_purposes_all(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Null purpose to all.",
            # no purpose_targets
        )

        data = _write_wm(db_session, running_session.id, tmp_path)
        g = data["human_guidance"][0]
        # NULL purpose_targets defaults to ["all"] in collect_active_guidance
        assert "all" in g["effective_purposes"]

    def test_extra_field_does_not_break_render(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        """render_guidance_block only reads 'message'; extra keys must not raise."""
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "WORKING_MEMORY_RENDER_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Render-safe guidance.",
        )

        _write_wm(db_session, running_session.id, tmp_path)

        from app.services.orchestration.working_memory import render_working_memory

        rendered = render_working_memory(str(tmp_path), _make_logger())
        assert "Render-safe guidance." in rendered


# ── 4. Backend/model filtering still respected ────────────────────────────────


class TestBackendModelFilteringRespected:
    def test_backend_filtered_guidance_not_in_wm(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Qwen-only guidance.",
            priority=50,
            backend_targets=["qwen"],
        )

        state = _make_orch_state(str(tmp_path))
        state.session_id = running_session.id
        write_working_memory(
            orchestration_state=state,
            task=_make_task(1),
            summary="done",
            logger=_make_logger(),
            db=db_session,
            guidance_backend="ollama",  # does not match qwen
        )

        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        messages = [g["message"] for g in data["human_guidance"]]
        assert "Qwen-only guidance." not in messages

    def test_all_backend_guidance_included(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="All backends guidance.",
            priority=50,
            backend_targets=["all"],
        )

        state = _make_orch_state(str(tmp_path))
        state.session_id = running_session.id
        write_working_memory(
            orchestration_state=state,
            task=_make_task(1),
            summary="done",
            logger=_make_logger(),
            db=db_session,
            guidance_backend="ollama",
        )

        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        messages = [g["message"] for g in data["human_guidance"]]
        assert "All backends guidance." in messages


# ── 5. Telemetry rows still recorded ─────────────────────────────────────────


class TestTelemetryRows:
    def test_usage_row_recorded_for_included_entry(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Telemetry test guidance.",
            purpose_targets=["planning"],
        )

        _write_wm(db_session, running_session.id, tmp_path)

        usage = (
            db_session.query(HumanGuidanceUsage)
            .filter(HumanGuidanceUsage.guidance_id == entry.id)
            .first()
        )
        assert usage is not None
        assert usage.rendered is True
        assert usage.source == "human_guidance_table"

    def test_no_usage_row_for_execution_only_entry(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        """Execution-only entries are filtered before selection, so no usage row."""
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Execution-only, no usage.",
            purpose_targets=["execution"],
        )

        _write_wm(db_session, running_session.id, tmp_path)

        usage = (
            db_session.query(HumanGuidanceUsage)
            .filter(HumanGuidanceUsage.guidance_id == entry.id)
            .first()
        )
        assert (
            usage is None
        ), "Filtered-out execution-only entry must not create a usage row"


# ── 6. Purpose isolation — multiple entries ───────────────────────────────────


class TestMixedPurposeEntries:
    def test_only_eligible_entries_written_when_mixed_pool(
        self,
        tmp_path,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
        monkeypatch,
    ):
        """Three entries: all-purpose, planning-only, execution-only.
        WM must contain first two, not the third."""
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="All purpose.",
            priority=90,
            purpose_targets=["all"],
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Planning only.",
            priority=80,
            purpose_targets=["planning"],
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Execution only.",
            priority=100,  # highest priority — must still be excluded
            purpose_targets=["execution"],
        )

        data = _write_wm(db_session, running_session.id, tmp_path)
        messages = [g["message"] for g in data["human_guidance"]]

        assert "All purpose." in messages
        assert "Planning only." in messages
        assert "Execution only." not in messages
        assert len(messages) == 2

    def test_p3_purpose_routing_collect_still_works(
        self,
        db_session: Session,
        user: User,
        project: Project,
        running_session: SessionModel,
    ):
        """P3 purpose routing in collect_active_guidance is unchanged.

        Planning-purpose query returns planning+all entries.
        Execution-purpose query returns execution+all entries.
        """
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="All-purpose rule.",
            purpose_targets=["all"],
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Planning rule.",
            purpose_targets=["planning"],
        )
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            scope="session",
            message="Execution rule.",
            purpose_targets=["execution"],
        )

        planning_results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
            purpose="planning",
        )
        planning_messages = {r["message"] for r in planning_results}
        assert "All-purpose rule." in planning_messages
        assert "Planning rule." in planning_messages
        assert "Execution rule." not in planning_messages

        execution_results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=running_session.id,
            task_id=None,
            purpose="execution",
        )
        execution_messages = {r["message"] for r in execution_results}
        assert "All-purpose rule." in execution_messages
        assert "Execution rule." in execution_messages
        assert "Planning rule." not in execution_messages
