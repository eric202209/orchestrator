"""PHASE36-MAINT-GR2 — existing-file whole-rewrite grounding authority.

REENTRY-3 promoted a whole-file ``write_file`` of ``app/api/v1/router.py``
built from a 1959/5164-character Planning slice.  These cases drive the real
materializer and ``ValidatorService.validate_plan``: a whole-file replacement of
an existing source file is authorized only by complete, Planning-visible,
version-fenced evidence for that exact path.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

from app.services.orchestration.phases.post_plan_source_grounding import (
    ground_post_plan_source_materialization,
)
from app.services.orchestration.planning.source_materialization import (
    PlannerSourceMaterialization,
    materialize_planner_source_context,
)
from app.services.orchestration.validation.validator import ValidatorService

TARGET = "app/router.py"
CODE = "existing_file_rewrite_requires_complete_planning_source"
TASK = (
    "Authenticated clients should be able to GET /api/permissions/pending. "
    "Repair the externally observable route behavior in app/router.py."
)
LARGE_ROUTER = (
    '"""API router."""\n\nfrom fastapi import APIRouter\n\napi_router = APIRouter()\n\n'
    + "".join(
        f"# Router block {i}\napi_router.include_router(\n    module_{i}.router,\n"
        f'    prefix="/area{i}",\n    tags=["area{i}"],\n)\n\n'
        for i in range(60)
    )
)
TASK_UNNAMED = "Authenticated clients should be able to GET /api/permissions/pending."
SMALL_ROUTER = '"""API router."""\n\nPREFIX = "/permissions"\n'
REWRITE = '"""API router."""\n\nPREFIX = ""\n'
VERIFY = "python -c \"import ast; ast.parse(open('app/router.py').read())\""


def _workspace(tmp_path: Path, content: str) -> Path:
    root = tmp_path.resolve()
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / TARGET).write_text(content, encoding="utf-8")
    return root


def _materialize(
    root: Path, *expected: str, task: str = TASK
) -> PlannerSourceMaterialization:
    return materialize_planner_source_context(
        root, task_description=task, expected_paths=list(expected or (TARGET,))
    )


def _plan(ops=(), commands=(), expected_files=(TARGET,)):
    return [
        {
            "step_number": 1,
            "description": "Apply the route repair.",
            "commands": list(commands),
            "verification": VERIFY,
            "rollback": None,
            "expected_files": list(expected_files),
            "ops": list(ops),
        }
    ]


def _validate(root: Path, plan, materialization, task: str = TASK):
    return ValidatorService().validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=task,
        execution_profile="full_lifecycle",
        project_dir=root,
        source_materialization=materialization,
    )


def _gr2_reasons(verdict) -> list[str]:
    return [
        item["reason"]
        for item in (verdict.details or {}).get(
            "existing_file_rewrite_without_full_source", []
        )
    ]


def _assert_gr2_rejected(verdict, reason: str) -> None:
    assert not verdict.accepted
    assert any(CODE in str(item) for item in verdict.reasons), verdict.reasons
    assert reason in _gr2_reasons(verdict), verdict.details


def _write(content: str = REWRITE, path: str = TARGET) -> dict:
    return {"op": "write_file", "path": path, "content": content}


def test_r1_reentry3_truncated_planning_slice_rejects_whole_file_write(tmp_path):
    root = _workspace(tmp_path, LARGE_ROUTER)
    materialization = _materialize(root)
    record = materialization.file_map()[TARGET]
    assert record.truncated is True
    assert record.included_source_bytes < record.full_source_bytes

    verdict = _validate(root, _plan([_write()]), materialization)

    _assert_gr2_rejected(verdict, "source_materialization_truncated")
    assert (root / TARGET).read_text(encoding="utf-8") == LARGE_ROUTER


def test_r2_complete_current_evidence_is_not_rejected_by_gr2(tmp_path):
    root = _workspace(tmp_path, SMALL_ROUTER)
    materialization = _materialize(root)
    assert materialization.file_map()[TARGET].truncated is False

    verdict = _validate(root, _plan([_write()]), materialization)

    assert _gr2_reasons(verdict) == []
    assert verdict.accepted, verdict.reasons


def test_r3_missing_materialization_rejects_whole_file_write(tmp_path):
    root = _workspace(tmp_path, SMALL_ROUTER)
    (root / "app/other.py").write_text("VALUE = 1\n", encoding="utf-8")
    materialization = _materialize(root, "app/other.py", task=TASK_UNNAMED)
    assert TARGET not in materialization.file_map()

    verdict = _validate(root, _plan([_write()]), materialization, task=TASK_UNNAMED)

    assert not verdict.accepted
    assert verdict.details.get("new_file_write_without_creation_authorization")


def test_r3_post_plan_grounded_target_is_not_planning_visible(tmp_path):
    """Grounding after the Plan never retroactively authorizes that Plan."""

    root = _workspace(tmp_path, SMALL_ROUTER)
    (root / "app/other.py").write_text("VALUE = 1\n", encoding="utf-8")
    initial = _materialize(root, "app/other.py", task=TASK_UNNAMED)
    assert TARGET not in initial.file_map()
    plan = _plan([_write()])

    grounding = ground_post_plan_source_materialization(
        plan, project_dir=root, source_materialization=initial
    )
    assert grounding.ok, grounding.to_dict()
    assert grounding.grounded_paths == (TARGET,)
    grounded = grounding.materialization.file_map()[TARGET]
    assert grounded.truncated is False and grounded.planning_visible is False
    assert grounding.materialization.file_map()["app/other.py"].planning_visible

    verdict = _validate(root, plan, grounding.materialization, task=TASK_UNNAMED)

    _assert_gr2_rejected(verdict, "source_not_visible_to_planning")


def test_r4_source_changed_after_complete_materialization_rejects(tmp_path):
    root = _workspace(tmp_path, SMALL_ROUTER)
    materialization = _materialize(root)
    record = materialization.file_map()[TARGET]
    (root / TARGET).write_text(SMALL_ROUTER + "EXTRA = True\n", encoding="utf-8")
    stat = (root / TARGET).stat()
    os.utime(root / TARGET, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert record.truncated is False

    verdict = _validate(root, _plan([_write()]), materialization)

    _assert_gr2_rejected(verdict, "source_version_changed_since_materialization")


def test_r5_repair_escalation_to_whole_file_write_is_rechecked(tmp_path):
    root = _workspace(tmp_path, LARGE_ROUTER)
    materialization = _materialize(root)
    visible = materialization.file_map()[TARGET].content
    old = "api_router = APIRouter()"
    assert old in visible

    narrow = _plan([{"op": "replace_in_file", "path": TARGET, "old": old, "new": old}])
    initial = _validate(root, narrow, materialization)
    assert _gr2_reasons(initial) == []

    repaired = _plan([_write()])
    verdict = _validate(root, repaired, materialization)

    _assert_gr2_rejected(verdict, "source_materialization_truncated")


def test_r6_grounded_narrow_replace_keeps_existing_behavior(tmp_path):
    root = _workspace(tmp_path, LARGE_ROUTER)
    materialization = _materialize(root)
    old = "api_router = APIRouter()"
    plan = _plan(
        [
            {
                "op": "replace_in_file",
                "path": TARGET,
                "old": old,
                "new": "api_router = APIRouter(redirect_slashes=False)",
            }
        ]
    )

    verdict = _validate(root, plan, materialization)

    assert _gr2_reasons(verdict) == []
    assert verdict.accepted, verdict.reasons


def test_r7_genuine_new_file_creation_is_not_an_existing_rewrite(tmp_path):
    root = _workspace(tmp_path, LARGE_ROUTER)
    materialization = _materialize(root)
    new_path = "app/permissions_routes.py"
    plan = _plan(
        [_write("ROUTES = []\n", path=new_path)],
        expected_files=(new_path,),
    )

    verdict = _validate(root, plan, materialization)

    assert _gr2_reasons(verdict) == []
    assert verdict.accepted, verdict.reasons
    assert not (root / new_path).exists()


def test_r8_complete_evidence_for_another_path_does_not_authorize_target(tmp_path):
    root = _workspace(tmp_path, LARGE_ROUTER)
    (root / "app/other.py").write_text("VALUE = 1\n", encoding="utf-8")
    materialization = _materialize(root, "app/other.py", task=TASK_UNNAMED)
    assert TARGET not in materialization.file_map()
    assert materialization.file_map()["app/other.py"].truncated is False

    verdict = _validate(root, _plan([_write()]), materialization, task=TASK_UNNAMED)

    assert not verdict.accepted
    assert verdict.details.get("new_file_write_without_creation_authorization")


def test_r8_substituted_content_under_target_identity_is_rejected(tmp_path):
    root = _workspace(tmp_path, SMALL_ROUTER)
    materialization = _materialize(root)
    record = materialization.file_map()[TARGET]
    forged = 'PREFIX = "/forged"\n'
    forged_record = replace(
        record,
        content=forged,
        content_hash=hashlib.sha256(forged.encode()).hexdigest(),
        full_source_bytes=len(forged.encode()),
        included_source_bytes=len(forged.encode()),
    )
    forged_materialization = replace(materialization, files=(forged_record,))

    verdict = _validate(root, _plan([_write()]), forged_materialization)

    _assert_gr2_rejected(verdict, "source_materialization_differs_from_current_source")


def test_r8_foreign_workspace_identity_is_rejected(tmp_path):
    root = _workspace(tmp_path / "runtime", SMALL_ROUTER)
    other = _workspace(tmp_path / "other", SMALL_ROUTER)
    foreign = _materialize(other)
    foreign_record = replace(
        foreign.file_map()[TARGET],
        version_identity=_materialize(root).file_map()[TARGET].version_identity,
    )
    materialization = replace(foreign, files=(foreign_record,))

    verdict = _validate(root, _plan([_write()]), materialization)

    _assert_gr2_rejected(verdict, "workspace_identity_mismatch")


def test_alt_shell_heredoc_and_redirect_overwrite_are_checked(tmp_path):
    root = _workspace(tmp_path, LARGE_ROUTER)
    materialization = _materialize(root)
    for command in (
        f"cat > {TARGET} <<'EOF'\n{REWRITE}EOF",
        f"printf 'x = 1\\n' > {TARGET}",
        f"printf 'x = 1\\n' | tee {TARGET}",
    ):
        verdict = _validate(root, _plan(commands=[command]), materialization)
        _assert_gr2_rejected(verdict, "source_materialization_truncated")


def test_alt_shell_append_and_complete_overwrite_are_not_gr2_findings(tmp_path):
    root = _workspace(tmp_path, LARGE_ROUTER)
    truncated = _materialize(root)
    for command in (
        f"printf '# note\\n' >> {TARGET}",
        f"printf '# note\\n' | tee -a {TARGET}",
        f"python -m pytest -q > /dev/null 2>&1",
    ):
        verdict = _validate(root, _plan(commands=[command]), truncated)
        assert _gr2_reasons(verdict) == [], command

    small_root = _workspace(tmp_path / "small", SMALL_ROUTER)
    complete = _materialize(small_root)
    verdict = _validate(
        small_root,
        _plan(commands=[f"printf 'x = 1\\n' > {TARGET}"]),
        complete,
    )
    assert _gr2_reasons(verdict) == []
