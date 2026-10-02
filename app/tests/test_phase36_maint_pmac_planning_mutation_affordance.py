"""PHASE36-MAINT PMAC — Planning-visible mutation affordance vs admission authority.

REENTRY-7 told Planning "Legacy replace_in_file is unavailable" for an observed,
truncated, non-expected ``router.py`` whose exact defect text was in the read-only
observation, while admission (Phase 32H-1, 33C-3 APA, DGM1, GR5) accepts that
narrow edit after version-fenced full-file verification.  The affordance now
offers legacy old/new for any existing record Planning was given source for in
semantic scope (``expected`` or an observed path), truncated or not.  These cases
pin that the affordance changed and that no authority did.  No provider is called.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.phases.planning_guidance_enforcement import (
    collect_repair_guidance_block,
)
from app.services.orchestration.phases.post_plan_source_grounding import (
    FAILURE_CLASS_PLAN_TARGET_UNGROUNDABLE,
    POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
    POST_PLAN_GROUNDING_MISSING,
    POST_PLAN_GROUNDING_PATH_REJECTED,
    POST_PLAN_GROUNDING_SYMLINK,
    POST_PLAN_GROUNDING_VERSION_STALE,
    ground_post_plan_source_materialization,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.planning_prompts import (
    build_minimal_planning_prompt,
    build_ultra_minimal_planning_prompt,
)
from app.services.orchestration.planning.read_only_discovery import (
    render_discovery_observation,
)
from app.services.orchestration.planning.repair_prompts import (
    render_repair_source_materialization,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
    observed_candidate_paths,
    provider_complete_existing_source_available,
    provider_planning_contract_capabilities,
)
from app.services.orchestration.planning.source_operation_verification import (
    SOURCE_EVIDENCE_FULL_FILE_SAME_VERSION,
    verify_replace_in_file,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_path_authority_from_verdict,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.test_phase36_maint_gr4_long_file_grounding_continuation import (
    NARROW_OLD,
    ROUTER,
    TARGET,
    TASK,
    VERIFY,
)

UNAVAILABLE = "Legacy replace_in_file is unavailable"
NON_REPLACE_ONLY = "use only non-replace operations"
LEGACY_OFFERED = "Legacy replace_in_file may use exact old/new"
VERBATIM = "Copy old verbatim from text supplied for that path"
WHOLE_FILE_CONTRACT = "Existing file: no bare `write_file`"
GR2_CODE = "existing_file_rewrite_requires_complete_planning_source"
NEW_TEXT = NARROW_OLD.replace('    prefix="/permissions",\n', "")
SMALL = "app/small.py"
SMALL_SOURCE = '"""Small module."""\n\nVALUE = 1\n'
PERMISSIONS = "app/api/v1/endpoints/permissions.py"
DESTRUCTIVE_PLAN = [
    {
        "step_number": 1,
        "description": "Fix the doubled permissions route.",
        "commands": [],
        "verification": VERIFY,
        "expected_files": [PERMISSIONS],
        "ops": [
            {
                "op": "write_file",
                "path": PERMISSIONS,
                "content": 'router = APIRouter()\n\n@router.get("/pending")\n',
            }
        ],
    }
]


def _workspace(root: Path) -> Path:
    root = root.resolve()
    (root / "app/api/v1/endpoints").mkdir(parents=True)
    (root / TARGET).write_text(ROUTER, encoding="utf-8")
    (root / PERMISSIONS).write_text("# permissions\n" * 400, encoding="utf-8")
    (root / SMALL).write_text(SMALL_SOURCE, encoding="utf-8")
    return root


def _reentry7(root: Path):
    from app.tests.test_phase36_maint_gr5_planning_visible_narrow_mutation_authority import (
        _observed,
        _read_observation,
    )

    observation = _read_observation(root)
    return observation, _observed(root, observation)


def _plan(*ops: dict, expected=(TARGET,), description="Apply the route repair."):
    return [
        {
            "step_number": 1,
            "description": description,
            "commands": [],
            "verification": VERIFY,
            "rollback": None,
            "expected_files": list(expected),
            "ops": list(ops),
        }
    ]


def _replace(path=TARGET, old=NARROW_OLD, new=NEW_TEXT) -> dict:
    return {"op": "replace_in_file", "path": path, "old": old, "new": new}


def _admit(root: Path, plan, materialization):
    grounding = ground_post_plan_source_materialization(
        plan, project_dir=root, source_materialization=materialization
    )
    if not grounding.ok:
        return grounding, None
    verdict = ValidatorService().validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=TASK,
        execution_profile="full_lifecycle",
        project_dir=root,
        source_materialization=grounding.materialization,
    )
    return grounding, verdict


def _block(materialization, observation=None) -> str:
    return materialization.to_prompt_block(
        provider_safe=True,
        additional_candidate_paths=observed_candidate_paths(observation),
    )


def test_reentry7_shape_offers_narrow_replace_and_admits_it(tmp_path):
    root = _workspace(tmp_path)
    observation, materialization = _reentry7(root)
    record = materialization.file_map()[TARGET]
    # GR3/GR4: still advisory-only evidence for the defect region.
    assert record.expected is False and record.truncated is True
    assert record.target_hint is None and record.creation_authorized is False
    assert NARROW_OLD in observation.content and NARROW_OLD not in record.content

    candidates = observed_candidate_paths(observation)
    assert provider_planning_contract_capabilities(
        materialization, additional_candidate_paths=candidates
    ) == (False, True)
    block = _block(materialization, observation)
    assert LEGACY_OFFERED in block and VERBATIM in block
    assert UNAVAILABLE not in block and NON_REPLACE_ONLY not in block
    assert "Never reconstruct a whole file from a partial excerpt." in block

    plan = _plan(_replace())
    grounding, verdict = _admit(root, plan, materialization)
    assert grounding.ok and verdict.accepted, verdict.reasons
    # GR5: authority comes from system-side verification, never from visibility.
    check = verify_replace_in_file(materialization, TARGET, NARROW_OLD, root)
    assert check.visibility == SOURCE_EVIDENCE_FULL_FILE_SAME_VERSION
    result = ExecutorService.execute_file_ops(
        root,
        plan[0]["ops"],
        accepted_path_authority=accepted_path_authority_from_verdict(verdict),
    )
    assert result["success"], result
    assert (root / TARGET).read_text(encoding="utf-8") == ROUTER.replace(
        NARROW_OLD, NEW_TEXT
    )


def test_initial_prompts_match_admission_for_reentry7_shape(tmp_path):
    root = _workspace(tmp_path)
    observation, materialization = _reentry7(root)
    candidates = observed_candidate_paths(observation)
    for builder in (build_minimal_planning_prompt, build_ultra_minimal_planning_prompt):
        prompt = builder(
            TASK,
            root,
            workspace_has_existing_files=True,
            source_materialization=materialization,
            additional_candidate_paths=candidates,
        )
        assert "Legacy replace mode is unavailable" not in prompt
        assert "{op,path,old,new}" in prompt
        # GR2: a truncated record never advertises the whole-file rewrite route.
        assert WHOLE_FILE_CONTRACT not in prompt


def test_repair_prompts_keep_the_same_affordance(tmp_path):
    root = _workspace(tmp_path)
    observation, materialization = _reentry7(root)
    ctx = SimpleNamespace(
        planner_source_materialization=materialization,
        read_only_observation=observation,
        prompt=TASK,
        db=None,
        project=None,
        session_id=None,
        task_id=None,
    )
    guidance = "\n\n".join(
        [collect_repair_guidance_block(ctx), render_discovery_observation(observation)]
    )
    assert LEGACY_OFFERED in guidance and UNAVAILABLE not in guidance
    # GR12: the advisory observation still travels with its own label.
    assert "## READ-ONLY OBSERVATION" in guidance and NARROW_OLD in guidance
    malformed = json.dumps(DESTRUCTIVE_PLAN)
    normal = PlannerService.build_planning_repair_prompt_with_metadata(
        TASK, malformed, root, guidance_block=guidance
    ).prompt
    compact = PlannerService.build_compact_planning_repair_prompt(
        malformed, guidance_block=guidance
    )
    projection = render_repair_source_materialization(
        materialization, compaction_level=1, provider_safe=True
    )
    for prompt in (normal, compact, projection):
        assert UNAVAILABLE not in prompt and NON_REPLACE_ONLY not in prompt
        assert WHOLE_FILE_CONTRACT not in prompt
    assert "## READ-ONLY OBSERVATION" in compact


def test_complete_expected_source_is_unchanged(tmp_path):
    root = _workspace(tmp_path)
    materialization = materialize_planner_source_context(
        root, task_description=TASK, expected_paths=[SMALL]
    )
    assert provider_planning_contract_capabilities(materialization) == (False, True)
    assert provider_complete_existing_source_available(materialization) is True
    plan = _plan(_replace(SMALL, "VALUE = 1\n", "VALUE = 2\n"), expected=[SMALL])
    assert _admit(root, plan, materialization)[1].accepted


def test_truncated_expected_source_offers_narrow_but_not_whole_file(tmp_path):
    root = _workspace(tmp_path)
    materialization = materialize_planner_source_context(
        root, task_description=TASK, expected_paths=[TARGET]
    )
    assert materialization.file_map()[TARGET].truncated is True
    assert provider_planning_contract_capabilities(materialization)[1] is True
    assert provider_complete_existing_source_available(materialization) is False
    assert _admit(root, _plan(_replace()), materialization)[1].accepted


def test_unobserved_supporting_source_stays_readonly_in_the_prompt(tmp_path):
    # R2A intent preserved: supporting context the task did not scope and the
    # observation did not select is not offered for replacement.
    root = _workspace(tmp_path)
    materialization = materialize_planner_source_context(
        root, task_description=TASK, expected_paths=[], supporting_paths=[SMALL]
    )
    assert provider_planning_contract_capabilities(materialization) == (False, False)
    assert UNAVAILABLE in _block(materialization)


def test_stale_missing_and_ambiguous_old_fail_closed(tmp_path):
    root = _workspace(tmp_path)
    _, materialization = _reentry7(root)
    missing = NARROW_OLD.replace("/permissions", "/permission")
    grounding, verdict = _admit(root, _plan(_replace(old=missing)), materialization)
    assert grounding.ok and not verdict.accepted
    assert any("stale_replace" in str(reason) for reason in verdict.reasons)

    duplicate = "    dependencies=[Depends(get_current_active_user)],\n"
    assert ROUTER.count(duplicate) > 1
    plan = _plan(_replace(old=duplicate, new="    dependencies=[],\n"))
    grounding, verdict = _admit(root, plan, materialization)
    assert verdict.accepted
    result = ExecutorService.execute_file_ops(
        root,
        plan[0]["ops"],
        accepted_path_authority=accepted_path_authority_from_verdict(verdict),
    )
    assert result["success"] is False and "ambiguous" in result["output"]
    assert (root / TARGET).read_text(encoding="utf-8") == ROUTER

    (root / TARGET).write_text(ROUTER + "\n# changed\n", encoding="utf-8")
    grounding, _ = _admit(root, _plan(_replace()), materialization)
    assert grounding.failure_code == POST_PLAN_GROUNDING_VERSION_STALE


def test_path_policy_denials_are_unchanged(tmp_path):
    root = _workspace(tmp_path)
    _, materialization = _reentry7(root)
    os.symlink(root / TARGET, root / "app/api/v1/link.py")
    for path, code in (
        ("../outside.py", POST_PLAN_GROUNDING_PATH_REJECTED),
        ("app/api/v1/link.py", POST_PLAN_GROUNDING_SYMLINK),
        ("app/api/v1/absent.py", POST_PLAN_GROUNDING_MISSING),
    ):
        grounding, _ = _admit(
            root, _plan(_replace(path=path), expected=[path]), materialization
        )
        assert grounding.failure_code == code, path


def test_whole_file_rewrites_stay_blocked(tmp_path):
    root = _workspace(tmp_path)
    _, materialization = _reentry7(root)
    rewrite = _plan(
        {"op": "write_file", "path": TARGET, "content": "api_router = None\n"},
        description="Rewrite and replace the router file.",
    )
    grounding, verdict = _admit(root, rewrite, materialization)
    assert grounding.ok and not verdict.accepted
    assert any(GR2_CODE in str(reason) for reason in verdict.reasons)

    # The REENTRY-6/7 destructive permissions.py rewrite and a narrow edit of
    # the same unmaterialized long file both remain ungroundable (GR13 class).
    for plan in (
        DESTRUCTIVE_PLAN,
        _plan(_replace(PERMISSIONS, "# permissions\n", "#\n"), expected=[PERMISSIONS]),
    ):
        grounding, _ = _admit(root, plan, materialization)
        assert grounding.failure_code == POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE
        assert grounding.failure_class == FAILURE_CLASS_PLAN_TARGET_UNGROUNDABLE


def test_new_file_creation_is_unaffected(tmp_path):
    root = _workspace(tmp_path)
    _, materialization = _reentry7(root)
    path = "app/api/v1/route_paths.py"
    plan = _plan(
        {"op": "write_file", "path": path, "content": 'PENDING = "/pending"\n'},
        expected=[path],
    )
    grounding, verdict = _admit(root, plan, materialization)
    assert grounding.ok and verdict.accepted, verdict.reasons
