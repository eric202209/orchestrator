"""Provider-free contract and lifecycle tests for PGI1 Slice 2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.config import Settings, settings
from app.services.orchestration.planning.grounding import (
    GroundingAssessmentKind,
    GroundingBudgetLimits,
    GroundingCoordinator,
    GroundingExecutionError,
    GroundingExecutor,
    GroundingInvariantError,
    GroundingLifecycleState,
    GroundingProposal,
    GroundingRunConfig,
    GroundingTaskReference,
    GroundingTerminalReason,
    parse_grounding_provider_response,
    transition_grounding_state,
)
from app.services.orchestration.phases.planning_grounding_integration import (
    prepare_planning_source_context,
)
from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_EXISTING,
)


@dataclass
class FakeProvider:
    responses: list

    def __post_init__(self):
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
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
    max_steps: int = 4,
    max_provider_requests: int = 3,
    mechanical_skip: bool = False,
):
    config = GroundingRunConfig(
        grounding_run_id="s2-run",
        task_reference=GroundingTaskReference(task_id="task-1"),
        workspace_identity=str(root.resolve()),
        snapshot_identity="snapshot-1",
        max_steps=max_steps,
        max_provider_requests=max_provider_requests,
        budget_limits=GroundingBudgetLimits(
            source_evidence_bytes=12 * 1024,
            distinct_files=4,
            positive_regions=4,
        ),
        operator_task="Find the implementation.",
        orientation_advisory={},
        mechanical_skip=mechanical_skip,
    )
    return GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity="snapshot-1"),
        provider=provider,
        config=config,
    )


def _search(query: str = "needle"):
    return {"action": "search_text", "query": query, "scopes": ["app"]}


def _inspect(path: str = "app/sample.py"):
    return {"action": "inspect_file", "path": path}


def _sufficient(context):
    observation = context.state.observation_history[-1]
    return {
        "decision": "SUFFICIENT",
        "cited_observation_ids": [observation.observation_id],
        "rationale": "The cited bounded observation is sufficient.",
    }


def test_direct_success_has_explicit_terminal_state_and_citation(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    provider = FakeProvider([_search(), _sufficient])

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.SUFFICIENT
    assert len(result.requests) == 1
    assert len(result.observations) == 1
    assert result.cited_observation_ids == (result.observations[0].observation_id,)
    assert result.source_versions["app/sample.py"]
    assert result.terminal_state.terminal
    assert result.state_projection.lifecycle_history == (
        GroundingLifecycleState.NOT_STARTED,
        GroundingLifecycleState.ORIENTED,
        GroundingLifecycleState.REQUESTING,
        GroundingLifecycleState.OBSERVED,
        GroundingLifecycleState.ASSESSING,
        GroundingLifecycleState.SUFFICIENT,
    )


def test_provider_budget_two_bounds_refinement_before_final_assessment(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    provider = FakeProvider(
        [
            _search("wrong"),
            lambda context: {
                "decision": "NEED_MORE_EVIDENCE",
                "next_action": _inspect(),
                "rationale": "The negative result needs one bounded refinement.",
            },
            _sufficient,
        ]
    )

    result = _coordinator(root, provider, max_provider_requests=2).run()

    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.BUDGET_EXHAUSTED
    assert result.provider_model_telemetry["provider_requests"] == 2
    assert tuple(item.outcome.value for item in result.observations) == (
        "NOT_FOUND",
        "FOUND",
    )
    assert len(provider.contexts) == 2


def test_negative_then_duplicate_request_is_allowed_and_consumes_action_budget(
    tmp_path,
):
    root = _repo(tmp_path, {"app/sample.py": "value = 1\n"})
    provider = FakeProvider(
        [
            _search("absent"),
            lambda context: {
                "decision": "NEED_MORE_EVIDENCE",
                "next_action": _search("absent"),
                "rationale": "Repeat the same bounded request.",
            },
        ]
    )

    result = _coordinator(root, provider, max_provider_requests=2).run()

    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.BUDGET_EXHAUSTED
    assert result.provider_model_telemetry["provider_requests"] == 2
    assert result.grounding_diagnostics["repository_actions"] == 2
    assert len(result.observations) == 2


def test_model_owned_insufficient_is_truthfully_terminal(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    provider = FakeProvider(
        [
            _search("needle"),
            {"decision": "INSUFFICIENT", "reason": "The evidence is not enough."},
        ]
    )

    result = _coordinator(root, provider).run()

    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.INSUFFICIENT_GROUNDING
    assert result.assessments[0].decision is GroundingAssessmentKind.INSUFFICIENT


def test_malformed_first_assessment_fails_closed(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    result = _coordinator(
        root,
        FakeProvider(
            [
                {
                    "decision": "SUFFICIENT",
                    "cited_observation_ids": ["x"],
                    "rationale": "x",
                }
            ]
        ),
    ).run()

    assert result.terminal_state is GroundingLifecycleState.FAILED
    assert result.observations == ()


def test_post_observation_bare_action_fails_closed(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    result = _coordinator(root, FakeProvider([_search(), _search()])).run()

    assert result.terminal_state is GroundingLifecycleState.FAILED
    assert len(result.observations) == 1


def test_unsafe_request_is_recorded_and_can_receive_one_corrective_turn(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    provider = FakeProvider(
        [
            _inspect("../escape.py"),
            _inspect(),
        ]
    )

    result = _coordinator(root, provider, max_provider_requests=2).run()

    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.BUDGET_EXHAUSTED
    assert len(result.rejections) == 1
    assert result.rejections[0].code == "invalid_request"
    assert len(result.observations) == 1


def test_provider_error_and_executor_error_are_failed_not_negative(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})

    class TimeoutProvider:
        def decide(self, _context):
            raise TimeoutError("provider timeout")

    timeout_result = _coordinator(root, TimeoutProvider()).run()
    assert timeout_result.terminal_state is GroundingLifecycleState.FAILED

    def fail(_self, _request, **_kwargs):
        raise GroundingExecutionError("source_stability_failed", "unstable")

    monkeypatch.setattr(GroundingExecutor, "execute", fail)
    executor_result = _coordinator(root, FakeProvider([_search()])).run()
    assert executor_result.terminal_state is GroundingLifecycleState.FAILED
    assert executor_result.terminal_reason is GroundingTerminalReason.EXECUTOR_FAILURE
    assert executor_result.observations == ()


def _unknown_citation(_context):
    return {
        "decision": "SUFFICIENT",
        "cited_observation_ids": ["does-not-exist"],
        "rationale": "bad citation",
    }


def _not_found_citation(context):
    return {
        "decision": "SUFFICIENT",
        "cited_observation_ids": [context.state.observation_history[0].observation_id],
        "rationale": "bad outcome",
    }


@pytest.mark.parametrize(
    ("first_action", "response_factory"),
    [(_search(), _unknown_citation), (_search("missing"), _not_found_citation)],
)
def test_invalid_sufficiency_is_insufficient_not_corrected(
    tmp_path, first_action, response_factory
):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    result = _coordinator(root, FakeProvider([first_action, response_factory])).run()

    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.INVALID_MODEL_REQUEST


def test_no_orientation_is_valid_and_mechanical_skip_uses_no_provider(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    result = _coordinator(
        root,
        FakeProvider([]),
        mechanical_skip=True,
    ).run()

    assert result.terminal_state is GroundingLifecycleState.SKIPPED
    assert result.orientation_advisory == {}


def test_strict_parser_rejects_unknown_wire_fields():
    with pytest.raises(ValueError):
        parse_grounding_provider_response(
            {"action": "inspect_file", "path": "app/a.py", "extra": True},
            after_observation=False,
        )
    with pytest.raises(ValueError):
        parse_grounding_provider_response(
            {"decision": "INSUFFICIENT", "reason": "x", "extra": True},
            after_observation=True,
        )


def test_illegal_lifecycle_transition_fails_closed(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "x = 1\n"})
    coordinator = _coordinator(root, FakeProvider([]))
    state = coordinator._initial_state()

    with pytest.raises(GroundingInvariantError):
        transition_grounding_state(state, GroundingLifecycleState.SUFFICIENT)


def test_default_settings_flag_is_independently_off(monkeypatch):
    monkeypatch.delenv("ENABLE_TYPED_GROUNDING_COORDINATOR", raising=False)
    assert Settings(_env_file=None).ENABLE_TYPED_GROUNDING_COORDINATOR is False
    assert settings.ENABLE_TYPED_GROUNDING_COORDINATOR is False


def _integration_context():
    state = SimpleNamespace(project_dir="/tmp/project")
    return SimpleNamespace(
        prompt="Find implementation",
        planner_contract=None,
        intent_mode="default",
        orchestration_state=state,
        emit_live=lambda *args, **kwargs: None,
        grounding_result=None,
    )


def _materialization(*, status="unknown", complete=False):
    item = SimpleNamespace(
        expected=True,
        status=status,
        creation_authorized=False,
        version_identity="version-1" if complete else None,
        content_hash="hash-1" if complete else None,
        content="source" if complete else None,
        truncated=False,
    )
    return SimpleNamespace(files=(item,), available=True, unavailable_reasons=())


def test_flag_off_calls_legacy_prepare_only(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_TYPED_GROUNDING_COORDINATOR", False)
    ctx = _integration_context()
    calls = []

    def legacy(**kwargs):
        calls.append(kwargs)
        ctx.planner_source_materialization = _materialization(
            status="existing", complete=True
        )

    result = prepare_planning_source_context(
        ctx=ctx,
        planning_timeout_seconds=1,
        extract_structured_text=lambda value: str(value),
        planner_service=object,
        emit_phase_event=lambda *args, **kwargs: None,
        materialize=lambda **kwargs: pytest.fail(
            "flag-off must not pre-materialize here"
        ),
        finalize_failure=lambda **kwargs: None,
        run_typed_grounding=lambda _ctx: pytest.fail("flag-off must not ground"),
        fail_typed_grounding=lambda **kwargs: pytest.fail(
            "flag-off must not fail typed"
        ),
        prepare_discovery=legacy,
    )

    assert result is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("terminal_state", "reason", "expected_reason"),
    [
        (
            "SUFFICIENT",
            GroundingTerminalReason.SUFFICIENT,
            "planning_grounding_handoff_failed",
        ),
        (
            "INSUFFICIENT",
            GroundingTerminalReason.INSUFFICIENT_GROUNDING,
            "planning_grounding_insufficient",
        ),
        (
            "FAILED",
            GroundingTerminalReason.EXECUTOR_FAILURE,
            "planning_grounding_failed",
        ),
    ],
)
def test_flag_on_malformed_sufficient_result_fails_before_planning(
    monkeypatch, terminal_state, reason, expected_reason
):
    monkeypatch.setattr(settings, "ENABLE_TYPED_GROUNDING_COORDINATOR", True)
    ctx = _integration_context()
    failures = []
    result_obj = SimpleNamespace(
        terminal_state=SimpleNamespace(value=terminal_state), terminal_reason=reason
    )

    result = prepare_planning_source_context(
        ctx=ctx,
        planning_timeout_seconds=1,
        extract_structured_text=lambda value: str(value),
        planner_service=object,
        emit_phase_event=lambda *args, **kwargs: None,
        materialize=lambda **kwargs: _materialization(),
        finalize_failure=lambda **kwargs: None,
        run_typed_grounding=lambda _ctx: result_obj,
        fail_typed_grounding=lambda **kwargs: failures.append(kwargs)
        or {"status": "failed"},
        prepare_discovery=lambda **kwargs: pytest.fail("legacy discovery must not run"),
    )

    assert result == {"status": "failed"}
    assert ctx.grounding_result is result_obj
    assert failures[0]["reason"] == expected_reason
    assert not hasattr(ctx, "planner_source_materialization")


def test_flag_on_mechanical_skip_can_use_initial_materialization(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_TYPED_GROUNDING_COORDINATOR", True)
    ctx = _integration_context()
    initial = _materialization(status=SOURCE_STATUS_EXISTING, complete=True)
    calls = []

    def materialize(**kwargs):
        calls.append(kwargs)
        return initial

    def run(ctx):
        assert ctx.grounding_mechanical_skip is True
        return SimpleNamespace(
            terminal_state=SimpleNamespace(value="SKIPPED"),
            terminal_reason=GroundingTerminalReason.SKIPPED,
        )

    result = prepare_planning_source_context(
        ctx=ctx,
        planning_timeout_seconds=1,
        extract_structured_text=lambda value: str(value),
        planner_service=object,
        emit_phase_event=lambda *args, **kwargs: None,
        materialize=materialize,
        finalize_failure=lambda **kwargs: None,
        run_typed_grounding=run,
        fail_typed_grounding=lambda **kwargs: pytest.fail("skip must not fail"),
        prepare_discovery=lambda **kwargs: pytest.fail("legacy discovery must not run"),
    )

    assert result is None
    assert ctx.planner_source_materialization is initial
    assert len(calls) == 1
