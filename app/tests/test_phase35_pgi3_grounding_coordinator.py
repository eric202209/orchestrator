"""Provider-free PHASE35-PGI3 coordinator and boundary proof."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from types import SimpleNamespace

from app.config import settings
from app.services.orchestration.planning.grounding import (
    GroundingAssessmentKind,
    GroundingCoordinator,
    GroundingExecutor,
    GroundingOutcome,
    GroundingProposal,
    GroundingRunConfig,
    GroundingTaskReference,
    GroundingTerminalReason,
    apply_grounding_result_to_planning_context,
)
from app.services.orchestration.planning.read_only_discovery import (
    prepare_discovery_context,
)
from app.services.orchestration.phases import planning_flow


def _git_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, shell=False)
    return tmp_path


@dataclass
class ScriptedProvider:
    steps: list

    def __post_init__(self):
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        step = self.steps.pop(0)
        return step(context)


def _coordinator(
    root: Path,
    provider: ScriptedProvider,
    *,
    max_steps: int = 4,
    max_exploration_provider_requests: int = 8,
    snapshot_supplier=None,
):
    snapshot = "snapshot-1"
    config = GroundingRunConfig(
        grounding_run_id="grounding-run-test",
        task_reference=GroundingTaskReference(task_id="task-1"),
        workspace_identity=str(root.resolve()),
        snapshot_identity=snapshot,
        max_steps=max_steps,
        max_exploration_provider_requests=max_exploration_provider_requests,
        operator_task="Find the implementation for the requested behavior.",
        snapshot_identity_supplier=snapshot_supplier,
    )
    return GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity=snapshot),
        provider=provider,
        config=config,
    )


def test_case_a_direct_success_returns_frozen_result_without_plan_authority(tmp_path):
    root = _git_repo(
        tmp_path,
        {"app/sample.py": "def target():\n    return 'needle'\n"},
    )

    def search(_context):
        return GroundingProposal(
            action_payload={
                "action": "search_text",
                "query": "needle",
                "scopes": ["app"],
            }
        )

    def sufficient(context):
        observation = context.state.observation_history[0]
        return GroundingProposal(
            assessment_kind=GroundingAssessmentKind.SUFFICIENT,
            cited_observation_ids=(observation.observation_id,),
            cited_source_paths=("app/sample.py",),
            rationale="The bounded search found the implementation source.",
            unresolved_risk=False,
        )

    provider = ScriptedProvider([search, sufficient])
    result = _coordinator(root, provider).run()

    assert result.terminal_reason is GroundingTerminalReason.SUFFICIENT
    assert result.cited_source_paths == ("app/sample.py",)
    assert result.state_projection.observation_outcomes == (
        GroundingOutcome.FOUND.value,
    )
    assert result.cited_source_evidence[0].source_version
    assert not hasattr(result, "plan")
    assert not hasattr(result, "apa")
    assert (
        provider.contexts[1].state.observation_history[0].outcome
        is GroundingOutcome.FOUND
    )


def test_case_b_not_found_remains_visible_and_refines_to_found(tmp_path):
    root = _git_repo(
        tmp_path,
        {"app/sample.py": "def target():\n    return 'needle'\n"},
    )

    def first_search(_context):
        return GroundingProposal(
            action_payload={
                "action": "search_text",
                "query": "wrong-hypothesis",
                "scopes": ["app"],
            }
        )

    def refine(context):
        assert "NOT_FOUND" in context.rendered_grounding_state
        observation = context.state.observation_history[0]
        return GroundingProposal(
            assessment_kind=GroundingAssessmentKind.NEED_MORE_EVIDENCE,
            rationale="The first bounded query produced truthful negative evidence.",
            action_payload={"action": "inspect_file", "path": "app/sample.py"},
        )

    def sufficient(context):
        observation = context.state.observation_history[-1]
        return GroundingProposal(
            assessment_kind=GroundingAssessmentKind.SUFFICIENT,
            cited_observation_ids=(observation.observation_id,),
            cited_source_paths=("app/sample.py",),
            rationale="The refined inspection found the bounded source evidence.",
            unresolved_risk=False,
        )

    provider = ScriptedProvider([first_search, refine, sufficient])
    result = _coordinator(root, provider).run()

    assert result.terminal_reason is GroundingTerminalReason.SUFFICIENT
    assert result.state_projection.observation_outcomes == (
        GroundingOutcome.NOT_FOUND.value,
        GroundingOutcome.FOUND.value,
    )
    assert (
        result.state_projection.assessment_history[0].kind
        is GroundingAssessmentKind.NEED_MORE_EVIDENCE
    )
    assert result.state_projection.assessment_history[0].next_action_digest


def test_case_c_exact_duplicate_negative_is_a_signal_not_an_action(tmp_path):
    root = _git_repo(tmp_path, {"app/sample.py": "value = 1\n"})
    request = {
        "action": "search_text",
        "query": "absent",
        "scopes": ["app"],
    }

    def first(_context):
        return GroundingProposal(action_payload=request)

    def duplicate(context):
        assert (
            context.state.observation_history[0].outcome is GroundingOutcome.NOT_FOUND
        )
        return GroundingProposal(
            assessment_kind=GroundingAssessmentKind.NEED_MORE_EVIDENCE,
            rationale="The exact negative request is being reconsidered mechanically.",
            action_payload=request,
        )

    def stop(context):
        return GroundingProposal(
            assessment_kind=GroundingAssessmentKind.TERMINAL_STOP,
            rationale="No further evidence was selected by the scripted provider.",
            terminal_reason=GroundingTerminalReason.INSUFFICIENT_GROUNDING,
        )

    provider = ScriptedProvider([first, duplicate, stop])
    result = _coordinator(
        root, provider, max_steps=2, max_exploration_provider_requests=4
    ).run()

    assert result.terminal_reason is GroundingTerminalReason.INSUFFICIENT_GROUNDING
    assert result.state_projection.observation_outcomes == (
        GroundingOutcome.NOT_FOUND.value,
        GroundingOutcome.NOT_FOUND.value,
    )
    assert result.state_projection.remaining_budget["repository_actions"] == 0
    assert len(provider.contexts) == 3


def test_case_d_action_budget_exhaustion_stops_before_second_repository_action(
    tmp_path,
):
    root = _git_repo(tmp_path, {"app/sample.py": "value = 1\n"})

    def first(_context):
        return GroundingProposal(
            action_payload={
                "action": "search_text",
                "query": "absent",
                "scopes": ["app"],
            }
        )

    def second(context):
        return GroundingProposal(
            assessment_kind=GroundingAssessmentKind.NEED_MORE_EVIDENCE,
            rationale="A different bounded hypothesis would be needed.",
            action_payload={"action": "inspect_file", "path": "app/sample.py"},
        )

    provider = ScriptedProvider([first, second])
    result = _coordinator(
        root, provider, max_steps=1, max_exploration_provider_requests=3
    ).run()

    assert result.terminal_reason is GroundingTerminalReason.BUDGET_EXHAUSTED
    assert result.state_projection.observation_outcomes == (
        GroundingOutcome.NOT_FOUND.value,
    )
    assert result.state_projection.remaining_budget["repository_actions"] == 0


def test_case_e_invalid_request_is_not_a_repository_observation(tmp_path):
    root = _git_repo(tmp_path, {"app/sample.py": "value = 1\n"})
    unsafe = lambda _context: GroundingProposal(  # noqa: E731
        action_payload={"action": "inspect_file", "path": "../escape.py"}
    )
    # PHASE35-CPR1 grants one mechanical correction, so the unsafe request is
    # offered a single repair.  Repeating it must still reach no observation.
    provider = ScriptedProvider([unsafe, unsafe])

    result = _coordinator(root, provider, max_exploration_provider_requests=1).run()

    assert result.terminal_reason is GroundingTerminalReason.INVALID_MODEL_REQUEST
    assert result.terminal_state.value == "INSUFFICIENT"
    assert result.state_projection.observation_ids == ()
    assert len(result.rejections) == 2
    assert result.cited_source_paths == ()
    assert result.provider_model_telemetry["correction_provider_requests"] == 1


def test_case_f_snapshot_identity_change_fails_closed(tmp_path):
    root = _git_repo(tmp_path, {"app/sample.py": "value = 1\n"})
    identities = iter(("snapshot-1", "snapshot-2"))
    provider = ScriptedProvider(
        [
            lambda _context: GroundingProposal(
                action_payload={"action": "inspect_file", "path": "app/sample.py"}
            )
        ]
    )

    result = _coordinator(
        root,
        provider,
        snapshot_supplier=lambda: next(identities),
    ).run()

    assert result.terminal_reason is GroundingTerminalReason.SOURCE_VERSION_CHANGED
    assert len(result.state_projection.observation_ids) == 1
    assert len(provider.contexts) == 1


def test_planning_context_adapter_keeps_operator_task_separate(tmp_path):
    root = _git_repo(tmp_path, {"app/sample.py": "needle = True\n"})
    provider = ScriptedProvider(
        [
            lambda _context: GroundingProposal(
                action_payload={
                    "action": "search_text",
                    "query": "needle",
                    "scopes": ["app"],
                }
            ),
            lambda context: GroundingProposal(
                assessment_kind=GroundingAssessmentKind.SUFFICIENT,
                cited_observation_ids=(
                    context.state.observation_history[0].observation_id,
                ),
                cited_source_paths=("app/sample.py",),
                rationale="The bounded evidence is sufficient.",
                unresolved_risk=False,
            ),
        ]
    )
    result = _coordinator(root, provider).run()

    class Context:
        prompt = "OPERATOR TASK: change the behavior"

    ctx = Context()
    rendered = apply_grounding_result_to_planning_context(ctx, result)

    assert ctx.prompt == "OPERATOR TASK: change the behavior"
    assert "## DETERMINISTIC GROUNDING EVIDENCE" in rendered
    assert "needle" in rendered
    assert "OPERATOR TASK: change the behavior" not in rendered


def test_enabled_planning_boundary_accepts_scripted_provider_without_plan_authority(
    tmp_path,
):
    root = _git_repo(tmp_path, {"app/sample.py": "needle = True\n"})

    provider = ScriptedProvider(
        [
            lambda _context: GroundingProposal(
                action_payload={
                    "action": "search_text",
                    "query": "needle",
                    "scopes": ["app"],
                }
            ),
            lambda context: GroundingProposal(
                assessment_kind=GroundingAssessmentKind.SUFFICIENT,
                cited_observation_ids=(
                    context.state.observation_history[0].observation_id,
                ),
                cited_source_paths=("app/sample.py",),
                rationale="The scripted provider cites the bounded search evidence.",
                unresolved_risk=False,
            ),
        ]
    )
    events = []

    class Context:
        session_id = 10
        task_id = 11
        task_execution_id = 12
        prompt = "Find the implementation."
        grounding_decision_provider = provider
        grounding_max_steps = 2
        grounding_max_provider_requests = 4
        grounding_snapshot_identity = "snapshot-1"
        orchestration_state = SimpleNamespace(project_dir=str(root))
        logger = SimpleNamespace(debug=lambda *args, **kwargs: None)

        @property
        def control_state_location(self):
            return root

    original_append = planning_flow.append_orchestration_event
    planning_flow.append_orchestration_event = lambda **kwargs: events.append(
        kwargs
    ) or {"event_id": "event-1"}
    try:
        result = planning_flow._run_typed_grounding_for_planning(Context())
    finally:
        planning_flow.append_orchestration_event = original_append

    assert result.terminal_reason is GroundingTerminalReason.SUFFICIENT
    assert result.cited_source_paths == ("app/sample.py",)
    assert all("bounded_content" not in event["details"] for event in events)
    assert not hasattr(result, "plan")


def test_default_off_boundary_preserves_legacy_discovery_entrypoint():
    assert settings.ENABLE_TYPED_GROUNDING_COORDINATOR is False
    from app.services.orchestration.phases import planning_flow

    assert planning_flow.prepare_discovery_context is prepare_discovery_context
