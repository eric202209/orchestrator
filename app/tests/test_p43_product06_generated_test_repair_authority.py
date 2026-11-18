"""P43-PRODUCT-06 generated-test Debug Repair authority controls."""

from pathlib import Path
from copy import deepcopy
import ast
import json
import hashlib
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from types import SimpleNamespace

from app.services.orchestration.diagnostics.debug_feedback import (
    DebugFeedbackEnvelope,
    bounded_debug_repair_ops_authority_rejection,
    build_bounded_debug_repair_prompt_with_metadata,
    normalize_bounded_debug_repair_payload_detailed,
)
from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.phases.execution_debug_repair import (
    derive_bounded_debug_repair_generated_test_paths,
)
from app.services.orchestration.prompt_templates import StepResult
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_plan_identity,
)
from app.services.orchestration.validation.path_authority import (
    AcceptedPathAuthority,
    GrantClass,
    GrantProvenance,
    PathGrant,
    declare,
)
from app.tests.test_p43_product03_repair_grounding_and_retry import (
    PAGEKIT_BUGGY_TOTAL,
    PAGEKIT_FIXED_TOTAL,
    PAGEKIT_TASK,
    _pagekit_workspace,
)


PLAN = [
    {
        "step_number": 1,
        "description": "Repair paging and add a regression test",
        "commands": [],
        "verification": "python3 -m pytest -q",
        "rollback": None,
        "expected_files": [
            "pagekit/paging.py",
            "tests/test_pagination_regression.py",
        ],
        "ops": [
            {
                "op": "replace_in_file",
                "path": "pagekit/paging.py",
                "old": "return items",
                "new": "return list(items)",
            },
            {
                "op": "write_file",
                "path": "tests/test_pagination_regression.py",
                "content": "def test_page():\n    assert True\n",
            },
        ],
    }
]


def _write_fixture(project_dir: Path) -> None:
    (project_dir / "pagekit").mkdir()
    (project_dir / "tests").mkdir()
    (project_dir / "pagekit" / "paging.py").write_text(
        "def paginate(items):\n    return list(items)\n", encoding="utf-8"
    )
    (project_dir / "tests" / "test_pagination_regression.py").write_text(
        "def test_page():\n    assert [8, 9] == [9]\n", encoding="utf-8"
    )


def _authority(project_dir: Path) -> AcceptedPathAuthority:
    grants = (
        PathGrant(
            path=declare("pagekit/paging.py"),
            grant_class=GrantClass.EXISTING_MUTABLE,
            provenance=GrantProvenance.ACCEPTED_PLAN,
            baseline_content_hash="a" * 64,
        ),
        PathGrant(
            path=declare("tests/test_pagination_regression.py"),
            grant_class=GrantClass.CREATION_AUTHORIZED,
            provenance=GrantProvenance.ACCEPTED_PLAN,
        ),
    )
    return AcceptedPathAuthority.create(
        accepted_plan_identity=accepted_plan_identity(PLAN),
        workspace_identity=str(project_dir.resolve()),
        maximum_scope_digest="b" * 64,
        grants=grants,
    )


def _state(project_dir: Path, *, execution_id: int = 404, status: str = "debugging"):
    return SimpleNamespace(
        plan=PLAN,
        task_description=(
            "Fix paging.\n\n"
            "Independent acceptance contract (required):\n"
            "- `python3 -m pytest tests/test_pagination_acceptance.py -q`\n"
        ),
        status=status,
        project_dir=project_dir,
        execution_results=[
            StepResult(
                step_number=1,
                status="failed",
                files_changed=[
                    "pagekit/paging.py",
                    "tests/test_pagination_regression.py",
                ],
                created_files=["tests/test_pagination_regression.py"],
                task_execution_id=execution_id,
                workspace_identity=str(project_dir.resolve()),
                accepted_plan_identity=accepted_plan_identity(PLAN),
            )
        ],
    )


def _envelope(project_dir: Path) -> DebugFeedbackEnvelope:
    return DebugFeedbackEnvelope(
        task_execution_id=404,
        task_id=314,
        step_index=1,
        failure_phase="execution",
        failed_command="python3 -m pytest -q",
        stderr_excerpt="tests/test_pagination_regression.py:2: AssertionError",
        workspace_path=str(project_dir),
        failure_class="pytest_failure",
    )


def _write_e2e_fixture(project_dir: Path) -> None:
    (project_dir / "pagekit").mkdir()
    (project_dir / "tests").mkdir()
    (project_dir / "pagekit" / "__init__.py").write_text(
        "from pagekit.paging import paginate\n", encoding="utf-8"
    )
    (project_dir / "pagekit" / "paging.py").write_text(
        "def paginate(items):\n    return items\n", encoding="utf-8"
    )
    (project_dir / "tests" / "test_pagination_acceptance.py").write_text(
        "from pagekit import paginate\n\n"
        "def test_acceptance():\n    assert paginate([1, 2]) == [1, 2]\n",
        encoding="utf-8",
    )


def test_authorized_two_file_repair_executes_and_preserves_independent_acceptance(
    tmp_path,
):
    _write_e2e_fixture(tmp_path)
    acceptance = tmp_path / "tests" / "test_pagination_acceptance.py"
    acceptance_before = acceptance.read_bytes()
    authority = _authority(tmp_path)
    initial_ops = [
        {
            "op": "replace_in_file",
            "path": "pagekit/paging.py",
            "old": "    return items",
            "new": "    return list(items)",
        },
        {
            "op": "write_file",
            "path": "tests/test_pagination_regression.py",
            "content": (
                "from pagekit import paginate\n\n"
                "def test_generated_regression():\n"
                "    assert paginate([8, 9]) == [9]\n"
            ),
        },
    ]
    initial = ExecutorService.execute_file_ops(
        tmp_path, initial_ops, accepted_path_authority=authority
    )
    assert initial["success"] is True
    result = StepResult(
        step_number=1,
        status="failed",
        files_changed=initial["files_changed"],
        created_files=["tests/test_pagination_regression.py"],
        task_execution_id=404,
        workspace_identity=str(tmp_path.resolve()),
        accepted_plan_identity=accepted_plan_identity(PLAN),
    )
    state = _state(tmp_path)
    state.execution_results = [result]
    generated_paths = derive_bounded_debug_repair_generated_test_paths(
        accepted_path_authority=authority,
        accepted_plan=PLAN,
        orchestration_state=state,
        current_step_result=result,
        task_execution_id=404,
        task_execution_status="running",
        project_dir=tmp_path,
        task=SimpleNamespace(workspace_status="isolated", promoted_at=None),
    )
    assert generated_paths == ("tests/test_pagination_regression.py",)
    prompt = build_bounded_debug_repair_prompt_with_metadata(
        _envelope(tmp_path),
        prior_source_paths=["pagekit/paging.py"],
        authorized_generated_test_paths=generated_paths,
    )
    assert "## CURRENT CONTENT OF AUTHORIZED GENERATED TEST FILES" in prompt.prompt
    assert "assert paginate([8, 9]) == [9]" in prompt.prompt
    assert "Existing baseline tests" in prompt.prompt

    repair_ops = [
        {
            "op": "replace_in_file",
            "path": "pagekit/paging.py",
            "old": "    return list(items)",
            "new": "    return list(items)",
        },
        {
            "op": "replace_in_file",
            "path": "tests/test_pagination_regression.py",
            "old": "== [9]",
            "new": "== [8, 9]",
        },
    ]
    assert (
        bounded_debug_repair_ops_authority_rejection(
            repair_ops,
            authority=authority,
            project_dir=tmp_path,
            generated_test_paths=generated_paths,
        )
        is None
    )
    repaired = ExecutorService.execute_file_ops(
        tmp_path,
        repair_ops[1:],
        accepted_path_authority=authority,
        debug_repair_generated_test_paths=generated_paths,
    )
    assert repaired["success"] is True
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert acceptance.read_bytes() == acceptance_before


def test_existing_and_unknown_test_paths_remain_rejected(tmp_path):
    _write_fixture(tmp_path)
    authority = _authority(tmp_path)
    operation = {
        "op": "replace_in_file",
        "path": "tests/test_pagination_regression.py",
        "old": "== [9]",
        "new": "== [8, 9]",
    }
    assert (
        bounded_debug_repair_ops_authority_rejection(
            [operation], authority=authority, project_dir=tmp_path
        )
        == "unauthorized_source_path"
    )
    assert (
        bounded_debug_repair_ops_authority_rejection(
            [
                {
                    **operation,
                    "path": "tests/test_unknown.py",
                }
            ],
            authority=authority,
            project_dir=tmp_path,
            generated_test_paths=("tests/test_unknown.py",),
        )
        == "unauthorized_source_path"
    )
    executor_result = ExecutorService.execute_file_ops(
        tmp_path, [operation], accepted_path_authority=authority
    )
    assert executor_result["success"] is False
    assert executor_result["failure_category"] == "validation_failure"


def _portable_test_functions(source: str) -> set[str]:
    return {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    }


def _portable_copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".agent", "__pycache__", ".pytest_cache"),
    )


def _portable_run_pytest(cwd: Path, targets: list[str]) -> dict:
    report = cwd / ".portable-oracle-junit.xml"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
            *targets,
        ],
        cwd=cwd,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    cases = {}
    if report.exists():
        for case in ET.parse(report).iter("testcase"):
            outcome = "passed"
            for child in case:
                if child.tag in ("failure", "error"):
                    outcome = "failed"
                elif child.tag == "skipped":
                    outcome = "skipped"
            cases[case.get("name", "")] = outcome
        report.unlink()
    return {"exit": process.returncode, "cases": cases}


def _run_portable_completion_oracle(
    baseline: Path,
    change_set: Path,
    acceptance: str,
    existing: list[str],
    tmp_path: Path,
) -> dict:
    """Keep the independent completion control usable when ignored evidence is absent."""

    manifest = json.loads((change_set / "manifest.json").read_text())
    copied = list(manifest.get("copied_files") or [])
    deleted = list(manifest.get("deleted_files") or [])
    candidate = tmp_path / "portable-oracle-candidate"
    _portable_copy_tree(baseline, candidate)
    for relative in copied:
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(change_set / "files" / relative, target)
    for relative in deleted:
        (candidate / relative).unlink(missing_ok=True)

    acceptance_protected = (
        acceptance not in copied
        and hashlib.sha256((candidate / acceptance).read_bytes()).digest()
        == hashlib.sha256((baseline / acceptance).read_bytes()).digest()
    )

    functional = _portable_run_pytest(candidate, [acceptance])
    functional_summary = {
        "passed": sum(value == "passed" for value in functional["cases"].values()),
        "total": len(functional["cases"]),
        "exit": functional["exit"],
    }
    functional_pass = functional["exit"] == 0 and bool(functional["cases"])

    preserved = tmp_path / "portable-oracle-preserved"
    _portable_copy_tree(candidate, preserved)
    for relative in existing:
        shutil.copy2(baseline / relative, preserved / relative)
    preservation = _portable_run_pytest(preserved, existing)
    weakened = []
    for relative in existing:
        if relative in copied:
            before = _portable_test_functions((baseline / relative).read_text())
            after = _portable_test_functions((candidate / relative).read_text())
            if not before <= after:
                weakened.append(relative)
    preservation_pass = preservation["exit"] == 0 and not weakened

    delivered = {}
    for relative in copied:
        name = relative.rsplit("/", 1)[-1]
        if relative == acceptance or not (
            name.endswith(".py")
            and (name.startswith("test_") or name.endswith("_test.py"))
        ):
            continue
        after = _portable_test_functions((candidate / relative).read_text())
        before = (
            _portable_test_functions((baseline / relative).read_text())
            if (baseline / relative).exists()
            else set()
        )
        new = sorted(after - before)
        if new:
            delivered[relative] = new
    new_names = {name for names in delivered.values() for name in names}

    if not delivered:
        deliverable = "MISSING"
    else:
        on_candidate = _portable_run_pytest(candidate, sorted(delivered))
        candidate_new = {
            name: on_candidate["cases"].get(name, "absent") for name in new_names
        }
        regress = tmp_path / "portable-oracle-regress"
        _portable_copy_tree(baseline, regress)
        for relative in delivered:
            target = regress / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate / relative, target)
        on_baseline = _portable_run_pytest(regress, sorted(delivered))
        baseline_new = {
            name: on_baseline["cases"].get(name, "absent") for name in new_names
        }
        if any(value != "passed" for value in candidate_new.values()):
            deliverable = "FAILING_ON_CANDIDATE"
        elif not any(value != "passed" for value in baseline_new.values()):
            deliverable = "INEFFECTIVE"
        else:
            deliverable = "COMPLETE"

    return {
        "acceptance_protected": acceptance_protected,
        "functional": functional_summary,
        "preservation_pass": preservation_pass,
        "deliverable": deliverable,
        "complete_delivery": bool(
            acceptance_protected
            and functional_pass
            and preservation_pass
            and deliverable == "COMPLETE"
        ),
    }


def test_pagekit_reference_candidate_passes_external_completion_oracle(tmp_path):
    baseline = _pagekit_workspace(tmp_path / "baseline")
    change_set = tmp_path / "changeset"
    (change_set / "files" / "pagekit").mkdir(parents=True)
    (change_set / "files" / "tests").mkdir()
    source = (baseline / "pagekit" / "paging.py").read_text(encoding="utf-8")
    (change_set / "files" / "pagekit" / "paging.py").write_text(
        source.replace(PAGEKIT_BUGGY_TOTAL, PAGEKIT_FIXED_TOTAL), encoding="utf-8"
    )
    generated_test = (
        "import pytest\n"
        "from pagekit import paginate\n\n\n"
        "def test_final_partial_page_is_returned():\n"
        "    assert paginate(list(range(10)), 3, 4).items == [8, 9]\n\n\n"
        "def test_empty_sequence_has_one_empty_page():\n"
        "    assert paginate([], 1, 4).total_pages == 1\n\n\n"
        "def test_partial_page_has_no_next():\n"
        "    assert not paginate(list(range(10)), 3, 4).has_next\n\n\n"
        "def test_full_page_is_preserved():\n"
        "    assert paginate(list(range(10)), 2, 4).items == [4, 5, 6, 7]\n\n\n"
        "def test_page_beyond_last_still_fails():\n"
        '    with pytest.raises(ValueError, match="page number out of range"):\n'
        "        paginate(list(range(10)), 4, 4)\n\n\n"
        "def test_invalid_page_size_message_is_preserved():\n"
        '    with pytest.raises(ValueError, match="per_page must be at least 1"):\n'
        "        paginate([], 1, 0)\n"
    )
    generated_path = change_set / "files" / "tests" / "test_pagination_regression.py"
    generated_path.write_text(generated_test, encoding="utf-8")
    (change_set / "manifest.json").write_text(
        json.dumps(
            {
                "task_execution_id": 404,
                "copied_files": [
                    "pagekit/paging.py",
                    "tests/test_pagination_regression.py",
                ],
                "deleted_files": [],
            }
        ),
        encoding="utf-8",
    )
    verdict = _run_portable_completion_oracle(
        baseline,
        change_set,
        "tests/test_pagekit_acceptance.py",
        ["tests/test_paging.py"],
        tmp_path,
    )
    assert verdict["acceptance_protected"] is True
    assert verdict["functional"] == {"exit": 0, "passed": 1, "total": 1}
    assert verdict["preservation_pass"] is True
    assert verdict["deliverable"] == "COMPLETE"
    assert verdict["complete_delivery"] is True


def test_pagekit_reference_two_file_debug_repair_uses_production_interfaces(
    tmp_path,
):
    root = _pagekit_workspace(tmp_path / "runtime")
    acceptance_before = (root / "tests" / "test_pagekit_acceptance.py").read_bytes()
    generated_path = "tests/test_pagination_regression.py"
    generated_wrong = (
        "from pagekit import paginate\n\n\n"
        "def test_final_partial_page_is_returned():\n"
        "    assert paginate(list(range(10)), 3, 4).items == [9]\n\n\n"
        "def test_empty_sequence_has_one_page():\n"
        "    assert paginate([], 1, 4).total_pages == 1\n"
    )
    plan = [
        {
            "step_number": 1,
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "pagekit/paging.py",
                    "old": PAGEKIT_BUGGY_TOTAL,
                    "new": "        return -(-self.total_items // self.per_page)\n",
                },
                {
                    "op": "write_file",
                    "path": generated_path,
                    "content": generated_wrong,
                },
            ],
        }
    ]
    source_baseline_hash = hashlib.sha256(
        (root / "pagekit" / "paging.py").read_bytes()
    ).hexdigest()
    authority = AcceptedPathAuthority.create(
        accepted_plan_identity=accepted_plan_identity(plan),
        workspace_identity=str(root.resolve()),
        maximum_scope_digest="d" * 64,
        grants=(
            PathGrant(
                path=declare("pagekit/paging.py"),
                grant_class=GrantClass.EXISTING_MUTABLE,
                provenance=GrantProvenance.ACCEPTED_PLAN,
                baseline_content_hash=source_baseline_hash,
            ),
            PathGrant(
                path=declare(generated_path),
                grant_class=GrantClass.CREATION_AUTHORIZED,
                provenance=GrantProvenance.ACCEPTED_PLAN,
            ),
        ),
    )
    initial = ExecutorService.execute_file_ops(
        root,
        plan[0]["ops"],
        accepted_path_authority=authority,
    )
    assert initial["success"] is True
    before = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_pagination_regression.py",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert before.returncode == 1
    state = SimpleNamespace(
        plan=plan,
        task_description=PAGEKIT_TASK,
        status="debugging",
        project_dir=root,
        execution_results=[],
    )
    current_result = StepResult(
        step_number=1,
        status="failed",
        files_changed=initial["files_changed"],
        created_files=[generated_path],
        task_execution_id=404,
        workspace_identity=str(root.resolve()),
        accepted_plan_identity=accepted_plan_identity(plan),
    )
    generated_paths = derive_bounded_debug_repair_generated_test_paths(
        accepted_path_authority=authority,
        accepted_plan=plan,
        orchestration_state=state,
        current_step_result=current_result,
        task_execution_id=404,
        task_execution_status="running",
        project_dir=root,
        task=SimpleNamespace(workspace_status="isolated", promoted_at=None),
    )
    assert generated_paths == (generated_path,)
    correction = [
        {
            "op": "replace_in_file",
            "path": "pagekit/paging.py",
            "old": "        return -(-self.total_items // self.per_page)\n",
            "new": PAGEKIT_FIXED_TOTAL,
        },
        {
            "op": "replace_in_file",
            "path": generated_path,
            "old": "== [9]",
            "new": "== [8, 9]",
        },
    ]
    normalized = normalize_bounded_debug_repair_payload_detailed(
        [
            {
                "repair_type": "ops_fix",
                "ops": correction,
                "verification_command": "python3 -m pytest -q",
            }
        ],
        envelope=DebugFeedbackEnvelope(
            task_execution_id=404,
            task_id=314,
            step_index=1,
            failure_phase="execution",
            failed_command="python3 -m pytest -q",
            workspace_path=str(root),
            failure_class="pytest_failure",
        ),
        source_edit_context=True,
        authorized_generated_test_paths=generated_paths,
    )
    assert normalized.payload is not None
    assert (
        bounded_debug_repair_ops_authority_rejection(
            correction,
            authority=authority,
            project_dir=root,
            generated_test_paths=generated_paths,
        )
        is None
    )
    repaired = ExecutorService.execute_file_ops(
        root,
        correction,
        accepted_path_authority=authority,
        debug_repair_generated_test_paths=generated_paths,
    )
    assert repaired["success"] is True, repaired
    after = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_pagination_regression.py",
            "tests/test_paging.py",
            "tests/test_pagekit_acceptance.py",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert after.returncode == 0, after.stdout + after.stderr
    assert (
        root / "tests" / "test_pagekit_acceptance.py"
    ).read_bytes() == acceptance_before


def test_current_blocker_red_control_before_authority_extension(tmp_path):
    _write_fixture(tmp_path)
    envelope = _envelope(tmp_path)
    result = normalize_bounded_debug_repair_payload_detailed(
        [
            {
                "repair_type": "ops_fix",
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": "tests/test_pagination_regression.py",
                        "old": "== [9]",
                        "new": "== [8, 9]",
                    }
                ],
                "verification_command": "python3 -m pytest -q",
            }
        ],
        envelope=envelope,
        source_edit_context=True,
    )
    assert result.payload is None
    assert result.rejection_reason == "unauthorized_source_path"


def test_authorized_generated_test_path_is_accepted_after_extension(tmp_path):
    _write_fixture(tmp_path)
    state = _state(tmp_path)
    paths = derive_bounded_debug_repair_generated_test_paths(
        accepted_path_authority=_authority(tmp_path),
        accepted_plan=PLAN,
        orchestration_state=state,
        current_step_result=state.execution_results[0],
        task_execution_id=404,
        task_execution_status="running",
        project_dir=tmp_path,
        task=SimpleNamespace(workspace_status="isolated", promoted_at=None),
    )
    assert paths == ("tests/test_pagination_regression.py",)

    result = normalize_bounded_debug_repair_payload_detailed(
        [
            {
                "repair_type": "ops_fix",
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": "tests/test_pagination_regression.py",
                        "old": "== [9]",
                        "new": "== [8, 9]",
                    }
                ],
                "verification_command": "python3 -m pytest -q",
            }
        ],
        envelope=_envelope(tmp_path),
        source_edit_context=True,
        authorized_generated_test_paths=paths,
    )
    assert result.payload is not None


def test_generated_test_authority_fails_closed_for_wrong_execution_and_publication(
    tmp_path,
):
    _write_fixture(tmp_path)
    authority = _authority(tmp_path)
    state = _state(tmp_path, execution_id=405)
    assert (
        derive_bounded_debug_repair_generated_test_paths(
            accepted_path_authority=authority,
            accepted_plan=PLAN,
            orchestration_state=state,
            current_step_result=state.execution_results[0],
            task_execution_id=404,
            task_execution_status="running",
            project_dir=tmp_path,
            task=SimpleNamespace(workspace_status="isolated", promoted_at=None),
        )
        == ()
    )


def test_generated_test_authority_rejects_stale_origin_plan_and_protected_acceptance(
    tmp_path,
):
    _write_fixture(tmp_path)
    authority = _authority(tmp_path)
    state = _state(tmp_path)
    result = state.execution_results[0]

    result.created_files = []
    assert (
        derive_bounded_debug_repair_generated_test_paths(
            accepted_path_authority=authority,
            accepted_plan=PLAN,
            orchestration_state=state,
            current_step_result=result,
            task_execution_id=404,
            task_execution_status="cancelled",
            project_dir=tmp_path,
            task=SimpleNamespace(workspace_status="isolated", promoted_at=None),
        )
        == ()
    )
    result.created_files = ["tests/test_not_in_plan.py"]
    result.files_changed = ["tests/test_not_in_plan.py"]
    assert (
        derive_bounded_debug_repair_generated_test_paths(
            accepted_path_authority=authority,
            accepted_plan=PLAN,
            orchestration_state=state,
            current_step_result=result,
            task_execution_id=404,
            task_execution_status="running",
            project_dir=tmp_path,
            task=SimpleNamespace(workspace_status="isolated", promoted_at=None),
        )
        == ()
    )

    protected_path = "tests/test_pagination_acceptance.py"
    (tmp_path / protected_path).write_text("def test_acceptance():\n    assert True\n")
    protected_plan = deepcopy(PLAN)
    protected_plan[0]["ops"].append(
        {
            "op": "write_file",
            "path": protected_path,
            "content": "def test_acceptance():\n    assert True\n",
        }
    )
    protected_plan[0]["expected_files"].append(protected_path)
    protected_authority = AcceptedPathAuthority.create(
        accepted_plan_identity=accepted_plan_identity(protected_plan),
        workspace_identity=str(tmp_path.resolve()),
        maximum_scope_digest="c" * 64,
        grants=authority.grants
        + (
            PathGrant(
                path=declare(protected_path),
                grant_class=GrantClass.CREATION_AUTHORIZED,
                provenance=GrantProvenance.ACCEPTED_PLAN,
            ),
        ),
    )
    state.plan = protected_plan
    state.execution_results = [
        StepResult(
            step_number=1,
            status="failed",
            files_changed=[protected_path],
            created_files=[protected_path],
            task_execution_id=404,
            workspace_identity=str(tmp_path.resolve()),
            accepted_plan_identity=accepted_plan_identity(protected_plan),
        )
    ]
    assert (
        derive_bounded_debug_repair_generated_test_paths(
            accepted_path_authority=protected_authority,
            accepted_plan=protected_plan,
            orchestration_state=state,
            current_step_result=state.execution_results[0],
            task_execution_id=404,
            task_execution_status="running",
            project_dir=tmp_path,
            task=SimpleNamespace(workspace_status="isolated", promoted_at=None),
        )
        == ()
    )

    state = _state(tmp_path)
    assert (
        derive_bounded_debug_repair_generated_test_paths(
            accepted_path_authority=authority,
            accepted_plan=PLAN,
            orchestration_state=state,
            current_step_result=state.execution_results[0],
            task_execution_id=404,
            task_execution_status="failed",
            project_dir=tmp_path,
            task=SimpleNamespace(workspace_status="isolated", promoted_at=None),
        )
        == ()
    )

    assert (
        derive_bounded_debug_repair_generated_test_paths(
            accepted_path_authority=authority,
            accepted_plan=PLAN,
            orchestration_state=state,
            current_step_result=state.execution_results[0],
            task_execution_id=404,
            task_execution_status="running",
            project_dir=tmp_path,
            task=SimpleNamespace(workspace_status="promoted", promoted_at=True),
        )
        == ()
    )
