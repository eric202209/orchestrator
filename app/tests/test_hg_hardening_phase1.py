"""HG Hardening Phase 1 — security and correctness regression tests.

Covers:
  1. Mobile guidance IDOR fix
  2. Repair guidance wrong flag gate
  3. Repair slot separation (hg_repair_prompt_used)
  4. WM file lock (non-clobber under concurrent writes)
  5. N+1 usage count (single query, unchanged values)
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from app.config import settings
from app.models import (
    GuidanceStatus,
    HumanGuidance,
    HumanGuidanceUsage,
    Project,
    User,
)

MOBILE_KEY = "hardening-phase1-mobile-key"
HEADERS = {"X-OpenClaw-API-Key": MOBILE_KEY}


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_user(db, email="hardening@example.com") -> User:
    user = User(email=email, hashed_password="x", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_project(db, user_id: int, deleted: bool = False) -> Project:
    from datetime import datetime, timezone

    project = Project(
        name="Hardening Test Project",
        workspace_path="/tmp/hardening",
        user_id=user_id,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_guidance(db, project_id, user_id, message="Never use mutable defaults."):
    from app.services.human_guidance_service import create_guidance

    entry, _ = create_guidance(
        db,
        user_id=user_id,
        project_id=project_id,
        scope="project",
        message=message,
        priority=50,
    )
    return entry


# ── 1. IDOR fix: mobile patch/archive ─────────────────────────────────────────


class TestMobileGuidanceIDOR:
    def test_mobile_patch_own_project_guidance_succeeds(
        self, api_client, db_session, monkeypatch
    ):
        monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)
        user = _make_user(db_session)
        project = _make_project(db_session, user.id)
        entry = _make_guidance(db_session, project.id, user.id)

        resp = api_client.patch(
            f"/api/v1/mobile/guidance/{entry.id}",
            json={"priority": 75},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["priority"] == 75

    def test_mobile_archive_own_project_guidance_succeeds(
        self, api_client, db_session, monkeypatch
    ):
        monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)
        user = _make_user(db_session, "hardening2@example.com")
        project = _make_project(db_session, user.id)
        entry = _make_guidance(db_session, project.id, user.id)

        resp = api_client.delete(
            f"/api/v1/mobile/guidance/{entry.id}",
            headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"

    def test_mobile_patch_missing_guidance_returns_404(
        self, api_client, db_session, monkeypatch
    ):
        monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)
        resp = api_client.patch(
            "/api/v1/mobile/guidance/99999",
            json={"priority": 10},
            headers=HEADERS,
        )
        assert resp.status_code == 404

    def test_mobile_archive_missing_guidance_returns_404(
        self, api_client, db_session, monkeypatch
    ):
        monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)
        resp = api_client.delete(
            "/api/v1/mobile/guidance/99999",
            headers=HEADERS,
        )
        assert resp.status_code == 404

    def test_mobile_patch_guidance_with_deleted_project_returns_404(
        self, api_client, db_session, monkeypatch
    ):
        monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)
        user = _make_user(db_session, "hardening3@example.com")
        # Create the project live first so guidance FK is satisfied, then soft-delete.
        project = _make_project(db_session, user.id, deleted=False)
        entry = _make_guidance(db_session, project.id, user.id)

        # Soft-delete the project
        from datetime import datetime, timezone

        project.deleted_at = datetime.now(timezone.utc)
        db_session.commit()

        resp = api_client.patch(
            f"/api/v1/mobile/guidance/{entry.id}",
            json={"priority": 10},
            headers=HEADERS,
        )
        assert resp.status_code == 404

    def test_mobile_archive_guidance_with_deleted_project_returns_404(
        self, api_client, db_session, monkeypatch
    ):
        monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)
        user = _make_user(db_session, "hardening4@example.com")
        project = _make_project(db_session, user.id, deleted=False)
        entry = _make_guidance(db_session, project.id, user.id)

        from datetime import datetime, timezone

        project.deleted_at = datetime.now(timezone.utc)
        db_session.commit()

        resp = api_client.delete(
            f"/api/v1/mobile/guidance/{entry.id}",
            headers=HEADERS,
        )
        assert resp.status_code == 404

    def test_web_guidance_endpoints_unchanged(
        self, api_client, db_session, monkeypatch
    ):
        """Web JWT endpoints still reject unauthenticated requests."""
        monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)
        user = _make_user(db_session, "hardening5@example.com")
        project = _make_project(db_session, user.id)

        resp = api_client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "Test rule.", "scope": "project"},
        )
        assert resp.status_code == 401

    def test_mobile_key_not_accepted_on_web_endpoints(
        self, api_client, db_session, monkeypatch
    ):
        monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)
        user = _make_user(db_session, "hardening6@example.com")
        project = _make_project(db_session, user.id)

        resp = api_client.get(
            f"/api/v1/projects/{project.id}/guidance",
            headers=HEADERS,
        )
        assert resp.status_code == 401


# ── 2. Repair guidance wrong flag gate ────────────────────────────────────────


class TestRepairGuidanceFlagGate:
    def _render(self, entries, *, table_enabled=True, conflict_enabled=True):
        from app.services.human_guidance_plan_validator import (
            render_active_guidance_for_repair,
        )

        settings_mock = MagicMock()
        settings_mock.HUMAN_GUIDANCE_TABLE_ENABLED = table_enabled
        settings_mock.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED = conflict_enabled

        with patch("app.config.settings", settings_mock):
            with patch(
                "app.services.human_guidance_activation_service.check_activation_flag",
                return_value=True,
            ):
                with patch(
                    "app.services.human_guidance_service.collect_active_guidance",
                    return_value=entries,
                ):
                    return render_active_guidance_for_repair(
                        MagicMock(),
                        project_id=1,
                        session_id=10,
                        task_id=100,
                        user_id=999,
                    )

    def test_conflict_flag_off_table_flag_on_renders(self):
        result = self._render(
            [{"message": "Never use mutable defaults."}],
            table_enabled=True,
            conflict_enabled=False,
        )
        assert "Never use mutable defaults." in result

    def test_table_flag_off_returns_empty(self):
        result = self._render(
            [{"message": "Never use mutable defaults."}],
            table_enabled=False,
            conflict_enabled=True,
        )
        assert result == ""

    def test_conflict_detection_behavior_unchanged(self):
        """Conflict detection service itself is unaffected by this change."""
        from app.services.human_guidance_conflict_service import _PATTERN_PAIRS

        assert any(name == "mutable_default" for name, _, _ in _PATTERN_PAIRS)


# ── 3. Repair slot separation ─────────────────────────────────────────────────


class TestRepairSlotSeparation:
    def _make_retry_state(self):
        from app.services.orchestration.phases.planning_support import (
            _PlanningRetryState,
        )

        return _PlanningRetryState(persisted_failures=0)

    def test_hg_repair_prompt_used_initialized_false(self):
        rs = self._make_retry_state()
        assert rs.hg_repair_prompt_used is False

    def test_structural_repair_used_does_not_block_hg_repair(self):
        """After structural repair, HG repair should still fire."""
        from app.services.orchestration.phases.planning_guidance_enforcement import (
            run_guidance_plan_enforcement,
        )

        rs = self._make_retry_state()
        rs.repair_prompt_used = True  # structural repair already used
        # hg_repair_prompt_used is still False

        repair_called = []

        def fake_repair(**kwargs):
            repair_called.append(True)
            return {"plan": []}

        ctx = MagicMock()
        ctx.orchestration_state.plan = [
            {
                "ops": [
                    {
                        "op": "write_file",
                        "path": "x.py",
                        "content": "def f(x=[]):pass",
                    }
                ]
            }
        ]
        ctx.project.id = 1
        ctx.project.user_id = 99

        settings_mock = MagicMock()
        settings_mock.HUMAN_GUIDANCE_TABLE_ENABLED = True
        settings_mock.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED = True

        with patch("app.config.settings", settings_mock):
            with patch(
                "app.services.human_guidance_activation_service.check_activation_flag",
                return_value=True,
            ):
                with patch(
                    "app.services.human_guidance_service.collect_active_guidance",
                    return_value=[
                        {
                            "message": "Never use mutable default arguments. Use None.",
                            "id": 1,
                        }
                    ],
                ):
                    result = run_guidance_plan_enforcement(
                        ctx,
                        retry_state=rs,
                        output_text="[]",
                        planning_timeout_seconds=30,
                        prompt_profile="default",
                        repair_fn=fake_repair,
                        emit_diagnostics_fn=MagicMock(),
                    )

        assert repair_called, "HG repair should fire even after structural repair used"
        assert rs.hg_repair_prompt_used is True

    def test_hg_repair_used_once_second_violation_warns_only(self):
        """Second HG violation after HG repair already used should warn, not loop."""
        from app.services.orchestration.phases.planning_guidance_enforcement import (
            run_guidance_plan_enforcement,
        )

        rs = self._make_retry_state()
        rs.hg_repair_prompt_used = True  # HG repair already spent

        repair_called = []

        def fake_repair(**kwargs):
            repair_called.append(True)
            return {}

        ctx = MagicMock()
        ctx.orchestration_state.plan = [
            {
                "ops": [
                    {
                        "op": "write_file",
                        "path": "x.py",
                        "content": "def f(x=[]):pass",
                    }
                ]
            }
        ]
        ctx.project.id = 1
        ctx.project.user_id = 99

        settings_mock = MagicMock()
        settings_mock.HUMAN_GUIDANCE_TABLE_ENABLED = True
        settings_mock.HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED = True

        with patch("app.config.settings", settings_mock):
            with patch(
                "app.services.human_guidance_activation_service.check_activation_flag",
                return_value=True,
            ):
                with patch(
                    "app.services.human_guidance_service.collect_active_guidance",
                    return_value=[
                        {
                            "message": "Never use mutable default arguments. Use None.",
                            "id": 1,
                        }
                    ],
                ):
                    result = run_guidance_plan_enforcement(
                        ctx,
                        retry_state=rs,
                        output_text="[]",
                        planning_timeout_seconds=30,
                        prompt_profile="default",
                        repair_fn=fake_repair,
                        emit_diagnostics_fn=MagicMock(),
                    )

        assert result is None, "Should return None (warn-only), not a repair result"
        assert (
            not repair_called
        ), "repair_fn should not be called on second HG violation"

    def test_structural_repair_flag_unchanged(self):
        """repair_prompt_used is still independent of hg_repair_prompt_used."""
        rs = self._make_retry_state()
        rs.repair_prompt_used = True
        assert rs.hg_repair_prompt_used is False

        rs.hg_repair_prompt_used = True
        assert rs.repair_prompt_used is True


# ── 4. WM file lock — no clobber under concurrent writes ─────────────────────


class TestWMFileLock:
    def test_concurrent_writes_do_not_clobber_guidance(self, tmp_path, monkeypatch):
        """Two concurrent write_working_memory calls must not lose guidance entries."""
        from unittest.mock import MagicMock

        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", False)

        project_dir = str(tmp_path)
        results: list = []
        errors: list = []

        def _make_state(task_id, guidance_msg):
            state = MagicMock()
            state.project_dir = project_dir
            state.session_id = 1
            state.plan = []
            state.changed_files = []
            state.validation_history = []
            return state

        def _make_task(tid, title):
            t = MagicMock()
            t.id = tid
            t.title = title
            t.plan_position = tid
            return t

        def _write(tid, msg):
            from app.services.orchestration.working_memory import write_working_memory

            try:
                write_working_memory(
                    orchestration_state=_make_state(tid, msg),
                    task=_make_task(tid, f"task-{tid}"),
                    summary=f"summary-{tid}",
                    logger=MagicMock(),
                    db=None,
                )
                results.append(tid)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_write, args=(1, "guidance-1"))
        t2 = threading.Thread(target=_write, args=(2, "guidance-2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Concurrent writes raised: {errors}"

        wm_path = tmp_path / ".agent" / "working_memory.json"
        assert wm_path.exists()
        wm = json.loads(wm_path.read_text())

        # Both tasks must appear in files_by_task
        assert "1" in wm["files_by_task"]
        assert "2" in wm["files_by_task"]

        # Both implementation_strategy entries must survive
        task_ids_in_strategy = [s["task_id"] for s in wm["implementation_strategy"]]
        assert 1 in task_ids_in_strategy
        assert 2 in task_ids_in_strategy

    def test_lock_failure_is_non_fatal(self, tmp_path, monkeypatch):
        """If fcntl is unavailable the write still succeeds."""
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", False)

        import app.services.orchestration.working_memory as wm_mod

        original_fcntl = wm_mod._fcntl
        wm_mod._fcntl = None  # simulate unavailable lock
        try:
            state = MagicMock()
            state.project_dir = str(tmp_path)
            state.session_id = 1
            state.plan = []
            state.changed_files = []
            state.validation_history = []
            task = MagicMock()
            task.id = 1
            task.title = "t"
            task.plan_position = 1

            from app.services.orchestration.working_memory import write_working_memory

            write_working_memory(
                orchestration_state=state,
                task=task,
                summary="ok",
                logger=MagicMock(),
                db=None,
            )
        finally:
            wm_mod._fcntl = original_fcntl

        wm_path = tmp_path / ".agent" / "working_memory.json"
        assert wm_path.exists()


# ── 5. N+1 usage count — single query, unchanged values ───────────────────────


class TestUsageCountNoN1:
    def test_usage_count_correct_single_row(self, db_session):
        from app.services.human_guidance_service import collect_active_guidance

        user = _make_user(db_session, "n1test@example.com")
        project = _make_project(db_session, user.id)
        entry = _make_guidance(db_session, project.id, user.id)

        # Record 3 usage rows
        for _ in range(3):
            db_session.add(
                HumanGuidanceUsage(
                    guidance_id=entry.id,
                    project_id=project.id,
                    rendered=True,
                    selected=True,
                    trimmed=False,
                    source="human_guidance_table",
                )
            )
        db_session.commit()

        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        assert len(results) == 1
        assert results[0]["usage_count"] == 3

    def test_usage_count_zero_when_no_usage(self, db_session):
        from app.services.human_guidance_service import collect_active_guidance

        user = _make_user(db_session, "n1zero@example.com")
        project = _make_project(db_session, user.id)
        _make_guidance(db_session, project.id, user.id)

        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        assert len(results) == 1
        assert results[0]["usage_count"] == 0

    def test_usage_count_independent_per_guidance(self, db_session):
        from app.services.human_guidance_service import collect_active_guidance

        user = _make_user(db_session, "n1multi@example.com")
        project = _make_project(db_session, user.id)
        e1 = _make_guidance(db_session, project.id, user.id, "Rule A.")
        e2 = _make_guidance(db_session, project.id, user.id, "Rule B.")

        # e1 used twice, e2 used once
        for _ in range(2):
            db_session.add(
                HumanGuidanceUsage(
                    guidance_id=e1.id,
                    project_id=project.id,
                    rendered=True,
                    selected=True,
                    trimmed=False,
                    source="human_guidance_table",
                )
            )
        db_session.add(
            HumanGuidanceUsage(
                guidance_id=e2.id,
                project_id=project.id,
                rendered=True,
                selected=True,
                trimmed=False,
                source="human_guidance_table",
            )
        )
        db_session.commit()

        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        counts = {r["message"]: r["usage_count"] for r in results}
        assert counts["Rule A."] == 2
        assert counts["Rule B."] == 1

    def test_selection_ordering_unchanged(self, db_session):
        """Higher priority guidance still appears first after N+1 fix."""
        from app.services.human_guidance_service import collect_active_guidance

        user = _make_user(db_session, "n1order@example.com")
        project = _make_project(db_session, user.id)

        from app.services.human_guidance_service import create_guidance

        low, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Low priority rule.",
            priority=10,
        )
        high, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="High priority rule.",
            priority=90,
        )

        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        messages = [r["message"] for r in results]
        assert messages.index("High priority rule.") < messages.index(
            "Low priority rule."
        )
