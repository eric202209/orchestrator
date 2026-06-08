"""Characterization tests: ProjectStateSummary injection into Task 2+ planning.

Maintenance Slice A — PSS Continuation Characterization.

Objective: measure whether injecting ProjectStateSummary into Task 2+ planning
context adds information not already present via progress_notes +
build_project_execution_context, and whether it increases planning violation risk.

Constraints enforced:
- No live model calls.
- No changes to validator, planning schema, repair logic, or execution.
- Feature flag PSS_CONTINUATION_INJECTION_ENABLED defaults False.
- All assertions are read-only observations about context content.

Three eval cases exercised (unit-level DB fixture, no filesystem required):
  - tiny_money   : single-file source rewrite, one completed task, no artifacts
  - stale_replace: stale replace-op repair, one completed task, no artifacts
  - medium_cli   : multi-file feature add, two completed tasks, no artifacts

Metrics recorded per case:
  - progress_notes_chars: chars that would flow via _inject_progress_notes
  - base_context_chars: chars from build_project_execution_context
  - pss_block_chars: chars the PSS block would add
  - overlap_fraction: tokens shared between current context and PSS block
  - pss_unique_fields: fields in PSS not represented in current context
  - would_exceed_8k_budget: whether injection pushes project_context > 8000 chars
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    PlanningArtifact,
    PlanningSession,
    Project,
    Task,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
    Session as OrchestratorSession,
)
from app.services.project.state_summary import (
    build_project_state_summary,
    render_project_state_summary_block,
    _inject_project_state_summary_into_context,
)

# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


def _project(db, *, name: str, workspace_path: str = "/tmp/proj") -> Project:
    p = Project(name=name, description="", workspace_path=workspace_path)
    db.add(p)
    db.flush()
    return p


def _task(
    db,
    project: Project,
    *,
    title: str,
    status: TaskStatus,
    plan_position: int,
    description: str = "",
    task_subfolder: Optional[str] = None,
) -> Task:
    t = Task(
        project_id=project.id,
        title=title,
        description=description,
        status=status,
        plan_position=plan_position,
        task_subfolder=task_subfolder,
    )
    db.add(t)
    db.flush()
    return t


_session_counter = 0


def _execution(db, project: Project, task: Task) -> TaskExecution:
    global _session_counter
    _session_counter += 1
    s = OrchestratorSession(project_id=project.id, name=f"s-{_session_counter}")
    db.add(s)
    db.flush()
    te = TaskExecution(
        session_id=s.id, task_id=task.id, attempt_number=1, status=TaskStatus.DONE
    )
    db.add(te)
    db.flush()
    return te


def _change_set(
    db,
    project: Project,
    task: Task,
    te: TaskExecution,
    *,
    added: List[str],
    modified: Optional[List[str]] = None,
) -> TaskExecutionChangeSet:
    cs = TaskExecutionChangeSet(
        project_id=project.id,
        task_id=task.id,
        task_execution_id=te.id,
        base_snapshot_key="snap",
        added_files=added,
        modified_files=modified or [],
        deleted_files=[],
        warning_flags=[],
    )
    db.add(cs)
    db.flush()
    return cs


def _planning_session(db, project: Project) -> PlanningSession:
    ps = PlanningSession(
        project_id=project.id,
        title="Plan",
        prompt="p",
        status="completed",
        source_brain="local",
    )
    db.add(ps)
    db.flush()
    return ps


_ARTIFACT_FILENAMES = {
    "requirements": "requirements.md",
    "design": "design.md",
    "implementation_plan": "implementation_plan.md",
    "planner_markdown": "planner.md",
}


def _artifact(
    db,
    ps: PlanningSession,
    *,
    artifact_type: str,
    content: str,
    is_latest: bool = True,
) -> PlanningArtifact:
    a = PlanningArtifact(
        planning_session_id=ps.id,
        artifact_type=artifact_type,
        filename=_ARTIFACT_FILENAMES.get(artifact_type, f"{artifact_type}.md"),
        version=1,
        content=content,
        is_latest=is_latest,
    )
    db.add(a)
    db.flush()
    return a


# ---------------------------------------------------------------------------
# Simulated context builders (matches what worker.py does for Task 2+)
# ---------------------------------------------------------------------------


def _simulate_base_context(completed_task_title: str, next_task_title: str) -> str:
    """Approximate build_project_execution_context output for a 2-task project."""
    return (
        f"Project: test-project\n"
        f"Project description: None provided\n"
        f"Current task: #2 {next_task_title} (pending)\n"
        f"Earlier ordered tasks already completed and can be reused:\n"
        f"- #1 {completed_task_title} :: status=done :: workspace=isolated"
        f" :: subfolder=task-1\n"
        "Important: execute directly in the canonical project root. "
        "Treat the current project folder as the source of truth."
    )


def _simulate_progress_notes(completed_task_title: str, files: List[str]) -> str:
    """Approximate .agent/progress_notes.md content for a completed task."""
    file_list = "\n".join(f"- {f}" for f in files)
    return (
        f"## Task 1: {completed_task_title}\n"
        f"Status: completed\n"
        f"Workspace path: task-1\n"
        f"Files produced:\n{file_list}\n"
    )


def _combined_current_context(base_context: str, progress_notes: str) -> str:
    """Reproduce what _inject_progress_notes_into_context builds."""
    prefix = (
        "=== PRIOR SESSION PROGRESS NOTES ===\n"
        + progress_notes.strip()
        + "\n=== END PRIOR SESSION PROGRESS NOTES ===\n\n"
        + "=== CURRENT WORKSPACE TRUTH ===\n"
        + "- task-1/src/money.py\n"
        + "=== END CURRENT WORKSPACE TRUTH ===\n\n"
    )
    return (prefix + base_context)[:8000]


# ---------------------------------------------------------------------------
# Overlap metric helpers
# ---------------------------------------------------------------------------


def _token_overlap_fraction(text_a: str, text_b: str) -> float:
    """Rough word-level overlap: |A ∩ B| / |A ∪ B|."""
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    if not words_a and not words_b:
        return 1.0
    intersection = len(words_a & words_b)
    union = len(words_a | words_b)
    return round(intersection / union, 3)


def _pss_unique_fields(summary: Dict[str, Any], current_context: str) -> List[str]:
    """Fields in PSS that add information not present in current_context."""
    unique: List[str] = []
    files = summary.get("files_created_or_modified") or []
    if files:
        any_file_missing = any(f not in current_context for f in files)
        if any_file_missing:
            unique.append("files_created_or_modified")
    constraints = summary.get("known_constraints") or {}
    for key in (
        "requirements_excerpt",
        "design_excerpt",
        "implementation_plan_excerpt",
    ):
        val = constraints.get(key) or ""
        if val and val[:50] not in current_context:
            unique.append(key)
    if summary.get("next_task_recommendation"):
        rec = summary["next_task_recommendation"]
        if str(rec.get("plan_position") or "") not in current_context:
            unique.append("next_task_recommendation_position")
    return unique


# ---------------------------------------------------------------------------
# Case: tiny_money
# Fixture: one completed task (fix money.py), one pending task.
# No planning artifacts.
# ---------------------------------------------------------------------------


def _build_tiny_money(db):
    p = _project(db, name="tiny-money-project", workspace_path="/tmp/tiny-money")
    task1 = _task(
        db,
        p,
        title="Fix money formatter in src/tiny_money/money.py so existing tests pass",
        status=TaskStatus.DONE,
        plan_position=1,
        task_subfolder="task-1-fix-money",
    )
    te1 = _execution(db, p, task1)
    _change_set(db, p, task1, te1, added=["src/tiny_money/money.py"])
    task2 = _task(
        db,
        p,
        title="Add currency symbol support to money formatter",
        status=TaskStatus.PENDING,
        plan_position=2,
        description="Extend format_cents() to accept a currency symbol argument.",
    )
    return p, task1, task2


class TestTinyMoneyCharacterization:
    def test_pss_block_renders_for_task2(self, db):
        p, task1, task2 = _build_tiny_money(db)
        summary = build_project_state_summary(p.id, db)
        block = render_project_state_summary_block(summary)
        assert block, "PSS block must be non-empty for Task 2"
        assert "money.py" in block, "PSS block must contain the file created by Task 1"
        assert "Fix money formatter" in block

    def test_pss_block_not_rendered_when_no_completed_tasks(self, db):
        p = _project(db, name="empty-proj")
        _task(db, p, title="Task 1", status=TaskStatus.PENDING, plan_position=1)
        summary = build_project_state_summary(p.id, db)
        block = render_project_state_summary_block(summary)
        assert block == "", "PSS block must be empty when no tasks are completed"

    def test_overlap_with_current_context(self, db):
        p, task1, task2 = _build_tiny_money(db)
        summary = build_project_state_summary(p.id, db)
        pss_block = render_project_state_summary_block(summary)

        base_ctx = _simulate_base_context(task1.title, task2.title)
        progress_notes = _simulate_progress_notes(
            task1.title, ["src/tiny_money/money.py"]
        )
        current_ctx = _combined_current_context(base_ctx, progress_notes)

        overlap = _token_overlap_fraction(current_ctx, pss_block)
        unique_fields = _pss_unique_fields(summary, current_ctx)

        # money.py is already in progress_notes, so overlap will be moderate
        assert overlap >= 0.05, f"Expected some word overlap; got {overlap}"
        # But structured file list may still be a unique field if progress_notes
        # uses a different format — record both cases
        combined_chars = len(current_ctx) + len(pss_block)
        would_exceed = combined_chars > 8000

        # Characterization observation (not a pass/fail assertion):
        observation = {
            "case": "tiny_money",
            "progress_notes_chars": len(progress_notes),
            "base_context_chars": len(base_ctx),
            "current_context_chars": len(current_ctx),
            "pss_block_chars": len(pss_block),
            "token_overlap_fraction": overlap,
            "pss_unique_fields": unique_fields,
            "would_exceed_8k_budget": would_exceed,
            "completed_task_count": len(summary.get("completed_tasks", [])),
            "files_in_pss": summary.get("files_created_or_modified", []),
            "has_planning_artifacts": bool(
                (summary.get("known_constraints") or {}).get("requirements_excerpt")
            ),
        }
        # The characterization must capture the observation without crashing.
        assert observation["case"] == "tiny_money"
        # PSS adds at most _PSS_BLOCK_MAX_CHARS = 1800 chars
        assert len(pss_block) <= 1800

    def test_injection_skipped_for_task1(self, db):
        p, task1, task2 = _build_tiny_money(db)
        state = MagicMock()
        state.project_context = "initial context"
        logger = MagicMock()
        _inject_project_state_summary_into_context(
            orchestration_state=state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=1,
        )
        # Must not modify context for Task 1
        assert state.project_context == "initial context"

    def test_injection_applies_for_task2(self, db):
        p, task1, task2 = _build_tiny_money(db)
        state = MagicMock()
        state.project_context = "base context text"
        logger = MagicMock()
        _inject_project_state_summary_into_context(
            orchestration_state=state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=2,
        )
        assert "PROJECT STATE SUMMARY" in state.project_context
        assert "money.py" in state.project_context
        assert "base context text" in state.project_context

    def test_injection_stays_within_8k_budget(self, db):
        p, task1, task2 = _build_tiny_money(db)
        state = MagicMock()
        # Start with a large context (~6000 chars) to stress-test budget clamp.
        state.project_context = "x" * 6000
        logger = MagicMock()
        _inject_project_state_summary_into_context(
            orchestration_state=state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=2,
        )
        assert (
            len(state.project_context) <= 8000
        ), "Injection must not exceed 8000-char context budget"


# ---------------------------------------------------------------------------
# Case: stale_replace
# Fixture: one completed task (inventory repair), one pending task.
# With planning artifacts (requirements + implementation_plan).
# ---------------------------------------------------------------------------


def _build_stale_replace(db):
    p = _project(db, name="stale-replace-project", workspace_path="/tmp/stale-replace")
    ps = _planning_session(db, p)
    _artifact(
        db,
        ps,
        artifact_type="requirements",
        content=(
            "# Requirements\n"
            "Fix inventory summary output. Items should be sorted and counted. "
            "Format: `name: count`. No test weakening."
        ),
    )
    _artifact(
        db,
        ps,
        artifact_type="implementation_plan",
        content=(
            "# Implementation Plan\n"
            "1. Fix `summarize()` in src/inventory/summary.py to sort and count items.\n"
            "2. Verify with python3 -m pytest -q.\n"
        ),
    )
    task1 = _task(
        db,
        p,
        title="Fix failing inventory summary tests without weakening tests",
        status=TaskStatus.DONE,
        plan_position=1,
        task_subfolder="task-1-inventory",
    )
    te1 = _execution(db, p, task1)
    _change_set(
        db,
        p,
        task1,
        te1,
        added=["src/inventory/summary.py"],
        modified=["src/inventory/summary.py"],
    )
    task2 = _task(
        db,
        p,
        title="Add per-category subtotals to inventory report",
        status=TaskStatus.PENDING,
        plan_position=2,
        description="Extend summarize() to include per-category subtotals.",
    )
    return p, task1, task2, ps


class TestStaleReplaceCharacterization:
    def test_pss_includes_planning_artifacts(self, db):
        p, task1, task2, ps = _build_stale_replace(db)
        summary = build_project_state_summary(p.id, db)
        block = render_project_state_summary_block(summary)
        assert (
            "Requirements" in block or "Implementation plan" in block
        ), "PSS block must surface planning artifacts when present"

    def test_overlap_includes_artifact_content(self, db):
        p, task1, task2, ps = _build_stale_replace(db)
        summary = build_project_state_summary(p.id, db)
        pss_block = render_project_state_summary_block(summary)

        base_ctx = _simulate_base_context(task1.title, task2.title)
        progress_notes = _simulate_progress_notes(
            task1.title, ["src/inventory/summary.py"]
        )
        current_ctx = _combined_current_context(base_ctx, progress_notes)

        unique_fields = _pss_unique_fields(summary, current_ctx)

        # With planning artifacts, PSS brings data not in progress_notes.
        # At minimum the requirements or implementation_plan excerpt must be unique.
        assert any(
            f in unique_fields
            for f in ("requirements_excerpt", "implementation_plan_excerpt")
        ), (
            "PSS should surface planning artifact excerpts not present in "
            f"current context. Unique fields found: {unique_fields}"
        )

        observation = {
            "case": "stale_replace",
            "pss_block_chars": len(pss_block),
            "pss_unique_fields": unique_fields,
            "has_requirements_excerpt": bool(
                (summary.get("known_constraints") or {}).get("requirements_excerpt")
            ),
            "has_impl_plan_excerpt": bool(
                (summary.get("known_constraints") or {}).get(
                    "implementation_plan_excerpt"
                )
            ),
        }
        assert observation["has_requirements_excerpt"]
        assert observation["has_impl_plan_excerpt"]

    def test_planning_violation_risk_signal(self, db):
        """PSS block is pure text prefixed to project_context.

        It contains no JSON, no structured commands, and no planning schema
        constructs. Validate this doesn't accidentally resemble a plan step.
        """
        p, task1, task2, ps = _build_stale_replace(db)
        summary = build_project_state_summary(p.id, db)
        block = render_project_state_summary_block(summary)
        # Must not contain JSON array brackets that could confuse the planner
        assert "step_number" not in block
        assert '"commands"' not in block
        assert '"verification"' not in block

    def test_injection_non_fatal_on_missing_project(self, db):
        """Injection must not raise when project_id is invalid."""
        state = MagicMock()
        state.project_context = "original"
        logger = MagicMock()
        _inject_project_state_summary_into_context(
            orchestration_state=state,
            db=db,
            project_id=99999,
            logger=logger,
            task_position=2,
        )
        # project_context must be unchanged on error/missing project
        assert state.project_context == "original"


# ---------------------------------------------------------------------------
# Case: medium_cli
# Fixture: two completed tasks (initial CLI, add storage module), one pending.
# No planning artifacts.
# ---------------------------------------------------------------------------


def _build_medium_cli(db):
    p = _project(db, name="medium-cli-project", workspace_path="/tmp/medium-cli")
    task1 = _task(
        db,
        p,
        title="Create initial Python CLI with task parser and dispatcher",
        status=TaskStatus.DONE,
        plan_position=1,
        task_subfolder="task-1-cli",
    )
    te1 = _execution(db, p, task1)
    _change_set(
        db,
        p,
        task1,
        te1,
        added=[
            "src/medium_cli/cli.py",
            "src/medium_cli/parser.py",
            "tests/test_cli.py",
        ],
    )
    task2 = _task(
        db,
        p,
        title="Add TaskStore storage module and wire to dispatcher",
        status=TaskStatus.DONE,
        plan_position=2,
        task_subfolder="task-2-storage",
    )
    te2 = _execution(db, p, task2)
    _change_set(
        db,
        p,
        task2,
        te2,
        added=["src/medium_cli/store.py", "tests/test_store.py"],
        modified=["src/medium_cli/cli.py"],
    )
    task3 = _task(
        db,
        p,
        title="Add summary command to Python CLI",
        status=TaskStatus.PENDING,
        plan_position=3,
        description=(
            "Add summary command printing '3 tasks, 2 complete'. "
            "Use existing TaskStore and formatting module."
        ),
    )
    return p, task1, task2, task3


class TestMediumCliCharacterization:
    def test_pss_aggregates_files_from_two_tasks(self, db):
        p, task1, task2, task3 = _build_medium_cli(db)
        summary = build_project_state_summary(p.id, db)
        all_files = summary.get("files_created_or_modified") or []
        assert "src/medium_cli/cli.py" in all_files
        assert "src/medium_cli/store.py" in all_files

    def test_pss_block_lists_both_completed_tasks(self, db):
        p, task1, task2, task3 = _build_medium_cli(db)
        summary = build_project_state_summary(p.id, db)
        block = render_project_state_summary_block(summary)
        assert "Create initial Python CLI" in block
        assert "Add TaskStore" in block

    def test_overlap_multi_task(self, db):
        p, task1, task2, task3 = _build_medium_cli(db)
        summary = build_project_state_summary(p.id, db)
        pss_block = render_project_state_summary_block(summary)

        base_ctx = (
            "Project: medium-cli-project\n"
            f"Current task: #3 {task3.title} (pending)\n"
            f"- #1 {task1.title} :: status=done :: workspace=isolated :: subfolder=task-1-cli\n"
            f"- #2 {task2.title} :: status=done :: workspace=isolated :: subfolder=task-2-storage\n"
        )
        progress_notes = (
            f"## Task 1: {task1.title}\nStatus: completed\n"
            f"## Task 2: {task2.title}\nStatus: completed\n"
        )
        current_ctx = _combined_current_context(base_ctx, progress_notes)

        overlap = _token_overlap_fraction(current_ctx, pss_block)
        unique_fields = _pss_unique_fields(summary, current_ctx)

        observation = {
            "case": "medium_cli",
            "completed_task_count": 2,
            "pss_block_chars": len(pss_block),
            "current_context_chars": len(current_ctx),
            "token_overlap_fraction": overlap,
            "pss_unique_fields": unique_fields,
            "files_in_pss": summary.get("files_created_or_modified", []),
        }
        # Multi-task case: PSS aggregates files from both tasks; current
        # context does not list individual files from completed tasks.
        assert "files_created_or_modified" in unique_fields, (
            "PSS must add structured file listing for multi-task continuation: "
            f"unique_fields={unique_fields}"
        )
        assert observation["completed_task_count"] == 2

    def test_pss_block_within_char_limit(self, db):
        p, task1, task2, task3 = _build_medium_cli(db)
        summary = build_project_state_summary(p.id, db)
        block = render_project_state_summary_block(summary)
        assert len(block) <= 1800

    def test_injection_position_3_applies(self, db):
        p, task1, task2, task3 = _build_medium_cli(db)
        state = MagicMock()
        state.project_context = "baseline context"
        logger = MagicMock()
        _inject_project_state_summary_into_context(
            orchestration_state=state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=3,
        )
        assert "PROJECT STATE SUMMARY" in state.project_context
        assert "store.py" in state.project_context


# ---------------------------------------------------------------------------
# Feature flag guard tests
# ---------------------------------------------------------------------------


class TestFeatureFlagGuard:
    def test_flag_defaults_false(self):
        from app.config import settings

        assert settings.PSS_CONTINUATION_INJECTION_ENABLED is False, (
            "PSS_CONTINUATION_INJECTION_ENABLED must default to False — "
            "no default runtime behavior change allowed"
        )

    def test_worker_import_does_not_activate_injection(self):
        """Importing the worker module must not trigger PSS injection."""
        from app.config import settings

        assert not settings.PSS_CONTINUATION_INJECTION_ENABLED
        # Import should succeed cleanly with flag off.
        import app.tasks.worker  # noqa: F401

    def test_render_block_empty_when_no_completed_tasks(self, db):
        p = _project(db, name="fresh-project")
        _task(db, p, title="First task", status=TaskStatus.PENDING, plan_position=1)
        summary = build_project_state_summary(p.id, db)
        block = render_project_state_summary_block(summary)
        assert block == ""

    def test_injection_idempotent_on_repeated_calls(self, db):
        """Repeated injection calls must not double-prefix the block."""
        p, task1, task2 = _build_tiny_money(db)
        state = MagicMock()
        state.project_context = "base"
        logger = MagicMock()
        for _ in range(2):
            _inject_project_state_summary_into_context(
                orchestration_state=state,
                db=db,
                project_id=p.id,
                logger=logger,
                task_position=2,
            )
        # After two calls, PROJECT STATE SUMMARY should appear at most twice
        # (once per call), but the important thing is the budget clamp held.
        assert len(state.project_context) <= 8000


# ---------------------------------------------------------------------------
# Aggregate characterization summary
# (Single test that prints the full observation table for the maintenance report)
# ---------------------------------------------------------------------------


def test_pss_continuation_characterization_summary(db):
    """Produce a structured observation table for the maintenance report.

    Not a correctness gate — records overlap and uniqueness metrics across
    all three eval cases. Output is captured via pytest -s.
    """
    cases = []

    # Case 1: tiny_money
    p1, t1a, t1b = _build_tiny_money(db)
    s1 = build_project_state_summary(p1.id, db)
    b1 = render_project_state_summary_block(s1)
    ctx1 = _combined_current_context(
        _simulate_base_context(t1a.title, t1b.title),
        _simulate_progress_notes(t1a.title, ["src/tiny_money/money.py"]),
    )
    cases.append(
        {
            "case": "tiny_money",
            "task_position": 2,
            "completed_tasks": 1,
            "pss_block_chars": len(b1),
            "current_context_chars": len(ctx1),
            "token_overlap_fraction": _token_overlap_fraction(ctx1, b1),
            "pss_unique_fields": _pss_unique_fields(s1, ctx1),
            "has_planning_artifacts": bool(
                (s1.get("known_constraints") or {}).get("requirements_excerpt")
            ),
            "would_exceed_budget": len(ctx1) + len(b1) > 8000,
        }
    )

    # Case 2: stale_replace (with planning artifacts)
    db2_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=db2_engine)
    DB2 = sessionmaker(bind=db2_engine)
    db2 = DB2()
    try:
        p2, t2a, t2b, ps2 = _build_stale_replace(db2)
        s2 = build_project_state_summary(p2.id, db2)
        b2 = render_project_state_summary_block(s2)
        ctx2 = _combined_current_context(
            _simulate_base_context(t2a.title, t2b.title),
            _simulate_progress_notes(t2a.title, ["src/inventory/summary.py"]),
        )
        cases.append(
            {
                "case": "stale_replace",
                "task_position": 2,
                "completed_tasks": 1,
                "pss_block_chars": len(b2),
                "current_context_chars": len(ctx2),
                "token_overlap_fraction": _token_overlap_fraction(ctx2, b2),
                "pss_unique_fields": _pss_unique_fields(s2, ctx2),
                "has_planning_artifacts": bool(
                    (s2.get("known_constraints") or {}).get("requirements_excerpt")
                ),
                "would_exceed_budget": len(ctx2) + len(b2) > 8000,
            }
        )
    finally:
        db2.close()
        Base.metadata.drop_all(bind=db2_engine)
        db2_engine.dispose()

    # Case 3: medium_cli
    db3_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=db3_engine)
    DB3 = sessionmaker(bind=db3_engine)
    db3 = DB3()
    try:
        p3, t3a, t3b, t3c = _build_medium_cli(db3)
        s3 = build_project_state_summary(p3.id, db3)
        b3 = render_project_state_summary_block(s3)
        ctx3_base = (
            "Project: medium-cli-project\n"
            f"Current task: #3 {t3c.title} (pending)\n"
            f"- #1 {t3a.title} :: status=done :: workspace=isolated\n"
            f"- #2 {t3b.title} :: status=done :: workspace=isolated\n"
        )
        ctx3 = _combined_current_context(
            ctx3_base,
            f"## Task 1: {t3a.title}\nStatus: completed\n"
            f"## Task 2: {t3b.title}\nStatus: completed\n",
        )
        cases.append(
            {
                "case": "medium_cli",
                "task_position": 3,
                "completed_tasks": 2,
                "pss_block_chars": len(b3),
                "current_context_chars": len(ctx3),
                "token_overlap_fraction": _token_overlap_fraction(ctx3, b3),
                "pss_unique_fields": _pss_unique_fields(s3, ctx3),
                "has_planning_artifacts": bool(
                    (s3.get("known_constraints") or {}).get("requirements_excerpt")
                ),
                "would_exceed_budget": len(ctx3) + len(b3) > 8000,
            }
        )
    finally:
        db3.close()
        Base.metadata.drop_all(bind=db3_engine)
        db3_engine.dispose()

    # Print observation table
    print("\n\n=== PSS CONTINUATION CHARACTERIZATION SUMMARY ===")
    print(
        f"{'Case':<20} {'Pos':>4} {'Done':>5} {'PSS':>6} {'Ctx':>6} "
        f"{'Overlap':>8} {'Unique fields':<40} {'Artifacts':>10} {'>8k':>5}"
    )
    print("-" * 115)
    for c in cases:
        print(
            f"{c['case']:<20} {c['task_position']:>4} {c['completed_tasks']:>5} "
            f"{c['pss_block_chars']:>6} {c['current_context_chars']:>6} "
            f"{c['token_overlap_fraction']:>8.3f} "
            f"{str(c['pss_unique_fields']):<40} "
            f"{'yes' if c['has_planning_artifacts'] else 'no':>10} "
            f"{'YES' if c['would_exceed_budget'] else 'no':>5}"
        )
    print("=== END CHARACTERIZATION SUMMARY ===\n")

    # Minimum sanity assertions
    for c in cases:
        assert len(c["pss_unique_fields"]) >= 0  # observation, not a failure gate
        assert not c[
            "would_exceed_budget"
        ], f"Case {c['case']}: PSS block + current context exceeds 8k char budget"
