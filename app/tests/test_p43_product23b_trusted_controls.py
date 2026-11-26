"""P43-PRODUCT-23B: trusted controls, snapshot metadata, image preflight (offline).

Evidence classes (see the PRODUCT-23B report):

* contract: material pinning, per-control guard, Change Set integrity, the
  snapshot exclusions and the Host pytest-image preflight (Docker stubbed);
* actual local subprocess: the reference control through the real Worker body
  and strict Launcher with the 23A ``LocalPytestExecutor`` double, then the
  frozen P02 oracle and hidden suite (uncontained, trusted code only);
* fake-Docker integration: the containment probe with an executor double,
  since its assertions hold only inside the real Sandbox.

The frozen PRODUCT-18 material is read from the local, git-ignored evidence
tree and never vendored; tests needing it skip when it is absent (CI).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from app.config import settings
from app.models import TaskExecution, TaskStatus
from app.services.orchestration.execution.sandbox_gateway import (
    SandboxSnapshotRejected,
    build_workspace_snapshot,
)
from app.services.sandbox_bridge.contract import ExecutionResult
from app.tests import test_p43_product22_c2_c3b_live_execution as c3b
from app.tests import test_p43_product23a_contained_coding as c23a
from scripts.maintenance import p43_c2_trusted_submitter as submitter

host, c2_env, no_network = c23a.host, c23a.c2_env, c23a.no_network
no_local_step_execution, lifecycle = c23a.no_local_step_execution, c23a.lifecycle

REPO = c23a.REPO
EVIDENCE = c23a.EVIDENCE
HOST_SCRIPT = REPO / "scripts" / "maintenance" / "p43_sandbox_bridge_host.sh"
LIVE_RUN = REPO / "scripts" / "maintenance" / "p43_c2_live_run.sh"
SUBMITTER = REPO / "scripts" / "maintenance" / "p43_c2_trusted_submitter.py"


def _require_evidence() -> None:
    if not (EVIDENCE / submitter.U1_DIR / "manifest.json").is_file():
        pytest.skip("frozen PRODUCT-18 U1 evidence is not present (docs/ is local)")


def _stage(tmp_path: Path, parts: str) -> Path:
    _require_evidence()
    destination = tmp_path / f"material-{parts}"
    result = subprocess.run(
        [
            "python3",
            "-I",
            str(SUBMITTER),
            "stage-material",
            str(EVIDENCE),
            str(destination),
            "--parts",
            parts,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return destination


# --- Snapshot metadata ------------------------------------------------------


def _workspace(root: Path) -> Path:
    for rel, text in {
        "pkg/__init__.py": "",
        "pkg/runtime.json": "{}",
        "pkg/.agent/notes.txt": "project-owned",
        "runtime.json": '{"runtime_schema_version": 1}',
        ".agent/auto-snapshots/pre-run/pkg/__init__.py": "",
        "tests/test_pkg.py": "def test_ok():\n    assert True\n",
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_snapshot_skips_root_control_state_and_task_sandbox_metadata_only(tmp_path):
    root = _workspace(tmp_path / "ws")
    plain = [e["path"] for e in build_workspace_snapshot(root)[0]]
    sandbox = [
        e["path"] for e in build_workspace_snapshot(root, task_sandbox_root=True)[0]
    ]
    # The root .agent is never sent; nested project files of the same names are.
    assert plain == [
        "pkg/.agent/notes.txt",
        "pkg/__init__.py",
        "pkg/runtime.json",
        "runtime.json",
        "tests/test_pkg.py",
    ]
    assert sandbox == [path for path in plain if path != "runtime.json"]


def test_snapshot_metadata_skips_keep_fail_closed_checks(tmp_path):
    root = _workspace(tmp_path / "ws")
    shutil.rmtree(root / ".agent")
    (root / ".agent").symlink_to(root / "pkg")
    with pytest.raises(SandboxSnapshotRejected, match="symlink"):
        build_workspace_snapshot(root)
    (root / ".agent").unlink()
    (root / "runtime.json").unlink()
    (root / "runtime.json").symlink_to(root / "pkg" / "runtime.json")
    with pytest.raises(SandboxSnapshotRejected, match="symlink"):
        build_workspace_snapshot(root, task_sandbox_root=True)


# --- Material pinning --------------------------------------------------------


def test_material_verification_is_pinned_exact_and_hidden_free(tmp_path):
    submit_material = _stage(tmp_path, "submit")
    manifest = submitter.verify_u1_material(submit_material, submitter.SUBMIT_PARTS)
    assert manifest["id"] == "U1"
    u1 = submit_material / submitter.U1_DIR
    # The submitter mount carries no hidden suite, naive control or tools.
    assert not (u1 / "hidden").exists() and not (u1 / "controls" / "naive").exists()
    assert not (submit_material / submitter.ORACLE_PATH).exists()
    evaluation = _stage(tmp_path, "evaluation")
    submitter.verify_u1_material(evaluation, submitter.EVALUATION_PARTS)
    # Extra material (here: the hidden suite) in the submitter mount is refused.
    with pytest.raises(submitter.SubmitterRefused, match="u1_material_mismatch"):
        submitter.verify_u1_material(evaluation, submitter.SUBMIT_PARTS)


def test_material_tampering_is_refused(tmp_path):
    material = _stage(tmp_path, "submit")
    target = material / submitter.U1_DIR / "controls/reference/files/rangekit/ranges.py"
    target.chmod(0o644)
    target.write_text(target.read_text() + "\n# tampered\n")
    with pytest.raises(submitter.SubmitterRefused, match="differing="):
        submitter.verify_u1_material(material, submitter.SUBMIT_PARTS)
    (material / submitter.U1_DIR / "manifest.json").chmod(0o644)
    (material / submitter.U1_DIR / "manifest.json").write_text("{}")
    with pytest.raises(submitter.SubmitterRefused, match="u1_manifest_sha256_mismatch"):
        submitter.verify_u1_material(material, submitter.SUBMIT_PARTS)


def test_unpinned_manifest_is_refused_without_evidence(tmp_path):
    manifest = tmp_path / submitter.U1_DIR / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"task_sha256": "0"}')
    with pytest.raises(submitter.SubmitterRefused, match="u1_manifest_sha256_mismatch"):
        submitter.verify_u1_material(tmp_path, submitter.SUBMIT_PARTS)
    with pytest.raises(submitter.SubmitterRefused, match="u1_manifest_sha256_mismatch"):
        submitter.stage_material(tmp_path, tmp_path / "out", submitter.SUBMIT_PARTS)


def test_submitter_embeds_no_reference_or_hidden_material():
    source = SUBMITTER.read_text(encoding="utf-8")
    for fragment in ("sorted(_checked", "start <= merged", "def test_u1_hidden"):
        assert fragment not in source
    _require_evidence()
    u1 = EVIDENCE / submitter.U1_DIR
    for path in (u1 / "controls" / "reference" / "files").rglob("*.py"):
        for line in path.read_text().splitlines():
            if len(line.strip()) > 30:
                assert line not in source


def test_reference_plan_is_derived_from_verified_material(tmp_path):
    material = _stage(tmp_path, "submit")
    plan = submitter.u1_reference_plan(material)
    files = material / submitter.U1_DIR / "controls" / "reference" / "files"
    ops = [op for step in plan for op in step.get("ops", [])]
    assert [op["path"] for op in ops] == [
        "rangekit/ranges.py",
        "tests/test_merge_ranges_regression.py",
    ]
    for op in ops:
        assert op["op"] == "write_file"
        assert op["content"] == (files / op["path"]).read_text()
    assert [step["verification"] for step in plan] == [
        "python -m pytest tests/test_ranges.py -q",
        "python -m pytest tests/test_merge_ranges_regression.py -q",
        "python -m pytest tests/test_rangekit_acceptance.py -q",
    ]


# --- Change Set integrity ----------------------------------------------------

_MANIFEST = {
    "acceptance": "tests/test_rangekit_acceptance.py",
    "existing_tests": ["tests/test_ranges.py"],
    "expected_changed_files": ["rangekit/ranges.py", "new test file"],
    "baseline_files": {
        "rangekit/__init__.py": "a",
        "rangekit/ranges.py": "b",
        "tests/test_rangekit_acceptance.py": "c",
        "tests/test_ranges.py": "d",
    },
}


def _change_set(root: Path, copied, deleted=()) -> Path:
    (root / "files").mkdir(parents=True)
    for rel in copied:
        (root / "files" / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / "files" / rel).write_text("x = 1\n")
    (root / "manifest.json").write_text(
        json.dumps({"copied_files": list(copied), "deleted_files": list(deleted)})
    )
    return root


@pytest.mark.parametrize(
    ("copied", "deleted", "reason"),
    [
        (["rangekit/ranges.py", "tests/test_new.py"], [], None),
        (["rangekit/helpers.py", "tests/test_new.py"], [], None),
        (["tests/conftest.py"], [], "test_environment_modification:tests/conftest.py"),
        (["conftest.py"], [], "test_environment_modification:conftest.py"),
        (["pytest.ini"], [], "test_environment_modification:pytest.ini"),
        (["pyproject.toml"], [], "test_environment_modification:pyproject.toml"),
        (["sitecustomize.py"], [], "test_environment_modification:sitecustomize.py"),
        (["rangekit/hook.pth"], [], "test_environment_modification:rangekit/hook.pth"),
        (["pytest.py"], [], "unauthorized_addition:pytest.py"),
        (["tests/helpers.py"], [], "unauthorized_addition:tests/helpers.py"),
        (["tests/test_rangekit_acceptance.py"], [], "acceptance_modified"),
        ([], ["tests/test_ranges.py"], "protected_test_deleted:tests/test_ranges.py"),
    ],
)
def test_change_set_integrity_rejects_test_environment_changes(
    tmp_path, copied, deleted, reason
):
    verdict = submitter.change_set_integrity(
        _MANIFEST, _change_set(tmp_path / "cs", copied, deleted)
    )
    if reason is None:
        assert verdict["ok"] is True, verdict
    else:
        assert verdict["ok"] is False
        assert any(item.startswith(reason) for item in verdict["reasons"]), verdict


def test_change_set_integrity_binds_held_row_to_artifact(tmp_path):
    cs = _change_set(tmp_path / "cs", ["rangekit/ranges.py", "tests/test_new.py"])
    row = {
        "added_files": ["tests/test_new.py"],
        "modified_files": ["rangekit/ranges.py"],
        "deleted_files": [],
    }
    assert submitter.change_set_integrity(_MANIFEST, cs, row)["ok"] is True
    # A change the product held but the artifact (and so the oracle) omits.
    hidden_change = dict(row, added_files=["tests/test_new.py", "tests/conftest.py"])
    verdict = submitter.change_set_integrity(_MANIFEST, cs, hidden_change)
    assert "change_set_row_does_not_match_artifact" in verdict["reasons"]
    assert "test_environment_modification:tests/conftest.py" in verdict["reasons"]
    (cs / "files" / "extra.py").write_text("")
    verdict = submitter.change_set_integrity(_MANIFEST, cs, row)
    assert "artifact_files_do_not_match_manifest" in verdict["reasons"]


# --- Per-control submission guard -------------------------------------------


def _te_count(db_session_factory) -> int:
    db = db_session_factory()
    try:
        return db.query(TaskExecution).count()
    finally:
        db.close()


def test_controls_are_single_submission_and_never_concurrent(
    c2_env, monkeypatch, db_session_factory
):
    queued: list[int] = []

    def queue(db, session, task_id, timeout_seconds):
        execution = TaskExecution(
            task_id=task_id,
            session_id=session.id,
            attempt_number=1,
            status=TaskStatus.RUNNING,
        )
        db.add(execution)
        db.commit()
        queued.append(execution.id)
        return {"task_execution_id": execution.id, "status": "queued"}

    monkeypatch.setattr(
        "app.services.session.session_runtime_service.queue_task_for_session", queue
    )
    first = submitter.submit(db_session_factory, control="probe")
    assert first["control"] == "probe" and set(first["seeded_files"]) == set(
        submitter.PROBE_FILES
    )
    with pytest.raises(submitter.SubmitterRefused, match="control_already_submitted"):
        submitter.submit(db_session_factory, control="probe")
    with pytest.raises(submitter.SubmitterRefused, match="control_in_progress"):
        submitter.submit(db_session_factory, control="reference")
    # The PRODUCT-22 exit check still demands a fresh database.
    with pytest.raises(submitter.SubmitterRefused, match="database_not_fresh"):
        submitter.submit(db_session_factory)
    with pytest.raises(submitter.SubmitterRefused, match="unknown_control"):
        submitter.submit(db_session_factory, control="naive")
    assert _te_count(db_session_factory) == 1 and len(queued) == 1


def test_controls_refuse_a_database_that_ran_the_exit_check(
    c2_env, monkeypatch, db_session_factory
):
    monkeypatch.setattr(
        "app.services.session.session_runtime_service.queue_task_for_session",
        lambda *a, **k: {"task_execution_id": None, "status": "queued"},
    )
    submitter.submit(db_session_factory)
    with pytest.raises(
        submitter.SubmitterRefused, match="database_not_disposable_controls"
    ):
        submitter.submit(db_session_factory, control="probe")


# --- Controls through the real Worker body ----------------------------------


def test_reference_control_reaches_calibrated_verdicts(
    c2_env,
    host,
    no_network,
    no_local_step_execution,
    monkeypatch,
    db_session_factory,
    tmp_path,
):
    _require_evidence()
    # In-process staging: the no-local-execution guard is already active.
    material, evaluation = tmp_path / "submit", tmp_path / "evaluation"
    submitter.stage_material(EVIDENCE, material, submitter.SUBMIT_PARTS)
    submitter.stage_material(EVIDENCE, evaluation, submitter.EVALUATION_PARTS)
    monkeypatch.setattr(submitter, "U1_MATERIAL_ROOT", str(material))
    # Provider-free: the stored-plan path needs no execution context.
    monkeypatch.setattr(settings, "EXECUTION_CONTEXT_TOKENS", None)
    staging = tmp_path / "launcher-staging"
    staging.mkdir(mode=0o700)
    executor = c23a.LocalPytestExecutor(staging)
    deliveries = c3b._eager_delivery(
        monkeypatch, db_session_factory, lambda: host(executor)
    )

    submitted = submitter.submit(db_session_factory, control="reference")
    observed = submitter.observe(
        db_session_factory, deadline_seconds=0, control="reference"
    )

    assert len(deliveries) == 1 and no_network == []
    assert observed["task_execution_id"] == submitted["task_execution_id"]
    assert observed["task_execution_status"].lower() == "done", observed
    assert observed["expected_outcome_met"] is True, observed
    for call in executor.calls:
        names = [entry["path"] for entry in call.workspace_snapshot]
        assert "tests/test_rangekit_acceptance.py" in names
        # Orchestrator metadata stays out of the Sandbox snapshot.
        assert "runtime.json" not in names
        assert not any(name.startswith(".agent/") for name in names)
        assert not any(name.startswith(("hidden", "controls")) for name in names)
    change_set = Path(observed["change_set"]["directory"])
    oracle = c23a._run_json(
        evaluation / submitter.ORACLE_PATH,
        evaluation / submitter.U1_DIR / "baseline",
        change_set,
        "tests/test_rangekit_acceptance.py",
        "tests/test_ranges.py",
    )
    hidden = c23a._run_json(
        evaluation / submitter.HIDDEN_SUITE_PATH,
        evaluation / submitter.U1_DIR / "baseline",
        change_set,
        evaluation / submitter.U1_DIR / "hidden",
    )
    verdict = submitter.evaluation_verdict(
        "reference", evaluation, change_set, oracle, hidden, observed
    )
    assert verdict["verdict"] == "MATCHES_FROZEN_EXPECTATION", verdict
    assert verdict["observed"]["complete_delivery_with_hidden"] is True


def test_naive_control_is_rejected_by_the_independent_evaluation(tmp_path):
    evaluation = _stage(tmp_path, "evaluation")
    u1 = evaluation / submitter.U1_DIR
    naive = u1 / "controls" / "naive"
    oracle = c23a._run_json(
        evaluation / submitter.ORACLE_PATH,
        u1 / "baseline",
        naive,
        "tests/test_rangekit_acceptance.py",
        "tests/test_ranges.py",
    )
    hidden = c23a._run_json(
        evaluation / submitter.HIDDEN_SUITE_PATH, u1 / "baseline", naive, u1 / "hidden"
    )
    verdict = submitter.evaluation_verdict("naive", evaluation, naive, oracle, hidden)
    assert verdict["verdict"] == "MATCHES_FROZEN_EXPECTATION", verdict
    assert verdict["observed"]["oracle_complete_delivery"] is False
    assert verdict["observed"]["complete_delivery_with_hidden"] is False
    with pytest.raises(
        submitter.SubmitterRefused, match="reference_observation_required"
    ):
        submitter.evaluation_verdict("reference", evaluation, naive, oracle, hidden)


def test_probe_control_times_out_without_retry_provider_or_write_back(
    c2_env,
    host,
    no_network,
    no_local_step_execution,
    monkeypatch,
    db_session_factory,
):
    monkeypatch.setattr(settings, "EXECUTION_CONTEXT_TOKENS", None)
    calls = []

    def executor(request, _cancel):
        calls.append(request)
        timeout = "test_c2_timeout_probe" in request.code
        names = {entry["path"] for entry in request.workspace_snapshot}
        assert set(submitter.PROBE_FILES) <= names
        return ExecutionResult(
            request.request_id,
            "TIMEOUT" if timeout else "SUCCESS",
            None if timeout else 0,
            "",
            "",
            duration=10.0 if timeout else 1.0,
        )

    deliveries = c3b._eager_delivery(
        monkeypatch, db_session_factory, lambda: host(executor)
    )
    submitter.submit(db_session_factory, control="probe")
    observed = submitter.observe(
        db_session_factory, deadline_seconds=0, control="probe"
    )

    assert observed["task_execution_status"].lower() == "failed", observed
    assert observed["expected_outcome_met"] is True, observed
    assert [json.loads(call.code)["args"][0] for call in calls] == [
        "tests/test_c2_containment_probe.py",
        "tests/test_c2_timeout_probe.py",
    ]
    assert len(deliveries) == 1 and _te_count(db_session_factory) == 1
    assert no_network == []
    assert {Path(argv[0]).name for argv in no_local_step_execution} <= {"git"}


def test_probe_sources_are_valid_python():
    for rel, text in submitter.PROBE_FILES.items():
        compile(text, rel, "exec")
    plan = [step["commands"] for step in submitter.PROBE_PLAN]
    assert plan == [
        ["python -m pytest tests/test_c2_containment_probe.py -q"],
        ["python -m pytest tests/test_c2_timeout_probe.py -q"],
    ]


# --- Host pytest-image preflight (Docker stubbed) ---------------------------


def _shell_function(script: Path, name: str) -> str:
    text = script.read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}\n", text)
    assert match is not None, name
    return match.group(0)


def _run_bash(body: str) -> str:
    command = (
        "set -euo pipefail\nFAILURES=0\n"
        "ok() { printf 'PASS %s\\n' \"$*\"; }\n"
        "bad() { printf 'FAIL %s\\n' \"$*\"; FAILURES=$((FAILURES + 1)); }\n"
        "info() { printf 'INFO %s\\n' \"$*\"; }\n"
        + body
        + '\necho "failures=$FAILURES"\n'
    )
    out = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


PINNED = "sha256:" + "c" * 64


@pytest.mark.parametrize(
    ("pin", "inspect", "expected"),
    [
        (
            "",
            "",
            ["INFO pytest image unpinned", "C2_PYTEST_IMAGE=UNPINNED", "failures=0"],
        ),
        (
            "pytest:latest",
            "",
            ["FAIL pytest image pin is not an immutable", "failures=1"],
        ),
        (PINNED, "absent", ["FAIL pinned pytest image absent locally", "failures=1"]),
        (PINNED, "amd64", ["FAIL pinned pytest image identity mismatch", "failures=1"]),
        (
            PINNED,
            "rebased",
            ["FAIL pinned pytest image is not built FROM", "failures=1"],
        ),
        (PINNED, "ok", [f"C2_PYTEST_IMAGE={PINNED}", "failures=0"]),
    ],
)
def test_pytest_image_preflight_distinguishes_stages(pin, inspect, expected):
    docker = (
        "docker() {\n"
        '  local target="${@: -1}" fmt="$4"\n'
        f"  case {inspect or 'none'} in absent) [[ $target == $IMAGE ]] || return 1;; esac\n"
        "  if [[ $fmt == *RootFS* ]]; then\n"
        '    if [[ $target == $IMAGE ]]; then echo "l1 l2 "; '
        f"elif [[ {inspect or 'none'} == rebased ]]; then echo 'l9 l2 l3 '; "
        "else echo 'l1 l2 l3 '; fi\n"
        "  else\n"
        f"    arch=arm64; [[ {inspect or 'none'} == amd64 ]] && arch=amd64\n"
        '    echo "$target linux/$arch"\n'
        "  fi\n"
        "}\n"
    )
    body = (
        f"IMAGE=base@sha256:{'a' * 64}\nPYTEST_IMAGE={pin!r}\n"
        + docker
        + _shell_function(HOST_SCRIPT, "pytest_image_check")
        + "pytest_image_check\n"
    )
    out = _run_bash(body)
    for text in expected:
        assert text in out, out


def test_pytest_image_pin_is_read_from_black_wrapped_source(tmp_path):
    text = HOST_SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"(?ms)^PYTEST_IMAGE=\$\(.*?^\)$", text)
    assert block is not None
    source = tmp_path / "docker_executor.py"
    for literal, expected in (
        ("PYTEST_IMAGE: str | None = None\n", ""),
        (f'PYTEST_IMAGE: str | None = (\n    "{PINNED}"\n)\n', PINNED),
        (f'PYTEST_IMAGE: str | None = "{PINNED}"\n', PINNED),
    ):
        source.write_text('IMAGE = "x"\n' + literal)
        out = subprocess.run(
            [
                "bash",
                "-c",
                f"SRC={tmp_path}\n{block.group(0)}\nprintf '%s' \"$PYTEST_IMAGE\"",
            ],
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout == expected
    # The current source is the image-build preparation stage.
    real = subprocess.run(
        [
            "bash",
            "-c",
            f"SRC={REPO}/app/services/sandbox_bridge\n{block.group(0)}\n"
            "printf '%s' \"$PYTEST_IMAGE\"",
        ],
        capture_output=True,
        text=True,
    )
    from app.services.sandbox_bridge import docker_executor

    assert real.stdout == (docker_executor.PYTEST_IMAGE or "")


def test_live_run_refuses_unknown_controls_before_host_actions():
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **c3b._VALID_LIVE_INPUTS}
    for argv, reason in (
        (["submit", "--operator-approved", "naive"], "unknown control"),
        (["evaluate", "probe"], "unknown evaluation control"),
    ):
        out = subprocess.run(
            ["bash", str(LIVE_RUN), *argv],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert out.returncode == 78 and reason in out.stderr, out.stderr
