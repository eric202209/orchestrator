"""PHASE35-BPR1 — bounded exploration budget plus one terminal assessment.

Provider-free proof of the repaired grounding budget policy: exploration and
corrective turns are bounded by ``max_exploration_provider_requests`` and the
single terminal-assessment turn is reserved, non-renewable, and unable to
request evidence, act, or correct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess

import pytest

from app.services.orchestration.planning.grounding.contracts import (
    GroundingBudgetLimits,
)
from app.services.orchestration.planning.grounding.coordinator import (
    GroundingCoordinator,
)
from app.services.orchestration.planning.grounding.coordinator_contracts import (
    GroundingLifecycleState,
    GroundingProviderTurnMode,
    GroundingRunConfig,
    GroundingTaskReference,
    GroundingTerminalReason,
)
from app.services.orchestration.planning.grounding.executor import GroundingExecutor


@dataclass
class FakeProvider:
    responses: list
    contexts: list = field(default_factory=list)
    turn_modes: list = field(default_factory=list)

    def decide(self, context):
        self.contexts.append(context)
        self.turn_modes.append(context.turn_mode)
        if not self.responses:
            raise AssertionError("coordinator requested an unbudgeted provider turn")
        response = self.responses.pop(0)
        return response(context) if callable(response) else response


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, shell=False)
    return tmp_path


def _coordinator(
    root: Path,
    provider,
    *,
    max_steps: int = 2,
    max_exploration_provider_requests: int = 2,
):
    config = GroundingRunConfig(
        grounding_run_id="bpr1-run",
        task_reference=GroundingTaskReference(task_id="task-1"),
        workspace_identity=str(root.resolve()),
        snapshot_identity="snapshot-1",
        max_steps=max_steps,
        max_exploration_provider_requests=max_exploration_provider_requests,
        budget_limits=GroundingBudgetLimits(
            source_evidence_bytes=12 * 1024,
            distinct_files=4,
            positive_regions=4,
        ),
        operator_task="Find the implementation.",
        orientation_advisory={},
    )
    return GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity="snapshot-1"),
        provider=provider,
        config=config,
    )


def _search(query: str = "needle", scope: str = "app"):
    return {"action": "search_text", "query": query, "scopes": [scope]}


def _inspect(path: str = "app/sample.py"):
    return {"action": "inspect_file", "path": path}


def _sufficient(context):
    observation = context.state.observation_history[-1]
    return {
        "decision": "SUFFICIENT",
        "cited_observation_ids": [observation.observation_id],
        "rationale": "The cited bounded observation grounds the operator task.",
    }


def _need_more(action):
    return lambda context: {
        "decision": "NEED_MORE_EVIDENCE",
        "next_action": action,
        "rationale": "One bounded refinement is required for this task.",
    }


def _insufficient(context):
    return {"decision": "INSUFFICIENT", "reason": "The evidence remains inadequate."}


FILES = {"app/sample.py": "needle = True\n", "app/other.py": "target = 2\n"}


def _counts(result):
    """(total, exploration, correction, terminal, repository actions)."""

    telemetry = result.provider_model_telemetry
    return (
        telemetry["provider_requests"],
        telemetry["exploration_provider_requests"],
        telemetry["correction_provider_requests"],
        telemetry["terminal_assessment_requests"],
        result.repository_action_count,
    )


def test_p0_first_observation_sufficient_leaves_terminal_allowance_unspent(tmp_path):
    root = _repo(tmp_path, FILES)
    # EPR1: the first observation must be substantive for SUFFICIENT to be
    # legal.  One inspect_file is still exactly one repository action, so the
    # budget shape this test pins is unchanged.
    provider = FakeProvider([_inspect(), _sufficient])

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    assert _counts(result) == (2, 2, 0, 0, 1)
    assert provider.turn_modes == [
        GroundingProviderTurnMode.EXPLORATION,
        GroundingProviderTurnMode.EXPLORATION,
    ]


def test_p1_first_observation_insufficient_leaves_terminal_allowance_unspent(tmp_path):
    root = _repo(tmp_path, FILES)
    provider = FakeProvider([_search("needle"), _insufficient])

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.INSUFFICIENT_GROUNDING
    assert _counts(result) == (2, 2, 0, 0, 1)


def test_p2_one_useful_refinement_then_terminal_sufficient(tmp_path):
    root = _repo(tmp_path, FILES)
    provider = FakeProvider([_search("nomatch"), _need_more(_inspect()), _sufficient])

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.SUFFICIENT
    assert _counts(result) == (3, 2, 0, 1, 2)
    assert provider.turn_modes[-1] is GroundingProviderTurnMode.TERMINAL_ASSESSMENT
    assert result.cited_observation_ids == (result.observations[-1].observation_id,)


def test_p3_one_useful_refinement_then_terminal_insufficient(tmp_path):
    root = _repo(tmp_path, FILES)
    provider = FakeProvider([_search("nomatch"), _need_more(_inspect()), _insufficient])

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.INSUFFICIENT_GROUNDING
    assert _counts(result) == (3, 2, 0, 1, 2)


def test_p4_terminal_turn_cannot_request_more_evidence(tmp_path):
    root = _repo(tmp_path, FILES)
    provider = FakeProvider(
        [
            _search("nomatch"),
            _need_more(_inspect()),
            _need_more(_search("target", "app")),
        ]
    )

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.FAILED
    assert _counts(result) == (3, 2, 0, 1, 2)
    assert len(result.observations) == 2
    assert len(provider.contexts) == 3


def test_p5_terminal_turn_rejects_action_only_payload(tmp_path):
    root = _repo(tmp_path, FILES)
    provider = FakeProvider(
        [_search("nomatch"), _need_more(_inspect()), _search("target", "app")]
    )

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.FAILED
    assert _counts(result) == (3, 2, 0, 1, 2)
    assert len(result.observations) == 2


def test_p6_correction_spends_its_own_allowance_not_exploration_or_terminal(
    tmp_path,
):
    """PHASE35-CPR1 separated the mechanical correction from exploration depth."""

    root = _repo(tmp_path, FILES)
    provider = FakeProvider([_inspect("../escape.py"), _inspect(), _sufficient])

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    # The correction spends its own allowance, so the second exploration turn
    # that the old policy burned on the repair is still available afterwards.
    assert _counts(result) == (3, 2, 1, 0, 1)
    assert len(result.rejections) == 1
    assert provider.turn_modes == [
        GroundingProviderTurnMode.EXPLORATION,
        GroundingProviderTurnMode.CORRECTION,
        GroundingProviderTurnMode.EXPLORATION,
    ]


def test_p7_repeated_unproductive_refinement_stays_bounded(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "value = 1\n"})
    provider = FakeProvider(
        [_search("absent"), _need_more(_search("absent")), _insufficient]
    )

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT
    assert _counts(result) == (3, 2, 0, 1, 2)
    assert len(result.observations) == 2


def test_p8_repository_action_budget_is_never_exceeded(tmp_path):
    root = _repo(tmp_path, FILES)
    provider = FakeProvider(
        [
            _search("needle"),
            _need_more(_inspect()),
            _sufficient,
        ]
    )

    result = _coordinator(root, provider, max_steps=1).run()

    assert result.repository_action_count <= 1
    assert result.terminal_state.terminal
    assert result.state_projection.remaining_budget["repository_actions"] == 0


def test_terminal_allowance_is_not_spent_without_an_observation(tmp_path):
    root = _repo(tmp_path, FILES)
    # Every exploration turn is rejected, so no observation ever exists and the
    # reserved terminal assessment stays unreachable.
    provider = FakeProvider([_inspect("../escape.py"), _inspect("../escape.py")])

    result = _coordinator(root, provider).run()

    assert result.observations == ()
    assert result.provider_model_telemetry["terminal_assessment_requests"] == 0
    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT


def test_run_config_rejects_a_terminal_allowance_other_than_one():
    for value in (0, 2):
        with pytest.raises(ValueError, match="exactly 1"):
            GroundingRunConfig(
                grounding_run_id="bpr1-run",
                task_reference=GroundingTaskReference(task_id="task-1"),
                workspace_identity="/tmp/workspace",
                snapshot_identity="snapshot-1",
                max_steps=2,
                max_exploration_provider_requests=2,
                max_terminal_assessment_requests=value,
            )


def test_total_provider_ceiling_is_explicit_not_an_implicit_increment():
    config = GroundingRunConfig(
        grounding_run_id="bpr1-run",
        task_reference=GroundingTaskReference(task_id="task-1"),
        workspace_identity="/tmp/workspace",
        snapshot_identity="snapshot-1",
        max_steps=2,
        max_exploration_provider_requests=2,
    )

    assert config.max_total_provider_requests == 4


class _Adversary:
    """Never terminates voluntarily; tries to keep exploring forever."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls = 0
        self.turn_modes: list = []

    def decide(self, context):
        self.calls += 1
        self.turn_modes.append(context.turn_mode)
        if self.mode == "invalid":
            payload = _inspect("../escape.py")
        elif self.mode == "alternate":
            payload = _inspect("../escape.py") if self.calls % 2 == 0 else _search()
        else:
            payload = _search(f"needle{self.calls}")
        if not context.state.observation_history:
            return payload
        return {
            "decision": "NEED_MORE_EVIDENCE",
            "next_action": payload,
            "rationale": "Another bounded refinement is demanded here.",
        }


@pytest.mark.parametrize("mode", ["invalid", "alternate", "endless"])
def test_adversarial_provider_cannot_exceed_the_bounded_policy(tmp_path, mode):
    root = _repo(tmp_path, FILES)
    adversary = _Adversary(mode)

    result = _coordinator(root, adversary).run()

    total, exploration, correction, terminal, actions = _counts(result)
    assert exploration <= 2, "exploration budget was exceeded"
    assert correction <= 1, "the correction allowance was renewed"
    assert terminal <= 1, "the terminal allowance was renewed"
    assert total <= 4, "the truthful total provider ceiling was exceeded"
    assert (
        total == exploration + correction + terminal
    ), "provider accounting is not truthful"
    assert actions <= 2, "the repository action bound was exceeded"
    assert adversary.calls == total, "a provider call escaped budget accounting"
    assert result.terminal_state.terminal

    terminal_turns = [
        index
        for index, item in enumerate(adversary.turn_modes)
        if item is GroundingProviderTurnMode.TERMINAL_ASSESSMENT
    ]
    assert len(terminal_turns) <= 1, "a terminal assessment loop occurred"
    if terminal_turns:
        # The terminal turn must be last: it can neither act nor correct.
        assert terminal_turns[0] == len(adversary.turn_modes) - 1


def test_no_observation_is_ever_left_without_an_assessment_opportunity(tmp_path):
    """The BPV1 lifecycle gap: evidence read but structurally unassessable."""

    root = _repo(tmp_path, FILES)
    adversary = _Adversary("endless")

    result = _coordinator(root, adversary).run()

    telemetry = result.provider_model_telemetry
    assessment_turns = (
        telemetry["exploration_provider_requests"]
        + telemetry["correction_provider_requests"]
        - 1
        + telemetry["terminal_assessment_requests"]
    )
    assert assessment_turns >= len(result.observations)


def test_terminal_allowance_sufficiency_still_reaches_canonical_handoff(tmp_path):
    """P2 SUFFICIENT must project into the canonical planning context intact."""

    from app.services.orchestration.planning.grounding import (
        GroundingPlanningContext,
        build_grounding_planning_context,
    )

    root = _repo(tmp_path, FILES)
    provider = FakeProvider([_search("nomatch"), _need_more(_inspect()), _sufficient])

    result = _coordinator(root, provider).run()
    context = build_grounding_planning_context(result, project_dir=root)

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    assert result.provider_model_telemetry["terminal_assessment_requests"] == 1
    assert isinstance(context, GroundingPlanningContext)
    # Citation revalidation kept only the cited FOUND observation.
    assert tuple(item.observation_id for item in context.cited_observations) == (
        result.cited_observation_ids[0],
    )
    assert [item.relative_path for item in context.source_materialization.files] == [
        "app/sample.py"
    ]
    assert [item.outcome.value for item in result.observations] == [
        "NOT_FOUND",
        "FOUND",
    ]
    assert "## GROUNDING EVIDENCE" in context.grounding_section
    assert "not operator instruction" in context.grounding_section


def test_stale_citation_after_terminal_assessment_still_fails_closed(tmp_path):
    from app.services.orchestration.planning.grounding import GroundingHandoffError
    from app.services.orchestration.planning.grounding import (
        build_grounding_planning_context,
    )

    root = _repo(tmp_path, FILES)
    provider = FakeProvider([_search("nomatch"), _need_more(_inspect()), _sufficient])
    result = _coordinator(root, provider).run()

    (root / "app/sample.py").write_text("needle = False\n", encoding="utf-8")

    with pytest.raises(GroundingHandoffError):
        build_grounding_planning_context(result, project_dir=root)
