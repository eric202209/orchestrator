"""HG-P3 routing integration validation.

Validates that scope + backend + model + purpose routing works together
across all four targeting dimensions in a single controlled project.

Project: hg-p3-routing-integration-validation
Guidance matrix: G1-G6 (see docstring below).

G1  global / all         / all   / all      — "Global rule applies everywhere."
G2  project/ direct_ollama/ qwen  / planning — "Planning rule: never use mutable default arguments."
G3  project/ direct_ollama/ qwen  / repair   — "Repair rule: preserve existing tests during repair."
G4  project/ direct_ollama/ qwen  / execution— "Execution rule: all runtime output must go to stdout."
G5  project/ local_openclaw/ claude/ planning — "OpenClaw Claude-only rule."
G6  project/ direct_ollama/ llama / planning  — "Ollama Llama-only rule."

Expected per (backend=direct_ollama, model_family=qwen):
  planning  → G1, G2
  repair    → G1, G3
  execution → G1, G4
  validation→ G1

Expected for (backend=local_openclaw, model_family=claude):
  planning  → G1, G5
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    GuidanceScope,
    GuidanceStatus,
    HumanGuidance,
    HumanGuidanceUsage,
    Project,
    Session as SessionModel,
    User,
)
from app.services.human_guidance_activation_service import readiness_status
from app.services.human_guidance_conflict_service import detect_guidance_task_conflicts
from app.services.human_guidance_plan_validator import (
    check_plan_guidance_violations_if_enabled,
    render_active_guidance_for_repair,
)
from app.services.human_guidance_service import collect_active_guidance, create_guidance
from app.services.orchestration.working_memory import _FILENAME, write_working_memory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(
        email="hg-p3-integration@example.com",
        hashed_password="hashed",
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def project(db_session: Session, user: User) -> Project:
    p = Project(
        name="hg-p3-routing-integration-validation",
        workspace_path=None,
        user_id=user.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def session_row(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="p3-integration-session",
        status="running",
        is_active=True,
        instance_id="p3-integration-instance",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def guidance_matrix(
    db_session: Session, user: User, project: Project
) -> dict[str, int]:
    """Seed G1-G6 and return a {name: id} map."""
    specs = [
        # (name, scope, backend, model, purpose, message)
        (
            "G1",
            GuidanceScope.GLOBAL,
            ["all"],
            ["all"],
            ["all"],
            "Global rule applies everywhere.",
        ),
        (
            "G2",
            GuidanceScope.PROJECT,
            ["direct_ollama"],
            ["qwen"],
            ["planning"],
            "Planning rule: never use mutable default arguments.",
        ),
        (
            "G3",
            GuidanceScope.PROJECT,
            ["direct_ollama"],
            ["qwen"],
            ["repair"],
            "Repair rule: preserve existing tests during repair.",
        ),
        (
            "G4",
            GuidanceScope.PROJECT,
            ["direct_ollama"],
            ["qwen"],
            ["execution"],
            "Execution rule: all runtime output must go to stdout.",
        ),
        (
            "G5",
            GuidanceScope.PROJECT,
            ["local_openclaw"],
            ["claude"],
            ["planning"],
            "OpenClaw Claude-only rule.",
        ),
        (
            "G6",
            GuidanceScope.PROJECT,
            ["direct_ollama"],
            ["llama"],
            ["planning"],
            "Ollama Llama-only rule.",
        ),
    ]
    ids: dict[str, int] = {}
    for name, scope, backend, model, purpose, message in specs:
        pid = project.id if scope != GuidanceScope.GLOBAL else None
        entry, _ = create_guidance(
            db_session,
            user_id=user.id,
            project_id=pid,
            scope=scope.value,
            message=message,
            backend_targets=backend,
            model_targets=model,
            purpose_targets=purpose,
        )
        ids[name] = entry.id
    return ids


def _msgs(entries: list[dict]) -> set[str]:
    return {e["message"] for e in entries}


def _ids(entries: list[dict]) -> set[int]:
    return {e["id"] for e in entries}


# ---------------------------------------------------------------------------
# A. Preview routing — collect_active_guidance
# ---------------------------------------------------------------------------


class TestPreviewRouting:
    """Validates that backend + model + purpose filtering compose correctly."""

    def test_direct_ollama_qwen_planning_returns_G1_G2(
        self, db_session, user, project, guidance_matrix
    ):
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="direct_ollama",
            model_family="qwen",
            purpose="planning",
        )
        msgs = _msgs(entries)
        gids = _ids(entries)

        assert guidance_matrix["G1"] in gids, "G1 (global/all/all/all) must be included"
        assert (
            guidance_matrix["G2"] in gids
        ), "G2 (direct_ollama/qwen/planning) must be included"
        assert (
            guidance_matrix["G3"] not in gids
        ), "G3 (repair) must be excluded by purpose"
        assert (
            guidance_matrix["G4"] not in gids
        ), "G4 (execution) must be excluded by purpose"
        assert (
            guidance_matrix["G5"] not in gids
        ), "G5 (local_openclaw/claude) must be excluded by backend"
        assert guidance_matrix["G6"] not in gids, "G6 (llama) must be excluded by model"
        assert len(entries) == 2

    def test_direct_ollama_qwen_repair_returns_G1_G3(
        self, db_session, user, project, guidance_matrix
    ):
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="direct_ollama",
            model_family="qwen",
            purpose="repair",
        )
        gids = _ids(entries)
        assert guidance_matrix["G1"] in gids
        assert guidance_matrix["G3"] in gids
        assert guidance_matrix["G2"] not in gids
        assert guidance_matrix["G4"] not in gids
        assert guidance_matrix["G5"] not in gids
        assert guidance_matrix["G6"] not in gids
        assert len(entries) == 2

    def test_direct_ollama_qwen_execution_returns_G1_G4(
        self, db_session, user, project, guidance_matrix
    ):
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="direct_ollama",
            model_family="qwen",
            purpose="execution",
        )
        gids = _ids(entries)
        assert guidance_matrix["G1"] in gids
        assert guidance_matrix["G4"] in gids
        assert guidance_matrix["G2"] not in gids
        assert guidance_matrix["G3"] not in gids
        assert guidance_matrix["G5"] not in gids
        assert guidance_matrix["G6"] not in gids
        assert len(entries) == 2

    def test_direct_ollama_qwen_validation_returns_G1_only(
        self, db_session, user, project, guidance_matrix
    ):
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="direct_ollama",
            model_family="qwen",
            purpose="validation",
        )
        gids = _ids(entries)
        assert guidance_matrix["G1"] in gids
        assert guidance_matrix["G2"] not in gids
        assert guidance_matrix["G3"] not in gids
        assert guidance_matrix["G4"] not in gids
        assert len(entries) == 1

    def test_local_openclaw_claude_planning_returns_G1_G5(
        self, db_session, user, project, guidance_matrix
    ):
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="local_openclaw",
            model_family="claude",
            purpose="planning",
        )
        gids = _ids(entries)
        assert guidance_matrix["G1"] in gids
        assert guidance_matrix["G5"] in gids
        assert guidance_matrix["G2"] not in gids  # direct_ollama filtered
        assert guidance_matrix["G6"] not in gids  # llama filtered
        assert len(entries) == 2

    def test_all_dimensions_unfiltered_returns_all_six(
        self, db_session, user, project, guidance_matrix
    ):
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="all",
            model_family="all",
            purpose="all",
        )
        gids = _ids(entries)
        for name in ("G1", "G2", "G3", "G4", "G5", "G6"):
            assert (
                guidance_matrix[name] in gids
            ), f"{name} missing from unfiltered result"
        assert len(entries) == 6


# ---------------------------------------------------------------------------
# B. Readiness — purpose_statistics + backend_statistics
# ---------------------------------------------------------------------------


class TestReadinessStatistics:
    def test_purpose_statistics_match_guidance_matrix(
        self, db_session, user, project, guidance_matrix, monkeypatch
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        result = readiness_status(
            db_session,
            project_id=project.id,
            session_id=None,
            backend="all",
            model_family="all",
        )
        ps = result["purpose_statistics"]
        # G1=all, G2=planning, G3=repair, G4=execution, G5=planning, G6=planning
        assert ps["all"] == 1
        assert ps["planning"] == 3
        assert ps["repair"] == 1
        assert ps["execution"] == 1
        assert ps["validation"] == 0

    def test_backend_statistics_direct_ollama_qwen(
        self, db_session, user, project, guidance_matrix, monkeypatch
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        result = readiness_status(
            db_session,
            project_id=project.id,
            session_id=None,
            backend="direct_ollama",
            model_family="qwen",
        )
        bs = result["backend_statistics"]
        # G1(all/all) + G2(direct_ollama/qwen) + G3(direct_ollama/qwen) + G4(direct_ollama/qwen) = 4
        # G5(local_openclaw/claude) + G6(direct_ollama/llama) excluded = 2
        assert bs["matching_guidance"] == 4
        assert bs["filtered_guidance"] == 2

    def test_backend_statistics_local_openclaw_claude(
        self, db_session, user, project, guidance_matrix, monkeypatch
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        result = readiness_status(
            db_session,
            project_id=project.id,
            session_id=None,
            backend="local_openclaw",
            model_family="claude",
        )
        bs = result["backend_statistics"]
        # G1(all/all) + G5(local_openclaw/claude) = 2
        # G2/G3/G4(direct_ollama/qwen) + G6(direct_ollama/llama) excluded = 4
        assert bs["matching_guidance"] == 2
        assert bs["filtered_guidance"] == 4


# ---------------------------------------------------------------------------
# C. Runtime routing — service layer
# ---------------------------------------------------------------------------


class TestRuntimePlanValidatorPlanning:
    """P2b uses purpose="planning": only G1+G2 visible for direct_ollama/qwen."""

    def _mutable_default_plan(self) -> list:
        return [
            {
                "step_number": 1,
                "description": "add function with mutable default",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "pkg.py",
                        "content": "def append_name(name: str, names: list = []) -> list:\n    names.append(name)\n    return names",
                    }
                ],
                "commands": [],
            }
        ]

    def test_planning_guidance_detects_mutable_default_violation(
        self, db_session, monkeypatch, user, project, session_row, guidance_matrix
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        violations = check_plan_guidance_violations_if_enabled(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
            plan_steps=self._mutable_default_plan(),
            backend="direct_ollama",
            model_family="qwen",
            # purpose="planning" is the default — not passed explicitly
        )
        assert any(
            "mutable_default" in v for v in violations
        ), "G2 planning guidance must flag mutable default"

    def test_repair_only_G3_does_not_leak_into_plan_validator(
        self, db_session, monkeypatch, user, project, session_row, guidance_matrix
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)
        # G3 says "preserve existing tests" — if it leaked into planning, its keywords
        # would not match any plan pattern here, but we verify it's not in the collection.
        # Override purpose to planning explicitly to confirm G3 is absent.
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="direct_ollama",
            model_family="qwen",
            purpose="planning",
        )
        assert guidance_matrix["G3"] not in _ids(entries)

    def test_openclaw_claude_planning_uses_G5_not_G2(
        self, db_session, monkeypatch, user, project, session_row, guidance_matrix
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="local_openclaw",
            model_family="claude",
            purpose="planning",
        )
        gids = _ids(entries)
        assert guidance_matrix["G5"] in gids
        assert guidance_matrix["G2"] not in gids


class TestRuntimeRepairRenderer:
    """Repair renderer uses purpose="repair": only G1+G3 visible for direct_ollama/qwen."""

    def test_repair_block_contains_G3(
        self, db_session, monkeypatch, user, project, session_row, guidance_matrix
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        block = render_active_guidance_for_repair(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
            backend="direct_ollama",
            model_family="qwen",
            # purpose="repair" is the default
        )
        assert "Repair rule: preserve existing tests during repair." in block

    def test_repair_block_excludes_G2_planning(
        self, db_session, monkeypatch, user, project, session_row, guidance_matrix
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        block = render_active_guidance_for_repair(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
            backend="direct_ollama",
            model_family="qwen",
        )
        assert "Planning rule: never use mutable default arguments." not in block

    def test_repair_block_excludes_G4_execution(
        self, db_session, monkeypatch, user, project, session_row, guidance_matrix
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        block = render_active_guidance_for_repair(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
            backend="direct_ollama",
            model_family="qwen",
        )
        assert "Execution rule:" not in block

    def test_repair_block_contains_G1_global(
        self, db_session, monkeypatch, user, project, session_row, guidance_matrix
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        block = render_active_guidance_for_repair(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
            backend="direct_ollama",
            model_family="qwen",
        )
        assert "Global rule applies everywhere." in block


class TestRuntimeWMInjection:
    """WM injection uses purpose="execution": only G1+G4 written."""

    def test_wm_contains_G1_and_G4_only(
        self,
        db_session,
        tmp_path,
        monkeypatch,
        user,
        project,
        session_row,
        guidance_matrix,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        state = MagicMock()
        state.project_dir = str(tmp_path)
        state.session_id = session_row.id
        state.plan = []
        state.changed_files = []
        state.validation_history = []
        state.project_context = ""
        task = MagicMock()
        task.id = 1
        task.title = "p3 integration wm test"

        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=MagicMock(),
            db=db_session,
            guidance_backend="direct_ollama",
            guidance_model_family="qwen",
        )

        wm = json.loads((tmp_path / ".agent" / _FILENAME).read_text(encoding="utf-8"))
        wm_msgs = {e["message"] for e in wm["human_guidance"]}

        assert "Global rule applies everywhere." in wm_msgs, "G1 must be in WM"
        assert (
            "Execution rule: all runtime output must go to stdout." in wm_msgs
        ), "G4 must be in WM"
        assert (
            "Planning rule: never use mutable default arguments." not in wm_msgs
        ), "G2 must NOT be in WM"
        assert (
            "Repair rule: preserve existing tests during repair." not in wm_msgs
        ), "G3 must NOT be in WM"
        assert "OpenClaw Claude-only rule." not in wm_msgs, "G5 must NOT be in WM"
        assert "Ollama Llama-only rule." not in wm_msgs, "G6 must NOT be in WM"

    def test_wm_entry_count_is_two(
        self,
        db_session,
        tmp_path,
        monkeypatch,
        user,
        project,
        session_row,
        guidance_matrix,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        state = MagicMock()
        state.project_dir = str(tmp_path)
        state.session_id = session_row.id
        state.plan = []
        state.changed_files = []
        state.validation_history = []
        state.project_context = ""
        task = MagicMock()
        task.id = 1
        task.title = "p3 integration count test"

        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=MagicMock(),
            db=db_session,
            guidance_backend="direct_ollama",
            guidance_model_family="qwen",
        )

        wm = json.loads((tmp_path / ".agent" / _FILENAME).read_text(encoding="utf-8"))
        assert len(wm["human_guidance"]) == 2


class TestRuntimeConflictDetection:
    """Conflict detection uses purpose="planning": G2 active for direct_ollama/qwen."""

    def test_conflict_detects_G2_pattern(
        self, db_session, user, project, session_row, guidance_matrix
    ):
        # The conflict scanner matches guidance keywords against task text keywords.
        # G2 guidance contains "mutable default" (guidance_kw match).
        # Task description must contain one of "= []", "=[]", etc. (task_kw match).
        warnings = detect_guidance_task_conflicts(
            db_session,
            project_id=project.id,
            session_id=session_row.id,
            task_id=None,
            user_id=user.id,
            task_title="Add append_name function",
            task_description="def append_name(name: str, names: list = []) -> list:",
            backend="direct_ollama",
            model_family="qwen",
            # purpose="planning" is the default
        )
        assert len(warnings) >= 1

    def test_conflict_excludes_G3_repair_pattern(
        self, db_session, user, project, session_row, guidance_matrix
    ):
        # G3 is repair-only; conflict scan must not pull it in for planning
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="direct_ollama",
            model_family="qwen",
            purpose="planning",
        )
        assert guidance_matrix["G3"] not in _ids(entries)

    def test_conflict_excludes_G5_wrong_backend(
        self, db_session, user, project, session_row, guidance_matrix
    ):
        # direct_ollama/qwen should not see G5 (local_openclaw/claude)
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="direct_ollama",
            model_family="qwen",
            purpose="planning",
        )
        assert guidance_matrix["G5"] not in _ids(entries)

    def test_conflict_excludes_G6_wrong_model(
        self, db_session, user, project, session_row, guidance_matrix
    ):
        # direct_ollama/qwen should not see G6 (direct_ollama/llama)
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="direct_ollama",
            model_family="qwen",
            purpose="planning",
        )
        assert guidance_matrix["G6"] not in _ids(entries)


# ---------------------------------------------------------------------------
# D. Telemetry — HumanGuidanceUsage rows
# ---------------------------------------------------------------------------


class TestTelemetry:
    def test_usage_rows_created_for_execution_entries(
        self,
        db_session,
        tmp_path,
        monkeypatch,
        user,
        project,
        session_row,
        guidance_matrix,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        state = MagicMock()
        state.project_dir = str(tmp_path)
        state.session_id = session_row.id
        state.plan = []
        state.changed_files = []
        state.validation_history = []
        state.project_context = ""
        task = MagicMock()
        task.id = 1
        task.title = "p3 telemetry test"

        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=MagicMock(),
            db=db_session,
            guidance_backend="direct_ollama",
            guidance_model_family="qwen",
        )

        usage_rows = (
            db_session.query(HumanGuidanceUsage)
            .filter(HumanGuidanceUsage.session_id == session_row.id)
            .all()
        )
        assert (
            len(usage_rows) == 2
        ), f"Expected 2 usage rows (G1+G4), got {len(usage_rows)}"

        recorded_ids = {r.guidance_id for r in usage_rows}
        assert guidance_matrix["G1"] in recorded_ids, "G1 usage must be recorded"
        assert guidance_matrix["G4"] in recorded_ids, "G4 usage must be recorded"
        assert (
            guidance_matrix["G2"] not in recorded_ids
        ), "G2 usage must NOT be recorded"
        assert (
            guidance_matrix["G3"] not in recorded_ids
        ), "G3 usage must NOT be recorded"

    def test_usage_rows_all_marked_selected(
        self,
        db_session,
        tmp_path,
        monkeypatch,
        user,
        project,
        session_row,
        guidance_matrix,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        state = MagicMock()
        state.project_dir = str(tmp_path)
        state.session_id = session_row.id
        state.plan = []
        state.changed_files = []
        state.validation_history = []
        state.project_context = ""
        task = MagicMock()
        task.id = 1
        task.title = "p3 telemetry selected check"

        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=MagicMock(),
            db=db_session,
            guidance_backend="direct_ollama",
            guidance_model_family="qwen",
        )

        usage_rows = (
            db_session.query(HumanGuidanceUsage)
            .filter(HumanGuidanceUsage.session_id == session_row.id)
            .all()
        )
        for row in usage_rows:
            assert row.selected is True
            assert row.rendered is True
            assert row.trimmed is False


# ---------------------------------------------------------------------------
# E. Observability logs
# ---------------------------------------------------------------------------


class TestObservabilityLogs:
    def test_wm_log_includes_purpose_execution(
        self,
        db_session,
        tmp_path,
        monkeypatch,
        user,
        project,
        session_row,
        guidance_matrix,
        caplog,
    ):
        monkeypatch.setattr(settings, "WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)

        state = MagicMock()
        state.project_dir = str(tmp_path)
        state.session_id = session_row.id
        state.plan = []
        state.changed_files = []
        state.validation_history = []
        state.project_context = ""
        task = MagicMock()
        task.id = 1
        task.title = "p3 log test"

        with caplog.at_level(
            logging.INFO, logger="app.services.orchestration.working_memory"
        ):
            write_working_memory(
                orchestration_state=state,
                task=task,
                summary="done",
                logger=logging.getLogger("app.services.orchestration.working_memory"),
                db=db_session,
                guidance_backend="direct_ollama",
                guidance_model_family="qwen",
            )

        hg_backend_lines = [
            r.message for r in caplog.records if "[HG_BACKEND]" in r.message
        ]
        assert any(
            "purpose=execution" in line for line in hg_backend_lines
        ), f"Expected 'purpose=execution' in HG_BACKEND log. Got: {hg_backend_lines}"

    def test_plan_validator_log_includes_purpose_planning(
        self,
        db_session,
        monkeypatch,
        user,
        project,
        session_row,
        guidance_matrix,
        caplog,
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        plan = [
            {
                "step_number": 1,
                "description": "no-op",
                "ops": [{"op": "write_file", "path": "x.py", "content": "pass"}],
                "commands": [],
            }
        ]
        with caplog.at_level(
            logging.DEBUG, logger="app.services.human_guidance_plan_validator"
        ):
            check_plan_guidance_violations_if_enabled(
                db_session,
                project_id=project.id,
                session_id=session_row.id,
                task_id=None,
                user_id=user.id,
                plan_steps=plan,
                backend="direct_ollama",
                model_family="qwen",
            )

        pv_lines = [
            r.message for r in caplog.records if "GUIDANCE_PLAN_VALIDATION" in r.message
        ]
        assert any(
            "purpose=planning" in line for line in pv_lines
        ), f"Expected 'purpose=planning' in GUIDANCE_PLAN_VALIDATION log. Got: {pv_lines}"

    def test_conflict_log_includes_purpose_planning(
        self, db_session, user, project, session_row, guidance_matrix, caplog
    ):
        with caplog.at_level(
            logging.DEBUG, logger="app.services.human_guidance_conflict_service"
        ):
            detect_guidance_task_conflicts(
                db_session,
                project_id=project.id,
                session_id=session_row.id,
                task_id=None,
                user_id=user.id,
                task_title="test conflict log",
                task_description="",
                backend="direct_ollama",
                model_family="qwen",
            )

        conflict_lines = [
            r.message for r in caplog.records if "GUIDANCE_CONFLICT" in r.message
        ]
        assert any(
            "purpose=planning" in line for line in conflict_lines
        ), f"Expected 'purpose=planning' in GUIDANCE_CONFLICT log. Got: {conflict_lines}"

    def test_repair_renderer_log_includes_purpose_repair(
        self,
        db_session,
        monkeypatch,
        user,
        project,
        session_row,
        guidance_matrix,
        caplog,
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        with caplog.at_level(
            logging.DEBUG, logger="app.services.human_guidance_plan_validator"
        ):
            render_active_guidance_for_repair(
                db_session,
                project_id=project.id,
                session_id=session_row.id,
                task_id=None,
                user_id=user.id,
                backend="direct_ollama",
                model_family="qwen",
            )

        repair_lines = [
            r.message for r in caplog.records if "GUIDANCE_REPAIR_RENDER" in r.message
        ]
        assert any(
            "purpose=repair" in line for line in repair_lines
        ), f"Expected 'purpose=repair' in GUIDANCE_REPAIR_RENDER log. Got: {repair_lines}"
