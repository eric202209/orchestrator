"""PHASE36-MAINT-GR12 — Planning repair context continuity.

REENTRY-5's initial Plan saw the permission block only through the advisory
``read_file`` observation (the canonical head window ended before it).  GR2
rejected that Plan's whole-file rewrite, and every repair prompt was then built
without the observation, so the repair could see only the head of the file.

These cases drive the real discovery reader, materializer, initial prompt
builder, validator and ``PlannerService.repair_output``.  Only the provider
seam (``_invoke_repair_prompt``) is replaced; no provider is called.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.orchestration.phases.planning_support as planning_support
from app.services.orchestration.planning import planner as planner_module
from app.services.orchestration.planning.planner import (
    PlannerService,
    PlanningRepairBudgetExceeded,
)
from app.services.orchestration.planning.read_only_discovery import (
    MAX_OBSERVATION_BYTES,
    render_discovery_observation,
)
from app.tests.test_phase36_maint_gr4_long_file_grounding_continuation import (
    PERMISSION_BLOCK,
    ROUTER,
    TARGET,
    TASK,
)
from app.tests.test_phase36_maint_gr5_planning_visible_narrow_mutation_authority import (
    GR2_CODE,
    _observed,
    _plan,
    _read_observation,
    _replace,
    _search_observation,
    _validate,
    _workspace,
)

OBSERVATION_HEADER = "## READ-ONLY OBSERVATION"
SOURCE_HEADER = "## CURRENT SOURCE MATERIALIZATION"
DEFECT = 'prefix="/permissions"'
REWRITE_MARKER = "# GR12 regenerated tail marker"
REWRITE = ROUTER.replace(DEFECT + ",\n", "") + REWRITE_MARKER + "\n"
INSPECT_ONLY = [
    {
        "step_number": 1,
        "description": "Inspect the router.",
        "commands": [f"cat {TARGET}"],
        "verification": None,
        "rollback": None,
        "expected_files": [],
        "ops": [],
    }
]


@pytest.fixture
def captured(monkeypatch) -> list[str]:
    prompts: list[str] = []

    async def fake_invoke(runtime_service, repair_prompt, repair_timeout, **kwargs):
        prompts.append(repair_prompt)
        return {"status": "completed", "output": json.dumps(INSPECT_ONLY)}

    monkeypatch.setattr(
        PlannerService, "_invoke_repair_prompt", staticmethod(fake_invoke)
    )
    return prompts


def _fixture(tmp_path: Path):
    root = _workspace(tmp_path)
    observation = _read_observation(root)
    materialization = _observed(root, observation)
    rejected = _plan({"op": "write_file", "path": TARGET, "content": REWRITE})
    verdict = _validate(root, rejected, materialization)
    return root, observation, materialization, rejected, verdict


def _repair(
    root: Path,
    materialization: Any,
    malformed: Any,
    reasons: list[str],
    **kwargs: Any,
) -> dict[str, Any]:
    return PlannerService.repair_output(
        runtime_service=object(),
        task_description=TASK,
        malformed_output=json.dumps(malformed),
        project_dir=root,
        timeout_seconds=120,
        logger=logging.getLogger("gr12-test"),
        emit_live=lambda *args, **kw: None,
        reason="plan_validation_failed: " + (reasons[0] if reasons else ""),
        rejection_reasons=reasons,
        source_materialization=materialization,
        **kwargs,
    )


def _section(prompt: str, header: str) -> str:
    """Text from ``header`` up to the next heading or the rejected-Plan block."""
    start = prompt.index(header)
    ends = [
        index
        for index in (
            prompt.find("\n## ", start + len(header)),
            prompt.find("\nBad:\n", start),
        )
        if index != -1
    ]
    return prompt[start : min(ends)] if ends else prompt[start:]


# --- R1/R2: the initial attempt -------------------------------------------


def test_r1_initial_planning_receives_advisory_observation(tmp_path):
    root, observation, materialization, _, _ = _fixture(tmp_path)

    prompt = PlannerService.build_minimal_planning_prompt(
        TASK,
        root,
        source_materialization=materialization,
        read_only_observation=observation,
    )

    assert DEFECT in _section(prompt, OBSERVATION_HEADER)
    assert DEFECT not in _section(prompt, SOURCE_HEADER)


def test_r2_gr2_rejects_initial_whole_file_rewrite(tmp_path):
    *_, verdict = _fixture(tmp_path)

    assert not verdict.accepted
    assert any(GR2_CODE in str(reason) for reason in verdict.reasons)


# --- R3/R4: continuity across repair 1 and repair 2 ------------------------


def test_r3_r4_both_repairs_retain_the_observation(tmp_path, captured):
    root, observation, materialization, rejected, verdict = _fixture(tmp_path)

    _repair(
        root,
        materialization,
        rejected,
        list(verdict.reasons),
        read_only_observation=observation,
    )
    _repair(
        root,
        materialization,
        INSPECT_ONLY,
        ["plan_validation_failed: removed_materialization"],
        read_only_observation=observation,
    )

    assert len(captured) == 2
    for prompt in captured:
        block = _section(prompt, OBSERVATION_HEADER)
        assert block == render_discovery_observation(observation)
        assert PERMISSION_BLOCK.strip() in block


def test_entering_shape_without_observation_has_no_defect_region(tmp_path, captured):
    root, _, materialization, rejected, verdict = _fixture(tmp_path)

    _repair(root, materialization, rejected, list(verdict.reasons))

    assert OBSERVATION_HEADER not in captured[0]
    assert DEFECT not in captured[0]


# --- R5-R9: provenance, separation, retained context ----------------------


def test_r5_r6_observation_stays_advisory_and_separate_from_canonical_source(
    tmp_path, captured
):
    root, observation, materialization, rejected, verdict = _fixture(tmp_path)

    _repair(
        root,
        materialization,
        rejected,
        list(verdict.reasons),
        read_only_observation=observation,
    )
    prompt = captured[0]

    observation_block = _section(prompt, OBSERVATION_HEADER)
    assert "advisory context only" in observation_block
    assert "current source materialization and all existing validation remain " in (
        observation_block
    )
    canonical = _section(prompt, SOURCE_HEADER)
    assert "truncated: true" in canonical
    assert DEFECT not in canonical
    assert "Never reconstruct a whole file from a partial excerpt." in canonical
    # The observation is not merged into the task text or the canonical record.
    assert DEFECT not in TASK
    assert DEFECT not in materialization.file_map()[TARGET].content


def test_r7_r8_rejection_reason_and_rejected_plan_retained_not_as_source(
    tmp_path, captured
):
    root, observation, materialization, rejected, verdict = _fixture(tmp_path)

    _repair(
        root,
        materialization,
        rejected,
        list(verdict.reasons),
        read_only_observation=observation,
    )
    prompt = captured[0]

    assert GR2_CODE in prompt
    assert "Bad:" in prompt
    # The rejected rewrite's body never enters a source or observation section.
    assert REWRITE_MARKER not in _section(prompt, SOURCE_HEADER)
    assert REWRITE_MARKER not in _section(prompt, OBSERVATION_HEADER)


def test_r9_source_identity_is_the_same_materialization(tmp_path, captured):
    root, observation, materialization, rejected, verdict = _fixture(tmp_path)

    _repair(
        root,
        materialization,
        rejected,
        list(verdict.reasons),
        read_only_observation=observation,
    )

    assert materialization.to_prompt_block(provider_safe=True) in captured[0]


# --- R10-R12: boundedness, absence, supersession --------------------------


def test_r10_observation_is_rendered_once_and_bounded(tmp_path, captured):
    root, observation, materialization, rejected, verdict = _fixture(tmp_path)
    already = render_discovery_observation(observation)

    _repair(
        root,
        materialization,
        rejected,
        list(verdict.reasons),
        read_only_observation=observation,
        guidance_block=already,
    )

    assert captured[0].count(OBSERVATION_HEADER) == 1
    assert len(already.encode("utf-8")) <= MAX_OBSERVATION_BYTES


def test_r10_search_observation_is_bounded_and_labelled(tmp_path, captured):
    root = _workspace(tmp_path)
    observation = _search_observation(root, "permissions")
    materialization = _observed(root, observation)

    _repair(
        root,
        materialization,
        INSPECT_ONLY,
        ["plan_validation_failed: removed_materialization"],
        read_only_observation=observation,
    )

    block = _section(captured[0], OBSERVATION_HEADER)
    assert "action: search_text" in block
    assert "advisory context only" in block
    assert len(block.encode("utf-8")) <= MAX_OBSERVATION_BYTES


def test_r10_over_budget_observation_is_omitted_not_fatal(
    tmp_path, captured, monkeypatch
):
    root, observation, materialization, rejected, verdict = _fixture(tmp_path)
    # Match CI's fail-safe builder budget.  The local .env may declare a large
    # repair context, which otherwise hides compaction of the observation path.
    monkeypatch.setattr(
        planner_module.repair_prompts.settings,
        "PLANNING_REPAIR_CONTEXT_TOKENS",
        None,
    )
    _repair(root, materialization, rejected, list(verdict.reasons))
    entering_prompt = captured.pop()
    # A budget between the entering prompt and the observed prompt.
    monkeypatch.setattr(
        planner_module, "_repair_prompt_budget", lambda: len(entering_prompt) + 10
    )

    _repair(
        root,
        materialization,
        rejected,
        list(verdict.reasons),
        read_only_observation=observation,
    )

    assert captured[-1] == entering_prompt


def test_r10_builder_omission_reuses_entering_prompt(tmp_path, captured, monkeypatch):
    root, observation, materialization, rejected, verdict = _fixture(tmp_path)
    real_build = PlannerService.build_planning_repair_prompt_with_metadata.__func__

    def compact_without_observation(cls, *args, **kwargs):
        result = real_build(cls, *args, **kwargs)
        if OBSERVATION_HEADER in kwargs.get("guidance_block", ""):
            return SimpleNamespace(prompt="compact repair prompt", metadata={})
        return result

    monkeypatch.setattr(
        PlannerService,
        "build_planning_repair_prompt_with_metadata",
        classmethod(compact_without_observation),
    )

    _repair(root, materialization, rejected, list(verdict.reasons))
    entering_prompt = captured.pop()
    monkeypatch.setattr(
        planner_module, "_repair_prompt_budget", lambda: len(entering_prompt) + 10
    )

    _repair(
        root,
        materialization,
        rejected,
        list(verdict.reasons),
        read_only_observation=observation,
    )

    assert captured[-1] == entering_prompt


def test_r10_genuine_overflow_still_fails_closed(tmp_path, captured, monkeypatch):
    root, observation, materialization, rejected, verdict = _fixture(tmp_path)
    monkeypatch.setattr(planner_module, "PLANNING_REPAIR_PROMPT_MAX_CHARS", 100)

    with pytest.raises(PlanningRepairBudgetExceeded):
        _repair(
            root,
            materialization,
            rejected,
            list(verdict.reasons),
            read_only_observation=observation,
        )
    assert captured == []


def test_r11_no_observation_is_byte_identical_to_entering_prompt(tmp_path, captured):
    root, _, materialization, rejected, verdict = _fixture(tmp_path)

    _repair(root, materialization, rejected, list(verdict.reasons))
    _repair(
        root,
        materialization,
        rejected,
        list(verdict.reasons),
        read_only_observation=None,
    )

    assert captured[0] == captured[1]
    assert OBSERVATION_HEADER not in captured[0]


def _funnel_ctx(observation: Any) -> SimpleNamespace:
    return SimpleNamespace(
        planning_repair_evidence_seq=0,
        intent_mode="default",
        runtime_service=object(),
        prompt=TASK,
        orchestration_state=SimpleNamespace(project_dir=Path(".")),
        logger=logging.getLogger("gr12-funnel"),
        emit_live=lambda *args, **kw: None,
        workflow_profile="default",
        workflow_phases=[],
        workspace_has_existing_files=True,
        session_id=1,
        task_id=1,
        planner_contract=None,
        planner_source_materialization="materialization",
        grounding_planning_context=None,
        execution_profile="full_lifecycle",
        read_only_observation=observation,
    )


def test_r12_repair_funnel_forwards_the_current_ctx_observation(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    first = _read_observation(root)
    superseding = _search_observation(root, "permissions")
    received: list[dict[str, Any]] = []
    monkeypatch.setattr(planning_support, "_collect_repair_guidance", lambda ctx: "")
    monkeypatch.setattr(
        planning_support, "_planner_workspace_identity", lambda ctx: None
    )
    monkeypatch.setattr(
        planning_support.PlannerService,
        "repair_output",
        lambda **kwargs: received.append(kwargs) or {},
    )
    ctx = _funnel_ctx(first)

    for _ in range(2):
        planning_support._repair_planning_output(
            ctx=ctx,
            planning_timeout_seconds=60,
            malformed_output="[]",
            reason="plan_validation_failed",
        )
    ctx.read_only_observation = superseding
    planning_support._repair_planning_output(
        ctx=ctx,
        planning_timeout_seconds=60,
        malformed_output="[]",
        reason="plan_validation_failed",
    )

    assert [item["read_only_observation"] for item in received] == [
        first,
        first,
        superseding,
    ]
    # GR3: the observation never enters the task text.
    assert all(item["task_description"] == TASK for item in received)


# --- R13/R16: authority stays where it was --------------------------------


def test_r13_gr2_still_rejects_resubmitted_whole_file_rewrite(tmp_path, captured):
    root, observation, materialization, rejected, verdict = _fixture(tmp_path)

    _repair(
        root,
        materialization,
        rejected,
        list(verdict.reasons),
        read_only_observation=observation,
    )
    # A repair that echoes the rejected rewrite is judged on the same
    # materialization; the observation is never source evidence for GR2.
    again = _validate(root, rejected, materialization)

    assert not again.accepted
    assert any(GR2_CODE in str(reason) for reason in again.reasons)


def test_r16_gr5_narrow_replace_from_observation_is_verified_system_side(
    tmp_path,
):
    root = _workspace(tmp_path)
    observation = _read_observation(root)
    materialization = _observed(root, observation)
    narrow = _plan(_replace())

    accepted = _validate(root, narrow, materialization)
    # The same exact edit becomes stale once the file changes: authorization
    # is the system-side version fence, not the observation that showed it.
    (root / TARGET).write_text(
        ROUTER.replace(PERMISSION_BLOCK, PERMISSION_BLOCK.replace("Approval", "X")),
        encoding="utf-8",
    )
    stale = _validate(root, narrow, materialization)

    assert accepted.accepted
    assert not stale.accepted


# --- REENTRY-5 counterfactual prompt replay ---------------------------------


def test_reentry5_counterfactual_repair_prompt_exposes_defect_as_advisory(
    tmp_path, captured
):
    root, observation, materialization, rejected, verdict = _fixture(tmp_path)

    _repair(
        root,
        materialization,
        rejected,
        list(verdict.reasons),
        read_only_observation=observation,
    )
    _repair(
        root,
        materialization,
        INSPECT_ONLY,
        ["plan_validation_failed: removed_materialization"],
        read_only_observation=observation,
    )

    for prompt in captured:
        block = _section(prompt, OBSERVATION_HEADER)
        assert "permissions.router," in block
        assert DEFECT in block
        assert "dependencies=[Depends(get_current_active_user)]" in block
        assert "advisory context only" in block
        assert DEFECT not in _section(prompt, SOURCE_HEADER)


def test_r10_observation_that_breaks_repair_projection_is_omitted(
    tmp_path, captured, monkeypatch
):
    root, observation, materialization, rejected, verdict = _fixture(tmp_path)
    real_build = PlannerService.build_planning_repair_prompt_with_metadata.__func__

    def projection_fails_with_observation(cls, *args, **kwargs):
        result = real_build(cls, *args, **kwargs)
        if OBSERVATION_HEADER in kwargs.get("guidance_block", ""):
            return SimpleNamespace(
                prompt="",
                metadata={"repair_prompt_failure": {"reason": "over_bound"}},
            )
        return result

    monkeypatch.setattr(
        PlannerService,
        "build_planning_repair_prompt_with_metadata",
        classmethod(projection_fails_with_observation),
    )

    _repair(
        root,
        materialization,
        rejected,
        list(verdict.reasons),
        read_only_observation=observation,
    )

    assert len(captured) == 1
    assert OBSERVATION_HEADER not in captured[0]
