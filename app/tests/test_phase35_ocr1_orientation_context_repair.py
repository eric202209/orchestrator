"""Provider-free OCR1 tests: typed grounding regains bounded orientation paths.

QFR1 proved the typed grounding seam derived a bounded candidate path list and
then discarded it by projecting only ``RepositoryOrientation.as_details()``.
These tests pin the restored path visibility, the unchanged bounds, and the
unchanged authority of an oriented path.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from app.services.orchestration.phases.planning_grounding_integration import (
    run_typed_grounding_for_planning,
)
from app.services.orchestration.planning.grounding import (
    GroundingCoordinator,
    GroundingExecutor,
    GroundingRunConfig,
    GroundingTaskReference,
    GroundingTerminalReason,
)
from app.services.orchestration.planning.grounding.coordinator_contracts import (
    GroundingDecisionContext,
    GroundingProviderTurnMode,
)
from app.services.orchestration.planning.grounding.coordinator import (
    render_grounding_state,
)
from app.services.orchestration.planning.grounding.provider_adapter import (
    render_first_turn_prompt,
)
from app.services.orchestration.planning.repository_orientation import (
    ORIENTATION_BYTE_BUDGET,
    ORIENTATION_PATH_LIMIT,
    ORIENTATION_UNAVAILABLE_NOT_GIT,
    ORIENTATION_UNAVAILABLE_NO_CANDIDATES,
    derive_repository_orientation,
)


F2_TASK = (
    "Let me mark a project as dormant or on hold so it does not show up in my "
    "current project list."
)


def _repo(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=root, check=True, shell=False)
    return root


def _f2_repo(root: Path) -> Path:
    """A fixture shaped like the ODF2 F2 product vocabulary-drift task.

    The repository calls the state ``paused``; the task says ``dormant``.  No
    production module hard-codes any of these names.
    """

    return _repo(
        root,
        {
            "app/services/project/lifecycle.py": (
                "PAUSED = 'paused'\n\n\ndef pause_project(project):\n"
                "    project.status = PAUSED\n    return project\n"
            ),
            "app/services/project/state_summary.py": (
                "def summarize(project):\n    return {'status': project.status}\n"
            ),
            "app/api/v1/endpoints/projects.py": (
                "def list_projects(db):\n    return db.query('project').all()\n"
            ),
            "app/unrelated/telemetry.py": "COUNTER = 0\n",
        },
    )


def _advisory(root: Path, task: str) -> dict:
    return dict(derive_repository_orientation(root, task).as_provider_advisory())


def _context(root: Path, task: str) -> GroundingDecisionContext:
    config = GroundingRunConfig(
        grounding_run_id="ocr1-run",
        task_reference=GroundingTaskReference(task_id="task-ocr1"),
        workspace_identity=str(root.resolve()),
        snapshot_identity="snapshot-ocr1",
        max_steps=3,
        max_exploration_provider_requests=2,
        operator_task=task,
        orientation_advisory=_advisory(root, task),
    )
    state = GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity="snapshot-ocr1"),
        provider=_NeverCalled(),
        config=config,
    )._initial_state()
    return GroundingDecisionContext(
        state=state,
        rendered_grounding_state=render_grounding_state(state),
        operator_task=task,
        turn_mode=GroundingProviderTurnMode.EXPLORATION,
    )


class _NeverCalled:
    def decide(self, _context):  # pragma: no cover - guard only
        raise AssertionError("this test must not invoke a provider")


class _Provider:
    """Records the prompt it is shown, then terminates the run immediately."""

    def __init__(self):
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        from app.services.orchestration.planning.grounding.coordinator_contracts import (
            GroundingAssessmentKind,
            GroundingProposal,
        )

        return GroundingProposal(
            assessment_kind=GroundingAssessmentKind.INSUFFICIENT,
            rationale="ocr1 test stops here",
        )


# --------------------------------------------------------------------------
# T1/T2/T3 — the advisory carries bounded paths, keeps metadata, keeps order
# --------------------------------------------------------------------------


def test_t1_first_turn_advisory_contains_bounded_paths(tmp_path):
    root = _f2_repo(tmp_path)
    prompt = render_first_turn_prompt(_context(root, F2_TASK))
    orientation = derive_repository_orientation(root, F2_TASK)

    assert orientation.paths
    for path in orientation.paths:
        assert path in prompt


def test_t2_first_turn_advisory_preserves_existing_metadata(tmp_path):
    root = _f2_repo(tmp_path)
    advisory = _advisory(root, F2_TASK)

    for key in (
        "orientation_available",
        "orientation_scope",
        "orientation_entries_shown",
        "orientation_entries_total",
        "orientation_truncated",
        "orientation_bytes_used",
        "orientation_byte_budget",
        "orientation_unavailable_reason",
    ):
        assert key in advisory
    assert set(advisory) == {
        "orientation_available",
        "orientation_scope",
        "orientation_entries_shown",
        "orientation_entries_total",
        "orientation_truncated",
        "orientation_bytes_used",
        "orientation_byte_budget",
        "orientation_unavailable_reason",
        "paths",
    }


def test_t3_path_order_is_preserved_exactly(tmp_path):
    root = _f2_repo(tmp_path)
    orientation = derive_repository_orientation(root, F2_TASK)

    assert _advisory(root, F2_TASK)["paths"] == list(orientation.paths)


# --------------------------------------------------------------------------
# T4/T5 — the repair introduces no second, larger budget
# --------------------------------------------------------------------------


def test_t4_path_list_obeys_the_existing_entry_bound(tmp_path):
    root = _repo(
        tmp_path,
        {f"app/project/module_{index:03d}.py": "X = 1\n" for index in range(120)},
    )
    orientation = derive_repository_orientation(root, "Update the project module.")
    advisory = _advisory(root, "Update the project module.")

    assert len(advisory["paths"]) == orientation.entries_shown
    assert len(advisory["paths"]) <= ORIENTATION_PATH_LIMIT


def test_t5_path_list_obeys_the_existing_byte_bound(tmp_path):
    root = _repo(
        tmp_path,
        {f"app/project/module_{index:03d}.py": "X = 1\n" for index in range(120)},
    )
    advisory = _advisory(root, "Update the project module.")
    rendered = sum(len(f"- {path}\n".encode("utf-8")) for path in advisory["paths"])

    assert rendered <= ORIENTATION_BYTE_BUDGET
    assert advisory["orientation_byte_budget"] == ORIENTATION_BYTE_BUDGET


def test_t5b_truncated_orientation_stays_bounded(tmp_path):
    root = _repo(
        tmp_path,
        {f"app/project/module_{index:03d}.py": "X = 1\n" for index in range(120)},
    )
    advisory = _advisory(root, "Update the project module.")

    assert advisory["orientation_truncated"] is True
    assert advisory["orientation_entries_total"] > advisory["orientation_entries_shown"]
    assert len(advisory["paths"]) == advisory["orientation_entries_shown"]


# --------------------------------------------------------------------------
# T6 — unavailable orientation carries no paths and no crawl fallback
# --------------------------------------------------------------------------


def test_t6_non_git_workspace_carries_no_paths(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "project.py").write_text("X = 1\n", encoding="utf-8")

    advisory = _advisory(tmp_path, "Update the project module.")

    assert advisory["orientation_available"] is False
    assert advisory["paths"] == []
    assert advisory["orientation_unavailable_reason"] == ORIENTATION_UNAVAILABLE_NOT_GIT


def test_t6b_no_candidate_match_carries_no_paths(tmp_path):
    root = _repo(tmp_path, {"app/alpha.py": "X = 1\n"})

    advisory = _advisory(root, "Investigate the zzzzqqqq subsystem.")

    assert advisory["orientation_available"] is False
    assert advisory["paths"] == []
    assert (
        advisory["orientation_unavailable_reason"]
        == ORIENTATION_UNAVAILABLE_NO_CANDIDATES
    )


# --------------------------------------------------------------------------
# T7 — the mechanical skip path still sends no orientation to any provider
# --------------------------------------------------------------------------


def test_t7_mechanical_skip_carries_no_provider_orientation(tmp_path, monkeypatch):
    root = _f2_repo(tmp_path)
    captured = {}

    class _Ctx:
        session_id = "s1"
        task_id = "t1"
        task_execution_id = None
        prompt = F2_TASK
        grounding_decision_provider = None
        grounding_max_steps = 3
        grounding_max_provider_requests = 2
        grounding_mechanical_skip = True
        grounding_snapshot_identity = None
        control_state_location = str(root)
        timeout_seconds = 30
        orchestration_state = type("S", (), {"project_dir": str(root)})()
        logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()

    real_config = GroundingRunConfig

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real_config(*args, **kwargs)

    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_grounding_integration."
        "GroundingRunConfig",
        _spy,
    )
    result = run_typed_grounding_for_planning(_Ctx(), append_event=lambda **k: None)

    assert captured["orientation_advisory"] == {}
    assert result.terminal_reason is GroundingTerminalReason.SKIPPED


# --------------------------------------------------------------------------
# T8 — the F2 vocabulary-drift shape now surfaces its candidate paths
# --------------------------------------------------------------------------


def test_t8_f2_task_surfaces_project_candidate_paths(tmp_path):
    root = _f2_repo(tmp_path)
    advisory = _advisory(root, F2_TASK)
    prompt = render_first_turn_prompt(_context(root, F2_TASK))

    assert advisory["orientation_available"] is True
    assert advisory["orientation_entries_shown"] == len(advisory["paths"])

    # Fixture-only assertions.  No production module names these paths.
    for expected in (
        "app/services/project/lifecycle.py",
        "app/services/project/state_summary.py",
        "app/api/v1/endpoints/projects.py",
    ):
        assert expected in advisory["paths"]
        assert expected in prompt

    # The drift itself is untouched: the task word never becomes the code word.
    assert "dormant" not in json.dumps(advisory)
    assert "paused" not in json.dumps(advisory)


# --------------------------------------------------------------------------
# T9/T10 — an oriented path is not evidence and does not reach Planning
# --------------------------------------------------------------------------


def test_t9_orientation_paths_alone_are_not_substantive_evidence(tmp_path):
    root = _f2_repo(tmp_path)
    provider = _Provider()
    config = GroundingRunConfig(
        grounding_run_id="ocr1-evidence",
        task_reference=GroundingTaskReference(task_id="task-ocr1"),
        workspace_identity=str(root.resolve()),
        snapshot_identity="snapshot-ocr1",
        max_steps=3,
        max_exploration_provider_requests=2,
        operator_task=F2_TASK,
        orientation_advisory=_advisory(root, F2_TASK),
    )
    result = GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity="snapshot-ocr1"),
        provider=provider,
        config=config,
    ).run()

    assert result.observations == ()
    assert result.state_projection.discovered_source_paths == ()
    assert result.cited_observation_ids == ()
    assert result.terminal_reason is not GroundingTerminalReason.SUFFICIENT
    # The paths were visible to the provider and still produced no evidence.
    assert result.orientation_advisory["paths"]


def test_t10_orientation_paths_do_not_materialize_into_planning(tmp_path):
    from app.services.orchestration.planning.grounding import (
        GroundingHandoffError,
        build_grounding_planning_context,
    )

    root = _f2_repo(tmp_path)
    provider = _Provider()
    config = GroundingRunConfig(
        grounding_run_id="ocr1-handoff",
        task_reference=GroundingTaskReference(task_id="task-ocr1"),
        workspace_identity=str(root.resolve()),
        snapshot_identity="snapshot-ocr1",
        max_steps=3,
        max_exploration_provider_requests=2,
        operator_task=F2_TASK,
        orientation_advisory=_advisory(root, F2_TASK),
    )
    result = GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity="snapshot-ocr1"),
        provider=provider,
        config=config,
    ).run()

    with pytest.raises(GroundingHandoffError) as excinfo:
        build_grounding_planning_context(result, project_dir=root)

    assert excinfo.value.code == "result_not_sufficient"


# --------------------------------------------------------------------------
# T11/T12/T13/T14 — nothing else moved
# --------------------------------------------------------------------------


def test_t11_search_accounting_is_unchanged(tmp_path):
    from app.services.orchestration.planning.grounding.contracts import (
        parse_grounding_request,
    )

    root = _f2_repo(tmp_path)
    executor = GroundingExecutor(root, snapshot_identity="snapshot-ocr1")
    request = parse_grounding_request(
        {"action": "search_text", "query": "paused", "scopes": ["app"]},
        grounding_run_id="ocr1-accounting",
        request_id="ocr1-req-1",
    )
    observation = executor.execute(request)

    assert observation.budget_delta.repository_actions == 1
    assert observation.budget_delta.distinct_files == 0
    assert observation.budget_delta.positive_regions == 0


def test_t12_substantive_gate_is_unchanged(tmp_path):
    """Orientation did not make search_text substantive."""

    from app.services.orchestration.planning.grounding.contracts import (
        parse_grounding_request,
    )

    root = _f2_repo(tmp_path)
    executor = GroundingExecutor(root, snapshot_identity="snapshot-ocr1")
    search = executor.execute(
        parse_grounding_request(
            {"action": "search_text", "query": "paused", "scopes": ["app"]},
            grounding_run_id="ocr1-gate",
            request_id="ocr1-req-2",
        )
    )
    inspect = executor.execute(
        parse_grounding_request(
            {"action": "inspect_file", "path": "app/services/project/lifecycle.py"},
            grounding_run_id="ocr1-gate",
            request_id="ocr1-req-3",
        )
    )

    assert search.budget_delta.positive_regions == 0
    assert inspect.budget_delta.positive_regions == 1
    assert inspect.budget_delta.distinct_files == 1


def test_t13_oriented_path_carries_no_path_authority(tmp_path):
    root = _f2_repo(tmp_path)
    advisory = _advisory(root, F2_TASK)

    # Orientation is a plain mapping of strings.  It exposes no authority field
    # a downstream consumer could mistake for an APA or a creation grant.
    assert isinstance(advisory["paths"], list)
    assert all(isinstance(path, str) for path in advisory["paths"])
    for forbidden in (
        "expected",
        "creation_authorized",
        "accepted_path_authority",
        "apa",
        "mutation",
    ):
        assert forbidden not in json.dumps(advisory).lower()


def test_t14_orientation_derivation_mutates_no_file(tmp_path):
    root = _f2_repo(tmp_path)
    before = {
        path: path.read_bytes() for path in sorted(root.rglob("*.py")) if path.is_file()
    }

    _advisory(root, F2_TASK)
    render_first_turn_prompt(_context(root, F2_TASK))

    after = {
        path: path.read_bytes() for path in sorted(root.rglob("*.py")) if path.is_file()
    }
    assert before == after
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        shell=False,
    )
    assert "?? " not in status.stdout
