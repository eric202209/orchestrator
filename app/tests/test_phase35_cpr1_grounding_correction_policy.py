"""PHASE35-CPR1 — one bounded, non-renewable mechanical correction per run.

PHASE35-TBD1 confirmed that at the shipped exploration budget of 2 a rejected
action on the final exploration turn could never be repaired, stranding both an
acquired observation and the unspent terminal allowance.  These provider-free
tests pin the separated correction allowance and the repaired action-schema
boundary, and prove neither buys exploration depth, repository evidence depth,
nor terminal authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess

import pytest

from app.services.orchestration.planning.grounding import (
    GroundingCoordinator,
    GroundingExecutor,
    GroundingRunConfig,
    GroundingTaskReference,
    build_grounding_planning_context,
)
from app.services.orchestration.planning.grounding.contracts import (
    GroundingBudgetLimits,
)
from app.services.orchestration.planning.grounding.coordinator_contracts import (
    GroundingLifecycleState,
    GroundingProviderTurnMode,
    GroundingTerminalReason,
)


@dataclass
class FakeProvider:
    responses: list
    turn_modes: list = field(default_factory=list)
    calls: int = 0

    def decide(self, context):
        self.turn_modes.append(context.turn_mode)
        self.calls += 1
        if not self.responses:
            raise AssertionError("coordinator requested an unbudgeted provider turn")
        response = self.responses.pop(0)
        return response(context) if callable(response) else response


class _ExplodingProvider:
    """Fails on the correction turn to prove the charge happens before the call."""

    def __init__(self, first):
        self.first = first
        self.turn_modes = []
        self.calls = 0

    def decide(self, context):
        self.turn_modes.append(context.turn_mode)
        self.calls += 1
        if self.calls == 1:
            return self.first
        raise TimeoutError("provider timeout during correction")


FILES = {
    "app/sample.py": "needle = True\n\n\ndef helper():\n    return 1\n",
    "app/other.py": "target = 2\n",
}


def _repo(tmp_path: Path) -> Path:
    for name, content in FILES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, shell=False)
    return tmp_path


def _coordinator(root: Path, provider, *, max_steps: int = 2, exploration: int = 2):
    config = GroundingRunConfig(
        grounding_run_id="cpr1-run",
        task_reference=GroundingTaskReference(task_id="task-1"),
        workspace_identity=str(root.resolve()),
        snapshot_identity="cpr1-snapshot",
        max_steps=max_steps,
        max_exploration_provider_requests=exploration,
        budget_limits=GroundingBudgetLimits(
            source_evidence_bytes=12 * 1024,
            distinct_files=4,
            positive_regions=4,
        ),
        operator_task="Find the implementation.",
    )
    return GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity="cpr1-snapshot"),
        provider=provider,
        config=config,
    )


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


def _assert_bounds(result, provider=None):
    total, exploration, correction, terminal, actions = _counts(result)
    assert exploration <= 2
    assert correction <= 1
    assert terminal <= 1
    assert total <= 4
    assert actions <= 2
    assert total == exploration + correction + terminal
    if provider is not None:
        assert provider.calls == total, "a provider call escaped budget accounting"
    modes = getattr(provider, "turn_modes", []) if provider else []
    corrections = [m for m in modes if m is GroundingProviderTurnMode.CORRECTION]
    assert len(corrections) <= 1
    terminals = [
        index
        for index, mode in enumerate(modes)
        if mode is GroundingProviderTurnMode.TERMINAL_ASSESSMENT
    ]
    if terminals:
        assert terminals[0] == len(modes) - 1, "a turn followed the terminal assessment"


SEARCH_OK = {"action": "search_text", "query": "needle", "scopes": ["app"]}
SEARCH_NO_SCOPES = {"action": "search_text", "query": "needle"}
INSPECT_OK = {"action": "inspect_file", "path": "app/sample.py"}
INSPECT_EXTRA = {"action": "inspect_file", "path": "app/sample.py", "extra": True}
RESOLVE_OK = {
    "action": "resolve_structure",
    "relation": "symbol_definition",
    "locator": {"path": "app/sample.py", "name": "helper"},
}
RESOLVE_EXTRA = {
    "action": "resolve_structure",
    "relation": "symbol_definition",
    "locator": {"path": "app/sample.py", "name": "helper", "extra": 1},
}
UNKNOWN = {"action": "grep_files", "query": "needle"}
MUTATION = {"action": "write_file", "path": "app/sample.py", "content": "x"}


def _need_more(action):
    return lambda context: {
        "decision": "NEED_MORE_EVIDENCE",
        "next_action": action,
        "rationale": "One bounded refinement is required.",
    }


def _sufficient(context):
    observation = context.state.observation_history[-1]
    return {
        "decision": "SUFFICIENT",
        "cited_observation_ids": [observation.observation_id],
        "rationale": "The cited bounded observation grounds the operator task.",
    }


def _sufficient_all(context):
    """Cite every FOUND observation, navigation and substantive alike.

    EPR1 makes this the ordinary terminal shape: search stays cited for
    provenance while the substantive observation is what actually grounds the
    task and what reaches Planning.
    """

    return {
        "decision": "SUFFICIENT",
        "cited_observation_ids": [
            item.observation_id
            for item in context.state.observation_history
            if item.outcome.value == "FOUND"
        ],
        "rationale": "The cited bounded observations ground the operator task.",
    }


def _insufficient(context):
    return {"decision": "INSUFFICIENT", "reason": "The evidence remains inadequate."}


# C0 -------------------------------------------------------------------------
def test_c0_no_rejection_leaves_the_correction_allowance_unspent(tmp_path):
    root = _repo(tmp_path)
    provider = FakeProvider([SEARCH_OK, _need_more(INSPECT_OK), _sufficient])

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    assert _counts(result) == (3, 2, 0, 1, 2)
    assert GroundingProviderTurnMode.CORRECTION not in provider.turn_modes
    _assert_bounds(result, provider)


# C1 -------------------------------------------------------------------------
def test_c1_malformed_first_known_action_reaches_one_correction(tmp_path):
    root = _repo(tmp_path)
    provider = FakeProvider(
        [SEARCH_NO_SCOPES, SEARCH_OK, _need_more(INSPECT_OK), _sufficient]
    )

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    assert [item.code for item in result.rejections] == ["invalid_request"]
    assert provider.turn_modes[1] is GroundingProviderTurnMode.CORRECTION
    assert _counts(result)[2] == 1
    _assert_bounds(result, provider)


# C2 -------------------------------------------------------------------------
def test_c2_pgv5_b1_shape_becomes_correctable(tmp_path):
    """The exact PGV5 B1 shape: rejection on the final exploration turn."""

    root = _repo(tmp_path)
    provider = FakeProvider(
        # The correction repairs the SAME search_text intent by supplying the
        # missing scopes; it may not switch to a different hypothesis.  EPR1:
        # the run opens with substantive evidence so the corrected search can
        # still reach SUFFICIENT; the rejection still lands on the final
        # exploration turn, which is the B1 shape this test pins.
        [
            INSPECT_OK,
            _need_more(SEARCH_NO_SCOPES),
            _need_more(SEARCH_OK),
            _sufficient_all,
        ]
    )

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    assert len(result.observations) == 2
    assert provider.turn_modes == [
        GroundingProviderTurnMode.EXPLORATION,
        GroundingProviderTurnMode.EXPLORATION,
        GroundingProviderTurnMode.CORRECTION,
        GroundingProviderTurnMode.TERMINAL_ASSESSMENT,
    ]
    assert _counts(result) == (4, 2, 1, 1, 2)
    _assert_bounds(result, provider)


# C3 -------------------------------------------------------------------------
def test_c3_malformed_inspect_file_field_set_is_correctable(tmp_path):
    root = _repo(tmp_path)
    provider = FakeProvider([INSPECT_EXTRA, INSPECT_OK, _sufficient])

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    assert [item.code for item in result.rejections] == ["invalid_request"]
    assert provider.turn_modes[1] is GroundingProviderTurnMode.CORRECTION
    _assert_bounds(result, provider)


# C4 -------------------------------------------------------------------------
def test_c4_malformed_resolve_structure_field_set_is_correctable(tmp_path):
    root = _repo(tmp_path)
    provider = FakeProvider([RESOLVE_EXTRA, RESOLVE_OK, _sufficient])

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    assert result.observations[0].action_identity == "resolve_structure"
    assert provider.turn_modes[1] is GroundingProviderTurnMode.CORRECTION
    _assert_bounds(result, provider)


# C5 -------------------------------------------------------------------------
def test_c5_unknown_action_keeps_its_typed_rejection_and_one_correction(tmp_path):
    root = _repo(tmp_path)
    provider = FakeProvider([UNKNOWN, SEARCH_OK, _need_more(INSPECT_OK), _sufficient])

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    assert len(result.rejections) == 1
    assert provider.turn_modes[1] is GroundingProviderTurnMode.CORRECTION
    _assert_bounds(result, provider)


# C6 -------------------------------------------------------------------------
def test_c6_correction_cannot_authorize_a_mutation_shaped_action(tmp_path):
    root = _repo(tmp_path)
    before = (tmp_path / "app/sample.py").read_text(encoding="utf-8")
    provider = FakeProvider([MUTATION, MUTATION])

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.INVALID_MODEL_REQUEST
    assert len(result.rejections) == 2
    assert result.observations == ()
    assert (tmp_path / "app/sample.py").read_text(encoding="utf-8") == before
    _assert_bounds(result, provider)


# C7 -------------------------------------------------------------------------
def test_c7_a_malformed_correction_gets_no_second_correction(tmp_path):
    root = _repo(tmp_path)
    provider = FakeProvider([SEARCH_NO_SCOPES, SEARCH_NO_SCOPES])

    result = _coordinator(root, provider).run()

    assert result.terminal_reason is GroundingTerminalReason.INVALID_MODEL_REQUEST
    assert len(result.rejections) == 2
    assert _counts(result) == (2, 1, 1, 0, 0)
    assert provider.turn_modes.count(GroundingProviderTurnMode.CORRECTION) == 1
    _assert_bounds(result, provider)


# C8 -------------------------------------------------------------------------
def test_c8_a_later_independent_rejection_does_not_renew_the_allowance(tmp_path):
    root = _repo(tmp_path)
    provider = FakeProvider([SEARCH_NO_SCOPES, SEARCH_OK, _need_more(SEARCH_NO_SCOPES)])

    result = _coordinator(root, provider).run()

    assert result.terminal_reason is GroundingTerminalReason.INVALID_MODEL_REQUEST
    assert _counts(result)[2] == 1, "the correction allowance was renewed"
    assert provider.turn_modes.count(GroundingProviderTurnMode.CORRECTION) == 1
    assert len(result.observations) == 1
    _assert_bounds(result, provider)


# C9 -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "terminal_response",
    [
        {"decision": "NEED_MORE_EVIDENCE", "next_action": INSPECT_OK, "rationale": "x"},
        INSPECT_OK,
        {"decision": "MAYBE", "reason": "unclear"},
    ],
)
def test_c9_a_malformed_terminal_response_is_never_corrected(
    tmp_path, terminal_response
):
    root = _repo(tmp_path)
    provider = FakeProvider([SEARCH_OK, _need_more(INSPECT_OK), terminal_response])

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.FAILED
    assert provider.turn_modes[-1] is GroundingProviderTurnMode.TERMINAL_ASSESSMENT
    assert _counts(result) == (3, 2, 0, 1, 2)
    _assert_bounds(result, provider)


# C10 / C11 ------------------------------------------------------------------
@pytest.mark.parametrize(
    "final,expected",
    [
        (_insufficient, GroundingLifecycleState.INSUFFICIENT),
        (_sufficient, GroundingLifecycleState.SUFFICIENT),
    ],
)
def test_c10_c11_a_valid_semantic_verdict_leaves_correction_unused(
    tmp_path, final, expected
):
    root = _repo(tmp_path)
    provider = FakeProvider([SEARCH_OK, _need_more(INSPECT_OK), final])

    result = _coordinator(root, provider).run()

    assert result.terminal_state is expected
    assert _counts(result)[2] == 0
    assert GroundingProviderTurnMode.CORRECTION not in provider.turn_modes
    _assert_bounds(result, provider)


# C12 ------------------------------------------------------------------------
def test_c12_a_correction_cannot_change_the_rejected_action_kind(tmp_path):
    root = _repo(tmp_path)
    provider = FakeProvider([SEARCH_NO_SCOPES, INSPECT_OK])

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.INVALID_MODEL_REQUEST
    assert result.observations == (), "a changed hypothesis was executed"
    _assert_bounds(result, provider)


# C13 ------------------------------------------------------------------------
def test_c13_a_correction_cannot_change_a_valid_hypothesis_field(tmp_path):
    root = _repo(tmp_path)
    # The rejected query was itself legal; only `scopes` was missing.
    provider = FakeProvider(
        [
            SEARCH_NO_SCOPES,
            {"action": "search_text", "query": "target", "scopes": ["app"]},
        ]
    )

    result = _coordinator(root, provider).run()

    assert result.terminal_reason is GroundingTerminalReason.INVALID_MODEL_REQUEST
    assert result.observations == ()
    _assert_bounds(result, provider)


def test_c13b_a_correction_may_replace_the_field_that_caused_the_rejection(tmp_path):
    """An illegal path must stay repairable, or correction is useless."""

    root = _repo(tmp_path)
    provider = FakeProvider(
        [{"action": "inspect_file", "path": "../escape.py"}, INSPECT_OK, _sufficient]
    )

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    assert result.observations[0].source_paths == ("app/sample.py",)
    _assert_bounds(result, provider)


# C14 ------------------------------------------------------------------------
def test_c14_a_correction_never_buys_an_extra_repository_action(tmp_path):
    root = _repo(tmp_path)
    provider = FakeProvider(
        [SEARCH_OK, _need_more(SEARCH_NO_SCOPES), _need_more(SEARCH_OK), _sufficient]
    )

    result = _coordinator(root, provider, max_steps=1).run()

    assert result.repository_action_count <= 1
    assert result.state_projection.remaining_budget["repository_actions"] == 0
    assert result.terminal_state.terminal
    _assert_bounds(result, provider)


# C15 ------------------------------------------------------------------------
def test_c15_a_failed_correction_call_is_still_counted_truthfully(tmp_path):
    root = _repo(tmp_path)
    provider = _ExplodingProvider(SEARCH_NO_SCOPES)

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.FAILED
    assert provider.turn_modes[1] is GroundingProviderTurnMode.CORRECTION
    total, exploration, correction, terminal, _ = _counts(result)
    assert correction == 1, "a correction call escaped accounting"
    assert total == exploration + correction + terminal == provider.calls


# C16 ------------------------------------------------------------------------
def test_c16_corrected_b1_shape_reaches_the_canonical_handoff(tmp_path):
    root = _repo(tmp_path)
    provider = FakeProvider(
        [
            INSPECT_OK,
            _need_more(SEARCH_NO_SCOPES),
            _need_more(SEARCH_OK),
            _sufficient_all,
        ]
    )

    result = _coordinator(root, provider).run()
    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT

    context = build_grounding_planning_context(
        result, project_dir=root, workspace_identity=str(root.resolve())
    )

    assert context.cited_source_evidence
    assert [item.source_path for item in context.cited_source_evidence] == [
        "app/sample.py"
    ]
    assert all(
        item.structural_identity is None for item in context.cited_source_evidence
    )
    assert _counts(result) == (4, 2, 1, 1, 2)
    _assert_bounds(result, provider)


# Contract-level bounds ------------------------------------------------------
def test_run_config_rejects_a_correction_allowance_other_than_one():
    for value in (0, 2):
        with pytest.raises(ValueError, match="exactly 1"):
            GroundingRunConfig(
                grounding_run_id="cpr1-run",
                task_reference=GroundingTaskReference(task_id="task-1"),
                workspace_identity="/tmp/workspace",
                snapshot_identity="snapshot-1",
                max_steps=2,
                max_exploration_provider_requests=2,
                max_correction_provider_requests=value,
            )


def test_total_provider_ceiling_includes_the_correction_allowance():
    config = GroundingRunConfig(
        grounding_run_id="cpr1-run",
        task_reference=GroundingTaskReference(task_id="task-1"),
        workspace_identity="/tmp/workspace",
        snapshot_identity="snapshot-1",
        max_steps=2,
        max_exploration_provider_requests=2,
    )

    assert config.max_correction_provider_requests == 1
    assert config.max_total_provider_requests == 4


def test_production_integration_keeps_the_shipped_semantic_budgets():
    from app.config import settings

    assert settings.TYPED_GROUNDING_MAX_PROVIDER_REQUESTS == 2
    assert settings.TYPED_GROUNDING_MAX_STEPS == 2
    assert settings.ENABLE_TYPED_GROUNDING_COORDINATOR is False


class _Adversary:
    """Never terminates voluntarily and always emits a malformed request."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls = 0
        self.turn_modes: list = []

    def decide(self, context):
        self.calls += 1
        self.turn_modes.append(context.turn_mode)
        if context.turn_mode.terminal_only:
            return {
                "decision": "NEED_MORE_EVIDENCE",
                "next_action": SEARCH_OK,
                "rationale": "keep going",
            }
        if not context.state.observation_history:
            return SEARCH_NO_SCOPES if self.mode == "invalid" else SEARCH_OK
        return {
            "decision": "NEED_MORE_EVIDENCE",
            "next_action": SEARCH_NO_SCOPES if self.mode == "invalid" else INSPECT_OK,
            "rationale": "keep going",
        }


@pytest.mark.parametrize("mode", ["invalid", "endless"])
def test_an_adversarial_provider_cannot_exceed_the_repaired_bounds(tmp_path, mode):
    root = _repo(tmp_path)
    adversary = _Adversary(mode)

    result = _coordinator(root, adversary).run()

    assert result.terminal_state.terminal
    _assert_bounds(result, adversary)
