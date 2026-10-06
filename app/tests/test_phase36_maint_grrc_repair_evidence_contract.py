"""PHASE36-MAINT GRRC — GR13 repair evidence contract.

REENTRY-13's GR13 repair provider received only the entering canonical source
(``permissions.py``) plus the GR12 observation.  The repair prompt builder's
own internal materialization (``router.py`` lines 1-71) was suppressed from the
prompt but still written to repair metadata, so the retained record implied
the provider had seen ``router.py`` (G1).  Repair metadata now labels every
builder record with whether it reached the provider and records the exact
provider-visible source separately.

These cases also pin that the production GR13 funnel keeps the initial
attempt's observed-path replace affordance (the RTRV G2 reproduction bypassed
``collect_repair_guidance_block``) and that no evidence, path or mutation
authority changed.  No provider is called.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from app.services.orchestration.phases import planning_support
from app.services.orchestration.phases.post_plan_source_grounding import (
    FAILURE_CLASS_PLAN_TARGET_UNGROUNDABLE,
    POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
    ground_post_plan_source_materialization,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.read_only_discovery import (
    execute_discovery_request,
    materialize_observation_source_context,
    parse_discovery_request,
)
from app.services.orchestration.planning.repair_prompts import (
    build_planning_repair_prompt_with_metadata,
)
from app.services.orchestration.planning.source_materialization import (
    MaterializedSourceFile,
    PlannerSourceMaterialization,
    describe_provider_visible_source,
    materialize_planner_source_context,
    observed_candidate_paths,
)

ROUTER = "app/api/v1/router.py"
PERM = "app/api/v1/endpoints/permissions.py"
TASK = (
    "The public permission routes are registered under a doubled path. "
    "Fix the registration so the endpoints are reachable at their documented "
    "location and keep all other routes unchanged."
)
ENDPOINTS = ["auth", "users", "projects", "tasks", "sessions", "isolation"]
ROUTER_SRC = (
    '"""API v1 router."""\n\nfrom fastapi import APIRouter, Depends\n\n'
    "from app.api.v1.endpoints import isolation, permissions, context\n"
    "from app.dependencies import get_current_active_user\n\n"
    "api_router = APIRouter()\n\n"
    + "".join(
        f"api_router.include_router(\n    {name}.router,\n"
        f'    prefix="/{name}{i}",\n    tags=["{name}"],\n)\n\n'
        for i in range(4)
        for name in ENDPOINTS
    )
    + "api_router.include_router(\n    permissions.router,\n"
    '    prefix="/permissions",\n    tags=["permissions"],\n'
    "    dependencies=[Depends(get_current_active_user)],\n)\n"
)
PERM_SRC = (
    '"""Permission API endpoints."""\n\nfrom fastapi import APIRouter\n\n'
    "router = APIRouter()\n\n\n"
    + "".join(
        f'@router.get("/permissions/r{i}")\nasync def r{i}():\n'
        f'    return {{"i": {i}}}\n\n\n'
        for i in range(120)
    )
)
FABRICATED_OLD = (
    'app.include_router(permissions.router, prefix="/permissions", '
    'tags=["permissions"])'
)
PLAN = [
    {
        "step_number": 1,
        "description": "Remove the doubled permissions prefix",
        "commands": [],
        "verification": f"python -m py_compile {ROUTER}",
        "rollback": None,
        "expected_files": [ROUTER],
        "ops": [
            {
                "op": "replace_in_file",
                "path": ROUTER,
                "old": FABRICATED_OLD,
                "new": "app.include_router(permissions.router)",
            }
        ],
    }
]
SOURCE_HEADER = "## CURRENT SOURCE MATERIALIZATION"
OBSERVATION_HEADER = "## READ-ONLY OBSERVATION"
LEGACY_OFFERED = "Legacy replace_in_file may use exact old/new"
VERBATIM = "Copy old verbatim from text supplied for that path"
UNAVAILABLE = "Legacy replace_in_file is unavailable"


def _workspace(root: Path) -> Path:
    root = root.resolve()
    (root / "app/api/v1/endpoints").mkdir(parents=True)
    (root / ROUTER).write_text(ROUTER_SRC, encoding="utf-8")
    (root / PERM).write_text(PERM_SRC, encoding="utf-8")
    return root


def _entering(root: Path, *, observe: bool = True):
    observation = execute_discovery_request(
        root,
        parse_discovery_request(json.dumps({"action": "read_file", "path": PERM})),
    )
    materialization = materialize_observation_source_context(
        project_dir=root,
        prompt=TASK,
        planner_contract=None,
        observation=observation,
        materialize=materialize_planner_source_context,
        source_cache={},
    )
    return (observation if observe else None), materialization


def _gr13_repair(root: Path, monkeypatch, *, observe: bool = True):
    """Drive the production GR13 dispatch funnel with only the provider faked."""

    observation, materialization = _entering(root, observe=observe)
    grounding = ground_post_plan_source_materialization(
        PLAN, project_dir=root, source_materialization=materialization
    )
    assert grounding.failure_class == FAILURE_CLASS_PLAN_TARGET_UNGROUNDABLE
    assert grounding.failure_code == POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE
    prompts: list[str] = []
    events: list[dict] = []

    async def fake_repair(runtime_service, repair_prompt, repair_timeout, **kwargs):
        prompts.append(repair_prompt)
        return {"status": "completed", "output": json.dumps(PLAN)}

    monkeypatch.setattr(
        PlannerService, "_invoke_repair_prompt", staticmethod(fake_repair)
    )
    monkeypatch.setattr(planning_support, "_planner_workspace_identity", lambda c: None)
    ctx = SimpleNamespace(
        db=None,
        project=SimpleNamespace(id=1, user_id=None),
        session_id=1,
        task_id=1,
        prompt=TASK,
        planner_source_materialization=materialization,
        read_only_observation=observation,
        intent_mode="default",
        execution_profile="full_lifecycle",
        runtime_service=object(),
        orchestration_state=SimpleNamespace(project_dir=root),
        logger=logging.getLogger("test.grrc"),
        emit_live=lambda *args, **kwargs: events.append(kwargs.get("metadata") or {}),
        workflow_profile="default",
        workflow_phases=[],
        workspace_has_existing_files=True,
        planner_contract=None,
        grounding_planning_context=None,
        planning_repair_evidence_seq=0,
    )
    planning_support._repair_planning_output(
        ctx=ctx,
        planning_timeout_seconds=240,
        malformed_output=json.dumps(PLAN),
        reason=f"{grounding.failure_code}: post-Plan grounding failed for {ROUTER}",
        rejection_reasons=planning_support._post_plan_grounding_repair_reasons(
            PLAN, grounding
        ),
    )
    assert len(prompts) == 1  # GR13 owns exactly one repair pass.
    metadata = next(event for event in events if "repair_source_evidence" in event)
    return prompts[0], metadata, observation, materialization


def _builder_file(metadata: dict, path: str) -> dict:
    files = metadata["planner_source_materialization"]["files"]
    return next(item for item in files if item["relative_path"] == path)


# --- G1: builder materialization vs provider-visible evidence ---------------


def test_g1_t5_reentry13_shape_never_reports_router_as_provider_visible(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path)
    prompt, metadata, observation, entering = _gr13_repair(root, monkeypatch)

    # What the provider actually received: no router.py source at all.
    assert f"### {ROUTER}" not in prompt
    assert "permissions.router,\n" not in prompt
    assert prompt.count(SOURCE_HEADER) == 1 and f"### {PERM}" in prompt

    # The builder still materialized router.py internally; it is labelled.
    builder = metadata["planner_source_materialization"]
    assert builder["role"] == "repair_builder_materialization"
    router = _builder_file(metadata, ROUTER)
    assert router["status"] == "existing_file_with_materialized_source"
    assert router["start_line"] == 1 and router["provider_visible"] is False
    assert router["provider_suppression_reason"] == (
        "entering_source_block_already_supplied"
    )
    assert builder["provider_visible_file_count"] == 0

    evidence = metadata["repair_source_evidence"]
    assert evidence["provider_visible_source_paths"] == [PERM]
    assert all(
        record["origin"] == "entering_planning_materialization"
        for record in evidence["provider_visible_source"]
    )
    assert [
        (record["relative_path"], record["provider_visible"])
        for record in evidence["builder_materialization"]
        if record["relative_path"] == ROUTER
    ] == [(ROUTER, False)]

    # G1-T3: the entering canonical record is provider-visible, and the
    # metadata reconstructs the exact rendered source section.
    (visible,) = evidence["provider_visible_source"]
    record = entering.file_map()[PERM]
    assert visible["provider_rendering"] == "complete_record"
    assert visible["version_identity"] == record.version_identity
    assert (visible["start_line"], visible["end_line"]) == (
        record.start_line,
        record.end_line,
    )
    assert f"content:\n{record.content}" in prompt
    assert (
        visible["provider_rendered_sha256"]
        == hashlib.sha256(record.content.encode("utf-8")).hexdigest()
    )

    # G1-T4: the GR12 observation is separately identified as advisory.
    advisory = evidence["provider_visible_advisory_observation"]
    assert advisory["authority"] == "advisory" and advisory["provider_visible"]
    assert advisory["paths"] == [PERM] and prompt.count(OBSERVATION_HEADER) == 1
    assert (
        advisory["content_sha256"]
        == hashlib.sha256(observation.content.encode("utf-8")).hexdigest()
    )


def test_g1_t1_t2_builder_record_visibility_follows_rendering(tmp_path):
    root = _workspace(tmp_path)
    malformed = json.dumps(PLAN)

    rendered = build_planning_repair_prompt_with_metadata(
        TASK, malformed, root, rejection_reasons=["plan_target_ungroundable"]
    )
    router = _builder_file(rendered.metadata, ROUTER)
    assert f"### {ROUTER}" in rendered.prompt
    assert router["provider_visible"] is True
    assert router["provider_suppression_reason"] is None
    assert router["provider_rendered_bytes"] > 0

    suppressed = build_planning_repair_prompt_with_metadata(
        TASK,
        malformed,
        root,
        rejection_reasons=["plan_target_ungroundable"],
        guidance_block=f"{SOURCE_HEADER}\n### {PERM}\nstatus: x\ncontent:\nentering",
    )
    router = _builder_file(suppressed.metadata, ROUTER)
    assert f"### {ROUTER}" not in suppressed.prompt
    assert router["provider_visible"] is False
    assert router["provider_suppression_reason"] == (
        "entering_source_block_already_supplied"
    )
    assert router["provider_rendered_sha256"] is None


def _record(path: str, content: str | None, status: str) -> MaterializedSourceFile:
    return MaterializedSourceFile(
        relative_path=path,
        workspace_identity="w",
        content=content,
        content_hash=None,
        version_identity="v1",
        status=status,
        truncated=False,
        source_length=None,
        source_length_chars=None,
        included_prompt_length=0,
        omission_reason=None if content is not None else "maximum_total_source_bytes",
    )


def test_g1_omitted_reduced_and_absent_source_are_not_overstated():
    kept = _record("a.py", "line one\nline two\nline three\n", "existing")
    omitted = _record("b.py", None, "source_omitted_by_explicit_bound")
    materialization = PlannerSourceMaterialization(
        workspace_identity="w", files=(kept, omitted)
    )
    full = materialization.to_prompt_block()

    records = describe_provider_visible_source(full, materialization, origin="t")
    assert [(r["relative_path"], r["provider_visible"]) for r in records] == [
        ("a.py", True),
        ("b.py", False),
    ]
    assert records[1]["suppression_reason"] == (
        "content_not_materialized:maximum_total_source_bytes"
    )

    reduced = full.replace(kept.content, "line two\n")
    (reduced_record, _) = describe_provider_visible_source(
        reduced, materialization, origin="t"
    )
    assert reduced_record["provider_rendering"] == "reduced_excerpt"

    # No source block: the provider-visible set is empty.
    assert not any(
        r["provider_visible"]
        for r in describe_provider_visible_source(
            "## READ-ONLY OBSERVATION\ncontent:\nline one\n",
            materialization,
            origin="t",
        )
    )
    assert describe_provider_visible_source(full, None, origin="t") == []


# --- G2: initial-vs-repair affordance continuity (production funnel) --------


def test_g2_t1_t3_gr13_repair_keeps_initial_affordance_without_router_evidence(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path)
    prompt, metadata, observation, entering = _gr13_repair(root, monkeypatch)
    initial = entering.to_prompt_block(
        provider_safe=True,
        additional_candidate_paths=observed_candidate_paths(observation),
    )

    for text in (initial, prompt):
        assert LEGACY_OFFERED in text and VERBATIM in text
        assert UNAVAILABLE not in text
        # G2-T2: no semantic target handle is advertised.
        assert "target_id: " not in text
    # G2-T3: the affordance is scoped to supplied text; router.py is not.
    assert (
        ROUTER
        not in metadata["repair_source_evidence"]["provider_visible_source_paths"]
    )
    assert f"### {ROUTER}" not in prompt


def test_g2_t5_without_observation_both_prompts_stay_unavailable(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    prompt, metadata, _, entering = _gr13_repair(root, monkeypatch, observe=False)
    initial = entering.to_prompt_block(provider_safe=True)

    for text in (initial, prompt):
        assert UNAVAILABLE in text and LEGACY_OFFERED not in text
    assert OBSERVATION_HEADER not in prompt
    assert (
        metadata["repair_source_evidence"]["provider_visible_advisory_observation"]
        is None
    )
