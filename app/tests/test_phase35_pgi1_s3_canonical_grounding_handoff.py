"""Provider-free Slice 3 canonical GroundingResult handoff tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.orchestration.context.assembly import assemble_planning_prompt
from app.services.orchestration.phases.planning_grounding_integration import (
    _mechanical_grounding_skip,
    prepare_planning_source_context,
)
from app.services.orchestration.planning.grounding import (
    GroundingAssessmentKind,
    GroundingCoordinator,
    GroundingExecutor,
    GroundingHandoffError,
    GroundingLifecycleState,
    GroundingPlanningContext,
    GroundingProposal,
    GroundingRunConfig,
    GroundingTaskReference,
    GroundingTerminalReason,
    build_grounding_planning_context,
    project_grounding_result_to_input_manifest,
)
from app.services.orchestration.planning.source_materialization import (
    SELECTION_HEAD_FALLBACK,
)
from app.services.planning.input_manifest import build_input_manifest
from app.services.planning.planning_brief_stage_support import (
    build_planning_brief_provider_input,
)


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, shell=False)
    return tmp_path


class _Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def decide(self, context):
        self.calls += 1
        response = self.responses.pop(0)
        return response(context) if callable(response) else response


def _result(root: Path, responses, *, max_exploration_provider_requests: int = 4):
    provider = _Provider(responses)
    config = GroundingRunConfig(
        grounding_run_id="slice3-run",
        task_reference=GroundingTaskReference(task_id="task-1"),
        workspace_identity=str(root.resolve()),
        snapshot_identity="snapshot-1",
        max_steps=5,
        max_exploration_provider_requests=max_exploration_provider_requests,
        operator_task="Find the implementation.",
    )
    result = GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity="snapshot-1"),
        provider=provider,
        config=config,
    ).run()
    return result, provider


def _sufficient(context):
    observation = context.state.observation_history[-1]
    return {
        "decision": "SUFFICIENT",
        "cited_observation_ids": [observation.observation_id],
        "rationale": "The cited repository evidence is sufficient.",
    }


def _manifest(result=None):
    return build_input_manifest(
        session_id=1,
        session_generation_id="generation-1",
        planning_request={"message_id": 1, "role": "user", "content": "task"},
        project_metadata={"project_id": 1, "name": "test"},
        runtime_configuration={"provider": "fake", "backend": "fake", "model": "fake"},
        grounding_result=result,
    )


def test_direct_success_projects_only_cited_found_evidence(tmp_path):
    """EPR1: a direct success projects the cited *substantive* evidence.

    Before EPR1 this drove a single-path search_text and asserted that the
    searched path materialized.  That contract is intentionally replaced: the
    single-path shape hid the ODF1 D1 duplication, and a search citation now
    carries no source of its own.
    """

    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    result, provider = _result(
        root,
        [
            {"action": "inspect_file", "path": "app/sample.py"},
            _sufficient,
        ],
    )

    context = build_grounding_planning_context(result, project_dir=root)

    assert provider.calls == 2
    assert isinstance(context, GroundingPlanningContext)
    assert context.result is result
    assert tuple(item.observation_id for item in context.cited_observations) == (
        result.cited_observation_ids[0],
    )
    assert [item.relative_path for item in context.source_materialization.files] == [
        "app/sample.py"
    ]
    assert context.source_materialization.files[0].content == "needle = True\n"
    assert context.source_materialization.files[0].target_hint is None
    assert "## GROUNDING EVIDENCE" in context.grounding_section
    assert "## CITED SOURCE EVIDENCE" in context.cited_source_section
    assert "not operator instruction" in context.grounding_section


def test_search_only_success_is_refused_before_planning_materialization(tmp_path):
    """EPR1 replaces the old search-only success contract (ODF1 D1).

    Multi-path on purpose: the pre-EPR1 projection duplicated one rendered hit
    block once per cited path, and a single-path fixture could never show it.
    """

    root = _repo(
        tmp_path,
        {f"app/mod{index}.py": "needle = True\n" for index in range(5)},
    )
    result, provider = _result(
        root,
        [
            {"action": "search_text", "query": "needle", "scopes": ["app"]},
            _sufficient,
        ],
    )

    # Grounding itself still fails closed: the coordinator refuses to call a
    # search-only citation SUFFICIENT.
    assert result.terminal_state is GroundingLifecycleState.INSUFFICIENT
    assert result.terminal_reason is GroundingTerminalReason.INVALID_MODEL_REQUEST
    assert provider.calls == 2

    with pytest.raises(GroundingHandoffError) as excinfo:
        build_grounding_planning_context(result, project_dir=root)
    assert excinfo.value.code == "result_not_sufficient"


def test_search_only_citation_cannot_cross_the_handoff_fence(tmp_path):
    """The handoff refuses a search-only citation on its own authority."""

    root = _repo(
        tmp_path,
        {f"app/mod{index}.py": "needle = True\n" for index in range(5)},
    )
    result, _provider = _result(
        root,
        [
            {"action": "search_text", "query": "needle", "scopes": ["app"]},
            lambda _context: {
                "decision": "NEED_MORE_EVIDENCE",
                "next_action": {"action": "inspect_file", "path": "app/mod0.py"},
                "rationale": "One candidate needs substantive inspection.",
            },
            _sufficient,
        ],
    )
    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    search = result.observations[0]
    assert search.action_identity == "search_text"
    assert len(search.source_paths) == 5

    # Forge the citation set the coordinator would never emit, so the handoff
    # gate is proven on its own rather than only behind the coordinator.
    forged = replace(
        result,
        cited_observation_ids=(search.observation_id,),
        cited_source_paths=search.source_paths,
    )

    with pytest.raises(GroundingHandoffError) as excinfo:
        build_grounding_planning_context(forged, project_dir=root)
    assert excinfo.value.code == "insufficient_substantive_evidence"


def test_not_found_history_is_not_materialized_when_refinement_cites_found(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})

    def refine(_context):
        return {
            "decision": "NEED_MORE_EVIDENCE",
            "next_action": {"action": "inspect_file", "path": "app/sample.py"},
            "rationale": "The first bounded query was negative.",
        }

    result, _ = _result(
        root,
        [
            {"action": "search_text", "query": "wrong", "scopes": ["app"]},
            refine,
            _sufficient,
        ],
        max_exploration_provider_requests=4,
    )

    context = build_grounding_planning_context(result, project_dir=root)

    assert [item.outcome.value for item in result.observations] == [
        "NOT_FOUND",
        "FOUND",
    ]
    assert len(context.cited_observations) == 1
    assert context.cited_observations[0].outcome.value == "FOUND"
    assert "NOT_FOUND" in context.grounding_section
    assert "wrong" not in context.cited_source_section


def test_structural_identity_is_preserved_without_a_target_hint(tmp_path):
    root = _repo(
        tmp_path,
        {"app/sample.py": "def target():\n    return 'needle'\n\nneedle = 2\n"},
    )

    def sufficient(context):
        observation = context.state.observation_history[-1]
        assert observation.structural_identity is not None
        return GroundingProposal(
            assessment_kind=GroundingAssessmentKind.SUFFICIENT,
            cited_observation_ids=(observation.observation_id,),
            rationale="The exact symbol region is cited.",
            unresolved_risk=False,
        )

    result, _ = _result(
        root,
        [
            {
                "action": "resolve_structure",
                "relation": "symbol_definition",
                "locator": {"path": "app/sample.py", "name": "target"},
            },
            sufficient,
        ],
    )

    context = build_grounding_planning_context(result, project_dir=root)
    record = context.source_materialization.files[0]
    identity = result.observations[0].structural_identity

    assert identity is not None
    assert record.selection_strategy == "grounding_structural_region"
    assert record.start_line == identity.start_line
    assert record.end_line == identity.end_line
    assert record.start_byte == identity.start_byte
    assert record.end_byte == identity.end_byte
    assert record.target_hint is None
    assert "symbol_definition" in context.cited_source_section


def test_mounted_route_projection_preserves_route_identity_and_source_fences(tmp_path):
    root = _repo(
        tmp_path,
        {
            "app/api/v1/endpoints/auth.py": (
                "from fastapi import APIRouter\n"
                "router = APIRouter()\n"
                "@router.post('/login')\n"
                "async def login():\n"
                "    return {'ok': True}\n"
            ),
            "app/api/v1/router.py": (
                "from fastapi import APIRouter\n"
                "from app.api.v1.endpoints.auth import router as auth_router\n"
                "api_router = APIRouter()\n"
                "api_router.include_router(auth_router, prefix='/auth')\n"
            ),
        },
    )
    result, _ = _result(
        root,
        [
            {
                "action": "resolve_structure",
                "relation": "mounted_route",
                "locator": {
                    "path": "app/api/v1/endpoints/auth.py",
                    "method": "POST",
                    "decorator_path": "/login",
                },
            },
            _sufficient,
        ],
    )

    context = build_grounding_planning_context(result, project_dir=root)
    identity = context.cited_observations[0].structural_identity

    assert identity is not None
    assert identity.decorator_path == "/login"
    assert identity.effective_route_path == "/auth/login"
    assert identity.handler_name == "login"
    assert "app/api/v1/endpoints/auth.py" in result.source_versions
    assert "app/api/v1/router.py" in result.source_versions
    assert "effective_route_path=/auth/login" in context.cited_source_section
    assert "source_version:" in context.cited_source_section


def test_stale_citation_fails_closed_before_planning(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    result, _ = _result(
        root,
        [
            {"action": "inspect_file", "path": "app/sample.py"},
            _sufficient,
        ],
    )
    (root / "app/sample.py").write_text(
        "needle = False\nchanged = True\n", encoding="utf-8"
    )

    with pytest.raises(GroundingHandoffError, match="stale_citation"):
        build_grounding_planning_context(result, project_dir=root)


def test_invalid_citation_never_reaches_materialization(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    result, _ = _result(
        root,
        [
            {"action": "inspect_file", "path": "app/sample.py"},
            _sufficient,
        ],
    )
    invalid = replace(result, cited_observation_ids=("missing-observation",))

    with pytest.raises(GroundingHandoffError, match="citation_unknown"):
        build_grounding_planning_context(invalid, project_dir=root)


def test_same_result_projects_into_protocol_v2_manifest_before_brief(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    result, _ = _result(
        root,
        [
            {"action": "inspect_file", "path": "app/sample.py"},
            _sufficient,
        ],
    )

    manifest = _manifest()
    projected = project_grounding_result_to_input_manifest(manifest, result)
    projected.validate()
    provider_input = build_planning_brief_provider_input(
        SimpleNamespace(
            input_manifest=projected,
            configuration={},
            session=SimpleNamespace(project_id=1),
        )
    )

    source = next(
        item for item in projected.sources if item.source_type == "grounding_evidence"
    )
    assert source.identity_metadata["grounding_run_id"] == result.grounding_run_id
    assert any(
        item["source_type"] == "grounding_evidence"
        and item["content"]["grounding_run_id"] == result.grounding_run_id
        for item in provider_input.sources
    )
    assert provider_input.manifest_hash == projected.manifest_hash


def test_grounding_projection_is_immutable_and_independent(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    result, _ = _result(
        root,
        [
            {"action": "inspect_file", "path": "app/sample.py"},
            _sufficient,
        ],
    )
    original_ids = result.cited_observation_ids
    legacy_context = build_grounding_planning_context(result, project_dir=root)
    v2_manifest = project_grounding_result_to_input_manifest(_manifest(), result)

    assert result.cited_observation_ids == original_ids
    assert legacy_context.result is result
    assert v2_manifest is not result
    with pytest.raises((AttributeError, TypeError)):
        result.cited_observation_ids += ("changed",)


def test_typed_prepare_seam_hands_off_without_legacy_fallback(monkeypatch, tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    result, _ = _result(
        root,
        [
            {"action": "inspect_file", "path": "app/sample.py"},
            _sufficient,
        ],
    )
    ctx = SimpleNamespace(
        prompt="Find the implementation.",
        planner_contract=None,
        intent_mode="default",
        orchestration_state=SimpleNamespace(project_dir=str(root)),
        emit_live=lambda *args, **kwargs: None,
        grounding_result=None,
    )
    materialize_calls = []
    monkeypatch.setattr(settings, "ENABLE_TYPED_GROUNDING_COORDINATOR", True)

    result_value = prepare_planning_source_context(
        ctx=ctx,
        planning_timeout_seconds=1,
        extract_structured_text=lambda value: str(value),
        planner_service=object,
        emit_phase_event=lambda *args, **kwargs: None,
        materialize=lambda **kwargs: materialize_calls.append(kwargs)
        or SimpleNamespace(files=(), available=True, unavailable_reasons=()),
        finalize_failure=lambda **kwargs: pytest.fail("typed success must not fail"),
        run_typed_grounding=lambda _ctx: result,
        fail_typed_grounding=lambda **kwargs: pytest.fail(
            "typed success must not invoke failure"
        ),
        prepare_discovery=lambda **kwargs: pytest.fail(
            "legacy discovery must not run after typed grounding"
        ),
    )

    assert result_value is None
    assert ctx.grounding_result is result
    assert ctx.planner_source_materialization.files[0].relative_path == "app/sample.py"
    assert "## GROUNDING EVIDENCE" in ctx.planning_grounding_context
    assert len(materialize_calls) == 1


def test_head_fallback_cannot_mechanically_satisfy_typed_grounding():
    item = SimpleNamespace(
        expected=True,
        status="existing_file_with_materialized_source",
        version_identity="v1",
        content_hash="h1",
        content="head",
        truncated=False,
        selection_strategy=SELECTION_HEAD_FALLBACK,
    )
    assert not _mechanical_grounding_skip(SimpleNamespace(files=(item,)), "default")


def test_operator_task_remains_separate_from_canonical_grounding_sections(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    result, _ = _result(
        root,
        [
            {"action": "inspect_file", "path": "app/sample.py"},
            _sufficient,
        ],
    )
    grounding_context = build_grounding_planning_context(result, project_dir=root)
    task = "Change the explicitly named target app/sample.py."
    state = SimpleNamespace(
        project_dir=str(root),
        project_workspace_path=str(root),
        project_context="Project context",
        phase_history=[],
        validation_history=[],
        session_id=None,
        task_id=None,
        project_name="test",
        artifact_supplement=None,
    )
    ctx = SimpleNamespace(
        orchestration_state=state,
        db=None,
        execution_profile="full_lifecycle",
        prompt=task,
        workflow_profile="default",
        planning_adaptation_profile="openclaw_default",
        planner_source_materialization=grounding_context.source_materialization,
        grounding_planning_context=grounding_context,
        planner_contract=None,
        read_only_observation=None,
        project=None,
    )

    prompt = assemble_planning_prompt(ctx, {"has_existing_files": True})

    assert task in prompt
    assert "## GROUNDING EVIDENCE" in prompt
    assert "## CITED SOURCE EVIDENCE" in prompt
    assert prompt.index("## GROUNDING EVIDENCE") > prompt.index(task)
    assert "target_hint" not in prompt
    assert grounding_context.source_materialization.files[0].target_hint is None


def test_v2_projection_does_not_issue_a_second_grounding_request(tmp_path):
    root = _repo(tmp_path, {"app/sample.py": "needle = True\n"})
    result, provider = _result(
        root,
        [
            {"action": "inspect_file", "path": "app/sample.py"},
            _sufficient,
        ],
    )
    provider_calls_before = provider.calls
    provider_input = build_planning_brief_provider_input(
        SimpleNamespace(
            input_manifest=_manifest(result),
            configuration={},
            session=SimpleNamespace(project_id=1),
        )
    )

    assert provider.calls == provider_calls_before == 2
    assert provider_input.manifest_id.startswith("manifest:")
