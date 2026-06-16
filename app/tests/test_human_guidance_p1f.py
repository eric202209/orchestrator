"""HG-P1f — Runtime Wiring tests.

Verifies that per-project/session activation controls gate:
  - table-backed WM guidance path (working_memory.py)
  - WM injection into planner context (worker.py call site)
  - conflict detection (human_guidance_conflict_service.py)

Backward compat rule: no activation row → global flag controls (same as pre-P1f).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    HumanGuidanceActivation,
    Project,
    Session as SessionModel,
    User,
)
from app.services.human_guidance_activation_service import (
    check_activation_flag,
    disable_activation,
    get_effective_activation,
    readiness_status,
    set_project_activation,
    set_session_activation,
)
from app.services.human_guidance_conflict_service import (
    run_conflict_detection_if_enabled,
)
from app.services.human_guidance_service import create_guidance


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(email="p1f@example.com", hashed_password="hashed", is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def project(db_session: Session, user: User) -> Project:
    p = Project(name="p1f-project", workspace_path=None, user_id=user.id)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def running_session(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="p1f-session",
        status="running",
        is_active=True,
        instance_id="p1f-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def _all_flags_on() -> dict:
    return {
        "table_enabled": True,
        "persistence_enabled": True,
        "render_enabled": True,
        "injection_enabled": True,
        "conflict_detection_enabled": True,
    }


def _make_guidance(db, project):
    return create_guidance(
        db,
        user_id=project.user_id,
        project_id=project.id,
        message="use logging not print",
        scope="project",
        created_by="p1f-test",
    )


# ── 1–7: check_activation_flag ───────────────────────────────────────────────


class TestCheckActivationFlag:
    def test_no_row_returns_true(self, db_session: Session, project: Project):
        """No activation row → True (backward compat: global flag controls)."""
        result = check_activation_flag(
            db_session, project_id=project.id, flag="table_enabled"
        )
        assert result is True

    def test_enabled_row_flag_on_returns_true(
        self, db_session: Session, project: Project
    ):
        """Activation row ON + global flag ON → True."""
        set_project_activation(
            db_session, project.id, {"table_enabled": True}, enabled_by="p1f-test"
        )
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True):
            result = check_activation_flag(
                db_session, project_id=project.id, flag="table_enabled"
            )
        assert result is True

    def test_enabled_row_flag_off_returns_false(
        self, db_session: Session, project: Project
    ):
        """Activation row with table_enabled=False → False."""
        set_project_activation(
            db_session, project.id, {"table_enabled": False}, enabled_by="p1f-test"
        )
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True):
            result = check_activation_flag(
                db_session, project_id=project.id, flag="table_enabled"
            )
        assert result is False

    def test_disabled_row_returns_false(self, db_session: Session, project: Project):
        """Disabled activation row → False."""
        disable_activation(db_session, "project", project.id, disabled_by="p1f-test")
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True):
            result = check_activation_flag(
                db_session, project_id=project.id, flag="table_enabled"
            )
        assert result is False

    def test_session_disabled_overrides_project_enabled(
        self, db_session: Session, project: Project, running_session: SessionModel
    ):
        """Session disabled row overrides project enabled row → False."""
        set_project_activation(
            db_session, project.id, {"table_enabled": True}, enabled_by="p1f-test"
        )
        disable_activation(
            db_session, "session", running_session.id, disabled_by="p1f-test"
        )
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True):
            result = check_activation_flag(
                db_session,
                project_id=project.id,
                session_id=running_session.id,
                flag="table_enabled",
            )
        assert result is False

    def test_global_flag_bounds_effective(self, db_session: Session, project: Project):
        """Global flag=False overrides activation ON → False."""
        set_project_activation(
            db_session, project.id, {"table_enabled": True}, enabled_by="p1f-test"
        )
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", False):
            result = check_activation_flag(
                db_session, project_id=project.id, flag="table_enabled"
            )
        assert result is False

    def test_db_error_returns_true(self, db_session: Session, project: Project):
        """Non-fatal: DB error → True (allow path)."""
        bad_db = MagicMock()
        bad_db.query.side_effect = Exception("db down")
        result = check_activation_flag(
            bad_db, project_id=project.id, flag="table_enabled"
        )
        assert result is True


# ── 8–11: WM table path gating ───────────────────────────────────────────────


class TestWmTablePathGating:
    def _write_wm(self, db, session_id, project_dir):
        from app.services.orchestration.working_memory import write_working_memory

        mock_state = MagicMock()
        mock_state.session_id = session_id
        mock_state.project_dir = project_dir
        mock_logger = MagicMock()

        with patch.object(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True):
            with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True):
                write_working_memory(
                    orchestration_state=mock_state,
                    task=MagicMock(id=1, title="t", description="d"),
                    summary=None,
                    logger=mock_logger,
                    db=db,
                )
        return mock_logger

    @staticmethod
    def _table_path_from_logs(mock_logger) -> bool:
        """Extract the table_path value from the [HG_ACTIVATION] log call args."""
        for call in mock_logger.info.call_args_list:
            args = call[0] if call[0] else ()
            if args and "[HG_ACTIVATION]" in str(args[0]):
                # format: ("[HG_ACTIVATION] ...", project_id, session_id, table_path)
                return bool(args[-1])
        raise AssertionError("[HG_ACTIVATION] log not found in mock_logger.info calls")

    def test_no_row_uses_table_path(
        self,
        db_session: Session,
        project: Project,
        running_session: SessionModel,
        tmp_path,
    ):
        """No activation row: table path fires (backward compat)."""
        mock_logger = self._write_wm(db_session, running_session.id, str(tmp_path))
        wm_file = tmp_path / ".agent" / "working_memory.json"
        assert wm_file.exists(), "working_memory.json must be written"
        assert self._table_path_from_logs(mock_logger) is True

    def test_activation_on_uses_table_path(
        self,
        db_session: Session,
        project: Project,
        running_session: SessionModel,
        tmp_path,
    ):
        """Activation row with table_enabled=True: table path fires."""
        set_project_activation(
            db_session, project.id, {"table_enabled": True}, enabled_by="p1f-test"
        )
        mock_logger = self._write_wm(db_session, running_session.id, str(tmp_path))
        assert self._table_path_from_logs(mock_logger) is True

    def test_activation_off_uses_legacy_path(
        self,
        db_session: Session,
        project: Project,
        running_session: SessionModel,
        tmp_path,
    ):
        """Activation row with table_enabled=False: legacy path fires."""
        set_project_activation(
            db_session, project.id, {"table_enabled": False}, enabled_by="p1f-test"
        )
        mock_logger = self._write_wm(db_session, running_session.id, str(tmp_path))
        assert self._table_path_from_logs(mock_logger) is False

    def test_disabled_activation_uses_legacy_path(
        self,
        db_session: Session,
        project: Project,
        running_session: SessionModel,
        tmp_path,
    ):
        """Disabled activation row: legacy path fires."""
        disable_activation(db_session, "project", project.id, disabled_by="p1f-test")
        mock_logger = self._write_wm(db_session, running_session.id, str(tmp_path))
        assert self._table_path_from_logs(mock_logger) is False


# ── 12–14: conflict detection gating ─────────────────────────────────────────


class TestConflictDetectionGating:
    def test_no_row_conflict_detection_fires(
        self,
        db_session: Session,
        project: Project,
        running_session: SessionModel,
    ):
        """No activation row: conflict detection fires when both global flags on."""
        _make_guidance(db_session, project)
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True):
            with patch.object(
                settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True
            ):
                results = run_conflict_detection_if_enabled(
                    db_session,
                    project_id=project.id,
                    session_id=running_session.id,
                    task_id=1,
                    user_id=project.user_id,
                    task_title="task",
                    task_description="print('hello')",
                )
        assert isinstance(results, list)

    def test_activation_on_conflict_detection_fires(
        self,
        db_session: Session,
        project: Project,
        running_session: SessionModel,
    ):
        """Activation row with conflict_detection_enabled=True: detection fires."""
        _make_guidance(db_session, project)
        set_project_activation(
            db_session,
            project.id,
            {"conflict_detection_enabled": True},
            enabled_by="p1f-test",
        )
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True):
            with patch.object(
                settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True
            ):
                results = run_conflict_detection_if_enabled(
                    db_session,
                    project_id=project.id,
                    session_id=running_session.id,
                    task_id=2,
                    user_id=project.user_id,
                    task_title="task2",
                    task_description="print('hello')",
                )
        assert isinstance(results, list)

    def test_activation_off_conflict_detection_skipped(
        self,
        db_session: Session,
        project: Project,
        running_session: SessionModel,
    ):
        """Activation row with conflict_detection_enabled=False: returns [] early."""
        _make_guidance(db_session, project)
        set_project_activation(
            db_session,
            project.id,
            {"conflict_detection_enabled": False},
            enabled_by="p1f-test",
        )
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True):
            with patch.object(
                settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True
            ):
                results = run_conflict_detection_if_enabled(
                    db_session,
                    project_id=project.id,
                    session_id=running_session.id,
                    task_id=3,
                    user_id=project.user_id,
                    task_title="task3",
                    task_description="print('hello')",
                )
        assert results == []


# ── 15–17: readiness runtime_effective ───────────────────────────────────────


class TestReadinessRuntimeEffective:
    def test_no_row_runtime_effective_mode_global_flag_only(
        self, db_session: Session, project: Project
    ):
        """No activation row: runtime_effective.mode = 'global_flag_only'."""
        _make_guidance(db_session, project)
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True):
            status = readiness_status(db_session, project_id=project.id)
        assert "runtime_effective" in status
        assert status["runtime_effective"]["mode"] == "global_flag_only"

    def test_row_present_runtime_effective_mode_activation_controlled(
        self, db_session: Session, project: Project
    ):
        """Activation row exists: runtime_effective.mode = 'activation_controlled'."""
        _make_guidance(db_session, project)
        set_project_activation(
            db_session, project.id, _all_flags_on(), enabled_by="p1f-test"
        )
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True):
            with patch.object(
                settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True
            ):
                status = readiness_status(db_session, project_id=project.id)
        assert status["runtime_effective"]["mode"] == "activation_controlled"

    def test_runtime_effective_table_flag_matches_runtime_decision(
        self, db_session: Session, project: Project
    ):
        """runtime_effective.table_enabled must match check_activation_flag result."""
        set_project_activation(
            db_session, project.id, {"table_enabled": True}, enabled_by="p1f-test"
        )
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True):
            flag_result = check_activation_flag(
                db_session, project_id=project.id, flag="table_enabled"
            )
            status = readiness_status(db_session, project_id=project.id)
        assert status["runtime_effective"]["table_enabled"] == flag_result


# ── 18–19: backward compat ───────────────────────────────────────────────────


class TestBackwardCompat:
    def test_no_activation_rows_identical_to_pre_p1f(
        self, db_session: Session, project: Project, running_session: SessionModel
    ):
        """No activation rows at all → check_activation_flag returns True for all flags."""
        for flag in (
            "table_enabled",
            "persistence_enabled",
            "render_enabled",
            "injection_enabled",
            "conflict_detection_enabled",
        ):
            result = check_activation_flag(
                db_session,
                project_id=project.id,
                session_id=running_session.id,
                flag=flag,
            )
            assert result is True, f"Expected True for {flag} with no activation row"

    def test_global_flag_off_always_wins(self, db_session: Session, project: Project):
        """Global flag=False gates runtime even if activation row says True."""
        set_project_activation(
            db_session,
            project.id,
            {"table_enabled": True, "injection_enabled": True},
            enabled_by="p1f-test",
        )
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", False):
            assert (
                check_activation_flag(
                    db_session, project_id=project.id, flag="table_enabled"
                )
                is False
            )
        with patch.object(settings, "WORKING_MEMORY_INJECTION_ENABLED", False):
            assert (
                check_activation_flag(
                    db_session, project_id=project.id, flag="injection_enabled"
                )
                is False
            )


# ── 20–22: regression ────────────────────────────────────────────────────────


class TestRegressionP1f:
    def test_hg_flags_default_off(self):
        assert settings.HUMAN_GUIDANCE_TABLE_ENABLED is False
        assert settings.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED is False
        assert settings.WORKING_MEMORY_PERSISTENCE_ENABLED is False
        assert settings.WORKING_MEMORY_RENDER_ENABLED is False
        assert settings.WORKING_MEMORY_INJECTION_ENABLED is False

    def test_p1e_effective_activation_unchanged(
        self, db_session: Session, project: Project
    ):
        """get_effective_activation behavior not changed by P1f."""
        act = get_effective_activation(db_session, project_id=project.id)
        assert act["requested"]["status"] == "disabled"
        assert all(
            act["effective"][k] is False
            for k in (
                "table_enabled",
                "persistence_enabled",
                "render_enabled",
                "injection_enabled",
                "conflict_detection_enabled",
            )
        )

    def test_conflict_global_flags_off_returns_empty(
        self, db_session: Session, project: Project, running_session: SessionModel
    ):
        """Global flags off → conflict detection returns [] immediately."""
        _make_guidance(db_session, project)
        results = run_conflict_detection_if_enabled(
            db_session,
            project_id=project.id,
            session_id=running_session.id,
            task_id=99,
            user_id=project.user_id,
            task_title="t",
            task_description="print(x)",
        )
        assert results == []


# ── 23–25: smoke ─────────────────────────────────────────────────────────────


class TestSmokeP1f:
    def test_smoke_a_no_row_global_flag_controls(
        self, db_session: Session, project: Project, running_session: SessionModel
    ):
        """Smoke A: global flags ON, no activation row → runtime uses global flags."""
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True):
            assert (
                check_activation_flag(
                    db_session,
                    project_id=project.id,
                    session_id=running_session.id,
                    flag="table_enabled",
                )
                is True
            )
            status = readiness_status(db_session, project_id=project.id)
            assert "activation_disabled" in status["blocking_reasons"]
            assert status["runtime_effective"]["mode"] == "global_flag_only"

    def test_smoke_b_activation_on_table_path(
        self, db_session: Session, project: Project, running_session: SessionModel
    ):
        """Smoke B: global flags ON, activation ON → table path + readiness=True."""
        _make_guidance(db_session, project)
        set_project_activation(
            db_session, project.id, _all_flags_on(), enabled_by="p1f-test"
        )
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True):
            with patch.object(
                settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True
            ):
                assert (
                    check_activation_flag(
                        db_session, project_id=project.id, flag="table_enabled"
                    )
                    is True
                )
                status = readiness_status(db_session, project_id=project.id)
                assert status["runtime_effective"]["mode"] == "activation_controlled"
                assert status["runtime_effective"]["table_enabled"] is True
                assert status["ready"] is True

    def test_smoke_c_session_disabled_legacy_path(
        self,
        db_session: Session,
        project: Project,
        running_session: SessionModel,
    ):
        """Smoke C: project ON + session disabled → legacy path, readiness blocked."""
        set_project_activation(
            db_session, project.id, _all_flags_on(), enabled_by="p1f-test"
        )
        disable_activation(
            db_session, "session", running_session.id, disabled_by="p1f-test"
        )
        with patch.object(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True):
            assert (
                check_activation_flag(
                    db_session,
                    project_id=project.id,
                    session_id=running_session.id,
                    flag="table_enabled",
                )
                is False
            )
            status = readiness_status(
                db_session,
                project_id=project.id,
                session_id=running_session.id,
            )
            assert "activation_disabled" in status["blocking_reasons"]
            assert status["runtime_effective"]["mode"] == "activation_controlled"
            assert status["runtime_effective"]["table_enabled"] is False
