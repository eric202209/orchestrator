"""PHASE36-MAINT-GR6 — narrow-replacement structural/semantic compatibility.

GR6 adjudicated whether REENTRY-4's admitted ``replace_in_file`` (the 11-byte
``APIRouter()`` region replaced by an ``api_router.include_router(...)`` call)
should have been rejected before Execution.  It should not: Plan validation
already compiles the simulated whole-file result of every Python file op
(``python_source_syntax_invalid``), and the REENTRY-4 result compiles.  The
replacement is an expression in an expression slot (Call for Call); its defect
is semantic (``api_router`` is read before it is bound, and the permissions
prefix it was meant to remove is untouched).  These cases replay the retained
historical operation against the byte-identical source and pin the existing
syntax gate's boundaries with real production functions.  No provider is
called.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from app.services.orchestration.diagnostics.signature_guard import (
    check_bounded_debug_repair_signature_contract,
)
from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.operations.source_region_identity import (
    SourceRegionIdentity,
)
from app.services.orchestration.phases.post_plan_source_grounding import (
    ground_post_plan_source_materialization,
)
from app.services.orchestration.planning.source_materialization import (
    current_source_version_identity,
    materialize_planner_source_context,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_path_authority_from_verdict,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.test_phase36_maint_gr4_long_file_grounding_continuation import (
    TASK,
    VERIFY,
)
from app.tests.test_phase36_maint_gr5_planning_visible_narrow_mutation_authority import (
    NARROW_OLD,
    NEW_OLD,
    SHORT_ROUTER,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET = "app/api/v1/router.py"
SYNTAX_CODE = "python_source_syntax_invalid"
GR2_CODE = "existing_file_rewrite_requires_complete_planning_source"
# Retained REENTRY-4 operation (Execution 374, step 2): the exact selector
# region and ``new`` payload recorded in the Plan.  The source version
# identity embeds mtime, so it is re-bound to the temporary copy.
REENTRY4_SOURCE_SHA256 = (
    "30c24fe831be08b411b4a02e593abd89f2052ccfba3ededb0eec13cfac4efb3c"
)
REENTRY4_START, REENTRY4_END = 1103, 1114
REENTRY4_REGION_SHA256 = (
    "7ef2497ea285d26b6cc39973d44fb28fe0974557a8d8f76ff85583fb117897df"
)
REENTRY4_NEW = (
    "api_router.include_router(\n    permissions.router,\n"
    '    tags=["permissions"],\n'
    "    dependencies=[Depends(get_current_active_user)],\n)"
)
MODULE = "pkg/mod.py"
NESTED = (
    "def f(x):\n    if x:\n        return 1\n    return 0\n\n\n"
    "class C:\n    value = 1\n"
)


def _historical_source() -> bytes:
    source = (REPO_ROOT / TARGET).read_bytes()
    if hashlib.sha256(source).hexdigest() != REENTRY4_SOURCE_SHA256:
        pytest.skip("router.py no longer matches the REENTRY-4 source blob")
    return source


def _workspace(root: Path, path: str, content: str | bytes) -> Path:
    root = root.resolve()
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    target.write_bytes(content)
    return root


def _plan(*ops: dict, path: str = TARGET):
    return [
        {
            "step_number": 1,
            "description": "Apply the narrow edit.",
            "commands": [],
            "verification": VERIFY,
            "rollback": None,
            "expected_files": [path],
            "ops": list(ops),
        }
    ]


def _replace(old: str, new: str, path: str = TARGET) -> dict:
    return {"op": "replace_in_file", "path": path, "old": old, "new": new}


def _selector(root: Path, path: str, start: int, end: int, new: str, digest=None):
    target = root / path
    region = target.read_bytes()[start:end]
    selector = SourceRegionIdentity.from_region(
        canonical_path=path,
        expected_source_version=current_source_version_identity(target),
        start_byte=start,
        end_byte=end,
        selected_region_sha256=digest or hashlib.sha256(region).hexdigest(),
    )
    return {
        "op": "replace_in_file",
        "path": path,
        "selector": selector.to_dict(),
        "new": new,
    }


def _historical_op(root: Path) -> dict:
    return _selector(
        root,
        TARGET,
        REENTRY4_START,
        REENTRY4_END,
        REENTRY4_NEW,
        digest=REENTRY4_REGION_SHA256,
    )


def _validate(root: Path, plan, materialization=None, path: str = TARGET):
    if materialization is None:
        materialization = materialize_planner_source_context(
            root, task_description=TASK, expected_paths=[path]
        )
    return ValidatorService().validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=TASK,
        execution_profile="full_lifecycle",
        project_dir=root,
        source_materialization=materialization,
    )


def _execute(root: Path, plan, verdict) -> dict:
    return ExecutorService.execute_file_ops(
        root,
        plan[0]["ops"],
        accepted_path_authority=accepted_path_authority_from_verdict(verdict),
    )


def _syntax_rejected(verdict) -> bool:
    return not verdict.accepted and any(
        SYNTAX_CODE in str(reason) for reason in verdict.reasons
    )


def _assert_accepted_and_parses(root: Path, plan, path: str = TARGET) -> str:
    verdict = _validate(root, plan, path=path)
    assert verdict.accepted, verdict.reasons
    assert _execute(root, plan, verdict)["success"] is True
    result = (root / path).read_text(encoding="utf-8")
    ast.parse(result)
    return result


def test_r1_historical_reentry4_op_is_syntax_valid_and_semantically_wrong(tmp_path):
    root = _workspace(tmp_path, TARGET, _historical_source())
    plan = _plan(_historical_op(root))

    result = _assert_accepted_and_parses(root, plan)

    # Same syntactic slot: a Call replaced a Call as the assignment value.
    assert isinstance(ast.parse("APIRouter()", mode="eval").body, ast.Call)
    assert isinstance(ast.parse(REENTRY4_NEW, mode="eval").body, ast.Call)
    first = next(
        node
        for node in ast.parse(result).body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "api_router" for t in node.targets)
    )
    assert isinstance(first.value, ast.Call)
    # The defects are semantic: the name is read before it is bound, and the
    # doubled permissions prefix the task targeted is still present.
    assert any(
        isinstance(node, ast.Name) and node.id == "api_router"
        for node in ast.walk(first.value)
    )
    assert 'prefix="/permissions"' in result


def test_r1_same_shape_via_old_new_is_admitted_identically(tmp_path):
    root = _workspace(tmp_path, TARGET, _historical_source())
    _assert_accepted_and_parses(root, _plan(_replace("APIRouter()", REENTRY4_NEW)))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("APIRouter()", "APIRouter(dependencies=[])"),  # R2 expression
        ("api_router = APIRouter()\n", "api_router = APIRouter(tags=['v1'])\n"),  # R3
        (
            "api_router = APIRouter()\n",
            "api_router = APIRouter()\nAPI_VERSION = 'v1'\n",
        ),
        ("APIRouter()", "APIRouter()\napi_router.include_router(permissions.router)"),
    ],
    ids=["r2_expression", "r3_statement", "r4_multi_statement", "r4_statement_split"],
)
def test_r2_r4_valid_narrow_replacements_remain_accepted(tmp_path, old, new):
    root = _workspace(tmp_path, TARGET, _historical_source())
    _assert_accepted_and_parses(root, _plan(_replace(old, new)))


@pytest.mark.parametrize(
    "new",
    ["APIRouter(", "del api_router", '@api_router.get("/x")'],
    ids=[
        "unclosed_call",
        "statement_in_expression_slot",
        "decorator_in_expression_slot",
    ],
)
def test_r5_invalid_resulting_python_fails_before_execution(tmp_path, new):
    root = _workspace(tmp_path, TARGET, _historical_source())
    before = (root / TARGET).read_bytes()

    assert _syntax_rejected(_validate(root, _plan(_replace("APIRouter()", new))))
    selector_plan = _plan(_selector(root, TARGET, REENTRY4_START, REENTRY4_END, new))
    assert _syntax_rejected(_validate(root, selector_plan))
    assert (root / TARGET).read_bytes() == before


@pytest.mark.parametrize(
    ("old", "new", "valid"),
    [
        ("    return 0\n", "    y = 0\n    return y\n", True),
        ("        return 1\n", "        z = 1\n        return z\n", True),
        ("    value = 1\n", "    value = 2\n    other = 3\n", True),
        ("    return 0\n", "      return 0\n", False),
        ("        return 1\n", "return 1\n", False),
        ("    value = 1\n", "value = 1\n  other = 2\n", False),
    ],
    ids=[
        "function_ok",
        "conditional_ok",
        "class_ok",
        "function_bad",
        "conditional_bad",
        "class_bad",
    ],
)
def test_r6_indentation_is_checked_in_context(tmp_path, old, new, valid):
    root = _workspace(tmp_path, MODULE, NESTED)
    plan = _plan(_replace(old, new, MODULE), path=MODULE)
    if valid:
        _assert_accepted_and_parses(root, plan, MODULE)
    else:
        assert _syntax_rejected(_validate(root, plan, path=MODULE))


def test_r7_sequential_edits_are_checked_against_simulated_state(tmp_path):
    source = "def f():\n    x = 1\ny = 2\n"
    # Valid against the original, invalid after op 1 dedents the body.
    breaks = _plan(
        _replace("def f():\n    x = 1\n", "x = 1\n", MODULE),
        _replace("y = 2\n", "    y = 2\n", MODULE),
        path=MODULE,
    )
    assert _syntax_rejected(
        _validate(_workspace(tmp_path / "a", MODULE, source), breaks, path=MODULE)
    )

    # Invalid against the original, valid only after op 1.
    source = "x = 1\ny = 2\n"
    heals = _plan(
        _replace("x = 1\n", "def f():\n    x = 1\n", MODULE),
        _replace("y = 2\n", "    y = 2\n", MODULE),
        path=MODULE,
    )
    root = _workspace(tmp_path / "b", MODULE, source)
    result = _assert_accepted_and_parses(root, heals, MODULE)
    assert result == "def f():\n    x = 1\n    y = 2\n"


def test_r8_unseen_exact_narrow_edit_is_governed_by_result_validity_only(tmp_path):
    root = _workspace(tmp_path, TARGET, SHORT_ROUTER)
    initial = materialize_planner_source_context(
        root, task_description=TASK, supporting_paths=()
    )
    assert TARGET not in initial.file_map()

    valid = _plan(_replace(NARROW_OLD, NEW_OLD))
    grounding = ground_post_plan_source_materialization(
        valid, project_dir=root, source_materialization=initial
    )
    assert grounding.ok, grounding.to_dict()
    assert grounding.materialization.file_map()[TARGET].planning_visible is False
    # R8/R9: GR5 unseen narrow edit and DGM1 post-Plan record stay admissible.
    assert _validate(root, valid, grounding.materialization).accepted

    broken = _plan(_replace(NARROW_OLD, NEW_OLD + "    (\n"))
    grounding = ground_post_plan_source_materialization(
        broken, project_dir=root, source_materialization=initial
    )
    assert grounding.ok, grounding.to_dict()
    assert _syntax_rejected(_validate(root, broken, grounding.materialization))


def test_r10_whole_file_replacement_remains_gr2_governed(tmp_path):
    root = _workspace(tmp_path, TARGET, _historical_source())
    verdict = _validate(
        root, _plan({"op": "write_file", "path": TARGET, "content": "x = 1\n"})
    )

    assert not verdict.accepted
    assert any(GR2_CODE in str(reason) for reason in verdict.reasons)


@pytest.mark.parametrize(
    ("path", "source", "old", "new"),
    [
        ("web/app.js", "export const x = 1;\n", "1;", "1 +;"),
        ("cfg/app.json", '{"a": 1}\n', "1}", "1"),
    ],
    ids=["javascript", "json"],
)
def test_r13_non_python_edits_get_no_python_syntax_rule(
    tmp_path, path, source, old, new
):
    root = _workspace(tmp_path, path, source)
    verdict = _validate(root, _plan(_replace(old, new, path), path=path), path=path)

    assert verdict.accepted, verdict.reasons
    assert not any(SYNTAX_CODE in str(reason) for reason in verdict.reasons)


def test_r14_new_file_creation_uses_write_file_rules(tmp_path):
    root = _workspace(tmp_path, TARGET, SHORT_ROUTER)
    new_path = "app/api/v1/permission_routes.py"
    ok = _plan(
        {"op": "write_file", "path": new_path, "content": "ROUTES = ()\n"},
        path=new_path,
    )
    bad = _plan(
        {"op": "write_file", "path": new_path, "content": "ROUTES = (\n"}, path=new_path
    )
    materialization = materialize_planner_source_context(
        root,
        task_description=TASK,
        expected_paths=[new_path],
        creation_authorized_paths=[new_path],
    )

    assert _validate(root, ok, materialization).accepted
    assert _syntax_rejected(_validate(root, bad, materialization))


def test_r15_debug_ops_fix_guard_covers_old_new_but_not_selector_ops(tmp_path):
    root = _workspace(tmp_path, MODULE, NESTED)
    broken_old_new = _replace("    return 0\n", "    return (0\n", MODULE)
    broken_selector = _selector(root, MODULE, 0, 3, "de f")

    violations = check_bounded_debug_repair_signature_contract(
        project_dir=root, ops=[broken_old_new]
    )
    assert [v.violation_type for v in violations] == ["post_parse_error"]
    # Carried gap: the ops_fix guard does not simulate selector replacements.
    assert (
        check_bounded_debug_repair_signature_contract(
            project_dir=root, ops=[broken_selector]
        )
        == []
    )
