"""P43-PRODUCT-23A: contained coding capability (offline).

Evidence classes, kept separate (see the PRODUCT-23A report):

* contract/routing: wire contract, digest binding, admission, refusals;
* fake-Docker integration: the real strict ``HostSocketLauncher`` with an
  executor double that never runs code;
* actual local subprocess: an executor double that stages the snapshot with
  the real ``StagedWorkspace`` and runs the real ``PYTEST_BOOTSTRAP`` with a
  local interpreter. This is real pytest execution, **not** containment.

No real Docker, Host, provider, or broker is used. The U1 reference evaluation
reads the frozen PRODUCT-18A evidence (never vendored here, so the reference
solution and hidden suite stay out of the repository) and skips when absent.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from app.config import settings
from app.models import (
    Task,
    TaskCheckpoint,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
)
from app.services.orchestration.execution import sandbox_gateway
from app.services.orchestration.execution.sandbox_gateway import (
    SandboxLocalExecutionRefused,
    SandboxSnapshotRejected,
    build_workspace_snapshot,
    c2_structured_file_ops_authority_check,
    sandbox_compileall_paths,
    sandbox_pytest_arguments,
)
from app.services.sandbox_bridge import docker_executor
from app.services.sandbox_bridge.authorization import (
    input_sha256,
    operation_sha256,
    verify_authorization,
)
from app.services.sandbox_bridge.contract import (
    MAX_SNAPSHOT_FILE_BYTES,
    MAX_SNAPSHOT_FILES,
    PROFILE,
    PYTEST_PROFILE,
    ExecutionRequest,
    ExecutionResult,
    RequestRejected,
    decode_tool_invocation,
    decode_workspace_snapshot,
    encode_tool_invocation,
)
from app.services.sandbox_bridge.staging import StagedWorkspace
from app.tests import test_p43_product22_c2_c2b_sandbox_slice as c2b
from app.tests import test_p43_product22_c2_c3b_live_execution as c3b
from app.tasks import worker

host, lifecycle = c2b.host, c2b.lifecycle
c2_env, no_network = c3b.c2_env, c3b.no_network
no_local_step_execution = c3b.no_local_step_execution

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "docs" / "roadmap" / "reports" / "evidence"
U1 = EVIDENCE / "p43-product-18" / "u1"
ORACLE = EVIDENCE / "p43-product-02" / "oracle.py"
HIDDEN_SUITE = EVIDENCE / "p43-product-18" / "tools" / "hidden_suite.py"
U1_TASK_SHA256 = "c1a574b32940a32da9d2568de14e1474379008c8b53762d444c74cc76eab9e7a"
ORACLE_SHA256_PREFIX = "b28ea4b2"
# Captured before the no-local-execution guard patches subprocess.
_REAL_POPEN = subprocess.Popen


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LocalPytestExecutor:
    """Host-side double: real staging + real bootstrap, local interpreter.

    Runs on the Launcher side of the authenticated socket, after the strict
    Launcher verified authorization. It is uncontained by design and is used
    only with trusted code (U1 reference control, synthetic fixtures).
    """

    def __init__(self, staging_root: Path):
        self.staging_root = staging_root
        self.calls = []

    def __call__(self, request, _cancel):
        try:
            return self._run(request)
        except BaseException:
            import traceback

            traceback.print_exc()
            raise

    def _run(self, request):
        self.calls.append(request)
        assert request.execution_profile == PYTEST_PROFILE and request.version == 3
        files, _digest = decode_workspace_snapshot(request.workspace_snapshot)
        staged = StagedWorkspace(self.staging_root, request.code, files)
        try:
            home = staged.path.parent / f"{staged.path.name}-tmp"
            home.mkdir()
            with _REAL_POPEN(
                [
                    sys.executable,
                    "-B",
                    "-E",
                    "-s",
                    "-c",
                    docker_executor.PYTEST_BOOTSTRAP,
                ],
                cwd=staged.source_path,
                env={"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(home)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as process:
                stdout, stderr = process.communicate(timeout=request.timeout_seconds)
            completed = subprocess.CompletedProcess(
                process.args, process.returncode, stdout, stderr
            )
            shutil.rmtree(home)
        finally:
            staged.cleanup()
        return ExecutionResult(
            request.request_id,
            "SUCCESS" if completed.returncode == 0 else "EXECUTION_FAILED",
            completed.returncode,
            completed.stdout[-60000:],
            completed.stderr[-4000:],
            duration=0.1,
        )


# --- U1 reference control through the real Worker body ----------------------


def _require_u1_evidence() -> None:
    if not (U1 / "manifest.json").is_file() or not ORACLE.is_file():
        pytest.skip("frozen PRODUCT-18A U1 evidence is not present (docs/ is local)")
    assert _sha(U1 / "task.txt") == U1_TASK_SHA256
    assert _sha(ORACLE).startswith(ORACLE_SHA256_PREFIX)
    manifest = json.loads((U1 / "manifest.json").read_text())
    assert (
        _sha(U1 / "baseline" / manifest["acceptance"]) == manifest["acceptance_sha256"]
    )


def _reference_plan() -> list[dict]:
    """The trusted reference control, expressed as existing structured ops."""

    reference = U1 / "controls" / "reference" / "files"
    baseline = (U1 / "baseline" / "rangekit" / "ranges.py").read_text()
    fixed = (reference / "rangekit" / "ranges.py").read_text()
    old = (
        "    for start, end in _checked(ranges):\n"
        "        if merged and start < merged[-1][1]:\n"
    )
    new = (
        "    for start, end in sorted(_checked(ranges)):\n"
        "        if merged and start <= merged[-1][1]:\n"
    )
    assert baseline.replace(old, new) == fixed
    test_path = "tests/test_merge_ranges_regression.py"
    return [
        {
            "step_number": 1,
            "description": "Merge touching ranges and accept unsorted input.",
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "rangekit/ranges.py",
                    "old": old,
                    "new": new,
                }
            ],
            "commands": ["python -m pytest tests/test_ranges.py -q"],
            "verification": "python -m pytest tests/test_ranges.py -q",
            "rollback": None,
            "expected_files": ["rangekit/ranges.py"],
        },
        {
            "step_number": 2,
            "description": "Add focused regression tests.",
            "ops": [
                {
                    "op": "write_file",
                    "path": test_path,
                    "content": (reference / test_path).read_text(),
                }
            ],
            "commands": [f"python -m pytest {test_path} -q"],
            "verification": f"python -m pytest {test_path} -q",
            "rollback": None,
            "expected_files": [test_path],
        },
        {
            "step_number": 3,
            "description": "Run the independent acceptance contract.",
            "commands": ["python -m pytest tests/test_rangekit_acceptance.py -q"],
            "verification": "python -m pytest tests/test_rangekit_acceptance.py -q",
            "rollback": None,
            "expected_files": [],
        },
    ]


def _submit_u1(db_factory, plan: list[dict] | None, execution_profile: str) -> dict:
    from app.api.v1.endpoints.projects import create_project
    from app.api.v1.endpoints.sessions import create_session
    from app.api.v1.endpoints.tasks import create_task
    from app.models import Project, User
    from app.schemas import ProjectCreate, SessionCreate, TaskCreate
    from app.services.session.session_runtime_service import queue_task_for_session
    from app.services.workspace.project_isolation_service import (
        resolve_project_workspace_path,
    )

    db = db_factory()
    try:
        user = User(email="u1@disposable.invalid", hashed_password="!", is_active=True)
        db.add(user)
        db.commit()
        db.refresh(user)
        project = create_project(
            ProjectCreate(name="rangekit-u1", workspace_path="rangekit-u1"),
            db=db,
            current_user=user,
        )
        workspace = resolve_project_workspace_path(
            db.get(Project, project.id).workspace_path, project.name, db=db
        )
        shutil.copytree(U1 / "baseline", workspace, dirs_exist_ok=True)
        task = create_task(
            TaskCreate(
                project_id=project.id,
                title="rangekit merge_ranges defect",
                description=(U1 / "task.txt").read_text(),
                execution_profile=execution_profile,
                steps=json.dumps(plan) if plan is not None else None,
            ),
            db=db,
            current_user=user,
        )
        session = create_session(
            SessionCreate(
                project_id=project.id, name="U1 offline", execution_mode="manual"
            ),
            db=db,
            current_user=user,
        )
        queued = queue_task_for_session(db, session, task.id, timeout_seconds=300)
        return {
            "task_id": task.id,
            "session_id": session.id,
            "workspace": Path(workspace),
            "task_execution_id": queued.get("task_execution_id"),
        }
    finally:
        db.close()


def test_u1_reference_control_completes_through_contained_pytest(
    c2_env,
    host,
    no_network,
    no_local_step_execution,
    monkeypatch,
    db_session_factory,
    tmp_path,
):
    _require_u1_evidence()
    monkeypatch.setattr(settings, "EXECUTION_CONTEXT_TOKENS", 65536)
    staging = tmp_path / "launcher-staging"
    staging.mkdir(mode=0o700)
    executor = LocalPytestExecutor(staging)
    deliveries = c3b._eager_delivery(
        monkeypatch, db_session_factory, lambda: host(executor)
    )
    monkeypatch.setattr(
        worker,
        "_execute_planning_phase",
        lambda *a, **k: pytest.fail("Planning must not run for the stored plan"),
    )

    submitted = _submit_u1(db_session_factory, _reference_plan(), "full_lifecycle")

    assert len(deliveries) == 1
    result = deliveries[0]["result"]
    db = db_session_factory()
    try:
        execution = db.get(TaskExecution, submitted["task_execution_id"])
        task = db.get(Task, submitted["task_id"])
        assert execution.status is TaskStatus.DONE, (task.error_message, result)
        assert task.status is TaskStatus.DONE
        assert db.query(TaskExecution).count() == 1  # no retry, no second attempt
        verdicts = [
            json.loads(row.state_snapshot)["status"]
            for row in db.query(TaskCheckpoint).all()
            if row.checkpoint_type == "validation_plan"
        ]
        assert verdicts == ["accepted"]
        change_set = (
            db.query(TaskExecutionChangeSet)
            .filter_by(task_execution_id=execution.id)
            .one()
        )
        # The captured artifact (P10 precedent: change-sets/<te> in control
        # state), not the disposed Runtime Workspace snapshot.
        change_set_dir = (
            Path(c2_env["RUNTIME_ROOT"])
            / "control"
            / "projects"
            / str(change_set.project_id)
            / "change-sets"
            / str(execution.id)
        )
        held = (change_set.disposition, change_set.review_decision)
    finally:
        db.close()
    assert no_network == []

    # Every executable operation was a v3 contained request for this attempt;
    # none ran locally (``no_local_step_execution`` would have failed).
    tools = [json.loads(call.code)["tool"] for call in executor.calls]
    assert set(tools) == {"pytest", "compileall"}
    for call in executor.calls:
        assert call.version == 3 and call.execution_profile == PYTEST_PROFILE
        assert call.authorization["task_execution_id"] == execution.id
        assert call.authorization["attempt_number"] == 1
        names = [entry["path"] for entry in call.workspace_snapshot]
        assert "tests/test_rangekit_acceptance.py" in names
        assert not any(name.startswith(("hidden", "controls")) for name in names)

    # Held, never promoted: the Project workspace is still the frozen baseline.
    assert held[0] == "captured" and held[1]["outcome"] == "hold_for_review", held
    manifest = json.loads((U1 / "manifest.json").read_text())
    for rel, digest in manifest["baseline_files"].items():
        assert _sha(submitted["workspace"] / rel) == digest, rel

    # Independent verdicts on the captured Change Set (trusted reference code).
    oracle = _run_json(
        ORACLE,
        U1 / "baseline",
        change_set_dir,
        manifest["acceptance"],
        ",".join(manifest["existing_tests"]),
    )
    hidden = _run_json(HIDDEN_SUITE, U1 / "baseline", change_set_dir, U1 / "hidden")
    assert oracle["acceptance_protected"] is True
    assert oracle["functional"]["passed"] == oracle["functional"]["total"] == 6
    assert oracle["deliverable"] == "COMPLETE"
    assert oracle["complete_delivery"] is True, oracle
    assert hidden["hidden_pass"] is True, hidden


def _run_json(script: Path, *args) -> dict:
    with _REAL_POPEN(
        [sys.executable, "-I", str(script), *map(str, args)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        stdout, stderr = process.communicate(timeout=600)
    try:
        return json.loads(stdout)
    except ValueError:
        pytest.fail(
            f"{script.name} produced no verdict: {stdout[-800:]} {stderr[-800:]}"
        )


def _install_u1_provider(monkeypatch, plan):
    """P41-PRODUCT-01 provider-shaped double: U1 Discovery, then ``plan``."""

    from app.tests.test_p41_product_01_deterministic_e2e import (
        _DeterministicProviderAdapter,
    )

    class U1Provider(_DeterministicProviderAdapter):
        discovery = [
            {"action": "read_file", "path": "rangekit/ranges.py"},
            {"action": "stop"},
        ]

        async def execute_task(self, prompt, timeout_seconds=300, **kwargs):
            if "READ-ONLY DISCOVERY ONLY" in prompt:
                step = self.discovery[min(len(self.calls), 1)]
                self.calls.append({"kind": "discovery", "prompt": prompt})
                return {"status": "completed", "output": json.dumps(step)}
            return await super().execute_task(prompt, timeout_seconds, **kwargs)

    provider = U1Provider(plan)
    monkeypatch.setattr(worker, "create_agent_runtime", lambda *a, **k: provider)
    return provider


def test_u1_discovery_and_planning_reach_contained_execution_without_provider(
    c2_env,
    host,
    no_network,
    no_local_step_execution,
    monkeypatch,
    db_session_factory,
    tmp_path,
):
    """Discovery → Planning → Validator run in C2 mode with a canned provider.

    The P41-PRODUCT-01 provider-shaped double replaces the role runtime (no
    network). It returns the reference-control Plan, so this resolves which
    routes Discovery/Planning reach in C2 mode; it is not model capability.
    """

    from app.tests.test_p41_product_01_deterministic_e2e import (
        _DeterministicProviderAdapter,
    )

    _require_u1_evidence()
    monkeypatch.setattr(settings, "EXECUTION_CONTEXT_TOKENS", 65536)
    staging = tmp_path / "launcher-staging"
    staging.mkdir(mode=0o700)
    executor = LocalPytestExecutor(staging)
    deliveries = c3b._eager_delivery(
        monkeypatch, db_session_factory, lambda: host(executor)
    )

    provider = _install_u1_provider(monkeypatch, _reference_plan())

    submitted = _submit_u1(
        db_session_factory, plan=None, execution_profile="full_lifecycle"
    )

    result = deliveries[0]["result"]
    db = db_session_factory()
    try:
        execution = db.get(TaskExecution, submitted["task_execution_id"])
        task = db.get(Task, submitted["task_id"])
        assert execution.status is TaskStatus.DONE, (task.error_message, result)
        assert db.query(TaskExecution).count() == 1
        held = (
            db.query(TaskExecutionChangeSet)
            .filter_by(task_execution_id=execution.id)
            .one()
            .review_decision["outcome"]
        )
    finally:
        db.close()
    assert held == "hold_for_review"
    kinds = [call["kind"] for call in provider.calls]
    assert kinds[0] == "discovery" and "planner_or_execution_reasoning" in kinds
    # No provider endpoint or external host was reached. Planning knowledge
    # retrieval tries the default loopback Qdrant (:6333) and falls back to
    # SQLite; in the Worker container nothing listens there.
    assert no_network and all(
        kind == "connect" and address[0] in ("127.0.0.1", "::1") and address[1] == 6333
        for kind, address in no_network
    ), no_network
    tools = {json.loads(call.code)["tool"] for call in executor.calls}
    assert tools == {"pytest", "compileall"}


# --- Contract: bounded snapshot wire format (contract/routing) --------------


def _entry(path: str, data: bytes = b"x = 1\n", **overrides) -> dict:
    entry = {
        "path": path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content": base64.b64encode(data).decode("ascii"),
    }
    entry.update(overrides)
    return entry


def test_snapshot_digest_is_canonical_over_path_size_and_content():
    entries = [_entry("pkg/a.py"), _entry("tests/test_a.py", b"def test(): pass\n")]
    files, digest = decode_workspace_snapshot(entries)
    assert [str(path) for path, _ in files] == ["pkg/a.py", "tests/test_a.py"]
    changed = [entries[0], _entry("tests/test_a.py", b"def test(): 1\n")]
    assert decode_workspace_snapshot(changed)[1] != digest


@pytest.mark.parametrize(
    "entries, reason",
    [
        ([], "invalid_snapshot_size"),
        ([_entry("../escape.py")], "invalid_snapshot_path"),
        ([_entry("/abs.py")], "invalid_snapshot_path"),
        ([_entry("a/./b.py")], "invalid_snapshot_path"),
        ([_entry("a\\b.py")], "invalid_snapshot_path"),
        ([_entry("a\nb.py")], "invalid_snapshot_path"),
        ([_entry("b.py"), _entry("a.py")], "snapshot_paths_not_canonical"),
        ([_entry("a.py"), _entry("a.py")], "snapshot_paths_not_canonical"),
        ([_entry("pkg"), _entry("pkg/a.py")], "snapshot_path_conflict"),
        ([_entry("a.py", size=99)], "snapshot_size_mismatch"),
        ([_entry("a.py", sha256="0" * 64)], "snapshot_digest_mismatch"),
        ([_entry("a.py", content="!!not-base64!!")], "invalid_snapshot_content"),
        ([dict(_entry("a.py"), mode=0o755)], "invalid_snapshot_entry"),
        (
            [_entry("big.py", b"x" * (MAX_SNAPSHOT_FILE_BYTES + 1))],
            "snapshot_file_too_large",
        ),
        (
            [_entry(f"f{i:04d}.py") for i in range(MAX_SNAPSHOT_FILES + 1)],
            "invalid_snapshot_size",
        ),
        (
            [_entry(f"f{i}.py", b"x" * MAX_SNAPSHOT_FILE_BYTES) for i in range(5)],
            "snapshot_too_large",
        ),
    ],
)
def test_malformed_snapshots_are_rejected(entries, reason):
    with pytest.raises(RequestRejected) as rejected:
        decode_workspace_snapshot(entries)
    assert str(rejected.value) == reason


@pytest.mark.parametrize(
    "code",
    [
        '["tests"]',
        '{"tool":"bash","args":["x"]}',
        '{"args":[],"tool":"pytest"}',
        '{"args": ["x"], "tool": "pytest"}',  # not canonical
        '{"args":["x"],"extra":1,"tool":"pytest"}',
    ],
)
def test_tool_invocation_is_closed_and_canonical(code):
    with pytest.raises(RequestRejected):
        decode_tool_invocation(code)


def _v3_request(entries=None, *, profile=PYTEST_PROFILE, version=3, code=None):
    import time
    import uuid

    entries = [_entry("tests/test_a.py")] if entries is None else entries
    code = code or encode_tool_invocation("pytest", ["tests/test_a.py", "-q"])
    _files, digest = decode_workspace_snapshot(entries)
    now = int(time.time())
    request_id = str(uuid.uuid4())
    auth = {
        "version": 1,
        "task_execution_id": 7,
        "session_id": 3,
        "session_instance_id": "inst",
        "task_id": 5,
        "attempt_number": 1,
        "worker_identity": "host:1:start",
        "dispatch_fence": 1,
        "dispatch_celery_task_id": "celery",
        "operation_sha256": operation_sha256(
            execution_profile=profile,
            code=code,
            timeout_seconds=10,
            snapshot_sha256=digest,
        ),
        "input_sha256": input_sha256(code, digest),
        "nonce": request_id,
        "issued_at": now,
        "expires_at": now + 20,
        "launcher_epoch": 9,
    }
    return ExecutionRequest(
        request_id,
        "5",
        "te-7-a1",
        str(uuid.uuid4()),
        profile,
        code,
        10,
        now,
        "a" * 64,
        version=version,
        authorization=auth,
        workspace_snapshot=entries,
    )


def test_v3_binding_covers_snapshot_and_v2_shape_is_unchanged():
    request = _v3_request()
    verify_authorization(request, launcher_epoch=9, require_epoch=True)
    wire = request.to_wire()
    assert ExecutionRequest.from_wire(wire) == request

    # Re-authenticated wire with different snapshot bytes: digest catches it.
    tampered = dict(wire, workspace_snapshot=[_entry("tests/test_a.py", b"ok = 0\n")])
    with pytest.raises(RequestRejected, match="operation_digest_mismatch"):
        verify_authorization(
            ExecutionRequest.from_wire(tampered), launcher_epoch=9, require_epoch=True
        )
    # The pytest profile exists only as v3 with a snapshot, and vice versa.
    for kwargs in (
        {"version": 2},
        {"profile": PROFILE},
    ):
        with pytest.raises(RequestRejected):
            _v3_request(**kwargs).validate()
    v2 = ExecutionRequest(
        request.request_id,
        "5",
        "te-7-a1",
        request.workspace_ref,
        PROFILE,
        "import sys; sys.exit(0)",
        10,
        request.issued_at,
        "a" * 64,
        version=2,
        authorization=request.authorization,
    )
    assert "workspace_snapshot" not in v2.to_wire()
    with pytest.raises(RequestRejected, match="invalid_snapshot_presence"):
        ExecutionRequest(
            *list(v2.__dict__.values())[:-1], workspace_snapshot=[_entry("a.py")]
        ).validate()


# --- Worker-side snapshot builder (contract/routing, real filesystem) --------


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "runtime" / "tasks" / "1" / "1"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "tests").mkdir()
    (root / "tests" / "test_pkg.py").write_text("def test_ok():\n    assert True\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / "pkg" / "__pycache__").mkdir()
    (root / "pkg" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    return root.resolve()


def test_snapshot_builder_reads_regular_files_and_skips_only_caches(tmp_path):
    root = _workspace(tmp_path)
    entries, digest = build_workspace_snapshot(root)
    assert [entry["path"] for entry in entries] == [
        "pkg/__init__.py",
        "tests/test_pkg.py",
    ]
    assert decode_workspace_snapshot(entries)[1] == digest


@pytest.mark.parametrize(
    "make, reason",
    [
        (
            lambda r: (r / "link.py").symlink_to(r / "pkg" / "__init__.py"),
            "snapshot_symlink_rejected",
        ),
        (lambda r: (r / "outside").symlink_to("/etc"), "snapshot_symlink_rejected"),
        (lambda r: os.mkfifo(r / "pipe"), "snapshot_special_file_rejected"),
        (
            lambda r: os.link(r / "pkg" / "__init__.py", r / "hard.py"),
            "snapshot_special_file_rejected",
        ),
        (lambda r: (r / ".env").write_text("TOKEN=x"), "snapshot_credential_material"),
        (lambda r: (r / "deploy.pem").write_text("k"), "snapshot_credential_material"),
        (
            lambda r: (r / "big.bin").write_bytes(b"x" * (MAX_SNAPSHOT_FILE_BYTES + 1)),
            "snapshot_file_too_large",
        ),
    ],
)
def test_snapshot_builder_fails_closed(tmp_path, make, reason):
    root = _workspace(tmp_path)
    make(root)
    with pytest.raises(SandboxSnapshotRejected) as rejected:
        build_workspace_snapshot(root)
    assert rejected.value.reason == reason


def test_snapshot_builder_rejects_a_symlinked_or_relative_root(tmp_path):
    root = _workspace(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(root)
    for candidate in (alias, Path("relative")):
        with pytest.raises(SandboxSnapshotRejected):
            build_workspace_snapshot(candidate)


# --- Command admission reuses the existing pytest grammar -------------------


@pytest.mark.parametrize(
    "command, expected",
    [
        ("python -m pytest tests/test_pkg.py -q", ["tests/test_pkg.py", "-q"]),
        (
            "python3 -m pytest tests/test_pkg.py::test_ok",
            ["tests/test_pkg.py::test_ok"],
        ),
        ("/usr/bin/python3 -m pytest tests/test_pkg.py", ["tests/test_pkg.py"]),
        ("venv/bin/python -m pytest tests", ["tests"]),
        ("pytest tests/test_pkg.py", ["tests/test_pkg.py"]),
        ("python -m pytest tests/test_pkg.py; rm -rf /", None),
        ("python -m pytest --pdb tests/test_pkg.py", None),
        ("python -m pytest -p evil tests", None),
        ("python -m pytest ../other/tests", None),
        ("python -m pytest /etc", None),
        ("python script.py", None),
        ("bash -c 'pytest'", None),
        ("python -m pytest", None),
    ],
)
def test_sandbox_pytest_admission(tmp_path, command, expected):
    assert sandbox_pytest_arguments(command, _workspace(tmp_path)) == expected


def test_sandbox_compileall_admission(tmp_path):
    root = _workspace(tmp_path)
    assert sandbox_compileall_paths(
        "/usr/bin/python3 -m compileall -q pkg/__init__.py", root
    ) == ["pkg/__init__.py"]
    for command in (
        "python -m compileall pkg/__init__.py",
        "python -m compileall -q /etc/passwd.py",
        "python -m compileall -q ../x.py",
        "python -m compileall -q pkg",
        "python -m compileall -q missing.py",
    ):
        assert sandbox_compileall_paths(command, root) is None, command


# --- Structured file ops in C2 mode (Worker-side, existing executor) --------


def _authority_for(project_dir: Path, *paths: str):
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

    return AcceptedPathAuthority.create(
        accepted_plan_identity=accepted_plan_identity([{"step_number": 1}]),
        workspace_identity=str(project_dir.resolve()),
        maximum_scope_digest="b" * 64,
        grants=tuple(
            PathGrant(
                path=declare(path),
                grant_class=GrantClass.CREATION_AUTHORIZED,
                provenance=GrantProvenance.ACCEPTED_PLAN,
            )
            for path in paths
        ),
    )


@pytest.fixture
def c2_file_ops(lifecycle, tmp_path, monkeypatch):
    _, db = lifecycle
    session, task, link, execution = c2b._claimed_attempt(db)
    monkeypatch.setenv("WORKER_ISOLATION_MODE", "disposable")
    monkeypatch.setenv("WORKER_BRIDGE_MODE", "c2b-sandbox")
    monkeypatch.setenv("RUNTIME_ROOT", str((tmp_path / "runtime").resolve()))
    monkeypatch.setenv("WORKSPACE_ROOT", str((tmp_path / "authoritative").resolve()))
    root = _workspace(tmp_path)
    context = c2b._context(db, session, task, execution)
    return db, session, task, link, execution, root, context


def _apply(root, ops, authority, context, check=lambda: None):
    from app.services.orchestration.execution.executor import ExecutorService

    return ExecutorService.execute_file_ops(
        root,
        ops,
        accepted_path_authority=authority,
        authority_check=check,
        attempt_context=context,
    )


def test_c2_file_ops_apply_only_under_path_and_attempt_authority(c2_file_ops):
    *_, root, context = c2_file_ops
    authority = _authority_for(root, "tests/test_new.py")
    written = {"op": "write_file", "path": "tests/test_new.py", "content": "x = 1\n"}
    result = _apply(root, [written], authority, context)
    assert result["success"], result
    assert (root / "tests" / "test_new.py").read_text() == "x = 1\n"

    for op in (
        {"op": "write_file", "path": "pkg/undeclared.py", "content": "x"},
        {"op": "write_file", "path": "../escape.py", "content": "x"},
        {"op": "write_file", "path": "/tmp/abs.py", "content": "x"},
    ):
        denied = _apply(root, [op], authority, context)
        assert not denied["success"], op
    assert not (root / "pkg" / "undeclared.py").exists()
    assert not (root.parent / "escape.py").exists()

    (root / "tests" / "linked.py").symlink_to(root / "pkg" / "__init__.py")
    linked = _authority_for(root, "tests/linked.py")
    denied = _apply(
        root,
        [{"op": "write_file", "path": "tests/linked.py", "content": "x"}],
        linked,
        context,
    )
    assert not denied["success"]
    assert (root / "pkg" / "__init__.py").read_text() == ""


def test_c2_file_ops_refuse_without_authority_or_outside_worker_workspace(
    c2_file_ops, tmp_path, monkeypatch
):
    *_, root, context = c2_file_ops
    authority = _authority_for(root, "tests/test_new.py")
    op = [{"op": "write_file", "path": "tests/test_new.py", "content": "x"}]
    for kwargs, reason in (
        ({"authority": None}, "path_authority_missing"),
        ({"context": None}, "attempt_authority_missing"),
    ):
        args = {"authority": authority, "context": context, **kwargs}
        with pytest.raises(SandboxLocalExecutionRefused, match=reason):
            _apply(root, op, args["authority"], args["context"])
    with pytest.raises(SandboxLocalExecutionRefused, match="attempt_authority_missing"):
        from app.services.orchestration.execution.executor import ExecutorService

        ExecutorService.execute_file_ops(
            root, op, accepted_path_authority=authority, attempt_context=context
        )
    outside = (tmp_path / "elsewhere").resolve()
    outside.mkdir()
    with pytest.raises(
        SandboxLocalExecutionRefused, match="workspace_not_worker_owned"
    ):
        _apply(outside, op, _authority_for(outside, "tests/test_new.py"), context)
    monkeypatch.setenv("WORKER_DATABASE_DIR", str(root.parent))
    with pytest.raises(
        SandboxLocalExecutionRefused, match="workspace_overlaps_worker_authority"
    ):
        _apply(root, op, authority, context)
    assert not (root / "tests" / "test_new.py").exists()


def test_c2_file_ops_stop_on_stale_or_cancelled_attempt(c2_file_ops):
    db, session, task, link, execution, root, context = c2_file_ops
    authority = _authority_for(root, "tests/a.py", "tests/b.py")
    ops = [
        {"op": "write_file", "path": "tests/a.py", "content": "a"},
        {"op": "write_file", "path": "tests/b.py", "content": "b"},
    ]
    calls = []

    def check():
        calls.append(1)
        if len(calls) == 2:  # a newer attempt appears between the two ops
            db.add(
                TaskExecution(
                    session_id=session.id,
                    task_id=task.id,
                    attempt_number=2,
                    status=TaskStatus.PENDING,
                )
            )
            db.commit()
        return None

    result = _apply(root, ops, authority, context, check)
    assert not result["success"]
    assert result["authority_lost"] == "stale_attempt"
    assert (root / "tests" / "a.py").exists() and not (root / "tests" / "b.py").exists()
    with pytest.raises(SandboxLocalExecutionRefused, match="stale_attempt"):
        _apply(root, ops[1:], authority, context)


def test_file_op_admission_is_inert_outside_c2_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKER_BRIDGE_MODE", raising=False)

    def original() -> None:
        return None

    assert (
        c2_structured_file_ops_authority_check(
            tmp_path,
            accepted_path_authority=None,
            authority_check=original,
            attempt_context=None,
        )
        is original
    )


# --- Fake-Docker integration: real strict Launcher, v3 requests --------------


def _pytest_step(project_dir, context, command):
    from app.services.orchestration.phases.execution_local_steps import (
        _execute_simple_verification_step,
    )

    return _execute_simple_verification_step(
        project_dir=project_dir,
        commands=[command],
        verification_command=command,
        sandbox_context=context,
    )


@pytest.fixture
def claimed(lifecycle, tmp_path):
    _, db = lifecycle
    session, task, link, execution = c2b._claimed_attempt(db)
    return db, c2b._context(db, session, task, execution), _workspace(tmp_path)


def test_pytest_step_reaches_strict_launcher_as_bound_v3_request(
    claimed, host, no_local_step_execution
):
    db, context, root = claimed
    seen = []

    def executor(request, _cancel):
        seen.append(request)
        return ExecutionResult(
            request.request_id, "SUCCESS", 0, "1 passed", "", duration=0.1
        )

    host(executor)
    result = _pytest_step(root, context, "python -m pytest tests/test_pkg.py -q")
    assert result["status"] == "completed", result
    request = seen[0]
    assert request.version == 3 and request.execution_profile == PYTEST_PROFILE
    assert decode_tool_invocation(request.code) == (
        "pytest",
        ["tests/test_pkg.py", "-q"],
    )
    _files, digest = decode_workspace_snapshot(request.workspace_snapshot)
    assert request.authorization["operation_sha256"] == operation_sha256(
        execution_profile=PYTEST_PROFILE,
        code=request.code,
        timeout_seconds=request.timeout_seconds,
        snapshot_sha256=digest,
    )
    assert result["_verification_evidence"][0]["execution_kind"] == "host_sandbox"


@pytest.mark.parametrize(
    "status, exit_code, step_status",
    [
        ("EXECUTION_FAILED", 1, "failed"),  # failing tests
        ("EXECUTION_FAILED", 5, "failed"),  # no tests collected
        ("TIMEOUT", None, "failed"),
        ("SUCCESS", 0, "completed"),
    ],
)
def test_pytest_exit_status_maps_to_step_verification(
    claimed, host, no_local_step_execution, status, exit_code, step_status
):
    db, context, root = claimed

    def executor(request, _cancel):
        return ExecutionResult(
            request.request_id, status, exit_code, "out", "", duration=0.1
        )

    host(executor)
    result = _pytest_step(root, context, "python -m pytest tests -q")
    assert result["status"] == step_status, result
    evidence = result["_verification_evidence"][0]
    assert evidence["process_exit_code"] == exit_code


def test_unsupported_commands_and_unavailable_sandbox_fail_closed(
    claimed, host, no_local_step_execution, tmp_path, monkeypatch
):
    db, context, root = claimed
    seen = []
    host(lambda request, _c: seen.append(request))
    from app.services.orchestration.execution.execution_flow import (
        _execute_verification_in_sandbox,
    )

    for command in (
        "python script.py",
        "python -m pytest tests; echo hi",
        "python -m pytest --pdb tests",
    ):
        result, _ = _execute_verification_in_sandbox(
            command, 10, context, project_dir=root
        )
        assert result["sandbox_rejection"] == "unsupported_operation", command
    result, _ = _execute_verification_in_sandbox(
        "python -m pytest tests", 10, None, project_dir=root
    )
    assert result["sandbox_rejection"] == "trusted_execution_context_missing"
    (root / ".env").write_text("TOKEN=x")
    result, _ = _execute_verification_in_sandbox(
        "python -m pytest tests", 10, context, project_dir=root
    )
    assert result["sandbox_rejection"] == "snapshot_credential_material"
    (root / ".env").unlink()
    monkeypatch.setenv("SANDBOX_BRIDGE_SOCKET", str(tmp_path / "absent.sock"))
    result, _ = _execute_verification_in_sandbox(
        "python -m pytest tests", 10, context, project_dir=root
    )
    assert result["sandbox_rejection"] == "host_unavailable"
    assert seen == []


def test_candidate_validation_routes_pytest_and_compileall_only(
    claimed, host, no_local_step_execution
):
    from app.services.orchestration.validation.candidate_checks import _run_command

    db, context, root = claimed
    seen = []

    def executor(request, _cancel):
        seen.append(decode_tool_invocation(request.code))
        return ExecutionResult(
            request.request_id, "EXECUTION_FAILED", 1, "boom", "", duration=0.1
        )

    host(executor)
    with sandbox_gateway.bound_sandbox_execution_context(context):
        assert _run_command(
            project_dir=root,
            command="/usr/bin/python3 -m pytest tests/test_pkg.py",
            timeout_seconds=10,
        ) == (1, "boom")
        assert (
            _run_command(
                project_dir=root,
                command="/usr/bin/python3 -m compileall -q pkg/__init__.py",
                timeout_seconds=10,
            )[0]
            == 1
        )
        for command in (
            "git diff --check -- pkg",
            "python -m black --check pkg/__init__.py",
        ):
            with pytest.raises(OSError, match="unsupported_operation"):
                _run_command(project_dir=root, command=command, timeout_seconds=10)
    with pytest.raises(OSError, match="trusted_execution_context_missing"):
        _run_command(
            project_dir=root, command="python -m pytest tests", timeout_seconds=10
        )
    assert seen == [
        ("pytest", ["tests/test_pkg.py"]),
        ("compileall", ["pkg/__init__.py"]),
    ]


def test_sandbox_writes_never_reach_the_worker_workspace(
    claimed, host, no_local_step_execution, tmp_path
):
    """Actual local subprocess: a test writes into its cwd and /tmp.

    The snapshot is read-only (EROFS under the Docker bind, EACCES for a
    non-root runner); root bypasses the mode bits.  Either way nothing may
    reach the Worker workspace.
    """

    db, context, root = claimed
    (root / "tests" / "test_pkg.py").write_text(
        "import pathlib, tempfile\n"
        "def test_writes():\n"
        "    pathlib.Path(tempfile.gettempdir(), 'scratch.txt').write_text('x')\n"
        "    for target in ('leak.txt', 'pkg/__init__.py'):\n"
        "        try:\n"
        "            pathlib.Path(target).write_text('tampered')\n"
        "        except OSError:\n"
        "            pass\n"
    )
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    host(LocalPytestExecutor(staging))
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = _pytest_step(root, context, "python -m pytest tests/test_pkg.py -q")
    assert result["status"] == "completed", result
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before
    assert list(staging.iterdir()) == []


# --- Host Launcher executor (real DockerExecutor code, no Docker) -----------


def test_pytest_profile_fails_closed_until_the_image_is_pinned(tmp_path, monkeypatch):
    # Independent of the committed pin, which PRODUCT-23B records.
    monkeypatch.setattr(docker_executor, "PYTEST_IMAGE", None)
    executor = docker_executor.DockerExecutor(
        tmp_path / "staging", docker="/nonexistent"
    )
    request = _v3_request()
    result = executor.execute(request, None)
    assert (result.execution_status, result.error_code) == (
        "REJECTED",
        "pytest_image_unpinned",
    )
    assert not (tmp_path / "staging").exists()


def test_pytest_profile_argv_is_fixed_isolated_and_request_independent(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(docker_executor, "PYTEST_IMAGE", "sha256:" + "c" * 64)
    executor = docker_executor.DockerExecutor(tmp_path, docker="docker")
    argv = executor._argv("p43-sbx-c2-x", tmp_path / "request-1", PYTEST_PROFILE)
    for flag in (
        "--pull=never",
        "--network=none",
        "--ipc=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65534:65534",
        "--pids-limit=64",
        f"type=bind,src={tmp_path / 'request-1'},dst=/ws,readonly",
        "/tmp:rw,noexec,nosuid,nodev,size=67108864,mode=1777",
        "--workdir=/ws/src",
    ):
        assert flag in argv, flag
    assert (
        argv[argv.index("sha256:" + "c" * 64) :][-1] == docker_executor.PYTEST_BOOTSTRAP
    )
    script_argv = executor._argv("p43-sbx-c2-x", tmp_path / "request-1", PROFILE)
    assert script_argv == executor._argv("p43-sbx-c2-x", tmp_path / "request-1")
    monkeypatch.setattr(docker_executor, "PYTEST_IMAGE", "python:latest")
    with pytest.raises(RuntimeError, match="pytest_image_unpinned"):
        executor._argv("n", tmp_path, PYTEST_PROFILE)


def test_snapshot_staging_is_read_only_and_cleaned(tmp_path):
    files, _ = decode_workspace_snapshot(
        [_entry("pkg/__init__.py"), _entry("tests/test_a.py")]
    )
    staged = StagedWorkspace(
        tmp_path / "staging", encode_tool_invocation("pytest", ["tests"]), files
    )
    assert (staged.path / "args.json").stat().st_mode & 0o777 == 0o444
    assert (staged.source_path / "pkg" / "__init__.py").stat().st_mode & 0o777 == 0o444
    assert (staged.source_path / "pkg").stat().st_mode & 0o777 == 0o555
    assert not (staged.path / "input.py").exists() and staged.output_path is None
    staged.cleanup()
    assert list((tmp_path / "staging").iterdir()) == []


@pytest.mark.parametrize("route", ["planning", "stored_plan"])
def test_u1_rewritten_acceptance_is_refused_at_completion_and_never_held(
    c2_env,
    host,
    no_network,
    no_local_step_execution,
    monkeypatch,
    db_session_factory,
    tmp_path,
    route,
):
    """Existing contract: acceptance protection is completion-time integrity.

    Neither Plan admission nor Path Authority blocks a declared write to the
    acceptance file; it lands only in the disposable Runtime Workspace. The
    completion hash check against the pre-execution snapshot must refuse the
    candidate, so a passing rewritten acceptance is never held or promoted
    and the authoritative workspace keeps the frozen acceptance bytes.
    """

    _require_u1_evidence()
    monkeypatch.setattr(settings, "EXECUTION_CONTEXT_TOKENS", 65536)
    staging = tmp_path / "launcher-staging"
    staging.mkdir(mode=0o700)
    executor = LocalPytestExecutor(staging)
    deliveries = c3b._eager_delivery(
        monkeypatch, db_session_factory, lambda: host(executor)
    )
    plan = _reference_plan()
    acceptance = "tests/test_rangekit_acceptance.py"
    # Still a real, passing test file, but no longer the operator's bytes.
    rewritten = (U1 / "baseline" / acceptance).read_text() + (
        "\n\ndef test_rewritten_by_candidate() -> None:\n"
        "    assert merge_ranges([]) == []\n"
    )
    plan[1]["ops"].append(
        {"op": "write_file", "path": acceptance, "content": rewritten}
    )
    plan[1]["expected_files"].append(acceptance)
    if route == "planning":
        _install_u1_provider(monkeypatch, plan)
    else:
        monkeypatch.setattr(
            worker,
            "_execute_planning_phase",
            lambda *a, **k: pytest.fail("Planning must not run for the stored plan"),
        )

    submitted = _submit_u1(
        db_session_factory, plan if route == "stored_plan" else None, "full_lifecycle"
    )

    db = db_session_factory()
    try:
        execution = db.get(TaskExecution, submitted["task_execution_id"])
        task = db.get(Task, submitted["task_id"])
        assert execution.status is TaskStatus.FAILED, deliveries[0].get("result")
        assert "acceptance" in (task.error_message or "").lower(), task.error_message
        change_sets = (
            db.query(TaskExecutionChangeSet)
            .filter_by(task_execution_id=execution.id)
            .all()
        )
        assert all(
            (cs.review_decision or {}).get("outcome") != "hold_for_review"
            and cs.disposition != "promoted"
            for cs in change_sets
        )
    finally:
        db.close()
    # The rewritten acceptance executed only inside the Sandbox path.
    assert executor.calls
    manifest = json.loads((U1 / "manifest.json").read_text())
    assert _sha(submitted["workspace"] / acceptance) == manifest["acceptance_sha256"]
