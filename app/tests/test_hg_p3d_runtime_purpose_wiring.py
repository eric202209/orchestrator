"""Tests for HG-P3d — Runtime Purpose-Aware Guidance Collection."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Project, Session as SessionModel, User
from app.services.human_guidance.conflicts import detect_guidance_task_conflicts
from app.services.human_guidance.plan_validator import (
    check_plan_guidance_violations_if_enabled,
    render_active_guidance_for_repair,
)
from app.services.human_guidance.service import collect_active_guidance, create_guidance
from app.services.orchestration.working_memory import _FILENAME, write_working_memory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(email="p3d@example.com", hashed_password="hashed", is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def project(db_session: Session, user: User) -> Project:
    p = Project(name="p3d-project", workspace_path=None, user_id=user.id)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def session_row(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="p3d-session",
        status="running",
        is_active=True,
        instance_id="p3d-instance",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def client(authenticated_client: TestClient, user: User) -> TestClient:
    return authenticated_client


def _messages(entries: list[dict]) -> set[str]:
    return {str(e["message"]) for e in entries}


def _seed_purpose_guidance(db: Session, user: User, project: Project) -> dict[str, int]:
    """Seed one entry per purpose and one all-purpose entry. Returns {purpose: id}."""
    ids: dict[str, int] = {}
    specs = [
        ("planning", "avoid mutable defaults"),
        ("repair", "preserve existing tests during repair"),
        ("execution", "use stdout"),
        ("validation", "require pytest evidence"),
        ("all", "global rule applies everywhere"),
    ]
    for purpose, message in specs:
        entry, _ = create_guidance(
            db,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message=message,
            purpose_targets=[purpose],
        )
        ids[purpose] = entry.id
    return ids


# ---------------------------------------------------------------------------
# Smoke: collect_active_guidance per purpose
# ---------------------------------------------------------------------------


class TestSmokePurposeRouting:
    def test_planning_sees_planning_and_all(self, db_session, user, project):
        _seed_purpose_guidance(db_session, user, project)
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            purpose="planning",
        )
        msgs = _messages(entries)
        assert "avoid mutable defaults" in msgs
        assert "global rule applies everywhere" in msgs
        assert "preserve existing tests during repair" not in msgs
        assert "use stdout" not in msgs
        assert "require pytest evidence" not in msgs

    def test_repair_sees_repair_and_all(self, db_session, user, project):
        _seed_purpose_guidance(db_session, user, project)
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            purpose="repair",
        )
        msgs = _messages(entries)
        assert "preserve existing tests during repair" in msgs
        assert "global rule applies everywhere" in msgs
        assert "avoid mutable defaults" not in msgs
        assert "use stdout" not in msgs
        assert "require pytest evidence" not in msgs

    def test_execution_sees_execution_and_all(self, db_session, user, project):
        _seed_purpose_guidance(db_session, user, project)
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            purpose="execution",
        )
        msgs = _messages(entries)
        assert "use stdout" in msgs
        assert "global rule applies everywhere" in msgs
        assert "avoid mutable defaults" not in msgs
        assert "preserve existing tests during repair" not in msgs
        assert "require pytest evidence" not in msgs

    def test_validation_sees_validation_and_all(self, db_session, user, project):
        _seed_purpose_guidance(db_session, user, project)
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            purpose="validation",
        )
        msgs = _messages(entries)
        assert "require pytest evidence" in msgs
        assert "global rule applies everywhere" in msgs
        assert "avoid mutable defaults" not in msgs
        assert "use stdout" not in msgs
        assert "preserve existing tests during repair" not in msgs

    def test_all_purpose_sees_everything(self, db_session, user, project):
        _seed_purpose_guidance(db_session, user, project)
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            purpose="all",
        )
        msgs = _messages(entries)
        assert "avoid mutable defaults" in msgs
        assert "preserve existing tests during repair" in msgs
        assert "use stdout" in msgs
        assert "require pytest evidence" in msgs
        assert "global rule applies everywhere" in msgs


# ---------------------------------------------------------------------------
# P2b plan validator uses purpose="planning"
# ---------------------------------------------------------------------------


class TestPlanValidatorPurposePlanning:
    def test_planning_only_guidance_triggers_violation(
        self,
        db_session,
        monkeypatch,
        user,
        project,
        session_row,
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="All output must go to stdout. Never use logging.",
            purpose_targets=["planning"],
        )
        plan = [
            {
                "step_number": 1,
                "description": "write",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "foo.py",
                        "content": "import logging\nlogger = logging.getLogger(__name__)",
                    }
                ],
                "commands": [],
            }
        ]
        violations = check_plan_guidance_violations_if_enabled(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
            plan_steps=plan,
        )
        assert any("stdout_vs_logging" in v for v in violations)

    def test_repair_only_guidance_does_not_trigger_plan_violation(
        self,
        db_session,
        monkeypatch,
        user,
        project,
        session_row,
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="All output must go to stdout. Never use logging.",
            purpose_targets=["repair"],
        )
        plan = [
            {
                "step_number": 1,
                "description": "write",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "foo.py",
                        "content": "import logging\nlogger = logging.getLogger(__name__)",
                    }
                ],
                "commands": [],
            }
        ]
        violations = check_plan_guidance_violations_if_enabled(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
            plan_steps=plan,
        )
        assert violations == []

    def test_all_purpose_guidance_triggers_violation_in_planning(
        self,
        db_session,
        monkeypatch,
        user,
        project,
        session_row,
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="All output must go to stdout. Never use logging.",
            purpose_targets=["all"],
        )
        plan = [
            {
                "step_number": 1,
                "description": "write",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "foo.py",
                        "content": "import logging\nlogger = logging.getLogger(__name__)",
                    }
                ],
                "commands": [],
            }
        ]
        violations = check_plan_guidance_violations_if_enabled(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
            plan_steps=plan,
        )
        assert any("stdout_vs_logging" in v for v in violations)

    def test_validation_only_guidance_does_not_appear_in_planning(
        self,
        db_session,
        monkeypatch,
        user,
        project,
        session_row,
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="All output must go to stdout. Never use logging.",
            purpose_targets=["validation"],
        )
        plan = [
            {
                "step_number": 1,
                "description": "write",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "foo.py",
                        "content": "import logging\nlogger = logging.getLogger(__name__)",
                    }
                ],
                "commands": [],
            }
        ]
        violations = check_plan_guidance_violations_if_enabled(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
            plan_steps=plan,
        )
        assert violations == []


# ---------------------------------------------------------------------------
# Repair prompt renderer uses purpose="repair"
# ---------------------------------------------------------------------------


class TestRepairRendererPurposeRepair:
    def test_repair_only_guidance_appears_in_repair_block(
        self,
        db_session,
        monkeypatch,
        user,
        project,
        session_row,
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="preserve existing tests during repair",
            purpose_targets=["repair"],
        )
        block = render_active_guidance_for_repair(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
        )
        assert "preserve existing tests during repair" in block

    def test_planning_only_guidance_excluded_from_repair_block(
        self,
        db_session,
        monkeypatch,
        user,
        project,
        session_row,
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="planning only instruction",
            purpose_targets=["planning"],
        )
        block = render_active_guidance_for_repair(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
        )
        assert "planning only instruction" not in block

    def test_all_purpose_guidance_appears_in_repair_block(
        self,
        db_session,
        monkeypatch,
        user,
        project,
        session_row,
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="global rule applies everywhere",
            purpose_targets=["all"],
        )
        block = render_active_guidance_for_repair(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
        )
        assert "global rule applies everywhere" in block


# ---------------------------------------------------------------------------
# Working Memory injection — planning-visible (excludes execution-only)
# HG Hardening Phase 3: WM now collects purpose=all and filters execution-only entries.
# Planning and repair guidance IS included; execution-only guidance is NOT.
# ---------------------------------------------------------------------------


class TestWMInjectionPurposeExecution:
    def test_execution_only_guidance_not_written_to_wm(
        self,
        db_session,
        tmp_path,
        monkeypatch,
        user,
        project,
        session_row,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="use stdout",
            purpose_targets=["execution"],
        )
        state = MagicMock()
        state.project_dir = str(tmp_path)
        state.session_id = session_row.id
        state.plan = []
        state.changed_files = []
        state.validation_history = []
        state.project_context = ""
        task = MagicMock()
        task.id = 1
        task.title = "execution wm test"

        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=MagicMock(),
            db=db_session,
        )

        wm = json.loads((tmp_path / ".agent" / _FILENAME).read_text(encoding="utf-8"))
        assert "use stdout" not in _messages(
            wm["human_guidance"]
        ), "Execution-only guidance must not appear in planning-context WM (Phase 3 fix)"

    def test_planning_only_guidance_included_in_wm(
        self,
        db_session,
        tmp_path,
        monkeypatch,
        user,
        project,
        session_row,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="planning only excluded from wm",
            purpose_targets=["planning"],
        )
        state = MagicMock()
        state.project_dir = str(tmp_path)
        state.session_id = session_row.id
        state.plan = []
        state.changed_files = []
        state.validation_history = []
        state.project_context = ""
        task = MagicMock()
        task.id = 1
        task.title = "wm exclusion test"

        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=MagicMock(),
            db=db_session,
        )

        wm = json.loads((tmp_path / ".agent" / _FILENAME).read_text(encoding="utf-8"))
        assert "planning only excluded from wm" in _messages(
            wm["human_guidance"]
        ), "Planning-only guidance must appear in planning-context WM (Phase 3 fix)"

    def test_all_purpose_guidance_included_in_wm(
        self,
        db_session,
        tmp_path,
        monkeypatch,
        user,
        project,
        session_row,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="global rule in wm",
            purpose_targets=["all"],
        )
        state = MagicMock()
        state.project_dir = str(tmp_path)
        state.session_id = session_row.id
        state.plan = []
        state.changed_files = []
        state.validation_history = []
        state.project_context = ""
        task = MagicMock()
        task.id = 1
        task.title = "all-purpose wm test"

        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=MagicMock(),
            db=db_session,
        )

        wm = json.loads((tmp_path / ".agent" / _FILENAME).read_text(encoding="utf-8"))
        assert "global rule in wm" in _messages(wm["human_guidance"])


# ---------------------------------------------------------------------------
# Conflict detection uses purpose="planning"
# ---------------------------------------------------------------------------


class TestConflictDetectionPurposePlanning:
    def test_planning_only_guidance_detected_in_conflict_scan(
        self, db_session, user, project, session_row
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="All output must go to stdout. Never use logging.",
            purpose_targets=["planning"],
        )
        warnings = detect_guidance_task_conflicts(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
            task_title="Add logging.getLogger calls.",
            task_description="",
        )
        assert len(warnings) >= 1

    def test_repair_only_guidance_excluded_from_conflict_scan(
        self, db_session, user, project, session_row
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="All output must go to stdout. Never use logging.",
            purpose_targets=["repair"],
        )
        warnings = detect_guidance_task_conflicts(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
            task_title="Add logging.getLogger calls.",
            task_description="",
        )
        assert warnings == []

    def test_all_purpose_guidance_detected_in_conflict_scan(
        self, db_session, user, project, session_row
    ):
        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="All output must go to stdout. Never use logging.",
            purpose_targets=["all"],
        )
        warnings = detect_guidance_task_conflicts(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
            task_title="Add logging.getLogger calls.",
            task_description="",
        )
        assert len(warnings) >= 1


# ---------------------------------------------------------------------------
# Rendered preview endpoint matches runtime planning selection
# ---------------------------------------------------------------------------


class TestRenderedPreviewMatchesPlanningRuntime:
    def test_preview_planning_excludes_repair_only(
        self, client, db_session, user, project
    ):
        client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "planning constraint p3d",
                "scope": "project",
                "purpose_targets": ["planning"],
            },
        )
        repair_resp = client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "repair constraint p3d",
                "scope": "project",
                "purpose_targets": ["repair"],
            },
        )
        repair_id = repair_resp.json()["id"]

        resp = client.get(
            f"/api/v1/projects/{project.id}/guidance/rendered?purpose=planning"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["purpose"] == "planning"
        assert "planning constraint p3d" in (data.get("block") or "")
        assert "repair constraint p3d" not in (data.get("block") or "")
        assert repair_id in data["filtered_purpose_ids"]

    def test_preview_repair_excludes_planning_only(
        self, client, db_session, user, project
    ):
        planning_resp = client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "planning only p3d preview",
                "scope": "project",
                "purpose_targets": ["planning"],
            },
        )
        planning_id = planning_resp.json()["id"]
        client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "repair only p3d preview",
                "scope": "project",
                "purpose_targets": ["repair"],
            },
        )

        resp = client.get(
            f"/api/v1/projects/{project.id}/guidance/rendered?purpose=repair"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "repair only p3d preview" in (data.get("block") or "")
        assert "planning only p3d preview" not in (data.get("block") or "")
        assert planning_id in data["filtered_purpose_ids"]


# ---------------------------------------------------------------------------
# Backward compatibility: ["all"] rows unaffected
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_all_purpose_rows_appear_in_every_context(
        self,
        db_session,
        monkeypatch,
        user,
        project,
        session_row,
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="legacy compat rule",
            purpose_targets=["all"],
        )
        for purpose in ("planning", "execution", "repair", "validation"):
            entries = collect_active_guidance(
                db_session,
                user_id=user.id,
                project_id=project.id,
                session_id=None,
                task_id=None,
                purpose=purpose,
            )
            assert "legacy compat rule" in _messages(
                entries
            ), f"all-purpose rule missing for purpose={purpose}"

    def test_null_purpose_targets_behaves_as_all(
        self,
        db_session,
        user,
        project,
    ):
        from app.models import GuidanceScope, GuidanceStatus, HumanGuidance

        entry = HumanGuidance(
            user_id=user.id,
            project_id=project.id,
            scope=GuidanceScope.PROJECT,
            message="null purpose legacy row",
            status=GuidanceStatus.ACTIVE,
            priority=0,
            revision=1,
            purpose_targets=None,
        )
        db_session.add(entry)
        db_session.commit()

        for purpose in ("planning", "execution", "repair", "validation"):
            entries = collect_active_guidance(
                db_session,
                user_id=user.id,
                project_id=project.id,
                session_id=None,
                task_id=None,
                purpose=purpose,
            )
            assert "null purpose legacy row" in _messages(
                entries
            ), f"null purpose_targets row missing for purpose={purpose}"

    def test_no_purpose_arg_returns_all_entries(self, db_session, user, project):
        _seed_purpose_guidance(db_session, user, project)
        all_entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        msgs = _messages(all_entries)
        assert "avoid mutable defaults" in msgs
        assert "preserve existing tests during repair" in msgs
        assert "use stdout" in msgs
        assert "require pytest evidence" in msgs
        assert "global rule applies everywhere" in msgs
