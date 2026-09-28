"""PHASE36-MAINT-GR5 — narrow-mutation authority vs Planning visibility.

GR5 adjudicated whether an existing-file ``replace_in_file`` must replace text
that was shown to the Plan-producing invocation.  The architecture answers no,
deliberately: Phase 32H-1 separated model-visible evidence from version-fenced
full-file verification, and Phase 34-DGM1 made accepted path authority (not
model visibility) the only mutation authority.  Narrow edits change only the
exact, uniquely located ``old`` bytes; whole-file replacement, which rewrites
unseen bytes, still requires complete Planning-visible source (GR2).  These
cases pin that policy and its fail-closed edges with real production functions.
No provider is called.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.phases.post_plan_source_grounding import (
    POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
    ground_post_plan_source_materialization,
)
from app.services.orchestration.planning.read_only_discovery import (
    execute_discovery_request,
    materialize_observation_source_context,
    parse_discovery_request,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.planning.source_operation_verification import (
    FAILURE_VERSION_CHANGED,
    FAILURE_WORKSPACE_IDENTITY_MISMATCH,
    SOURCE_EVIDENCE_FULL_FILE_SAME_VERSION,
    SOURCE_EVIDENCE_VISIBLE_IN_SPAN,
    verify_replace_in_file,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_path_authority_from_verdict,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.test_phase36_maint_gr4_long_file_grounding_continuation import (
    DEEP_ROUTER,
    NARROW_OLD,
    PERMISSION_BLOCK,
    ROUTER,
    TARGET,
    TASK,
    VERIFY,
)

GR2_CODE = "existing_file_rewrite_requires_complete_planning_source"
NEW_OLD = NARROW_OLD.replace('prefix="/permissions"', 'prefix=""')
SHORT_ROUTER = '"""API router."""\n\napi_router = APIRouter()\n' + PERMISSION_BLOCK


def _workspace(root: Path, content: str = ROUTER) -> Path:
    root = root.resolve()
    (root / "app/api/v1").mkdir(parents=True)
    (root / TARGET).write_text(content, encoding="utf-8")
    return root


def _read_observation(root: Path):
    request = parse_discovery_request(f'{{"action":"read_file","path":"{TARGET}"}}')
    return execute_discovery_request(root, request)


def _search_observation(root: Path, query: str):
    request = parse_discovery_request(
        json.dumps({"action": "search_text", "query": query, "paths": [TARGET]})
    )
    return execute_discovery_request(root, request)


def _observed(root: Path, observation):
    return materialize_observation_source_context(
        project_dir=root,
        prompt=TASK,
        planner_contract=None,
        observation=observation,
        materialize=materialize_planner_source_context,
        source_cache={},
    )


def _plan(*ops: dict, expected=(TARGET,)):
    return [
        {
            "step_number": 1,
            "description": "Apply the route repair.",
            "commands": [],
            "verification": VERIFY,
            "rollback": None,
            "expected_files": list(expected),
            "ops": list(ops),
        }
    ]


def _replace(old: str = NARROW_OLD, new: str = NEW_OLD) -> dict:
    return {"op": "replace_in_file", "path": TARGET, "old": old, "new": new}


def _validate(root: Path, plan, materialization):
    return ValidatorService().validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=TASK,
        execution_profile="full_lifecycle",
        project_dir=root,
        source_materialization=materialization,
    )


def _visibility(root: Path, materialization, old: str) -> str:
    verdict = verify_replace_in_file(materialization, TARGET, old, root)
    assert verdict.verified, verdict
    return verdict.visibility


def test_r1_canonical_visible_narrow_edit_is_accepted(tmp_path):
    root = _workspace(tmp_path)
    materialization = _observed(root, _read_observation(root))
    old = "api_router = APIRouter()\n"

    assert _validate(root, _plan(_replace(old, old)), materialization).accepted
    assert _visibility(root, materialization, old) == SOURCE_EVIDENCE_VISIBLE_IN_SPAN


def test_r2_advisory_only_edit_is_verified_not_reported_model_grounded(tmp_path):
    root = _workspace(tmp_path)
    observation = _read_observation(root)
    materialization = _observed(root, observation)
    record = materialization.file_map()[TARGET]
    assert NARROW_OLD in observation.content and NARROW_OLD not in record.content

    assert _validate(root, _plan(_replace()), materialization).accepted
    assert (
        _visibility(root, materialization, NARROW_OLD)
        == SOURCE_EVIDENCE_FULL_FILE_SAME_VERSION
    )


def test_r3_unseen_exact_edit_is_accepted_by_version_fenced_policy(tmp_path):
    root = _workspace(tmp_path, DEEP_ROUTER)
    observation = _read_observation(root)
    materialization = _observed(root, observation)
    assert NARROW_OLD not in observation.content
    assert NARROW_OLD not in materialization.file_map()[TARGET].content

    assert _validate(root, _plan(_replace()), materialization).accepted
    assert (
        _visibility(root, materialization, NARROW_OLD)
        == SOURCE_EVIDENCE_FULL_FILE_SAME_VERSION
    )


def test_r3_inexact_unseen_guess_fails_closed(tmp_path):
    root = _workspace(tmp_path, DEEP_ROUTER)
    materialization = _observed(root, _read_observation(root))
    guess = NARROW_OLD.replace("/permissions", "/permission")

    verdict = _validate(root, _plan(_replace(guess, NEW_OLD)), materialization)

    assert not verdict.accepted
    assert (root / TARGET).read_text(encoding="utf-8") == DEEP_ROUTER


def test_r4_partially_visible_old_is_not_reported_model_grounded(tmp_path):
    root = _workspace(tmp_path)
    materialization = _observed(root, _read_observation(root))
    record = materialization.file_map()[TARGET]
    encoded = ROUTER.encode("utf-8")
    partial = encoded[record.end_byte - 40 : record.end_byte + 40].decode("utf-8")
    assert partial not in record.content

    assert _validate(root, _plan(_replace(partial, partial)), materialization).accepted
    assert (
        _visibility(root, materialization, partial)
        == SOURCE_EVIDENCE_FULL_FILE_SAME_VERSION
    )


def test_r5_evidence_stale_before_validation_is_rejected(tmp_path):
    root = _workspace(tmp_path)
    materialization = _observed(root, _read_observation(root))
    (root / TARGET).write_text(ROUTER.replace("# Area 0", "# Area zero"), "utf-8")
    stat = (root / TARGET).stat()
    os.utime(root / TARGET, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    verdict = verify_replace_in_file(materialization, TARGET, NARROW_OLD, root)

    assert verdict.failure_code == FAILURE_VERSION_CHANGED
    assert not _validate(root, _plan(_replace()), materialization).accepted


def test_r6_post_plan_grounding_of_truncated_target_fails_closed(tmp_path):
    root = _workspace(tmp_path)
    initial = materialize_planner_source_context(
        root, task_description=TASK, supporting_paths=()
    )
    assert TARGET not in initial.file_map()

    grounding = ground_post_plan_source_materialization(
        _plan(_replace()), project_dir=root, source_materialization=initial
    )

    assert grounding.failure_code == POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE
    assert not _validate(root, _plan(_replace()), grounding.materialization).accepted


def test_r6_post_plan_record_supports_narrow_edit_but_never_gr2(tmp_path):
    root = _workspace(tmp_path, SHORT_ROUTER)
    initial = materialize_planner_source_context(
        root, task_description=TASK, supporting_paths=()
    )
    narrow = _plan(_replace())
    whole = _plan({"op": "write_file", "path": TARGET, "content": "x = 1\n"})

    grounding = ground_post_plan_source_materialization(
        narrow, project_dir=root, source_materialization=initial
    )
    assert grounding.ok, grounding.to_dict()
    assert grounding.materialization.file_map()[TARGET].planning_visible is False

    assert _validate(root, narrow, grounding.materialization).accepted
    whole_verdict = _validate(root, whole, grounding.materialization)
    assert not whole_verdict.accepted
    assert any(GR2_CODE in str(reason) for reason in whole_verdict.reasons)


def test_r7_foreign_workspace_evidence_is_rejected(tmp_path):
    own = _workspace(tmp_path / "own")
    foreign = _workspace(tmp_path / "foreign")
    foreign_materialization = _observed(foreign, _read_observation(foreign))

    verdict = verify_replace_in_file(foreign_materialization, TARGET, NARROW_OLD, own)

    assert verdict.failure_code == FAILURE_WORKSPACE_IDENTITY_MISMATCH
    assert not _validate(own, _plan(_replace()), foreign_materialization).accepted


def test_r8_advisory_partial_evidence_never_satisfies_gr2(tmp_path):
    root = _workspace(tmp_path)
    materialization = _observed(root, _read_observation(root))
    plan = _plan({"op": "write_file", "path": TARGET, "content": "x = 1\n"})

    verdict = _validate(root, plan, materialization)

    assert not verdict.accepted
    assert any(GR2_CODE in str(reason) for reason in verdict.reasons)


def test_r9_new_file_creation_is_unaffected(tmp_path):
    root = _workspace(tmp_path)
    materialization = _observed(root, _read_observation(root))
    new_path = "app/api/v1/permission_routes.py"
    plan = _plan(
        {"op": "write_file", "path": new_path, "content": "ROUTES = ()\n"},
        expected=(new_path,),
    )

    assert _validate(root, plan, materialization).accepted, "new file rejected"


def test_r10_search_snippet_edit_is_verified_not_model_grounded(tmp_path):
    root = _workspace(tmp_path)
    observation = _search_observation(root, '"permissions"')
    snippet = observation.hits[0].snippet
    materialization = _observed(root, observation)

    assert _validate(root, _plan(_replace(snippet, snippet)), materialization).accepted
    assert (
        _visibility(root, materialization, snippet)
        == SOURCE_EVIDENCE_FULL_FILE_SAME_VERSION
    )


def test_r11_duplicate_old_is_accepted_by_validator_but_never_executed(tmp_path):
    root = _workspace(tmp_path)
    materialization = _observed(root, _read_observation(root))
    duplicate = "    dependencies=[Depends(get_current_active_user)],\n"
    assert ROUTER.count(duplicate) > 1
    operation = _replace(duplicate, "    dependencies=[],\n")

    verdict = _validate(root, _plan(operation), materialization)
    assert verdict.accepted, verdict.reasons
    result = ExecutorService.execute_file_ops(
        root,
        [operation],
        accepted_path_authority=accepted_path_authority_from_verdict(verdict),
    )

    assert result["success"] is False
    assert "ambiguous" in result["output"]
    assert (root / TARGET).read_text(encoding="utf-8") == ROUTER
