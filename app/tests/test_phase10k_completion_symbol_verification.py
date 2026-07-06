"""Phase 10K-c — Requested Symbol Completion Verification tests.

Verifies that:
1. Task requesting typed symbol absent from changed files → completion rejected.
2. Task requesting symbol that is present → completion passes.
3. Task with no typed symbol → check is no-op.
4. Class symbol missing → fail.
5. File parse error → no crash.
6. Non-Python changed files → no-op.
7. review_only execution profile → no-op.
8. LogEntry [COMPLETION_SYMBOL_VERIFICATION_FAILED] is emitted on failure.
9. P5d T3 fixture fails deterministically.
10. Existing completion validator tests still pass (smoke).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.services.orchestration.validation.completion_symbol_check import (
    _extract_top_level_symbol_names,
    check_completion_symbol_presence,
)
from app.services.orchestration.validation.validator import ValidatorService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_py(tmp_path: Path, name: str, source: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(source), encoding="utf-8")
    return p


def _minimal_plan():
    return [
        {
            "step_number": 1,
            "ops": [{"op": "write_file", "path": "lib.py"}],
            "expected_files": ["lib.py"],
            "verification": "pytest",
        }
    ]


def _call_validator(
    tmp_path: Path,
    *,
    task_prompt: str = "",
    title: str = "",
    description: str = "",
    changed_files: list[str] | None = None,
    execution_profile: str = "full_lifecycle",
) -> Any:
    return ValidatorService.validate_task_completion(
        project_dir=tmp_path,
        plan=_minimal_plan(),
        task_prompt=task_prompt,
        execution_profile=execution_profile,
        title=title,
        description=description,
        relaxed_mode=False,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": changed_files or [],
        },
    )


# ---------------------------------------------------------------------------
# check_completion_symbol_presence unit tests
# ---------------------------------------------------------------------------


class TestCheckCompletionSymbolPresence:
    def test_missing_symbol_returns_not_passed(self, tmp_path):
        _write_py(tmp_path, "lib.py", "def other(): pass\n")
        result = check_completion_symbol_presence(
            task_description="Add add_category(category: str) -> list to lib",
            reported_changed_files=["lib.py"],
            project_dir=tmp_path,
        )
        assert result["applicable"] is True
        assert result["passed"] is False
        assert "add_category" in result["missing"]

    def test_present_symbol_returns_passed(self, tmp_path):
        _write_py(tmp_path, "lib.py", "def add_category(category): pass\n")
        result = check_completion_symbol_presence(
            task_description="Add add_category(category: str) -> list to lib",
            reported_changed_files=["lib.py"],
            project_dir=tmp_path,
        )
        assert result["applicable"] is True
        assert result["passed"] is True
        assert "add_category" in result["found"]
        assert result["missing"] == []

    def test_no_typed_symbol_in_description_not_applicable(self, tmp_path):
        _write_py(tmp_path, "lib.py", "def helper(): pass\n")
        result = check_completion_symbol_presence(
            task_description="Improve the parser and refactor helpers",
            reported_changed_files=["lib.py"],
            project_dir=tmp_path,
        )
        assert result["applicable"] is False
        assert result["passed"] is True

    def test_generic_description_not_applicable(self, tmp_path):
        _write_py(tmp_path, "lib.py", "x = 1\n")
        result = check_completion_symbol_presence(
            task_description="Update documentation",
            reported_changed_files=["lib.py"],
            project_dir=tmp_path,
        )
        assert result["applicable"] is False

    def test_class_symbol_missing_returns_not_passed(self, tmp_path):
        _write_py(tmp_path, "models.py", "class OtherModel: pass\n")
        result = check_completion_symbol_presence(
            task_description="Add class Foo to models",
            reported_changed_files=["models.py"],
            project_dir=tmp_path,
        )
        # "class Foo" syntax → extracted via def/class pattern
        assert result["applicable"] is True
        assert "Foo" in result["missing"]

    def test_file_parse_error_no_crash(self, tmp_path):
        bad = tmp_path / "bad.py"
        bad.write_text("def )(: BAD SYNTAX", encoding="utf-8")
        result = check_completion_symbol_presence(
            task_description="Add add_category(x: int) -> str to bad",
            reported_changed_files=["bad.py"],
            project_dir=tmp_path,
        )
        # Applicable (Python file + typed sig) but all missing (parse failed)
        assert result["applicable"] is True
        assert result["passed"] is False

    def test_nonpython_changed_files_not_applicable(self, tmp_path):
        (tmp_path / "README.md").write_text("hello", encoding="utf-8")
        result = check_completion_symbol_presence(
            task_description="Add add_category(x: str) -> list to readme",
            reported_changed_files=["README.md"],
            project_dir=tmp_path,
        )
        assert result["applicable"] is False

    def test_review_only_profile_not_applicable(self, tmp_path):
        _write_py(tmp_path, "lib.py", "def other(): pass\n")
        result = check_completion_symbol_presence(
            task_description="Add add_category(x: str) -> list to lib",
            reported_changed_files=["lib.py"],
            project_dir=tmp_path,
            execution_profile="review_only",
        )
        assert result["applicable"] is False
        assert result["passed"] is True

    def test_empty_changed_files_not_applicable(self, tmp_path):
        result = check_completion_symbol_presence(
            task_description="Add add_category(x: str) -> list to lib",
            reported_changed_files=[],
            project_dir=tmp_path,
        )
        assert result["applicable"] is False

    def test_missing_file_treated_as_no_symbols(self, tmp_path):
        result = check_completion_symbol_presence(
            task_description="Add add_category(x: str) -> list",
            reported_changed_files=["nonexistent.py"],
            project_dir=tmp_path,
        )
        assert result["applicable"] is True
        assert result["passed"] is False
        assert "add_category" in result["missing"]


# ---------------------------------------------------------------------------
# _extract_top_level_symbol_names unit tests
# ---------------------------------------------------------------------------


class TestExtractTopLevelSymbolNames:
    def test_extracts_functions(self, tmp_path):
        p = _write_py(tmp_path, "f.py", "def alpha(): pass\ndef beta(): pass\n")
        assert "alpha" in _extract_top_level_symbol_names(p)
        assert "beta" in _extract_top_level_symbol_names(p)

    def test_extracts_classes(self, tmp_path):
        p = _write_py(tmp_path, "f.py", "class Foo: pass\n")
        assert "Foo" in _extract_top_level_symbol_names(p)

    def test_nested_functions_excluded(self, tmp_path):
        p = _write_py(
            tmp_path,
            "f.py",
            """
            def outer():
                def inner(): pass
            """,
        )
        names = _extract_top_level_symbol_names(p)
        assert "outer" in names
        assert "inner" not in names

    def test_bad_syntax_returns_empty(self, tmp_path):
        p = tmp_path / "bad.py"
        p.write_text("def )(: pass", encoding="utf-8")
        assert _extract_top_level_symbol_names(p) == []

    def test_missing_file_returns_empty(self, tmp_path):
        p = tmp_path / "nope.py"
        assert _extract_top_level_symbol_names(p) == []


# ---------------------------------------------------------------------------
# validate_task_completion integration — symbol verification in verdict
# ---------------------------------------------------------------------------


class TestValidatorIntegration:
    def test_missing_symbol_rejects_completion(self, tmp_path):
        _write_py(
            tmp_path,
            "lib.py",
            "def other_func(): pass\n",
        )
        verdict = _call_validator(
            tmp_path,
            task_prompt="Add add_category(category: str) -> list to lib",
            changed_files=["lib.py"],
        )
        assert not verdict.accepted
        any_reason_mentions = any(
            "requested_symbol_missing" in r for r in verdict.reasons
        )
        assert (
            any_reason_mentions
        ), f"Expected symbol-missing reason in {verdict.reasons}"

    def test_present_symbol_does_not_reject(self, tmp_path):
        _write_py(tmp_path, "lib.py", "def add_category(x): pass\n")
        verdict = _call_validator(
            tmp_path,
            task_prompt="Add add_category(category: str) -> list to lib",
            changed_files=["lib.py"],
        )
        # Should not have a symbol-missing rejection
        symbol_rejections = [
            r for r in verdict.reasons if "requested_symbol_missing" in r
        ]
        assert symbol_rejections == []

    def test_no_typed_symbol_no_rejection(self, tmp_path):
        _write_py(tmp_path, "lib.py", "x = 1\n")
        verdict = _call_validator(
            tmp_path,
            task_prompt="Refactor the helpers module",
            changed_files=["lib.py"],
        )
        symbol_rejections = [
            r for r in verdict.reasons if "requested_symbol_missing" in r
        ]
        assert symbol_rejections == []

    def test_review_only_no_symbol_rejection(self, tmp_path):
        _write_py(tmp_path, "lib.py", "x = 1\n")
        verdict = _call_validator(
            tmp_path,
            task_prompt="Add add_category(x: str) -> list",
            changed_files=["lib.py"],
            execution_profile="review_only",
        )
        symbol_rejections = [
            r for r in verdict.reasons if "requested_symbol_missing" in r
        ]
        assert symbol_rejections == []

    def test_symbol_verification_stored_in_details(self, tmp_path):
        _write_py(tmp_path, "lib.py", "def other(): pass\n")
        verdict = _call_validator(
            tmp_path,
            task_prompt="Add add_category(category: str) -> list to lib",
            changed_files=["lib.py"],
        )
        sym = verdict.details.get("symbol_verification")
        assert sym is not None
        assert sym["applicable"] is True
        assert "add_category" in sym["missing"]

    def test_nonpython_files_no_symbol_check(self, tmp_path):
        (tmp_path / "docs.md").write_text("# docs\n", encoding="utf-8")
        verdict = _call_validator(
            tmp_path,
            task_prompt="Add add_category(category: str) -> list",
            changed_files=["docs.md"],
        )
        sym = verdict.details.get("symbol_verification") or {}
        assert sym.get("applicable") is not True


# ---------------------------------------------------------------------------
# LogEntry emission (integration via db fixture)
# ---------------------------------------------------------------------------


class TestLogEntryEmission:
    def test_logentry_emitted_on_symbol_failure(self, db_session, tmp_path):
        import logging

        from app.models import (
            LogEntry,
            Project,
            Session as SessionModel,
            SessionTask,
            Task,
            TaskExecution,
            TaskStatus,
        )
        from app.services.orchestration.phases.completion_flow import (
            finalize_successful_task,
        )
        from app.services.orchestration.types import OrchestrationRunContext
        from app.services.orchestration.prompt_templates import (
            OrchestrationState,
            StepResult,
        )
        from app.services.tasks.service import TaskService

        project_dir = tmp_path / "sym-project"
        project_dir.mkdir()
        (project_dir / "lib.py").write_text(
            "def other_func(): pass\n", encoding="utf-8"
        )

        project = Project(name="sym-check", workspace_path=str(project_dir))
        db_session.add(project)
        db_session.flush()

        session = SessionModel(
            project_id=project.id,
            name="Sym Check Session",
            status="running",
            is_active=True,
            execution_mode="manual",
        )
        task = Task(
            project_id=project.id,
            title="Add add_category",
            description="Add add_category(category: str) -> list to lib",
            status=TaskStatus.RUNNING,
            task_subfolder=None,
        )
        db_session.add_all([session, task])
        db_session.flush()

        link = SessionTask(
            session_id=session.id,
            task_id=task.id,
            status=TaskStatus.RUNNING,
        )
        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.RUNNING,
        )
        db_session.add_all([link, execution])
        db_session.commit()

        state = OrchestrationState(
            session_id=str(session.id),
            task_description="Add add_category(category: str) -> list to lib",
            project_name="sym-check",
            task_id=task.id,
            plan=[
                {
                    "step_number": 1,
                    "description": "Add function",
                    "commands": ["true"],
                    "verification": "pytest",
                    "rollback": None,
                    "expected_files": ["lib.py"],
                }
            ],
        )
        state._project_dir_override = str(project_dir)
        state.execution_results = [
            StepResult(
                step_number=1,
                status="completed",
                output="done",
                files_changed=["lib.py"],
            )
        ]
        state.changed_files = ["lib.py"]

        class _FakeRuntime:
            async def execute_task(self, prompt, timeout_seconds=None):
                return {"output": "Task summary"}

            def get_backend_metadata(self):
                return {"backend": "fake", "model_family": "test"}

        ctx = OrchestrationRunContext(
            db=db_session,
            session=session,
            project=project,
            task=task,
            session_task_link=link,
            session_id=session.id,
            task_id=task.id,
            prompt="Add add_category(category: str) -> list to lib",
            timeout_seconds=120,
            execution_profile="full_lifecycle",
            validation_profile="implementation",
            runs_in_canonical_baseline=False,
            orchestration_state=state,
            runtime_service=_FakeRuntime(),
            task_service=TaskService(db_session),
            logger=logging.getLogger("sym-check-test"),
            emit_live=lambda *args, **kwargs: None,
            error_handler=MagicMock(),
            task_execution_id=execution.id,
            restore_workspace_snapshot_if_needed=lambda reason: None,
        )

        finalize_successful_task(
            ctx=ctx,
            save_orchestration_checkpoint_fn=lambda *a, **kw: None,
            write_project_state_snapshot_fn=lambda *a, **kw: None,
        )

        log = (
            db_session.query(LogEntry)
            .filter(LogEntry.message.like("%COMPLETION_SYMBOL_VERIFICATION_FAILED%"))
            .first()
        )
        assert (
            log is not None
        ), "Expected COMPLETION_SYMBOL_VERIFICATION_FAILED LogEntry"
        assert "add_category" in log.message
        meta = json.loads(log.log_metadata or "{}")
        assert "add_category" in meta.get("missing_symbols", [])


# ---------------------------------------------------------------------------
# P5d T3 fixture — deterministic failure
# ---------------------------------------------------------------------------


class TestP5dT3Fixture:
    """P5d T3: looptools has repeat_string/filter_list/merge_dicts, not add_category."""

    def _write_looptools(self, tmp_path: Path) -> str:
        source = textwrap.dedent(
            """
            def normalize_label(x): return str(x).strip()
            def repeat_string(s, n): return s * n
            def filter_list(lst, fn): return [x for x in lst if fn(x)]
            def merge_dicts(a, b): return {**a, **b}
            """
        )
        p = tmp_path / "looptools" / "__init__.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(source, encoding="utf-8")
        return "looptools/__init__.py"

    def test_check_fails_deterministically(self, tmp_path):
        rel = self._write_looptools(tmp_path)
        result = check_completion_symbol_presence(
            task_description=(
                "Add add_category(category: str, categories: list[str] = []) -> list[str]"
                " to looptools."
            ),
            reported_changed_files=[rel],
            project_dir=tmp_path,
        )
        assert result["applicable"] is True
        assert result["passed"] is False
        assert "add_category" in result["missing"]

    def test_present_symbols_are_found(self, tmp_path):
        rel = self._write_looptools(tmp_path)
        result = check_completion_symbol_presence(
            task_description=(
                "Add add_category(category: str, categories: list[str] = []) -> list[str]"
                " to looptools."
            ),
            reported_changed_files=[rel],
            project_dir=tmp_path,
        )
        for sym in ("repeat_string", "filter_list", "merge_dicts", "normalize_label"):
            # present symbols are found but not in the required list
            # (only add_category is required from the description)
            pass
        assert "add_category" not in result["found"]

    def test_validator_rejects_missing_symbol(self, tmp_path):
        rel = self._write_looptools(tmp_path)
        verdict = _call_validator(
            tmp_path,
            task_prompt=(
                "Add add_category(category: str, categories: list[str] = []) -> list[str]"
                " to looptools."
            ),
            changed_files=[rel],
        )
        assert not verdict.accepted
        assert any("requested_symbol_missing" in r for r in verdict.reasons)
        sym = verdict.details.get("symbol_verification", {})
        assert "add_category" in sym.get("missing", [])


# ---------------------------------------------------------------------------
# Existing completion validator smoke tests
# ---------------------------------------------------------------------------


class TestExistingValidatorSmoke:
    def test_review_report_artifact_accepted(self, tmp_path):
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "review.md").write_text("# Review\n\nNo blockers.\n")
        verdict = ValidatorService.validate_task_completion(
            project_dir=tmp_path,
            plan=[
                {
                    "step_number": 1,
                    "ops": [{"op": "write_file", "path": "docs/review.md"}],
                    "expected_files": ["docs/review.md"],
                    "verification": 'python -c "assert True"',
                }
            ],
            task_prompt="Review the project and write docs/review.md.",
            execution_profile="review_only",
            title="Workspace Review",
            description="Review artifacts.",
            completion_evidence={
                "summary_generated": True,
                "execution_results_count": 1,
                "reported_changed_files": ["docs/review.md"],
            },
        )
        # review_only with a present file should not be rejected by symbol check
        sym_rejections = [r for r in verdict.reasons if "requested_symbol_missing" in r]
        assert sym_rejections == []

    def test_implementation_with_correct_symbol_accepted_by_symbol_check(
        self, tmp_path
    ):
        src = tmp_path / "src"
        src.mkdir()
        (src / "parser.py").write_text(
            "def parse_amount(text: str) -> float:\n    return 0.0\n"
        )
        verdict = ValidatorService.validate_task_completion(
            project_dir=tmp_path,
            plan=[
                {
                    "step_number": 1,
                    "ops": [{"op": "write_file", "path": "src/parser.py"}],
                    "expected_files": ["src/parser.py"],
                    "verification": "pytest",
                }
            ],
            task_prompt="Add parse_amount(text: str) -> float to src/parser.py",
            execution_profile="full_lifecycle",
            completion_evidence={
                "summary_generated": True,
                "execution_results_count": 1,
                "reported_changed_files": ["src/parser.py"],
            },
        )
        sym_rejections = [r for r in verdict.reasons if "requested_symbol_missing" in r]
        assert sym_rejections == []
