"""HG Hardening Phase 2 — post-write advisory checker tests.

Covers:
  - detects mutable default in changed file
  - detects logging usage when stdout guidance active
  - ignores unchanged / pre-existing old content (only scans changed_files list)
  - ignores binary / too-large files
  - creates HumanGuidanceConflict with source=post_write_check
  - writes LogEntry warning
  - deduplicates repeated check
  - non-fatal on file read error
  - respects backend/model filtering
  - respects purpose filtering
  - does not run when no active guidance
  - does not block task completion path
  - integration smoke: local_openclaw task with changed file → advisory conflict row
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.models import (
    HumanGuidanceConflict,
    LogEntry,
    Project,
    User,
)
from app.services.human_guidance.post_write_checker import (
    _backend_bypasses_structured_planning,
    _is_skippable_file,
    _read_file_safe,
    run_post_write_guidance_check,
    run_post_write_check_if_enabled,
    _POST_WRITE_SOURCE,
    _LOG_PREFIX,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_user(db, email="p2checker@example.com") -> User:
    user = User(email=email, hashed_password="x", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_project(db, user_id: int) -> Project:
    project = Project(
        name="P2 Checker Test",
        workspace_path="/tmp/p2checker",
        user_id=user_id,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_guidance(db, project_id, user_id, message, priority=50):
    from app.services.human_guidance.service import create_guidance

    entry, _ = create_guidance(
        db,
        user_id=user_id,
        project_id=project_id,
        scope="project",
        message=message,
        priority=priority,
    )
    return entry


def _enable_all_flags(db, project_id):
    from app.services.human_guidance.activation import set_project_activation

    set_project_activation(
        db,
        project_id,
        {
            "table_enabled": True,
            "persistence_enabled": True,
            "render_enabled": True,
            "injection_enabled": True,
            "conflict_detection_enabled": True,
        },
    )


def _write_py_file(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ── 1. _backend_bypasses_structured_planning ──────────────────────────────────


class TestBypassDetection:
    def test_local_openclaw_always_bypasses(self):
        assert (
            _backend_bypasses_structured_planning(["step1"], "local_openclaw") is True
        )

    def test_no_plan_steps_is_bypass(self):
        assert _backend_bypasses_structured_planning([], "qwen") is True
        assert _backend_bypasses_structured_planning(None, "qwen") is True

    def test_structured_plan_non_local_not_bypass(self):
        assert _backend_bypasses_structured_planning(["step1"], "qwen") is False

    def test_empty_plan_local_openclaw_is_bypass(self):
        assert _backend_bypasses_structured_planning([], "local_openclaw") is True


# ── 2. File utilities ─────────────────────────────────────────────────────────


class TestFileUtils:
    def test_skips_binary_extensions(self):
        for ext in [".pyc", ".so", ".jpg", ".png", ".gz", ".sqlite", ".db"]:
            assert _is_skippable_file(f"somefile{ext}") is True

    def test_does_not_skip_code_extensions(self):
        for ext in [".py", ".ts", ".tsx", ".js", ".go", ".rs"]:
            assert _is_skippable_file(f"somefile{ext}") is False

    def test_read_file_safe_returns_content(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("def foo(): pass\n", encoding="utf-8")
        assert _read_file_safe(f) == "def foo(): pass\n"

    def test_read_file_safe_returns_none_for_missing(self):
        assert _read_file_safe("/nonexistent/path/file.py") is None

    def test_read_file_safe_returns_none_for_large_file(self, tmp_path):
        f = tmp_path / "big.py"
        f.write_bytes(b"x" * (100 * 1024 + 1))
        assert _read_file_safe(f) is None

    def test_read_file_safe_returns_none_for_binary(self, tmp_path):
        f = tmp_path / "data.py"
        f.write_bytes(bytes(range(256)))
        assert _read_file_safe(f) is None


# ── 3. Core checker — pattern detection ───────────────────────────────────────


class TestPostWriteChecker:
    def test_detects_mutable_default_in_changed_file(
        self, db_session, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session)
        project = _make_project(db_session, user.id)
        _enable_all_flags(db_session, project.id)
        _make_guidance(
            db_session,
            project.id,
            user.id,
            "Never use mutable default arguments. Use None and initialize inside.",
        )

        changed_file = _write_py_file(
            tmp_path, "utils.py", "def process(items=[]):\n    return items\n"
        )

        results = run_post_write_guidance_check(
            db_session,
            project_id=project.id,
            session_id=1,
            task_id=10,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=[str(changed_file)],
            execution_backend="local_openclaw",
        )

        assert len(results) == 1
        assert results[0]["pattern"] == "mutable_default"
        assert results[0]["source"] == _POST_WRITE_SOURCE
        assert results[0]["severity"] == "advisory"

    def test_detects_logging_usage_when_stdout_guidance_active(
        self, db_session, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session, "p2checker2@example.com")
        project = _make_project(db_session, user.id)
        _enable_all_flags(db_session, project.id)
        _make_guidance(
            db_session,
            project.id,
            user.id,
            "All runtime output must go to stdout. Use print() for runtime reporting.",
        )

        changed_file = _write_py_file(
            tmp_path,
            "reporter.py",
            "import logging\nlogger = logging.getLogger(__name__)\ndef run(): logger.info('ok')\n",
        )

        results = run_post_write_guidance_check(
            db_session,
            project_id=project.id,
            session_id=1,
            task_id=11,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=[str(changed_file)],
        )

        assert any(r["pattern"] == "stdout_vs_logging" for r in results)

    def test_does_not_scan_files_not_in_changed_list(
        self, db_session, tmp_path, monkeypatch
    ):
        """Only files in changed_files are inspected — pre-existing files are ignored."""
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session, "p2checker3@example.com")
        project = _make_project(db_session, user.id)
        _enable_all_flags(db_session, project.id)
        _make_guidance(
            db_session,
            project.id,
            user.id,
            "Never use mutable default arguments. Use None.",
        )

        # Existing file with violation — NOT in changed_files
        _write_py_file(tmp_path, "old_code.py", "def f(x=[]):\n    pass\n")
        # Changed file is clean
        clean_file = _write_py_file(
            tmp_path, "new_code.py", "def f(x=None):\n    pass\n"
        )

        results = run_post_write_guidance_check(
            db_session,
            project_id=project.id,
            session_id=1,
            task_id=12,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=[str(clean_file)],  # only clean file
        )

        assert (
            results == []
        ), "Should not flag violations in files not listed as changed"

    def test_ignores_binary_files(self, db_session, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session, "p2checker4@example.com")
        project = _make_project(db_session, user.id)
        _enable_all_flags(db_session, project.id)
        _make_guidance(
            db_session, project.id, user.id, "Never use mutable default arguments."
        )

        binary_file = tmp_path / "output.pyc"
        binary_file.write_bytes(b"\x00\x01\x02" + b"= []" * 10)

        results = run_post_write_guidance_check(
            db_session,
            project_id=project.id,
            session_id=1,
            task_id=13,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=[str(binary_file)],
        )
        assert results == []

    def test_ignores_too_large_files(self, db_session, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session, "p2checker5@example.com")
        project = _make_project(db_session, user.id)
        _enable_all_flags(db_session, project.id)
        _make_guidance(
            db_session, project.id, user.id, "Never use mutable default arguments."
        )

        big_file = tmp_path / "huge.py"
        big_file.write_bytes(b"def f(x=[]):pass\n" * 7000)  # well over 100KB

        results = run_post_write_guidance_check(
            db_session,
            project_id=project.id,
            session_id=1,
            task_id=14,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=[str(big_file)],
        )
        assert results == []

    def test_creates_conflict_row_with_correct_fields(
        self, db_session, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session, "p2checker6@example.com")
        project = _make_project(db_session, user.id)
        _enable_all_flags(db_session, project.id)
        guidance = _make_guidance(
            db_session, project.id, user.id, "Never use mutable default arguments."
        )

        changed_file = _write_py_file(
            tmp_path, "app.py", "def handler(items=[]):\n    pass\n"
        )

        run_post_write_guidance_check(
            db_session,
            project_id=project.id,
            session_id=1,
            task_id=15,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=[str(changed_file)],
            task_title="Implement handler",
        )

        conflict = (
            db_session.query(HumanGuidanceConflict)
            .filter(
                HumanGuidanceConflict.project_id == project.id,
                HumanGuidanceConflict.source == _POST_WRITE_SOURCE,
            )
            .first()
        )
        assert conflict is not None
        assert conflict.severity == "advisory"
        assert conflict.status == "open"
        assert conflict.source == _POST_WRITE_SOURCE
        assert conflict.guidance_id == guidance.id
        assert conflict.task_id == 15
        assert conflict.task_title == "Implement handler"
        patterns = json.loads(conflict.conflict_patterns)
        assert "mutable_default" in patterns

    def test_writes_log_entry_warning(self, db_session, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session, "p2checker7@example.com")
        project = _make_project(db_session, user.id)
        _enable_all_flags(db_session, project.id)
        _make_guidance(
            db_session, project.id, user.id, "Never use mutable default arguments."
        )

        changed_file = _write_py_file(tmp_path, "mod.py", "def f(x=[]):pass\n")

        run_post_write_guidance_check(
            db_session,
            project_id=project.id,
            session_id=99,
            task_id=20,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=[str(changed_file)],
        )

        log = (
            db_session.query(LogEntry)
            .filter(
                LogEntry.task_id == 20,
                LogEntry.level == "WARNING",
                LogEntry.message.like(f"%{_LOG_PREFIX}%"),
            )
            .first()
        )
        assert log is not None
        assert "mutable_default" in log.message

    def test_deduplicates_repeated_check(self, db_session, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session, "p2checker8@example.com")
        project = _make_project(db_session, user.id)
        _enable_all_flags(db_session, project.id)
        guidance = _make_guidance(
            db_session, project.id, user.id, "Never use mutable default arguments."
        )

        changed_file = _write_py_file(tmp_path, "dup.py", "def f(x=[]):pass\n")

        kwargs = dict(
            project_id=project.id,
            session_id=1,
            task_id=30,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=[str(changed_file)],
        )
        run_post_write_guidance_check(db_session, **kwargs)
        run_post_write_guidance_check(db_session, **kwargs)

        count = (
            db_session.query(HumanGuidanceConflict)
            .filter(
                HumanGuidanceConflict.guidance_id == guidance.id,
                HumanGuidanceConflict.task_id == 30,
                HumanGuidanceConflict.source == _POST_WRITE_SOURCE,
                HumanGuidanceConflict.status == "open",
            )
            .count()
        )
        assert count == 1, "Second call must not create a duplicate open conflict row"

    def test_non_fatal_on_file_read_error(self, db_session, tmp_path, monkeypatch):
        """Unreadable file path should not raise — result is an empty list."""
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session, "p2checker9@example.com")
        project = _make_project(db_session, user.id)
        _enable_all_flags(db_session, project.id)
        _make_guidance(
            db_session, project.id, user.id, "Never use mutable default arguments."
        )

        results = run_post_write_guidance_check(
            db_session,
            project_id=project.id,
            session_id=1,
            task_id=40,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=["/nonexistent/path/file.py"],
        )
        assert results == []

    def test_does_not_run_when_no_active_guidance(
        self, db_session, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session, "p2checker10@example.com")
        project = _make_project(db_session, user.id)
        # No guidance rows created

        changed_file = _write_py_file(tmp_path, "code.py", "def f(x=[]):pass\n")

        results = run_post_write_guidance_check(
            db_session,
            project_id=project.id,
            session_id=1,
            task_id=50,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=[str(changed_file)],
        )
        assert results == []


# ── 4. Backend/model/purpose filtering ───────────────────────────────────────


class TestFilteringRespected:
    def test_respects_backend_filtering(self, db_session, tmp_path, monkeypatch):
        """Guidance targeting only 'qwen' should not match when backend='ollama'."""
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session, "p2filter1@example.com")
        project = _make_project(db_session, user.id)
        _enable_all_flags(db_session, project.id)

        # Create guidance targeting only 'qwen' backend
        from app.services.human_guidance.service import create_guidance

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Never use mutable default arguments.",
            priority=50,
            backend_targets=["qwen"],
        )

        changed_file = _write_py_file(tmp_path, "filtered.py", "def f(x=[]):pass\n")

        results = run_post_write_guidance_check(
            db_session,
            project_id=project.id,
            session_id=1,
            task_id=60,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=[str(changed_file)],
            guidance_backend="ollama",  # does not match qwen target
        )
        assert results == [], "Backend-filtered guidance should not produce violations"

    def test_respects_model_family_filtering(self, db_session, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session, "p2filter2@example.com")
        project = _make_project(db_session, user.id)
        _enable_all_flags(db_session, project.id)

        from app.services.human_guidance.service import create_guidance

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Never use mutable default arguments.",
            priority=50,
            model_targets=["gpt4"],
        )

        changed_file = _write_py_file(tmp_path, "mf_filtered.py", "def f(x=[]):pass\n")

        results = run_post_write_guidance_check(
            db_session,
            project_id=project.id,
            session_id=1,
            task_id=61,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=[str(changed_file)],
            guidance_model_family="qwen",  # does not match gpt4
        )
        assert results == []

    def test_purpose_validation_falls_back_to_planning(
        self, db_session, tmp_path, monkeypatch
    ):
        """Guidance with purpose_targets=['planning'] should be found via planning fallback."""
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session, "p2filter3@example.com")
        project = _make_project(db_session, user.id)
        _enable_all_flags(db_session, project.id)

        from app.services.human_guidance.service import create_guidance

        create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="Never use mutable default arguments.",
            priority=50,
            purpose_targets=[
                "planning"
            ],  # not "validation" — should be found via fallback
        )

        changed_file = _write_py_file(
            tmp_path, "purpose_test.py", "def g(items=[]):\n    pass\n"
        )

        results = run_post_write_guidance_check(
            db_session,
            project_id=project.id,
            session_id=1,
            task_id=62,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=[str(changed_file)],
        )
        assert (
            len(results) == 1
        ), "Planning-purpose guidance should be found via fallback"


# ── 5. run_post_write_check_if_enabled (flag-gated wrapper) ───────────────────


class TestFlagGatedWrapper:
    def _make_ctx(
        self,
        db,
        project,
        plan_steps=None,
        execution_backend="local_openclaw",
        table_enabled=True,
        conflict_detection_enabled=True,
        monkeypatch=None,
    ):
        if monkeypatch:
            monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", table_enabled)
            monkeypatch.setattr(
                settings,
                "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED",
                conflict_detection_enabled,
            )

        ctx = MagicMock()
        ctx.db = db
        ctx.session_id = 1
        ctx.task_id = 100
        ctx.project.id = project.id
        ctx.project.user_id = project.user_id
        ctx.orchestration_state.plan = plan_steps or []
        ctx.orchestration_state.project_dir = "/tmp/test"
        ctx.execution_backend = execution_backend
        ctx.guidance_backend = "all"
        ctx.guidance_model_family = "all"
        ctx.task.title = "test task"
        return ctx

    def test_does_not_run_when_table_flag_off(self, db_session, monkeypatch):
        user = _make_user(db_session, "p2wrap1@example.com")
        project = _make_project(db_session, user.id)
        ctx = self._make_ctx(
            db_session,
            project,
            table_enabled=False,
            conflict_detection_enabled=True,
            monkeypatch=monkeypatch,
        )

        with patch(
            "app.services.human_guidance.post_write_checker.run_post_write_guidance_check"
        ) as mock_check:
            run_post_write_check_if_enabled(ctx, reported_changed_files=[])
            mock_check.assert_not_called()

    def test_does_not_run_when_conflict_detection_flag_off(
        self, db_session, monkeypatch
    ):
        user = _make_user(db_session, "p2wrap2@example.com")
        project = _make_project(db_session, user.id)
        ctx = self._make_ctx(
            db_session,
            project,
            table_enabled=True,
            conflict_detection_enabled=False,
            monkeypatch=monkeypatch,
        )

        with patch(
            "app.services.human_guidance.post_write_checker.run_post_write_guidance_check"
        ) as mock_check:
            run_post_write_check_if_enabled(ctx, reported_changed_files=[])
            mock_check.assert_not_called()

    def test_does_not_run_when_structured_plan_exists(self, db_session, monkeypatch):
        """Checker must not run when plan_steps is non-empty and backend is not local_openclaw."""
        user = _make_user(db_session, "p2wrap3@example.com")
        project = _make_project(db_session, user.id)
        ctx = self._make_ctx(
            db_session,
            project,
            plan_steps=[{"step": 1}],
            execution_backend="qwen",
            table_enabled=True,
            conflict_detection_enabled=True,
            monkeypatch=monkeypatch,
        )

        with patch(
            "app.services.human_guidance.post_write_checker.run_post_write_guidance_check"
        ) as mock_check:
            run_post_write_check_if_enabled(ctx, reported_changed_files=["app.py"])
            mock_check.assert_not_called()

    def test_runs_for_local_openclaw(self, db_session, monkeypatch):
        user = _make_user(db_session, "p2wrap4@example.com")
        project = _make_project(db_session, user.id)
        ctx = self._make_ctx(
            db_session,
            project,
            plan_steps=[{"step": 1}],  # even with plan steps, local_openclaw triggers
            execution_backend="local_openclaw",
            table_enabled=True,
            conflict_detection_enabled=True,
            monkeypatch=monkeypatch,
        )

        with patch(
            "app.services.human_guidance.post_write_checker.run_post_write_guidance_check",
            return_value=[],
        ) as mock_check:
            with patch(
                "app.services.human_guidance.activation.check_activation_flag",
                return_value=True,
            ):
                run_post_write_check_if_enabled(ctx, reported_changed_files=["app.py"])
        mock_check.assert_called_once()

    def test_does_not_block_task_completion_on_exception(self, db_session, monkeypatch):
        """If the checker raises internally, it should not propagate."""
        user = _make_user(db_session, "p2wrap5@example.com")
        project = _make_project(db_session, user.id)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        ctx = self._make_ctx(
            db_session,
            project,
            execution_backend="local_openclaw",
            monkeypatch=None,  # already set above
        )

        with patch(
            "app.services.human_guidance.post_write_checker.run_post_write_guidance_check",
            side_effect=RuntimeError("db crashed"),
        ):
            with patch(
                "app.services.human_guidance.activation.check_activation_flag",
                return_value=True,
            ):
                # Must not raise
                run_post_write_check_if_enabled(ctx, reported_changed_files=["app.py"])


# ── 6. Integration smoke ───────────────────────────────────────────────────────


class TestIntegrationSmoke:
    def test_local_openclaw_task_creates_advisory_conflict(
        self, db_session, tmp_path, monkeypatch
    ):
        """Full path: local_openclaw task, changed file with mutable default → advisory conflict."""
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session, "p2smoke@example.com")
        project = _make_project(db_session, user.id)
        _enable_all_flags(db_session, project.id)

        guidance = _make_guidance(
            db_session,
            project.id,
            user.id,
            "Never use mutable default arguments. Use None and initialize inside.",
        )

        # Simulate local_openclaw writing a file with a labels: list[str] = [] pattern
        changed_file = _write_py_file(
            tmp_path,
            "service.py",
            "from typing import List\n\n"
            "def process(items: list = [], labels: list[str] = []):\n"
            "    return items + labels\n",
        )

        # local_openclaw path: plan_steps is empty
        results = run_post_write_guidance_check(
            db_session,
            project_id=project.id,
            session_id=5,
            task_id=99,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=[str(changed_file)],
            execution_backend="local_openclaw",
            plan_steps=[],
            task_title="Add process service",
        )

        # advisory conflict created
        assert len(results) >= 1
        assert results[0]["severity"] == "advisory"
        assert results[0]["source"] == _POST_WRITE_SOURCE

        # conflict row in DB
        conflict = (
            db_session.query(HumanGuidanceConflict)
            .filter(
                HumanGuidanceConflict.task_id == 99,
                HumanGuidanceConflict.source == _POST_WRITE_SOURCE,
            )
            .first()
        )
        assert conflict is not None
        assert conflict.severity == "advisory"
        assert conflict.guidance_id == guidance.id

    def test_task_status_unaffected_by_advisory(
        self, db_session, tmp_path, monkeypatch
    ):
        """Advisory conflict creation must not affect return value of task completion."""
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_TABLE_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_GUIDANCE_CONFLICT_DETECTION_ENABLED", True)

        user = _make_user(db_session, "p2smoke2@example.com")
        project = _make_project(db_session, user.id)
        _enable_all_flags(db_session, project.id)
        _make_guidance(
            db_session, project.id, user.id, "Never use mutable default arguments."
        )

        changed_file = _write_py_file(tmp_path, "task_file.py", "def f(x=[]):pass\n")

        # run_post_write_guidance_check returns a list (advisory findings)
        # and must not raise even if findings exist
        result = run_post_write_guidance_check(
            db_session,
            project_id=project.id,
            session_id=5,
            task_id=88,
            user_id=user.id,
            project_dir=tmp_path,
            changed_files=[str(changed_file)],
        )

        # Return value is a list; task completion is unaffected
        assert isinstance(result, list)
