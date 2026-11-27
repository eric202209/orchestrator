#!/usr/bin/env python3
"""Bounded Host-only reconstruction and evaluation procedure for PRODUCT-23C U1.

``reconstruct`` reads only retained PRODUCT-23C evidence, the accepted U1
Plan, the four retained provider responses and the frozen U1 material. It
creates a root-only reconstruction and never executes candidate code.

``evaluate --operator-approved`` is intentionally separate. It verifies the
approved local pytest image, creates a container-readable read-only staging
copy, runs the existing oracle/hidden evaluator and the generated regression
test in the complete candidate workspace, then checks candidate immutability.
It never submits a Task or calls a provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any, Mapping


EXPECTED_RESPONSES = {
    "0009": "1fad8adbb377e74a9ad7e1a15c49cf490d8946a5a5b41f24e9b392662dbf04c4",
    "0010": "0d2650dae0eea7948a6f2b80d657e659dc0bcdd887f051e8fceb5cd60ca9f192",
    "0011": "2715feb1f856df19f3f1aece51c3dd842515f8938ac6582f3acfd7c69381bd0a",
    "0012": "00600b50927244cda87510fb7b9fb183db456a05858bd4937ee7ea48ce469b67",
}
APPROVED_PYTEST_IMAGE = (
    "sha256:4fe5ac69330c35161326f6737210c24163ed525a230a24d894b1cb3b1b2fd42b"
)
REQUIRED_IDENTITY = (
    "task_execution_id",
    "task_id",
    "session_id",
    "attempt_number",
    "dispatch_lease_fence",
)


class ProcedureError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise ProcedureError(message)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"invalid JSON {path}: {exc}")


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(path, 0o600)


def safe_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        fail(f"unsafe relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        fail(f"unsafe relative path: {value!r}")
    if path.as_posix() != value:
        fail(f"non-canonical relative path: {value!r}")
    return value


def json_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        fail(f"{label} is not a list of strings")
    result = [safe_relative(item) for item in value]
    if len(result) != len(set(result)):
        fail(f"{label} contains duplicates")
    return result


def tree_hash(root: Path) -> str:
    entries: list[tuple[str, str, int]] = []
    if root.is_symlink() or not root.is_dir():
        fail(f"tree is not a directory: {root}")
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            fail(f"tree contains symlink: {relative}")
        if path.is_file():
            entries.append((relative, sha256_file(path), path.stat().st_size))
        elif not path.is_dir():
            fail(f"tree contains special file: {relative}")
    return sha256_bytes(
        json.dumps(entries, separators=(",", ":"), sort_keys=True).encode()
    )


def make_private(root: Path) -> None:
    if root.is_symlink():
        fail(f"refusing symlink root: {root}")
    root.mkdir(parents=True, exist_ok=True)
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            fail(f"refusing symlink: {path}")
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    os.chmod(root, 0o700)


def make_container_readonly(root: Path) -> None:
    """Make a bind-mounted copy readable by uid/gid 65534.

    The containing reconstruction root remains mode 0700. Docker's root
    daemon can bind-mount this nested tree; the container sees only these
    0555/0444 paths and cannot write them.
    """

    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            fail(f"container staging contains symlink: {path}")
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    os.chmod(root, 0o555)


def copy_tree(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        fail(f"destination already exists: {destination}")
    shutil.copytree(source, destination, symlinks=False)
    make_private(destination)


def response_document(evidence: Path, sequence: str) -> Any:
    path = evidence / "relay-bodies" / f"{sequence}.response.json"
    if not path.is_file() or path.is_symlink():
        fail(f"retained response missing: {path}")
    actual = sha256_file(path)
    if actual != EXPECTED_RESPONSES[sequence]:
        fail(f"response fingerprint mismatch for {sequence}: {actual}")
    payload = read_json(path)
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        fail(f"response {sequence} is not OpenAI-compatible JSON: {exc}")
    if not isinstance(content, str):
        fail(f"response {sequence} content is not serialized JSON")
    return json.loads(content)


def find_plan(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        if any("ops" in item for item in value):
            return value
    if isinstance(value, dict):
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        children = ()
    for child in children:
        found = find_plan(child)
        if found is not None:
            return found
    return None


def mutation_ops(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for step in plan:
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                fail("accepted Plan contains a non-object operation")
            result.append(operation)
    return result


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def no_op_debug_repair(value: Any) -> bool:
    pairs: list[tuple[Any, str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("old"), str) and isinstance(node.get("new"), str):
                pairs.append((node.get("path"), node["old"], node["new"]))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return bool(pairs) and all(old == new for _, old, new in pairs)


def load_records(evidence: Path, execution_id: int) -> tuple[dict[str, Any], ...]:
    observation_path = evidence / "observe.model.after-stop.json"
    if not observation_path.is_file():
        observation_path = evidence / "observe.model.json"
    observation = read_json(observation_path)
    if observation.get("terminal") is not True:
        fail("retained observation is not terminal")
    database = evidence / "orchestrator.db"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT te.id AS task_execution_id, te.session_id AS execution_session_id,
                   te.task_id AS execution_task_id, te.attempt_number,
                   te.status AS task_execution_status, te.dispatch_lease_fence,
                   t.id AS task_id, t.project_id AS task_project_id,
                   t.status AS task_status, t.steps,
                   s.id AS session_id, s.project_id AS session_project_id,
                   s.status AS session_status, p.id AS project_id,
                   cs.project_id AS change_project_id,
                   cs.task_id AS change_task_id, cs.session_id AS change_session_id,
                   cs.task_execution_id AS change_execution_id,
                   cs.added_files, cs.modified_files, cs.deleted_files,
                   cs.disposition, cs.review_decision, cs.status AS change_status
            FROM task_executions AS te
            JOIN tasks AS t ON t.id = te.task_id
            JOIN sessions AS s ON s.id = te.session_id
            JOIN projects AS p ON p.id = t.project_id
            JOIN task_execution_change_sets AS cs
              ON cs.task_execution_id = te.id
            WHERE te.id = ?
            """,
            (execution_id,),
        ).fetchone()
        if row is None:
            fail("authoritative joined execution/change-set record is absent")
        identity = {key: row[key] for key in REQUIRED_IDENTITY}
        for key in REQUIRED_IDENTITY:
            if observation.get(key) != identity[key]:
                fail(f"observation identity mismatch: {key}")
        if str(observation.get("task_execution_status", "")).lower() != str(
            row["task_execution_status"]
        ).lower():
            fail("TaskExecution status mismatch")
        if str(row["task_execution_status"]).lower() != "done":
            fail("captured reconstruction requires DONE TaskExecution")
        if str(row["task_status"]).lower() != "done":
            fail("captured reconstruction requires DONE Task")
        if str(observation.get("task_status", "")).lower() != str(
            row["task_status"]
        ).lower():
            fail("Task status mismatch")
        if observation.get("session_status") != row["session_status"]:
            fail("Session status mismatch")
        if row["execution_session_id"] != row["session_id"]:
            fail("TaskExecution/Session identity mismatch")
        if row["execution_task_id"] != row["task_id"]:
            fail("TaskExecution/Task identity mismatch")
        if row["task_project_id"] != row["project_id"]:
            fail("Task/Project identity mismatch")
        if row["session_project_id"] != row["project_id"]:
            fail("Session/Project identity mismatch")
        if connection.execute(
            "SELECT 1 FROM session_tasks WHERE session_id=? AND task_id=?",
            (row["session_id"], row["task_id"]),
        ).fetchone() is None:
            fail("SessionTask identity link is absent")
        if any(
            row[key] != row[f"change_{key}"]
            for key in ("task_id", "session_id")
        ):
            fail("Change Set Task/Session identity mismatch")
        if row["change_project_id"] != row["project_id"]:
            fail("Change Set Project identity mismatch")
        if row["change_execution_id"] != row["task_execution_id"]:
            fail("Change Set TaskExecution identity mismatch")
        if row["disposition"] != "captured":
            fail(f"unexpected Change Set disposition: {row['disposition']!r}")
        review = json.loads(row["review_decision"] or "null")
        if not isinstance(review, dict) or review.get("outcome") != "hold_for_review":
            fail("Change Set review outcome is not hold_for_review")
        observed_change = observation.get("change_set")
        if not isinstance(observed_change, dict):
            fail("terminal observation has no Change Set")
        for key in ("added_files", "modified_files", "deleted_files"):
            database_paths = json_string_list(json.loads(row[key] or "[]"), f"DB {key}")
            observed_paths = json_string_list(observed_change.get(key), f"observation {key}")
            if set(database_paths) != set(observed_paths):
                fail(f"authoritative {key} differs from observation")
        return (dict(row), observation)
    finally:
        connection.close()


def apply_ops(candidate: Path, operations: list[dict[str, Any]]) -> None:
    for operation in operations:
        name = operation.get("op")
        if name not in {"write_file", "append_file", "replace_in_file"}:
            fail(f"unsupported accepted operation: {name!r}")
        relative = safe_relative(operation.get("path"))
        target = candidate.joinpath(*PurePosixPath(relative).parts)
        cursor = candidate
        for part in PurePosixPath(relative).parts:
            cursor /= part
            if cursor.is_symlink():
                fail(f"candidate operation path contains symlink: {relative}")
        if not target.resolve().is_relative_to(candidate.resolve()):
            fail(f"candidate operation path escapes workspace: {relative}")
        if name == "write_file":
            content = operation.get("content")
            if not isinstance(content, str):
                fail(f"write_file content is invalid: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        elif name == "append_file":
            content = operation.get("content")
            if not isinstance(content, str) or not target.is_file():
                fail(f"append_file target/content is invalid: {relative}")
            with target.open("a", encoding="utf-8") as handle:
                handle.write(content)
        else:
            old, new = operation.get("old"), operation.get("new")
            if not isinstance(old, str) or not isinstance(new, str) or not old:
                fail(f"replace_in_file fields are invalid: {relative}")
            if not target.is_file():
                fail(f"replace_in_file target is missing: {relative}")
            original = target.read_text(encoding="utf-8")
            if original.count(old) != 1:
                fail(f"replace_in_file anchor is not exactly once: {relative}")
            target.write_text(original.replace(old, new, 1), encoding="utf-8")


def reconstruct(args: argparse.Namespace) -> int:
    evidence = Path(args.evidence)
    u1_material = Path(args.u1_material)
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        fail(f"output already exists: {output}")
    output.mkdir(parents=True, mode=0o700)
    repo = Path(args.repo).resolve()
    sys.path.insert(0, str(repo))
    from scripts.maintenance import p43_c2_trusted_submitter as trusted

    responses = {key: response_document(evidence, key) for key in EXPECTED_RESPONSES}
    if not no_op_debug_repair(responses["0012"]):
        fail("response 0012 is not an identical old/new no-op")
    _, observation = load_records(evidence, args.execution_id)
    accepted_raw = observation.get("accepted_plan_sha256")
    connection = sqlite3.connect(f"file:{evidence / 'orchestrator.db'}?mode=ro", uri=True)
    try:
        task = connection.execute("SELECT steps FROM tasks WHERE id=?", (args.task_id,)).fetchone()
    finally:
        connection.close()
    if task is None or not task[0]:
        fail("accepted Task.steps is absent")
    if observation.get("task_id") != args.task_id:
        fail("requested task_id does not match terminal observation")
    steps_text = task[0]
    if sha256_bytes(steps_text.encode()) != accepted_raw:
        fail("accepted Plan hash mismatch")
    accepted = json.loads(steps_text)
    response_plan = find_plan(responses["0011"])
    if response_plan is None:
        fail("accepted Plan was not found in response 0011")
    accepted_ops = mutation_ops(accepted)
    if canonical(mutation_ops(response_plan)) != canonical(accepted_ops):
        fail("Plan normalization changed mutation content")

    trusted.verify_u1_material(u1_material, trusted.EVALUATION_PARTS)
    staged = output / "u1-eval"
    trusted.stage_material(u1_material, staged, trusted.EVALUATION_PARTS)
    candidate = output / "candidate-workspace"
    copy_tree(staged / trusted.U1_DIR / "baseline", candidate)
    apply_ops(candidate, accepted_ops)

    row, _ = load_records(evidence, args.execution_id)
    added = json_string_list(json.loads(row["added_files"] or "[]"), "DB added_files")
    modified = json_string_list(json.loads(row["modified_files"] or "[]"), "DB modified_files")
    deleted = json_string_list(json.loads(row["deleted_files"] or "[]"), "DB deleted_files")
    operation_paths = {safe_relative(operation.get("path")) for operation in accepted_ops}
    if operation_paths != set(added) | set(modified) or deleted:
        fail("accepted operation paths do not match authoritative Change Set scope")

    change_set = output / "reconstructed-change-set"
    (change_set / "files").mkdir(parents=True)
    for relative in sorted(operation_paths):
        source = candidate.joinpath(*PurePosixPath(relative).parts)
        if not source.is_file() or source.is_symlink():
            fail(f"reconstructed changed file is missing: {relative}")
        target = change_set / "files" / Path(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    write_json(
        change_set / "manifest.json",
        {
            "schema": "openclaw.task_execution_change_set_artifact.v1",
            "reconstructed": True,
            "task_execution_id": row["task_execution_id"],
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "copied_files": sorted(operation_paths),
            "deleted_files": deleted,
        },
    )
    provenance = {
        "classification": "P43_PRODUCT_23C_RECONSTRUCTION_ONLY",
        "original_manifest_recovered": False,
        "response_sha256": EXPECTED_RESPONSES,
        "accepted_plan_sha256": accepted_raw,
        "mutation_content_equal_after_normalization": True,
        "applied_operations": [
            {"op": operation.get("op"), "path": operation.get("path")}
            for operation in accepted_ops
        ],
        "applied_operations_sha256": sha256_bytes(
            canonical(accepted_ops).encode()
        ),
        "debug_repair_no_op": True,
        "replayed_response_0010": False,
        "task_execution_id": row["task_execution_id"],
        "task_id": row["task_id"],
        "session_id": row["session_id"],
        "attempt_number": row["attempt_number"],
        "dispatch_lease_fence": row["dispatch_lease_fence"],
        "project_id": row["project_id"],
        "candidate_workspace_sha256": tree_hash(candidate),
        "change_set_delta_sha256": tree_hash(change_set),
    }
    write_json(output / "reconstruction.json", provenance)
    make_private(output)
    print(f"RECONSTRUCTION_ROOT={output}")
    print("RECONSTRUCTION_CLASS=P43_PRODUCT_23C_RECONSTRUCTION_ONLY")
    return 0


def run_process(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(command, 124, exc.stdout or "", exc.stderr or "TIMEOUT")


def classify_test(result: subprocess.CompletedProcess[str]) -> str:
    text = f"{result.stdout}\n{result.stderr}"
    if result.returncode == 124 or "TIMEOUT" in result.stderr:
        return "TIMEOUT"
    if result.returncode == 0:
        return "PASS"
    if result.returncode in {125, 126, 127} or "Unable to find image" in text:
        return "INFRASTRUCTURE_FAILURE"
    if "ERROR collecting" in text or "collected 0 items" in text or "ImportError" in text:
        return "COLLECTION_OR_IMPORT_FAILURE"
    if "AssertionError" in text and "failed" in text:
        return "ASSERTION_FAILURE"
    return "TEST_FAILURE_UNCLASSIFIED"


def docker_command(
    image: str,
    mounts: list[tuple[Path, str]],
    args: list[str],
    workdir: str | None = None,
) -> list[str]:
    command = [
        "docker", "run", "--rm", "--pull=never", "--network=none", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
        "--user=65534:65534", "--memory=1g", "--pids-limit=128",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m", "-e", "HOME=/tmp",
        "-e", "TMPDIR=/tmp",
    ]
    for source, target in mounts:
        command.extend(["-v", f"{source}:{target}:ro"])
    if workdir:
        command.extend(["-w", workdir])
    command.extend(["--entrypoint", "python3", image, "-I"])
    command.extend(args)
    return command


def validate_image(evidence: Path) -> str:
    run_vars = evidence / "run.vars"
    values = {}
    for line in run_vars.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if values.get("PYTEST_IMAGE") != APPROVED_PYTEST_IMAGE:
        fail("retained pytest image pin is not the approved PRODUCT-23B image")
    result = run_process(
        [
            "docker", "image", "inspect", APPROVED_PYTEST_IMAGE,
            "--format", "{{.Id}} {{.Os}}/{{.Architecture}} {{.Config.User}}",
        ],
        30,
    )
    if result.returncode != 0 or result.stdout.strip() != f"{APPROVED_PYTEST_IMAGE} linux/arm64 65534:65534":
        fail(f"pytest image is unavailable or mismatched: {result.stdout.strip()} {result.stderr.strip()}")
    return APPROVED_PYTEST_IMAGE


def stage_for_container(output: Path) -> dict[str, Path]:
    mount = output / "eval-mount"
    if mount.exists() or mount.is_symlink():
        fail(f"container staging already exists: {mount}")
    mount.mkdir(mode=0o700)
    paths = {
        "material": mount / "u1-eval",
        "change_set": mount / "change-set",
        "candidate": mount / "candidate-workspace",
        "baseline_regression": mount / "baseline-regression",
    }
    copy_tree(output / "u1-eval", paths["material"])
    copy_tree(output / "reconstructed-change-set", paths["change_set"])
    copy_tree(output / "candidate-workspace", paths["candidate"])
    baseline = output / "u1-eval" / "p43-product-18" / "u1" / "baseline"
    copy_tree(baseline, paths["baseline_regression"])
    generated = [
        path for path in (output / "reconstructed-change-set" / "files" / "tests").glob("test_*.py")
        if path.is_file()
    ]
    if len(generated) != 1:
        fail(f"expected one generated regression test, found {len(generated)}")
    generated_target = paths["baseline_regression"] / "tests" / generated[0].name
    generated_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(generated[0], generated_target)
    make_container_readonly(mount)
    return paths


def evaluate(args: argparse.Namespace) -> int:
    if not args.operator_approved:
        fail("Docker evaluation requires --operator-approved")
    output = Path(args.output)
    evidence = Path(args.evidence)
    provenance = read_json(output / "reconstruction.json")
    if provenance.get("classification") != "P43_PRODUCT_23C_RECONSTRUCTION_ONLY":
        fail("output is not a reconstruction-only candidate")
    candidate = output / "candidate-workspace"
    change_set = output / "reconstructed-change-set"
    before_candidate = tree_hash(candidate)
    before_change_set = tree_hash(change_set)
    image = validate_image(evidence)
    paths = stage_for_container(output)
    oracle = run_process(
        docker_command(
            image,
            [(paths["material"], "/eval"), (paths["change_set"], "/cs")],
            [
                "/eval/p43-product-02/oracle.py", "/eval/p43-product-18/u1/baseline",
                "/cs", "tests/test_rangekit_acceptance.py", "tests/test_ranges.py",
            ],
        ),
        300,
    )
    (output / "oracle.json").write_text(oracle.stdout, encoding="utf-8")
    os.chmod(output / "oracle.json", 0o600)
    if not oracle.stdout.strip().startswith("{"):
        fail(f"oracle did not return JSON: {classify_test(oracle)}")
    hidden = run_process(
        docker_command(
            image,
            [(paths["material"], "/eval"), (paths["change_set"], "/cs")],
            [
                "/eval/p43-product-18/tools/hidden_suite.py",
                "/eval/p43-product-18/u1/baseline", "/cs", "/eval/p43-product-18/u1/hidden",
            ],
        ),
        300,
    )
    (output / "hidden.json").write_text(hidden.stdout, encoding="utf-8")
    os.chmod(output / "hidden.json", 0o600)
    if not hidden.stdout.strip().startswith("{"):
        fail(f"hidden suite did not return JSON: {classify_test(hidden)}")

    test_path = "tests/" + next(
        path.name for path in (change_set / "files" / "tests").glob("test_*.py")
    )
    baseline = run_process(
        docker_command(
            image,
            [(paths["baseline_regression"], "/workspace")],
            ["-m", "pytest", test_path, "-q"],
            workdir="/workspace",
        ),
        300,
    )
    candidate_result = run_process(
        docker_command(
            image,
            [(paths["candidate"], "/workspace")],
            ["-m", "pytest", test_path, "-q"],
            workdir="/workspace",
        ),
        300,
    )
    baseline_class = classify_test(baseline)
    candidate_class = classify_test(candidate_result)
    write_json(
        output / "regression-results.json",
        {
            "baseline": {"classification": baseline_class, "returncode": baseline.returncode, "stdout": baseline.stdout, "stderr": baseline.stderr},
            "candidate": {"classification": candidate_class, "returncode": candidate_result.returncode, "stdout": candidate_result.stdout, "stderr": candidate_result.stderr},
        },
    )
    if baseline_class != "ASSERTION_FAILURE":
        fail(f"baseline regression was not an assertion failure: {baseline_class}")
    if candidate_class != "PASS":
        fail(f"candidate regression did not pass: {candidate_class}")

    repo = Path(args.repo).resolve()
    observed = evidence / "observe.model.after-stop.json"
    verdict = run_process(
        [
            sys.executable, "-I", str(repo / "scripts/maintenance/p43_c2_trusted_submitter.py"),
            "verdict", str(output / "u1-eval"), str(change_set), "--control", "model",
            "--oracle", str(output / "oracle.json"), "--hidden", str(output / "hidden.json"),
            "--observed", str(observed),
        ],
        60,
    )
    (output / "model-verdict.json").write_text(verdict.stdout, encoding="utf-8")
    os.chmod(output / "model-verdict.json", 0o600)
    if verdict.returncode != 0:
        fail(f"existing evaluator CLI failed: {verdict.stderr.strip()}")

    relay = output / "relay" / "bodies"
    relay.mkdir(parents=True)
    for source in sorted((evidence / "relay-bodies").glob("*.json")):
        if source.is_symlink() or not source.is_file():
            fail(f"invalid retained relay body: {source}")
        shutil.copyfile(source, relay / source.name)
    contamination = run_process(
        [
            sys.executable, "-I", str(repo / "scripts/maintenance/p43_c2_trusted_submitter.py"),
            "contamination", str(output / "u1-eval"), str(output / "relay"),
        ],
        60,
    )
    (output / "contamination.json").write_text(contamination.stdout, encoding="utf-8")
    os.chmod(output / "contamination.json", 0o600)
    if contamination.returncode != 0:
        fail(f"existing contamination CLI failed: {contamination.stderr.strip()}")
    summary = read_json(evidence / "relay-summary.json")
    if tuple(summary.get(key) for key in ("completions_admitted", "completions_forwarded", "completions_completed")) != (4, 4, 4):
        fail("retained provider-call budget is not 4/4/4")
    if tree_hash(candidate) != before_candidate or tree_hash(change_set) != before_change_set:
        fail("reconstructed candidate changed during evaluation")
    print(json.dumps({
        "classification": "P43_PRODUCT_23C_RECONSTRUCTION_ONLY",
        "candidate_immutable": True,
        "baseline_regression": baseline_class,
        "candidate_regression": candidate_class,
        "oracle_cli": "completed",
        "hidden_cli": "completed",
        "provider_budget": [4, 4, 4],
        "candidate_workspace_sha256_before": before_candidate,
        "candidate_workspace_sha256_after": tree_hash(candidate),
        "change_set_sha256_before": before_change_set,
        "change_set_sha256_after": tree_hash(change_set),
        "verdict": str(output / "model-verdict.json"),
    }, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("reconstruct", "evaluate"):
        command = sub.add_parser(name)
        command.add_argument("--evidence", required=True)
        command.add_argument("--output", required=True)
        command.add_argument("--repo", required=True)
        command.add_argument("--u1-material", required=name == "reconstruct")
        command.add_argument("--execution-id", type=int, default=1)
        command.add_argument("--task-id", type=int, default=1)
        if name == "evaluate":
            command.add_argument("--operator-approved", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return reconstruct(args) if args.command == "reconstruct" else evaluate(args)
    except ProcedureError as exc:
        print(f"P43-C2-U1 procedure refused: {exc}", file=sys.stderr)
        return 78
    except Exception as exc:
        print(f"P43-C2-U1 procedure failed closed: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
