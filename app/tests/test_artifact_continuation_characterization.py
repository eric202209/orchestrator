"""Characterization tests: Artifact-driven Continuation (Priority 7).

Objective: confirm that requirements_excerpt and implementation_plan_excerpt are
delivered to the planning prompt via a dedicated post-shaping block that bypasses
the _shape_project_context() 400c gate, and that the PSS block correctly suppresses
artifact lines when ARTIFACT_CONTINUATION_ENABLED=True.

Constraints:
- No live model calls.
- No changes to validator, planning schema, repair logic, or execution.
- ARTIFACT_CONTINUATION_ENABLED defaults False.
- All assertions are read-only observations about context content.

Cases:
  artifact_heavy   : two artifacts (requirements + impl_plan), one completed task
  requirements_only: requirements artifact only, no impl_plan
  no_artifacts     : no planning artifacts at all
  task1_skip       : Task 1 — artifact injection must be skipped
  duplication_check: PSS suppresses artifact lines when include_artifacts=False
  budget_cap       : artifact block must stay within _ARTIFACT_BLOCK_MAX_CHARS
"""

from __future__ import annotations

from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

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
    build_project_artifact_block,
    build_project_state_summary,
    render_project_state_summary_block,
    _inject_project_artifacts_into_context,
    _inject_project_state_summary_into_context,
    _ARTIFACT_BLOCK_MAX_CHARS,
    _ARTIFACT_BLOCK_REQUIREMENTS_CHARS,
    _ARTIFACT_BLOCK_IMPL_PLAN_CHARS,
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
# Builder helpers (shared with pss characterization test pattern)
# ---------------------------------------------------------------------------

_session_counter = 0


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
) -> Task:
    t = Task(
        project_id=project.id,
        title=title,
        status=status,
        plan_position=plan_position,
    )
    db.add(t)
    db.flush()
    return t


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
) -> TaskExecutionChangeSet:
    cs = TaskExecutionChangeSet(
        project_id=project.id,
        task_id=task.id,
        task_execution_id=te.id,
        base_snapshot_key="snap",
        added_files=added,
        modified_files=[],
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
        filename=f"{artifact_type}.md",
        version=1,
        content=content,
        is_latest=is_latest,
    )
    db.add(a)
    db.flush()
    return a


# ---------------------------------------------------------------------------
# Fixture factories
# ---------------------------------------------------------------------------

_REQ_CONTENT = (
    "Sort results alphabetically. Count items only when explicitly requested. "
    "Do not weaken any existing test. Return item count in the response envelope "
    "only when count=true param is set. Never remove existing endpoints."
)

_IMPL_CONTENT = (
    "Use sorted() builtin for title sort. Keep all existing tests unchanged. "
    "Add count field only when count param is present. Return 404 for missing resources."
)


def _build_artifact_heavy(db):
    """Project with requirements + impl_plan artifacts, one completed task."""
    p = _project(db, name="stale-replace-api", workspace_path="/tmp/stale")
    ps = _planning_session(db, p)
    _artifact(db, ps, artifact_type="requirements", content=_REQ_CONTENT)
    _artifact(db, ps, artifact_type="implementation_plan", content=_IMPL_CONTENT)
    task1 = _task(
        db,
        p,
        title="Add /replace endpoint with sorted output",
        status=TaskStatus.DONE,
        plan_position=1,
    )
    te1 = _execution(db, p, task1)
    _change_set(db, p, task1, te1, added=["api.py", "tests/test_api.py"])
    task2 = _task(
        db,
        p,
        title="Add pagination to /replace endpoint",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    return p, task1, task2, ps


def _build_requirements_only(db):
    """Project with requirements artifact only, no impl_plan."""
    p = _project(db, name="req-only-api", workspace_path="/tmp/req")
    ps = _planning_session(db, p)
    _artifact(db, ps, artifact_type="requirements", content=_REQ_CONTENT)
    task1 = _task(
        db,
        p,
        title="Create initial endpoint",
        status=TaskStatus.DONE,
        plan_position=1,
    )
    te1 = _execution(db, p, task1)
    _change_set(db, p, task1, te1, added=["app.py"])
    task2 = _task(
        db,
        p,
        title="Add filtering",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    return p, task1, task2, ps


def _build_no_artifacts(db):
    """Project with no planning artifacts."""
    p = _project(db, name="no-artifacts-proj", workspace_path="/tmp/bare")
    task1 = _task(
        db,
        p,
        title="Create money.py",
        status=TaskStatus.DONE,
        plan_position=1,
    )
    te1 = _execution(db, p, task1)
    _change_set(db, p, task1, te1, added=["money.py"])
    task2 = _task(
        db,
        p,
        title="Add unit tests",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    return p, task1, task2


# ---------------------------------------------------------------------------
# Case: artifact-heavy project
# ---------------------------------------------------------------------------


class TestArtifactHeavyProject:
    def test_build_project_artifact_block_contains_requirements(self, db):
        p, task1, task2, ps = _build_artifact_heavy(db)
        block = build_project_artifact_block(p.id, db)
        assert block, "Artifact block must be non-empty for artifact-heavy project"
        assert "=== PROJECT ARTIFACTS ===" in block
        assert "=== END PROJECT ARTIFACTS ===" in block
        assert "Requirements:" in block
        # Key constraint language must survive
        assert "Do not weaken" in block or "count=true" in block

    def test_build_project_artifact_block_contains_impl_plan(self, db):
        p, task1, task2, ps = _build_artifact_heavy(db)
        block = build_project_artifact_block(p.id, db)
        assert "Implementation plan:" in block
        assert "sorted()" in block or "Keep all existing tests" in block

    def test_artifact_block_within_budget(self, db):
        p, task1, task2, ps = _build_artifact_heavy(db)
        block = build_project_artifact_block(p.id, db)
        assert (
            len(block) <= _ARTIFACT_BLOCK_MAX_CHARS
        ), f"Artifact block {len(block)}c exceeds budget {_ARTIFACT_BLOCK_MAX_CHARS}c"

    def test_artifact_block_does_not_contain_json_schema(self, db):
        p, task1, task2, ps = _build_artifact_heavy(db)
        block = build_project_artifact_block(p.id, db)
        # Must not accidentally look like a plan step
        assert "step_number" not in block
        assert '"commands"' not in block
        assert '"verification"' not in block

    def test_inject_artifacts_sets_artifact_supplement(self, db):
        p, task1, task2, ps = _build_artifact_heavy(db)
        state = MagicMock()
        state.artifact_supplement = None
        logger = MagicMock()

        _inject_project_artifacts_into_context(
            orchestration_state=state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=2,
        )

        assert state.artifact_supplement is not None
        assert "PROJECT ARTIFACTS" in state.artifact_supplement
        assert "Requirements:" in state.artifact_supplement

    def test_inject_artifacts_logs_marker(self, db):
        p, task1, task2, ps = _build_artifact_heavy(db)
        state = MagicMock()
        state.artifact_supplement = None
        logger = MagicMock()

        _inject_project_artifacts_into_context(
            orchestration_state=state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=2,
        )

        logger.info.assert_called_once()
        call_msg = logger.info.call_args[0][0]
        assert "[ARTIFACT_INJECT]" in call_msg


# ---------------------------------------------------------------------------
# Case: requirements-only project
# ---------------------------------------------------------------------------


class TestRequirementsOnlyProject:
    def test_artifact_block_has_requirements_no_impl_plan(self, db):
        p, task1, task2, ps = _build_requirements_only(db)
        block = build_project_artifact_block(p.id, db)
        assert "Requirements:" in block
        assert "Implementation plan:" not in block

    def test_artifact_block_within_budget(self, db):
        p, task1, task2, ps = _build_requirements_only(db)
        block = build_project_artifact_block(p.id, db)
        assert len(block) <= _ARTIFACT_BLOCK_MAX_CHARS

    def test_inject_fires_for_requirements_only(self, db):
        p, task1, task2, ps = _build_requirements_only(db)
        state = MagicMock()
        state.artifact_supplement = None
        logger = MagicMock()

        _inject_project_artifacts_into_context(
            orchestration_state=state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=2,
        )

        assert state.artifact_supplement is not None
        assert "Requirements:" in state.artifact_supplement


# ---------------------------------------------------------------------------
# Case: no-artifact project
# ---------------------------------------------------------------------------


class TestNoArtifactProject:
    def test_artifact_block_empty_for_no_artifacts(self, db):
        p, task1, task2 = _build_no_artifacts(db)
        block = build_project_artifact_block(p.id, db)
        assert (
            block == ""
        ), "Artifact block must be empty when no planning artifacts exist"

    def test_inject_skipped_for_no_artifacts(self, db):
        p, task1, task2 = _build_no_artifacts(db)
        state = MagicMock()
        state.artifact_supplement = None
        logger = MagicMock()

        _inject_project_artifacts_into_context(
            orchestration_state=state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=2,
        )

        assert (
            state.artifact_supplement is None
        ), "artifact_supplement must not be set when no artifacts exist"
        logger.info.assert_not_called()

    def test_inject_skipped_for_missing_project(self, db):
        state = MagicMock()
        state.artifact_supplement = None
        logger = MagicMock()

        _inject_project_artifacts_into_context(
            orchestration_state=state,
            db=db,
            project_id=99999,
            logger=logger,
            task_position=2,
        )

        assert state.artifact_supplement is None


# ---------------------------------------------------------------------------
# Case: Task 1 skip
# ---------------------------------------------------------------------------


class TestTask1Skip:
    def test_inject_skipped_for_task_position_1(self, db):
        p, task1, task2, ps = _build_artifact_heavy(db)
        state = MagicMock()
        state.artifact_supplement = None
        logger = MagicMock()

        _inject_project_artifacts_into_context(
            orchestration_state=state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=1,
        )

        assert (
            state.artifact_supplement is None
        ), "Artifact injection must be skipped for Task 1"
        logger.info.assert_not_called()

    def test_inject_fires_for_task_position_none(self, db):
        """task_position=None (no plan_position set) must not be treated as Task 1."""
        p, task1, task2, ps = _build_artifact_heavy(db)
        state = MagicMock()
        state.artifact_supplement = None
        logger = MagicMock()

        _inject_project_artifacts_into_context(
            orchestration_state=state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=None,
        )

        assert state.artifact_supplement is not None

    def test_inject_fires_for_task_position_2(self, db):
        p, task1, task2, ps = _build_artifact_heavy(db)
        state = MagicMock()
        state.artifact_supplement = None
        logger = MagicMock()

        _inject_project_artifacts_into_context(
            orchestration_state=state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=2,
        )

        assert state.artifact_supplement is not None

    def test_inject_fires_for_task_position_4(self, db):
        p, task1, task2, ps = _build_artifact_heavy(db)
        state = MagicMock()
        state.artifact_supplement = None
        logger = MagicMock()

        _inject_project_artifacts_into_context(
            orchestration_state=state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=4,
        )

        assert state.artifact_supplement is not None


# ---------------------------------------------------------------------------
# Case: duplication suppression (include_artifacts=False)
# ---------------------------------------------------------------------------


class TestDuplicationSuppression:
    def test_pss_excludes_artifacts_when_include_artifacts_false(self, db):
        p, task1, task2, ps = _build_artifact_heavy(db)
        summary = build_project_state_summary(p.id, db)

        block_with = render_project_state_summary_block(summary, include_artifacts=True)
        block_without = render_project_state_summary_block(
            summary, include_artifacts=False
        )

        assert (
            "Requirements:" in block_with
        ), "include_artifacts=True must render requirements"
        assert (
            "Requirements:" not in block_without
        ), "include_artifacts=False must suppress requirements from PSS"
        assert "Implementation plan:" in block_with
        assert "Implementation plan:" not in block_without

    def test_pss_still_contains_task_history_when_artifacts_suppressed(self, db):
        p, task1, task2, ps = _build_artifact_heavy(db)
        summary = build_project_state_summary(p.id, db)

        block_without = render_project_state_summary_block(
            summary, include_artifacts=False
        )

        # Task history must survive even when artifacts are suppressed
        assert "PROJECT STATE SUMMARY" in block_without
        assert "Add /replace endpoint" in block_without
        assert "api.py" in block_without

    def test_pss_inject_passes_include_artifacts_false(self, db):
        """PSS injection with include_artifacts=False must not include artifact lines."""
        p, task1, task2, ps = _build_artifact_heavy(db)
        state = MagicMock()
        state.project_context = "base context"
        logger = MagicMock()

        _inject_project_state_summary_into_context(
            orchestration_state=state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=2,
            include_artifacts=False,
        )

        assert "PROJECT STATE SUMMARY" in state.project_context
        assert "api.py" in state.project_context
        # Artifact lines must be absent
        assert "Requirements:" not in state.project_context
        assert "Implementation plan:" not in state.project_context

    def test_no_duplication_when_both_injections_simulated(self, db):
        """Simulate the combined ARTIFACT_CONTINUATION_ENABLED=True scenario.

        PSS runs with include_artifacts=False; artifact block has artifacts.
        The planning prompt must contain Requirements exactly once.
        """
        p, task1, task2, ps = _build_artifact_heavy(db)

        # Step 1: PSS injection (include_artifacts=False)
        pss_state = MagicMock()
        pss_state.project_context = "base context"
        logger = MagicMock()
        _inject_project_state_summary_into_context(
            orchestration_state=pss_state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=2,
            include_artifacts=False,
        )

        # Step 2: Artifact injection
        art_state = MagicMock()
        art_state.artifact_supplement = None
        _inject_project_artifacts_into_context(
            orchestration_state=art_state,
            db=db,
            project_id=p.id,
            logger=logger,
            task_position=2,
        )

        # Simulate what assemble_planning_prompt does:
        # artifact_supplement prepended to raw_prompt;
        # PSS is in project_context (shaped separately)
        simulated_prompt = (
            (art_state.artifact_supplement or "") + "\n\n" + pss_state.project_context
        )

        requirements_count = simulated_prompt.count("Requirements:")
        assert requirements_count == 1, (
            f"Requirements: must appear exactly once in combined prompt; "
            f"found {requirements_count} times"
        )


# ---------------------------------------------------------------------------
# Case: prompt budget cap
# ---------------------------------------------------------------------------


class TestPromptBudgetCap:
    def test_artifact_block_respects_max_chars(self, db):
        p, task1, task2, ps = _build_artifact_heavy(db)
        block = build_project_artifact_block(p.id, db)
        assert len(block) <= _ARTIFACT_BLOCK_MAX_CHARS

    def test_artifact_block_custom_max_chars(self, db):
        p, task1, task2, ps = _build_artifact_heavy(db)
        block = build_project_artifact_block(p.id, db, max_chars=100)
        assert len(block) <= 100

    def test_requirements_excerpt_capped_at_300c(self, db):
        p = _project(db, name="long-req-proj")
        ps = _planning_session(db, p)
        long_req = "X" * 500
        _artifact(db, ps, artifact_type="requirements", content=long_req)
        task1 = _task(db, p, title="Task", status=TaskStatus.DONE, plan_position=1)
        te1 = _execution(db, p, task1)
        _change_set(db, p, task1, te1, added=["file.py"])

        block = build_project_artifact_block(p.id, db)
        # The requirements line must not contain all 500 Xs
        req_line = next(
            (line for line in block.splitlines() if l.startswith("Requirements:")), ""
        )
        req_content = req_line[len("Requirements:") :].strip()
        assert len(req_content) <= _ARTIFACT_BLOCK_REQUIREMENTS_CHARS, (
            f"requirements_excerpt must be capped at {_ARTIFACT_BLOCK_REQUIREMENTS_CHARS}c; "
            f"got {len(req_content)}c"
        )

    def test_impl_plan_excerpt_capped_at_200c(self, db):
        p = _project(db, name="long-impl-proj")
        ps = _planning_session(db, p)
        long_impl = "Y" * 400
        _artifact(db, ps, artifact_type="implementation_plan", content=long_impl)
        task1 = _task(db, p, title="Task", status=TaskStatus.DONE, plan_position=1)
        te1 = _execution(db, p, task1)
        _change_set(db, p, task1, te1, added=["file.py"])

        block = build_project_artifact_block(p.id, db)
        impl_line = next(
            (
                line
                for line in block.splitlines()
                if l.startswith("Implementation plan:")
            ),
            "",
        )
        impl_content = impl_line[len("Implementation plan:") :].strip()
        assert len(impl_content) <= _ARTIFACT_BLOCK_IMPL_PLAN_CHARS, (
            f"implementation_plan_excerpt must be capped at {_ARTIFACT_BLOCK_IMPL_PLAN_CHARS}c; "
            f"got {len(impl_content)}c"
        )

    def test_artifact_block_prompt_size_safe(self, db):
        """Artifact block + typical planning prompt must stay under 12000c cap."""
        p, task1, task2, ps = _build_artifact_heavy(db)
        block = build_project_artifact_block(p.id, db)
        # Worst case observed prompt: 8591c (C1 live validation)
        simulated_existing_prompt_chars = 8600
        total = simulated_existing_prompt_chars + len(block) + 2  # +2 for "\n\n"
        assert total < 12000, (
            f"Artifact block ({len(block)}c) + simulated prompt ({simulated_existing_prompt_chars}c)"
            f" = {total}c exceeds 12000c planning cap"
        )


# ---------------------------------------------------------------------------
# Assembly integration: artifact_supplement injected post-shaping
# ---------------------------------------------------------------------------


class TestAssemblyIntegration:
    def test_artifact_supplement_prepended_to_raw_prompt(self):
        """assemble_planning_prompt prepends artifact_supplement before task description."""
        from app.services.orchestration.context.assembly import assemble_planning_prompt

        fake_supplement = "=== PROJECT ARTIFACTS ===\nRequirements: No test weakening.\n=== END PROJECT ARTIFACTS ==="

        state = MagicMock()
        state.project_context = ""
        state.artifact_supplement = fake_supplement
        state.project_dir = "/tmp/nonexistent_dir_for_test"
        state.phase_history = []
        state.validation_history = []
        state.session_id = 1
        state.task_id = 1

        ctx = MagicMock()
        ctx.orchestration_state = state
        ctx.db = MagicMock()
        ctx.prompt = "Add pagination endpoint"
        ctx.execution_profile = "full_lifecycle"
        ctx.workflow_profile = "default"

        with (
            patch(
                "app.services.orchestration.context.assembly.render_workspace_path_for_prompt",
                return_value="/tmp/test",
            ),
            patch(
                "app.services.orchestration.context.assembly.build_workspace_inventory_summary",
                return_value="",
            ),
            patch(
                "app.services.orchestration.context.assembly._recent_operator_guidance",
                return_value="",
            ),
            patch(
                "app.services.orchestration.context.assembly._condense_dict_events",
                return_value="",
            ),
            patch(
                "app.services.orchestration.context.assembly._build_project_structure_capsule",
                return_value="",
            ),
            patch(
                "app.services.orchestration.context.assembly.python_test_source_context_from_tests",
                return_value="",
            ),
            patch(
                "app.services.orchestration.context.assembly.render_adapted_runtime_prompt",
                side_effect=lambda db, **kwargs: kwargs.get("prompt_body", ""),
            ),
            patch(
                "app.services.orchestration.context.provenance._maybe_emit_provenance",
            ),
            patch(
                "app.services.orchestration.context.assembly.get_workflow_phases",
                return_value=[],
            ),
        ):
            result = assemble_planning_prompt(ctx, workspace_review={})

        assert (
            fake_supplement in result
        ), "artifact_supplement must be present in assembled planning prompt"
        assert result.startswith(fake_supplement), (
            "artifact_supplement must be at the start of raw_prompt "
            "(before task description)"
        )

    def test_no_artifact_supplement_leaves_prompt_unchanged(self):
        """When artifact_supplement is None, prompt assembly is unchanged."""
        from app.services.orchestration.context.assembly import assemble_planning_prompt

        state = MagicMock()
        state.project_context = ""
        state.artifact_supplement = None
        state.project_dir = "/tmp/nonexistent"
        state.phase_history = []
        state.validation_history = []
        state.session_id = 1
        state.task_id = 1

        ctx = MagicMock()
        ctx.orchestration_state = state
        ctx.db = MagicMock()
        ctx.prompt = "Create initial endpoint"
        ctx.execution_profile = "full_lifecycle"
        ctx.workflow_profile = "default"

        captured = {}

        def fake_build_planning_prompt(**kwargs):
            captured["called"] = True
            return "RAW_PROMPT_BODY"

        with (
            patch(
                "app.services.orchestration.context.assembly.render_workspace_path_for_prompt",
                return_value="/tmp/test",
            ),
            patch(
                "app.services.orchestration.context.assembly.build_workspace_inventory_summary",
                return_value="",
            ),
            patch(
                "app.services.orchestration.context.assembly._recent_operator_guidance",
                return_value="",
            ),
            patch(
                "app.services.orchestration.context.assembly._condense_dict_events",
                return_value="",
            ),
            patch(
                "app.services.orchestration.context.assembly._build_project_structure_capsule",
                return_value="",
            ),
            patch(
                "app.services.orchestration.context.assembly.python_test_source_context_from_tests",
                return_value="",
            ),
            patch(
                "app.services.orchestration.context.assembly.render_adapted_runtime_prompt",
                side_effect=lambda db, **kwargs: kwargs.get("prompt_body", ""),
            ),
            patch(
                "app.services.orchestration.context.provenance._maybe_emit_provenance",
            ),
            patch(
                "app.services.orchestration.context.assembly.get_workflow_phases",
                return_value=[],
            ),
        ):
            result = assemble_planning_prompt(ctx, workspace_review={})

        assert "PROJECT ARTIFACTS" not in result


# ---------------------------------------------------------------------------
# Feature flag guard
# ---------------------------------------------------------------------------


class TestFeatureFlagGuard:
    def test_artifact_continuation_flag_defaults_false(self):
        from app.config import settings

        assert settings.ARTIFACT_CONTINUATION_ENABLED is False, (
            "ARTIFACT_CONTINUATION_ENABLED must default to False — "
            "no default runtime behavior change allowed"
        )

    def test_pss_flag_still_defaults_false(self):
        from app.config import settings

        assert settings.PSS_CONTINUATION_INJECTION_ENABLED is False


# ---------------------------------------------------------------------------
# Aggregate characterization summary
# ---------------------------------------------------------------------------


def test_artifact_continuation_characterization_summary(db):
    """Produce a structured observation table for the maintenance report.

    Not a correctness gate — records artifact block sizes and information gap
    coverage across all eval cases.
    """
    cases = []

    # Case A: artifact-heavy
    p1, t1a, t1b, ps1 = _build_artifact_heavy(db)
    block1 = build_project_artifact_block(p1.id, db)
    pss_summary1 = build_project_state_summary(p1.id, db)
    pss_with = render_project_state_summary_block(pss_summary1, include_artifacts=True)
    pss_without = render_project_state_summary_block(
        pss_summary1, include_artifacts=False
    )
    cases.append(
        {
            "case": "artifact_heavy",
            "task_position": 2,
            "artifact_block_chars": len(block1),
            "pss_with_artifacts_chars": len(pss_with),
            "pss_without_artifacts_chars": len(pss_without),
            "artifact_block_has_requirements": "Requirements:" in block1,
            "artifact_block_has_impl_plan": "Implementation plan:" in block1,
            "pss_suppresses_requirements": "Requirements:" not in pss_without,
            "within_budget": len(block1) <= _ARTIFACT_BLOCK_MAX_CHARS,
            "total_with_typical_prompt": len(block1) + 8600,
            "under_12k_cap": len(block1) + 8600 < 12000,
        }
    )

    # Case B: requirements-only
    engine2 = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine2)
    DB2 = sessionmaker(bind=engine2)
    db2 = DB2()
    try:
        p2, t2a, t2b, ps2 = _build_requirements_only(db2)
        block2 = build_project_artifact_block(p2.id, db2)
        cases.append(
            {
                "case": "requirements_only",
                "task_position": 2,
                "artifact_block_chars": len(block2),
                "pss_with_artifacts_chars": 0,
                "pss_without_artifacts_chars": 0,
                "artifact_block_has_requirements": "Requirements:" in block2,
                "artifact_block_has_impl_plan": "Implementation plan:" in block2,
                "pss_suppresses_requirements": True,
                "within_budget": len(block2) <= _ARTIFACT_BLOCK_MAX_CHARS,
                "total_with_typical_prompt": len(block2) + 8600,
                "under_12k_cap": len(block2) + 8600 < 12000,
            }
        )
    finally:
        db2.close()
        Base.metadata.drop_all(bind=engine2)
        engine2.dispose()

    # Case C: no artifacts
    engine3 = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine3)
    DB3 = sessionmaker(bind=engine3)
    db3 = DB3()
    try:
        p3, t3a, t3b = _build_no_artifacts(db3)
        block3 = build_project_artifact_block(p3.id, db3)
        cases.append(
            {
                "case": "no_artifacts",
                "task_position": 2,
                "artifact_block_chars": len(block3),
                "pss_with_artifacts_chars": 0,
                "pss_without_artifacts_chars": 0,
                "artifact_block_has_requirements": False,
                "artifact_block_has_impl_plan": False,
                "pss_suppresses_requirements": True,
                "within_budget": len(block3) == 0,
                "total_with_typical_prompt": len(block3) + 8600,
                "under_12k_cap": True,
            }
        )
    finally:
        db3.close()
        Base.metadata.drop_all(bind=engine3)
        engine3.dispose()

    # Print observation table
    print("\n\n=== ARTIFACT CONTINUATION CHARACTERIZATION SUMMARY ===")
    print(
        f"{'Case':<20} {'Pos':>4} {'ArtBlk':>7} {'PSSw':>6} {'PSSwo':>6} "
        f"{'Req':>4} {'Impl':>4} {'Supr':>5} {'<600':>5} {'<12k':>5}"
    )
    print("-" * 80)
    for c in cases:
        print(
            f"{c['case']:<20} {c['task_position']:>4} "
            f"{c['artifact_block_chars']:>7} "
            f"{c['pss_with_artifacts_chars']:>6} "
            f"{c['pss_without_artifacts_chars']:>6} "
            f"{'Y' if c['artifact_block_has_requirements'] else 'N':>4} "
            f"{'Y' if c['artifact_block_has_impl_plan'] else 'N':>4} "
            f"{'Y' if c['pss_suppresses_requirements'] else 'N':>5} "
            f"{'Y' if c['within_budget'] else 'N':>5} "
            f"{'Y' if c['under_12k_cap'] else 'N':>5}"
        )
    print("=== END ARTIFACT CONTINUATION CHARACTERIZATION SUMMARY ===\n")

    # Pass criteria
    for c in cases:
        assert c[
            "within_budget"
        ], f"Case {c['case']}: artifact block exceeds {_ARTIFACT_BLOCK_MAX_CHARS}c budget"
        assert c[
            "under_12k_cap"
        ], f"Case {c['case']}: artifact block + typical prompt exceeds 12000c cap"
