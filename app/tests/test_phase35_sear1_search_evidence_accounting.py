"""PHASE35-SEAR1 — action-sensitive search evidence accounting proofs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess

import pytest

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.planning.grounding import (
    GroundingCoordinator,
    GroundingExecutor,
    GroundingOutcome,
    GroundingRunConfig,
    GroundingTaskReference,
    GroundingTerminalReason,
    build_grounding_planning_context,
)
from app.services.orchestration.planning.grounding.contracts import (
    GroundingBudgetLimits,
    GroundingRequestRejection,
    GroundingRequest,
    parse_grounding_request,
)


def _repo(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=root, check=True, shell=False)
    return root


def _request(
    payload: dict[str, object],
    *,
    run_id: str = "sear1-run",
    request_id: str = "request-1",
) -> GroundingRequest:
    return parse_grounding_request(
        payload,
        grounding_run_id=run_id,
        request_id=request_id,
    )


@dataclass
class _Provider:
    responses: list[object]
    calls: int = 0
    contexts: list[object] = field(default_factory=list)

    def decide(self, context):
        self.calls += 1
        self.contexts.append(context)
        response = self.responses.pop(0)
        return response(context) if callable(response) else response


def _sufficient(context):
    observation = context.state.observation_history[-1]
    return {
        "decision": "SUFFICIENT",
        "cited_observation_ids": [observation.observation_id],
        "rationale": "The bounded repository observation is sufficient.",
    }


def _need(action):
    return lambda _context: {
        "decision": "NEED_MORE_EVIDENCE",
        "next_action": action,
        "rationale": "The candidate evidence requires one bounded refinement.",
    }


def _insufficient(_context):
    return {"decision": "INSUFFICIENT", "reason": "bounded test completion"}


def _coordinator(
    root: Path,
    responses: list[object],
    *,
    run_id: str = "sear1-run",
    max_exploration: int = 2,
    limits: GroundingBudgetLimits | None = None,
    event_sink=None,
):
    provider = _Provider(list(responses))
    config = GroundingRunConfig(
        grounding_run_id=run_id,
        task_reference=GroundingTaskReference(task_id="sear1-task"),
        workspace_identity=str(root.resolve()),
        snapshot_identity="sear1-snapshot",
        max_steps=12,
        max_exploration_provider_requests=max_exploration,
        operator_task="Find the implementation without changing the repository.",
        budget_limits=limits
        or GroundingBudgetLimits(
            repository_actions=2,
            source_evidence_bytes=12 * 1024,
            distinct_files=4,
            positive_regions=4,
        ),
    )
    result = GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity="sear1-snapshot"),
        provider=provider,
        config=config,
        event_sink=event_sink,
    ).run()
    return result, provider


def test_s0_exact_pra2_rate_limit_search_is_admitted(tmp_path):
    root = Path(__file__).parents[2]
    action = {"action": "search_text", "query": "rate limit", "scopes": ["app"]}
    raw = GroundingExecutor(root, snapshot_identity="sear1-snapshot").execute(
        _request(action, run_id="sear1-pra2", request_id="pra2-search")
    )

    assert raw.outcome is GroundingOutcome.FOUND
    assert raw.result_count == 16
    assert len(raw.source_paths) == 7
    assert len(raw.bounded_content) == 1998
    assert raw.budget_delta.repository_actions == 1
    assert raw.budget_delta.distinct_files == 0
    assert raw.budget_delta.positive_regions == 0

    result, _provider = _coordinator(
        root,
        [action, _insufficient],
        run_id="sear1-pra2-coordinator",
    )
    assert len(result.observations) == 1
    assert result.observations[0].outcome is GroundingOutcome.FOUND
    assert result.budget_snapshot.distinct_files == 0
    assert result.budget_snapshot.positive_regions == 0


def test_s1_twenty_hit_search_over_four_paths_is_admitted(tmp_path):
    root = _repo(
        tmp_path,
        {
            f"app/module_{index}.py": "\n".join("needle = True" for _ in range(3))
            + "\n"
            for index in range(8)
        },
    )
    request = _request({"action": "search_text", "query": "needle", "scopes": ["app"]})

    observation = GroundingExecutor(root).execute(
        request,
        limits=GroundingBudgetLimits(
            repository_actions=1,
            source_evidence_bytes=12 * 1024,
            distinct_files=4,
            positive_regions=4,
        ),
    )

    assert observation.outcome is GroundingOutcome.FOUND
    assert observation.result_count == 20
    assert len(observation.source_paths) > 4
    assert observation.budget_delta.distinct_files == 0
    assert observation.budget_delta.positive_regions == 0


def test_s2_not_found_search_charges_only_repository_action(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "value = True\n"})
    observation = GroundingExecutor(root).execute(
        _request({"action": "search_text", "query": "missing", "scopes": ["app"]}),
        limits=GroundingBudgetLimits(
            repository_actions=1,
            source_evidence_bytes=0,
            distinct_files=0,
            positive_regions=0,
        ),
    )

    assert observation.outcome is GroundingOutcome.NOT_FOUND
    assert observation.budget_delta.repository_actions == 1
    assert observation.budget_delta.source_evidence_bytes == 0
    assert observation.budget_delta.distinct_files == 0
    assert observation.budget_delta.positive_regions == 0


def test_s3_search_candidate_does_not_preconsume_inspect_file(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    inspect = {"action": "inspect_file", "path": "app/sample.py"}
    result, _provider = _coordinator(
        root,
        [
            {"action": "search_text", "query": "needle", "scopes": ["app"]},
            _need(inspect),
            _sufficient,
        ],
    )

    search, inspection = result.observations
    assert search.budget_delta.distinct_files == 0
    assert search.budget_delta.positive_regions == 0
    assert inspection.budget_delta.distinct_files == 1
    assert inspection.budget_delta.positive_regions == 1
    assert result.budget_snapshot.distinct_files == 1
    assert result.budget_snapshot.positive_regions == 1


def test_s4_search_candidate_does_not_preconsume_structure(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "def target():\n    return True\n"})
    structure = {
        "action": "resolve_structure",
        "relation": "symbol_definition",
        "locator": {"path": "app/sample.py", "name": "target"},
    }
    result, _provider = _coordinator(
        root,
        [
            {"action": "search_text", "query": "target", "scopes": ["app"]},
            _need(structure),
            _sufficient,
        ],
    )

    search, resolution = result.observations
    assert search.budget_delta.distinct_files == 0
    assert search.budget_delta.positive_regions == 0
    assert resolution.outcome is GroundingOutcome.FOUND
    assert resolution.budget_delta.distinct_files == 1
    assert resolution.budget_delta.positive_regions == 1


def test_s5_overlapping_searches_consume_actions_not_substantive_evidence(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    search = {"action": "search_text", "query": "needle", "scopes": ["app"]}
    result, _provider = _coordinator(
        root,
        [search, _need(search), _sufficient],
    )

    assert len(result.observations) == 2
    assert result.repository_action_count == 2
    assert all(item.budget_delta.distinct_files == 0 for item in result.observations)
    assert all(item.budget_delta.positive_regions == 0 for item in result.observations)


def test_s6_repeated_inspect_deduplicates_substantive_file_only(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    inspect = {"action": "inspect_file", "path": "app/sample.py"}
    result, _provider = _coordinator(
        root,
        [inspect, _need(inspect), _sufficient],
    )

    first, second = result.observations
    assert first.budget_delta.distinct_files == 1
    assert second.budget_delta.distinct_files == 0
    assert result.budget_snapshot.distinct_files == 1


def test_s7_fifth_substantive_file_is_rejected(tmp_path):
    files = {f"app/file_{index}.py": f"value = {index}\n" for index in range(5)}
    root = _repo(tmp_path, files)
    actions = [
        {"action": "inspect_file", "path": f"app/file_{index}.py"} for index in range(5)
    ]
    result, _provider = _coordinator(
        root,
        [actions[0], *[_need(action) for action in actions[1:]]],
        max_exploration=5,
        limits=GroundingBudgetLimits(
            repository_actions=5,
            source_evidence_bytes=12 * 1024,
            distinct_files=4,
            positive_regions=10,
        ),
    )

    assert len(result.observations) == 4
    assert result.budget_snapshot.distinct_files == 4
    assert result.terminal_reason is GroundingTerminalReason.BUDGET_EXHAUSTED


def test_s8_fifth_substantive_region_is_rejected(tmp_path):
    files = {f"app/file_{index}.py": f"value = {index}\n" for index in range(5)}
    root = _repo(tmp_path, files)
    actions = [
        {"action": "inspect_file", "path": f"app/file_{index}.py"} for index in range(5)
    ]
    result, _provider = _coordinator(
        root,
        [actions[0], *[_need(action) for action in actions[1:]]],
        max_exploration=5,
        limits=GroundingBudgetLimits(
            repository_actions=5,
            source_evidence_bytes=12 * 1024,
            distinct_files=10,
            positive_regions=4,
        ),
    )

    assert len(result.observations) == 4
    assert result.budget_snapshot.positive_regions == 4
    assert result.terminal_reason is GroundingTerminalReason.BUDGET_EXHAUSTED


def test_s9_source_byte_bound_remains_enforced_for_substantive_evidence(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "value = True\n" * 30})
    with pytest.raises(GroundingRequestRejection) as error:
        GroundingExecutor(root).execute(
            _request({"action": "inspect_file", "path": "app/sample.py"}),
            limits=GroundingBudgetLimits(
                repository_actions=1,
                source_evidence_bytes=100,
                distinct_files=4,
                positive_regions=4,
            ),
        )
    assert error.value.code == "budget_source_evidence_bytes_exceeded"


def test_s10_search_observation_retains_source_versions_and_hashes(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    observation = GroundingExecutor(root).execute(
        _request({"action": "search_text", "query": "needle", "scopes": ["app"]})
    )
    assert observation.source_versions == {
        "app/sample.py": observation.source_versions["app/sample.py"]
    }
    assert observation.source_hashes["app/sample.py"]


def test_s11_source_change_after_search_fails_closed(tmp_path):
    source = tmp_path / "app/sample.py"
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    mutated = False

    def sink(event, _payload):
        nonlocal mutated
        if event == EventType.GROUNDING_OBSERVATION and not mutated:
            source.write_text("needle = False\n", encoding="utf-8")
            mutated = True

    result, provider = _coordinator(
        root,
        [
            {"action": "search_text", "query": "needle", "scopes": ["app"]},
            _sufficient,
        ],
        event_sink=sink,
    )
    assert result.terminal_reason is GroundingTerminalReason.SOURCE_VERSION_CHANGED
    assert len(result.observations) == 1
    assert provider.calls == 1


def test_s12_search_order_is_deterministic(tmp_path):
    root = _repo(
        tmp_path,
        {
            "app/z.py": "needle = True\n",
            "app/a.py": "needle = True\n",
        },
    )
    executor = GroundingExecutor(root)
    first = executor.execute(
        _request(
            {"action": "search_text", "query": "needle", "scopes": ["app"]},
            request_id="first",
        )
    )
    second = executor.execute(
        _request(
            {"action": "search_text", "query": "needle", "scopes": ["app"]},
            request_id="second",
        )
    )
    assert [(hit.path, hit.line_number, hit.snippet) for hit in first.hits] == [
        (hit.path, hit.line_number, hit.snippet) for hit in second.hits
    ]


def test_s14_lifecycle_and_repository_action_budget_remain_separate(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    result, provider = _coordinator(
        root,
        [{"action": "search_text", "query": "needle", "scopes": ["app"]}, _sufficient],
    )
    assert provider.calls == 2
    assert result.repository_action_count == 1
    assert result.budget_snapshot.provider_requests == 2


def test_s19_search_citation_behavior_is_unchanged(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    result, _provider = _coordinator(
        root,
        [{"action": "search_text", "query": "needle", "scopes": ["app"]}, _sufficient],
    )
    context = build_grounding_planning_context(result, project_dir=root)
    assert context.cited_observations[0].action_identity == "search_text"
    assert context.source_materialization.files[0].relative_path == "app/sample.py"


def test_s20_search_does_not_mutate_repository(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    GroundingExecutor(root).execute(
        _request({"action": "search_text", "query": "needle", "scopes": ["app"]})
    )
    after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert before == after
