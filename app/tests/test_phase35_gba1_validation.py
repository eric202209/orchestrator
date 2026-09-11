"""Provider-free GBA1 coverage for authoritative grounding event budgets."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.services.orchestration.phases.planning_grounding_integration import (
    run_typed_grounding_for_planning,
)
from app.services.orchestration.planning.grounding.coordinator_contracts import (
    GroundingAssessmentKind,
    GroundingProposal,
)
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.workspace.control_state_paths import (
    ControlStateLocation,
    control_state_family_dir,
)


BUDGET_FIELDS = (
    "provider_requests",
    "exploration_provider_requests",
    "correction_provider_requests",
    "terminal_assessment_requests",
    "repository_actions",
    "source_evidence_bytes",
    "distinct_files",
    "positive_regions",
)


def _repo(root: Path) -> Path:
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "app" / "example.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n",
        encoding="utf-8",
    )
    (root / "app" / "other.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=root, check=True, shell=False)
    return root


class _ScriptedProvider:
    def __init__(self, steps: list[Any]) -> None:
        self.steps = steps
        self.calls = 0

    def decide(self, context):
        step = self.steps[self.calls]
        self.calls += 1
        if isinstance(step, dict):
            if context.state.observation_history:
                return GroundingProposal(
                    assessment_kind=GroundingAssessmentKind.NEED_MORE_EVIDENCE,
                    action_payload=step,
                    rationale="continue the bounded scripted observation sequence",
                )
            return GroundingProposal(action_payload=step)
        if step == "sufficient":
            return GroundingProposal(
                assessment_kind=GroundingAssessmentKind.SUFFICIENT,
                cited_observation_ids=tuple(
                    item.observation_id for item in context.state.observation_history
                ),
                rationale="the scripted substantive observation is sufficient",
            )
        if step == "insufficient":
            return GroundingProposal(
                assessment_kind=GroundingAssessmentKind.INSUFFICIENT,
                rationale="the scripted evidence is intentionally insufficient",
            )
        raise AssertionError(f"unsupported scripted step: {step!r}")


def _context(
    workspace: Path,
    control_root: Path,
    provider: _ScriptedProvider,
    *,
    session_id: int = 137,
    task_id: int = 351,
    task_execution_id: int = 1,
):
    location = ControlStateLocation(
        legacy_root=workspace, project_id=137
    ).with_control_root(control_root)
    return SimpleNamespace(
        session_id=session_id,
        task_id=task_id,
        task_execution_id=task_execution_id,
        prompt="Inspect the repository implementation.",
        grounding_decision_provider=provider,
        grounding_max_steps=4,
        grounding_max_provider_requests=5,
        grounding_mechanical_skip=False,
        grounding_snapshot_identity=None,
        control_state_location=location,
        timeout_seconds=30,
        orchestration_state=SimpleNamespace(project_dir=str(workspace)),
        logger=logging.getLogger("gba1-test"),
        db=None,
    )


def _run(
    workspace: Path,
    control_root: Path,
    steps: list[Any],
    *,
    session_id: int = 137,
    task_id: int = 351,
    task_execution_id: int = 1,
):
    context = _context(
        workspace,
        control_root,
        _ScriptedProvider(steps),
        session_id=session_id,
        task_id=task_id,
        task_execution_id=task_execution_id,
    )
    result = run_typed_grounding_for_planning(context)
    events = read_orchestration_events(
        context.control_state_location, session_id, task_id
    )
    observations = [
        event["details"]
        for event in events
        if event["event_type"] == "grounding_observation"
    ]
    return context, result, events, observations


def _snapshot_values(snapshot) -> dict[str, int]:
    return {name: getattr(snapshot, name) for name in BUDGET_FIELDS}


def _assert_event_budgets_match_authority(result, observation_events) -> None:
    assert len(observation_events) == len(result.observations)
    for event, observation in zip(observation_events, result.observations):
        assert event["budget"] == _snapshot_values(observation.budget_cumulative)


def test_t1_first_substantive_file_event_matches_authority(tmp_path):
    workspace = _repo(tmp_path / "workspace")
    _context_value, result, _events, observations = _run(
        workspace,
        tmp_path / "control" / "one",
        [{"action": "inspect_file", "path": "app/example.py"}, "sufficient"],
    )

    _assert_event_budgets_match_authority(result, observations)
    assert result.budget_snapshot.distinct_files == 1
    assert observations[0]["budget"]["distinct_files"] == 1


def test_t2_repeated_same_file_event_preserves_deduplicated_distinct_count(
    tmp_path,
):
    workspace = _repo(tmp_path / "workspace")
    inspect = {"action": "inspect_file", "path": "app/example.py"}
    _context_value, result, _events, observations = _run(
        workspace,
        tmp_path / "control" / "repeat",
        [inspect, inspect, "sufficient"],
    )

    _assert_event_budgets_match_authority(result, observations)
    assert [item.budget_cumulative.distinct_files for item in result.observations] == [
        1,
        1,
    ]
    assert [item["budget"]["distinct_files"] for item in observations] == [1, 1]


def test_t3_second_distinct_file_advances_event_count_once(tmp_path):
    workspace = _repo(tmp_path / "workspace")
    _context_value, result, _events, observations = _run(
        workspace,
        tmp_path / "control" / "two-files",
        [
            {"action": "inspect_file", "path": "app/example.py"},
            {"action": "inspect_file", "path": "app/other.py"},
            "sufficient",
        ],
    )

    _assert_event_budgets_match_authority(result, observations)
    assert [item["budget"]["distinct_files"] for item in observations] == [1, 2]


def test_t4_search_navigation_keeps_substantive_event_budget_aligned(tmp_path):
    workspace = _repo(tmp_path / "workspace")
    _context_value, result, _events, observations = _run(
        workspace,
        tmp_path / "control" / "search",
        [
            {"action": "search_text", "query": "alpha", "scopes": ["app"]},
            "insufficient",
        ],
    )

    _assert_event_budgets_match_authority(result, observations)
    assert result.terminal_reason.value == "INSUFFICIENT_GROUNDING"
    assert observations[0]["budget"]["distinct_files"] == 0
    assert observations[0]["budget"]["positive_regions"] == 0


def test_t5_positive_region_cases_match_current_authoritative_semantics(tmp_path):
    workspace = _repo(tmp_path / "workspace")
    cases = (
        (
            "same-region",
            [
                {"action": "inspect_file", "path": "app/example.py"},
                {"action": "inspect_file", "path": "app/example.py"},
            ],
            [1, 2],
            [1, 1],
        ),
        (
            "different-region",
            [
                {
                    "action": "resolve_structure",
                    "relation": "symbol_definition",
                    "locator": {"path": "app/example.py", "name": "alpha"},
                },
                {
                    "action": "resolve_structure",
                    "relation": "symbol_definition",
                    "locator": {"path": "app/example.py", "name": "beta"},
                },
            ],
            [1, 2],
            [1, 1],
        ),
        (
            "different-file",
            [
                {"action": "inspect_file", "path": "app/example.py"},
                {"action": "inspect_file", "path": "app/other.py"},
            ],
            [1, 2],
            [1, 2],
        ),
        (
            "failed-observation",
            [
                {"action": "inspect_file", "path": "app/missing.py"},
                {"action": "inspect_file", "path": "app/example.py"},
            ],
            [0, 1],
            [0, 1],
        ),
    )
    for offset, (label, actions, expected_regions, expected_files) in enumerate(cases):
        _context_value, result, _events, observations = _run(
            workspace,
            tmp_path / "control" / label,
            [*actions, "insufficient"],
            session_id=200 + offset,
            task_id=400 + offset,
        )
        _assert_event_budgets_match_authority(result, observations)
        assert [item["budget"]["positive_regions"] for item in observations] == (
            expected_regions
        )
        assert [item["budget"]["distinct_files"] for item in observations] == (
            expected_files
        )


def test_t6_durable_journal_reload_preserves_authoritative_observation_budgets(
    tmp_path,
):
    workspace = _repo(tmp_path / "workspace")
    context, result, events, observations = _run(
        workspace,
        tmp_path / "control" / "durable",
        [
            {"action": "inspect_file", "path": "app/example.py"},
            {"action": "inspect_file", "path": "app/example.py"},
            "sufficient",
        ],
        session_id=207,
        task_id=259,
        task_execution_id=348,
    )
    journal = (
        control_state_family_dir(context.control_state_location, "events")
        / "session_207_task_259.jsonl"
    )
    assert journal.exists()
    reloaded = read_orchestration_events(context.control_state_location, 207, 259)
    assert [event["event_id"] for event in reloaded] == [
        event["event_id"] for event in events
    ]
    reloaded_observations = [
        event["details"]
        for event in reloaded
        if event["event_type"] == "grounding_observation"
    ]
    _assert_event_budgets_match_authority(result, reloaded_observations)
    assert [item["budget"]["distinct_files"] for item in observations] == [1, 1]


def _semantic_signature(result):
    return (
        tuple(
            (
                request.action_identity,
                dict(request.normalized_payload),
            )
            for request in result.requests
        ),
        tuple(
            (
                observation.observation_id,
                observation.action_identity,
                observation.outcome.value,
                observation.bounded_content,
            )
            for observation in result.observations
        ),
        result.terminal_state.value,
        result.terminal_reason.value,
        result.cited_observation_ids,
    )


def test_t7_accounting_repair_does_not_change_grounding_outcome_sequence(tmp_path):
    workspace = _repo(tmp_path / "workspace")
    steps = [
        {"action": "inspect_file", "path": "app/example.py"},
        {"action": "inspect_file", "path": "app/example.py"},
        "sufficient",
    ]
    _context_a, result_a, _events_a, _observations_a = _run(
        workspace, tmp_path / "control" / "semantic-a", steps
    )
    _context_b, result_b, _events_b, _observations_b = _run(
        workspace, tmp_path / "control" / "semantic-b", steps
    )

    assert _semantic_signature(result_a) == _semantic_signature(result_b)


def test_t8_independent_grounding_attempts_start_with_fresh_budget(tmp_path):
    workspace = _repo(tmp_path / "workspace")
    control_root = tmp_path / "control" / "fresh"
    _context_a, result_a, _events_a, observations_a = _run(
        workspace,
        control_root,
        [{"action": "inspect_file", "path": "app/example.py"}, "sufficient"],
        session_id=207,
        task_id=259,
        task_execution_id=348,
    )
    _context_b, result_b, _events_b, observations_b = _run(
        workspace,
        control_root,
        [{"action": "inspect_file", "path": "app/other.py"}, "sufficient"],
        session_id=207,
        task_id=259,
        task_execution_id=348,
    )

    assert result_a.budget_snapshot.distinct_files == 1
    assert result_b.budget_snapshot.distinct_files == 1
    assert observations_a[0]["budget"]["distinct_files"] == 1
    assert observations_b[1]["budget"]["distinct_files"] == 1
