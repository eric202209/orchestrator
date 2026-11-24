"""P43-PRODUCT-22-C2-C3B: trusted submitter → full Worker body → C2 Launcher.

Offline. The trusted submitter creates the Project/Task/Session through the
existing endpoint functions and calls ``queue_task_for_session``. Delivery runs
the real ``execute_orchestration_task`` body through Celery's eager tracer
after the same publication gates ``apply_async`` enforces. The real strict
``HostSocketLauncher`` runs behind a fake Docker executor. Every TCP connect
is refused and recorded, standing in for the internal no-provider network.

Not exercised here (covered by their own suites or only by the live run):
Redis transport, the Worker boot manifest/candidate pin, real Docker.
"""

from __future__ import annotations

import json
import os
import pwd
import socket
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from celery.app.task import Task as CeleryTask
import pytest

from app import celery_app as celery_module
from app.config import DEFAULT_DATABASE_URL, settings
from app.models import (
    Project,
    Session as SessionModel,
    Task,
    TaskCheckpoint,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration import worker_identity
from app.services.orchestration.validation.validator import ValidatorService
from app.tasks import worker
from app.tests import test_p43_product22_c2_c2b_sandbox_slice as c2b
from app.tests.test_p43_product22_c2_c1_worker_foundation import (
    _make_contract_fixture,
    _run_entrypoint,
    _skip_if_host_exposes_privileged_socket,
)
from scripts.maintenance import p43_c2_trusted_submitter as submitter

host, FakeDocker = c2b.host, c2b.FakeDocker

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "scripts" / "worker-entrypoint.sh"
QUEUE = "orchestrator-c2-c1-test"
INSTANCE = "c2-c1-c3b-offline"


def _c2_environment(root: Path) -> dict[str, str]:
    dirs = {name: root / name for name in ("db", "runtime", "checkpoints", "identity")}
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    lines = (REPO / "config" / "worker-c2-live.env.example").read_text().splitlines()
    env = dict(
        line.split("=", 1) for line in lines if line and not line.startswith("#")
    )
    env.update(
        WORKER_INSTANCE_ID=INSTANCE,
        CONTROLLED_WORKER_NODE_IDENTITY=f"orchestrator-worker-c2-c1@{INSTANCE}",
        CELERY_BROKER_URL="redis://p43-c2-redis:6379/9",
        CELERY_RESULT_BACKEND="redis://p43-c2-redis:6379/10",
        CELERY_BROKER_PASSWORD="offline-test-only",
        WORKER_APP_ROOT=str(REPO),
        WORKER_SOURCE_ROOT=str(REPO),
        WORKER_DATABASE_DIR=str(dirs["db"]),
        WORKER_RUNTIME_ROOT=str(dirs["runtime"]),
        WORKER_CHECKPOINT_DIR=str(dirs["checkpoints"]),
        WORKER_IDENTITY_ROOT=str(dirs["identity"]),
        DATABASE_URL=f"sqlite:///{dirs['db']}/orchestrator.db",
        RUNTIME_ROOT=str(dirs["runtime"]),
        CHECKPOINT_DIR=str(dirs["checkpoints"]),
        WORKSPACE_ROOT=str(root / "workspace"),
    )
    # The submitter never carries Launcher material; the Worker gets it below.
    env.pop("SANDBOX_BRIDGE_SOCKET")
    env.pop("SANDBOX_BRIDGE_KEY_FILE")
    return env


def _no_forbidden_paths(path: str) -> bool:
    return False


@pytest.fixture
def c2_env(tmp_path, monkeypatch):
    env = _c2_environment(tmp_path / "c2")
    for name in ("SANDBOX_BRIDGE_SOCKET", "SANDBOX_BRIDGE_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    # Effective settings mirror the container environment (string fields).
    for name, value in env.items():
        if isinstance(getattr(settings, name, None), str):
            monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "EXECUTION_ENABLED", False)
    monkeypatch.setattr(settings, "DEV_API_ONLY", False)
    monkeypatch.setattr(
        "app.services.workspace.system_settings.settings.RUNTIME_ROOT",
        env["RUNTIME_ROOT"],
    )
    # This host exposes the deployed C1B material (real refusal covered by
    # test_submitter_refuses_visible_bridge_or_docker_material).
    monkeypatch.setattr(
        submitter,
        "FORBIDDEN_PATHS",
        tuple(
            str(tmp_path / "absent" / Path(p).name) for p in submitter.FORBIDDEN_PATHS
        ),
    )
    return env


@pytest.fixture
def no_network(monkeypatch):
    """No provider (or any TCP peer) is reachable; record every attempt.

    On the internal C2 network an unknown host such as ``model-provider``
    fails name resolution, so lookups are recorded as well as connects.
    """

    attempts: list[Any] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append(("connect", address))
            raise ConnectionRefusedError("C2 internal network: no TCP peer")
        return real_connect(self, address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append(("connect", address))
            return 111
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if host not in (None, "localhost", "127.0.0.1", "::1"):
            attempts.append(("resolve", host))
            raise socket.gaierror(socket.EAI_NONAME, "C2 internal network")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return attempts


@pytest.fixture
def no_local_step_execution(monkeypatch):
    """Fail on any local interpreter or step-command process; record the rest.

    The Runtime Workspace allocator legitimately runs Orchestrator-built
    ``git`` argv (C2-C2B: not migrated); nothing may run the step locally.
    """

    from app.services.orchestration.execution import execution_flow

    seen: list[list[str]] = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def _check(args, kwargs):
        argv = args[0] if args else kwargs.get("args")
        tokens = [str(argv)] if isinstance(argv, (str, bytes)) else list(argv)
        tokens = [str(item) for item in tokens]
        seen.append(tokens)
        joined = " ".join(tokens)
        if "sys.exit" in joined or Path(tokens[0]).name.startswith("python"):
            raise AssertionError(f"local execution fallback attempted: {joined}")

    def run(*args, **kwargs):
        _check(args, kwargs)
        return real_run(*args, **kwargs)

    class Popen(real_popen):
        def __init__(self, *args, **kwargs):
            _check(args, kwargs)
            super().__init__(*args, **kwargs)

    def portable(*_args, **_kwargs):
        raise AssertionError("local execution fallback attempted: portable")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", Popen)
    monkeypatch.setattr(execution_flow, "_execute_portable_posix_command", portable)
    return seen


def _eager_delivery(monkeypatch, db_session_factory, start_worker_launcher):
    """Real publication gates, then the real task body via the eager tracer.

    The Launcher socket/key reach the environment only at Worker delivery,
    after the submitter has already refused any visible Launcher material.
    """

    real_task = worker.execute_orchestration_task
    deliveries: list[dict[str, Any]] = []

    def apply_async(*, kwargs, task_id=None, **options):
        celery_module._require_execution_enabled(disposable_publication=True)
        celery_module._validate_disposable_publication(
            task_name=real_task.name, queue_name=options.get("queue") or QUEUE
        )
        deliveries.append({"kwargs": dict(kwargs), "task_id": task_id})
        start_worker_launcher()
        result = CeleryTask.apply(
            real_task, kwargs=kwargs, task_id=task_id, routing_key=QUEUE
        )
        deliveries[-1]["result"] = result.result
        return SimpleNamespace(id=task_id)

    monkeypatch.setattr(
        worker,
        "execute_orchestration_task",
        SimpleNamespace(
            name=real_task.name,
            apply_async=apply_async,
            # Handed to the completion coordinator; a manual one-Task Session
            # must never auto-advance.
            delay=lambda **_kw: pytest.fail("unexpected follow-up dispatch"),
        ),
    )
    monkeypatch.setattr(worker, "get_db_session", db_session_factory)
    # The pin/boot-manifest identity is proven by its own suites and the live
    # run; this offline process has no Worker boot manifest.
    monkeypatch.setattr(
        celery_module, "_require_fresh_disposable_worker_identity", lambda: None
    )
    monkeypatch.setattr(settings, "CONTROLLED_WORKER_IDENTITY_REQUIRED", False)
    return deliveries


def test_fixed_plan_is_accepted_by_existing_validator_only_as_test_only(tmp_path):
    plan = [dict(step) for step in submitter.FIXED_PLAN]
    assert plan[0]["commands"] == ["python3 -c 'import sys; sys.exit(0)'"]
    kwargs = dict(
        output_text=json.dumps(plan),
        task_prompt=submitter.TASK_DESCRIPTION,
        project_dir=tmp_path,
        title=submitter.TASK_TITLE,
        description=submitter.TASK_DESCRIPTION,
    )
    for severity in ("standard", "high"):
        verdict = ValidatorService.validate_plan(
            plan,
            execution_profile=submitter.EXECUTION_PROFILE,
            validation_severity=severity,
            **kwargs,
        )
        assert verdict.accepted, verdict.reasons
    # The validator is not weakened: an implementation profile still rejects.
    strict = ValidatorService.validate_plan(
        plan,
        execution_profile="full_lifecycle",
        validation_severity="high",
        **kwargs,
    )
    assert not strict.accepted


def test_submitter_cli_accepts_no_plan_input():
    with pytest.raises(SystemExit):
        submitter.main(["submit", "--plan", "[]"])
    with pytest.raises(SystemExit):
        submitter.main(["submit", "extra"])


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"WORKER_ISOLATION_MODE": "production"}, "disposable_c2_configuration"),
        ({"C2_DISPOSABLE_EXECUTION_ADMISSION": ""}, "disposable_c2_configuration"),
        ({"DATABASE_URL": DEFAULT_DATABASE_URL}, "disposable_c2_configuration"),
        ({"CELERY_QUEUE_NAME": "celery"}, "disposable_c2_configuration"),
        ({"WORKER_QUEUE_NAME": "celery"}, "disposable_worker_queue_required"),
        ({"EXECUTION_ENABLED": "true"}, "execution_enabled_must_be_false"),
        (
            {"CONTROLLED_WORKER_NODE_IDENTITY": "orchestrator-worker-c2-c1"},
            "controlled_worker_node_identity_mismatch",
        ),
        (
            {"CELERY_BROKER_URL": "redis://redis:6379/0"},
            "celery_broker_url_must_use_disposable_redis_database",
        ),
        (
            {"CELERY_RESULT_BACKEND": "redis://:pw@p43-c2-redis:6379/10"},
            "celery_result_backend_must_not_carry_credentials",
        ),
        (
            {"SANDBOX_BRIDGE_KEY_FILE": "/run/secrets/p43-sandbox-bridge-c2.key"},
            "sandbox_bridge_key_file_must_be_absent",
        ),
        (
            {"SANDBOX_BRIDGE_SOCKET": "/run/p43-sandbox-bridge-c2/launcher.sock"},
            "sandbox_bridge_socket_must_be_absent",
        ),
    ],
)
def test_submitter_refuses_non_disposable_configuration(tmp_path, overrides, reason):
    env = _c2_environment(tmp_path)
    assert submitter.refusal_reasons(env, path_exists=_no_forbidden_paths) == []
    env.update(overrides)
    reasons = submitter.refusal_reasons(env, path_exists=_no_forbidden_paths)
    assert any(item.startswith(reason) for item in reasons), reasons


@pytest.mark.parametrize("path", submitter.FORBIDDEN_PATHS)
def test_submitter_refuses_visible_bridge_or_docker_material(tmp_path, path):
    env = _c2_environment(tmp_path)
    reasons = submitter.refusal_reasons(env, path_exists=lambda p: p == path)
    assert reasons == [f"forbidden_material_visible:{path}"]


def test_submitter_refuses_existing_database_before_touching_it(c2_env, monkeypatch):
    Path(c2_env["DATABASE_URL"].removeprefix("sqlite:///")).write_bytes(b"")
    monkeypatch.setattr(
        "app.database.init_db",
        lambda: pytest.fail("init_db must not run on an existing database"),
    )
    with pytest.raises(submitter.SubmitterRefused, match="database_already_exists"):
        submitter.init_database()


def test_submitter_refuses_a_non_fresh_database(c2_env, db_session_factory):
    db = db_session_factory()
    db.add(Project(name="pre-existing"))
    db.commit()
    db.close()
    with pytest.raises(submitter.SubmitterRefused, match="database_not_fresh"):
        submitter.submit(db_session_factory)


def test_trusted_submitter_task_reaches_done_through_c2_launcher(
    c2_env,
    host,
    no_network,
    no_local_step_execution,
    monkeypatch,
    db_session_factory,
    caplog,
):
    caplog.set_level(
        "INFO", logger="app.services.orchestration.execution.sandbox_gateway"
    )
    docker = FakeDocker()
    deliveries = _eager_delivery(monkeypatch, db_session_factory, lambda: host(docker))
    monkeypatch.setattr(
        worker,
        "_execute_planning_phase",
        lambda *a, **k: pytest.fail("Planning must not run for the stored plan"),
    )

    submitted = submitter.submit(db_session_factory)

    # One real publication through the disposable admission, one delivery.
    assert len(deliveries) == 1
    delivery = deliveries[0]
    assert delivery["result"]["status"] == "completed", delivery["result"]
    observed = submitter.observe(db_session_factory, deadline_seconds=0)
    assert observed["terminal"] is True
    assert observed["task_execution_status"].lower() == "done", observed
    assert observed["task_status"].lower() == "done"
    assert observed["task_plan_commands"] == [[submitter.FIXED_COMMAND]]
    assert observed["attempt_number"] == 1
    assert observed["task_execution_id"] == submitted["task_execution_id"]
    assert observed["dispatch_celery_task_id"] == delivery["task_id"]
    assert observed["dispatch_lease_fence"] == 1

    # Exactly one authorized sandbox request, bound to this attempt.
    assert len(docker.calls) == 1
    admitted = docker.calls[0]
    assert admitted.code == "import sys; sys.exit(0)" and admitted.version == 2
    auth = admitted.authorization
    assert auth["task_execution_id"] == observed["task_execution_id"]
    assert auth["session_id"] == observed["session_id"]
    assert auth["attempt_number"] == 1
    assert auth["dispatch_fence"] == 1
    assert auth["dispatch_celery_task_id"] == delivery["task_id"]
    assert auth["session_instance_id"] == observed["dispatch_session_instance_id"]
    assert type(auth["launcher_epoch"]) is int and auth["launcher_epoch"] > 0
    assert auth["nonce"] == admitted.request_id

    # The existing Validator accepted the stored plan; no provider was reached.
    db = db_session_factory()
    try:
        verdicts = [
            json.loads(row.state_snapshot)
            for row in db.query(TaskCheckpoint).all()
            if row.checkpoint_type == "validation_plan"
        ]
        assert [v["status"] for v in verdicts] == ["accepted"], verdicts
        assert db.query(TaskExecution).count() == 1
        assert db.get(Task, observed["task_id"]).status is TaskStatus.DONE
        assert db.get(SessionModel, observed["session_id"]).status == "completed"
    finally:
        db.close()
    assert no_network == []
    # The Worker log binds the Launcher request ID to this exact attempt.
    correlation = [r.getMessage() for r in caplog.records if "[C2_SANDBOX]" in r.msg]
    assert correlation == [
        f"[C2_SANDBOX] request={admitted.request_id}"
        f" task_execution_id={observed['task_execution_id']} attempt=1"
        f" session_id={observed['session_id']} dispatch_fence=1"
        f" celery_task_id={delivery['task_id']}"
        f" launcher_epoch={auth['launcher_epoch']} status=SUCCESS exit=0"
    ]
    # Only Orchestrator-built git argv ran locally (Runtime Workspace).
    assert {Path(argv[0]).name for argv in no_local_step_execution} <= {"git"}


# --- Worker image release identity and candidate pin -----------------------

RELEASE_SCRIPT = REPO / "scripts" / "maintenance" / "p43_c2_worker_release.sh"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _dockerfile_modes(root: Path, *, keep_exec_bit: bool = True) -> None:
    """Apply Dockerfile.worker's release permissions to a staged context."""

    for current, dirs, files in os.walk(root):
        for name in files:
            path = Path(current) / name
            executable = keep_exec_bit and path.stat().st_mode & stat.S_IXUSR
            path.chmod(0o555 if executable else 0o444)
        Path(current).chmod(0o555)


def _restore_writable(root: Path) -> None:
    """Add only the owner write bit back, keeping each file's exec bit."""

    for current, dirs, files in os.walk(root):
        for path in (Path(current), *(Path(current) / name for name in files)):
            path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)


def _stage(release: Path) -> tuple[Path, str, str]:
    out = subprocess.run(
        ["bash", str(RELEASE_SCRIPT), "stage", "HEAD", str(release)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    values = dict(line.split("=", 1) for line in out.splitlines())
    return release, values["CANDIDATE_HEAD"], values["CANDIDATE_TREE"]


@pytest.fixture(scope="module")
def staged_release(tmp_path_factory):
    staged = _stage(tmp_path_factory.mktemp("c2-release") / "context")
    try:
        yield staged
    finally:
        _restore_writable(staged[0])


def test_release_script_refuses_unknown_commit_and_existing_output(tmp_path):
    missing = subprocess.run(
        ["bash", str(RELEASE_SCRIPT), "stage", "0" * 40, str(tmp_path / "x")],
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 78 and "commit not found" in missing.stderr
    existing = subprocess.run(
        ["bash", str(RELEASE_SCRIPT), "stage", "HEAD", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert existing.returncode == 78 and "already exists" in existing.stderr


def test_staged_image_release_reproduces_the_committed_identity(staged_release):
    release, head, tree = staged_release
    assert head == _git("rev-parse", "HEAD")
    assert tree == _git("rev-parse", "HEAD^{tree}")
    assert not (release / ".git").exists() and not (release / ".env").exists()
    try:
        _dockerfile_modes(release)
        assert worker_identity._source_identity(release) == ((), head, tree)
    finally:
        _restore_writable(release)


def test_flattened_exec_bits_or_changed_bytes_do_not_match_the_release(tmp_path):
    release, head, tree = _stage(tmp_path / "context")
    try:
        # The previous Dockerfile chmod (every file 0444) loses Git's 100755.
        _dockerfile_modes(release, keep_exec_bit=False)
        dirty, _, actual = worker_identity._source_identity(release)
        assert actual != tree and dirty == (f"release_tree_mismatch:{actual}",)
    finally:
        _restore_writable(release)
    release, head, tree = _stage(tmp_path / "changed")
    marker = release / "app" / "config.py"
    marker.write_bytes(marker.read_bytes() + b"# uncommitted\n")
    try:
        _dockerfile_modes(release)
        with pytest.raises(worker_identity.CandidateManifestError, match="dirty"):
            worker_identity.build_candidate_source_manifest(
                release, launcher_identity=_launcher(release)
            )
    finally:
        _restore_writable(release)


def _celery_argv(node: str) -> list[str]:
    return [
        "-A",
        "app.celery_app",
        "worker",
        f"--queues={QUEUE}",
        "--pool=solo",
        "--concurrency=1",
        "--loglevel=INFO",
        f"--hostname={node}",
    ]


def _launcher(release: Path) -> dict[str, Any]:
    celery = REPO / "venv" / "bin" / "celery"
    return worker_identity.worker_source_launcher_identity(
        release,
        executable=sys.executable,
        argv=[str(celery), *_celery_argv("orchestrator-worker-c2-c1@c2-c1-x")],
    )


def test_candidate_pin_worksheet_derives_from_the_staged_release(
    staged_release, tmp_path
):
    """The existing worksheet, run from the release itself, pins that release."""

    release, head, tree = staged_release
    celery = REPO / "venv" / "bin" / "celery"
    if not celery.is_file():
        pytest.skip("repository venv celery launcher is unavailable")
    interpreter = celery.read_text(encoding="utf-8").splitlines()[0][2:].strip()
    node = f"orchestrator-worker-c2-c1@{INSTANCE}"
    context = tmp_path / "launcher-context.json"
    context.write_text(
        json.dumps(
            {
                "context_schema_version": 1,
                "launcher": "celery-worker",
                "interpreter": interpreter,
                "script": str(celery),
                "args": _celery_argv(node),
                "cwd": str(release),
                "worker_node_identity": node,
            }
        ),
        encoding="utf-8",
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        **_c2_environment(tmp_path / "c2"),
        "WORKER_APP_ROOT": str(release),
        "WORKER_SOURCE_ROOT": str(release),
        "CONTROLLED_WORKER_NODE_IDENTITY": node,
    }
    try:
        _dockerfile_modes(release)
        out = subprocess.run(
            [
                interpreter,
                "-m",
                "scripts.maintenance.worker_pin_worksheet",
                "--source-root",
                str(release),
                "--expected-head",
                head,
                "--expected-tree",
                tree,
                "--launcher-context",
                str(context),
            ],
            cwd=release,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        _restore_writable(release)
    assert out.returncode == 0, out.stderr[-2000:]
    sheet = json.loads(out.stdout)
    assert sheet["approval_state"] == "NOT_APPROVED"
    body = sheet["proposed_pin_body"]
    assert (body["candidate_head"], body["candidate_tree"]) == (head, tree)
    assert body["source_root"] == str(release)
    assert body["worker_node_identity"] == node
    assert body["approval"] is None  # the loader refuses it until approved
    assert body["launcher_identity"]["argv"][-1] == f"--hostname={node}"
    assert "--pool=solo" in body["launcher_identity"]["argv"]


# --- Worker entrypoint launch line -----------------------------------------


def test_entrypoint_launches_solo_pool_with_the_controlled_node_name():
    text = ENTRYPOINT.read_text(encoding="utf-8")
    launch = text[text.index("exec celery") :]
    assert "--pool=solo" in launch
    assert '--hostname="$CONTROLLED_WORKER_NODE_IDENTITY"' in launch
    assert (
        'CONTROLLED_WORKER_NODE_IDENTITY="$(value_of CONTROLLED_WORKER_NODE_IDENTITY)'
        '@$(value_of WORKER_INSTANCE_ID)"\nexport CONTROLLED_WORKER_NODE_IDENTITY'
    ) in text
    # The composed name is formed only after the exact base value is checked.
    assert text.index(
        "require_exact CONTROLLED_WORKER_NODE_IDENTITY orchestrator-worker-c2-c1"
    ) < text.index("export CONTROLLED_WORKER_NODE_IDENTITY")


def test_entrypoint_exec_argv_and_node_identity(tmp_path):
    _skip_if_host_exposes_privileged_socket()
    env = _make_contract_fixture(tmp_path)
    env.pop("WORKER_ENTRYPOINT_CHECK_ONLY")
    env["WORKER_ALLOW_DISPOSABLE_START"] = "1"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "celery"
    fake.write_text(
        '#!/bin/sh\nprintf "%s\\n" "NODE=$CONTROLLED_WORKER_NODE_IDENTITY" "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    fake_bin.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    out = _run_entrypoint(env, user=pwd.getpwnam("nobody"))
    assert out.returncode == 0, out.stderr
    node = f"orchestrator-worker-c2-c1@{env['WORKER_INSTANCE_ID']}"
    assert out.stdout.splitlines() == [
        f"NODE={node}",
        *_celery_argv(node)[:4],
        "--pool=solo",
        "--concurrency=1",
        "--loglevel=INFO",
        f"--hostname={node}",
    ]


# --- Disposable broker credential ------------------------------------------


@pytest.mark.parametrize("isolation", ["disposable", ""])
def test_broker_password_applies_only_in_disposable_mode(tmp_path, isolation):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "WORKER_ISOLATION_MODE": isolation,
        "CELERY_BROKER_URL": "redis://p43-c2-redis:6379/9",
        "CELERY_RESULT_BACKEND": "redis://p43-c2-redis:6379/10",
        "CELERY_BROKER_PASSWORD": "offline-test-only",
        "DATABASE_URL": f"sqlite:///{tmp_path}/orchestrator.db",
    }
    probe = (
        "from app.celery_app import celery_app as a\n"
        "c = a.connection_for_write()\n"
        "print(c.password, a.backend.connparams.get('password'), c.as_uri())\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={**env, "PYTHONPATH": str(REPO)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    broker, backend, uri = out.stdout.split()
    expected = "offline-test-only" if isolation else "None"
    assert (broker, backend) == (expected, expected)
    assert "offline-test-only" not in uri


LIVE_RUN_SCRIPT = REPO / "scripts" / "maintenance" / "p43_c2_live_run.sh"
_VALID_LIVE_INPUTS = {
    "P43_C2_CANDIDATE": "a" * 40,
    "P43_C2_REDIS_IMAGE": "redis:7@sha256:" + "b" * 64,
    "P43_C2_OPERATOR": "operator",
}


@pytest.mark.parametrize(
    ("argv", "overrides", "reason"),
    [
        (["check"], {"P43_C2_CANDIDATE": ""}, "40-hex commit"),
        (["check"], {"P43_C2_REDIS_IMAGE": "redis:7"}, "digest-pinned"),
        (["check"], {"P43_C2_OPERATOR": ""}, "P43_C2_OPERATOR"),
        (["setup"], {}, "--operator-approved"),
        (["submit"], {}, "--operator-approved"),
        (["cleanup"], {}, "--operator-approved"),
    ],
)
def test_live_run_refuses_without_pinned_inputs_or_approval(argv, overrides, reason):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    env.update({**_VALID_LIVE_INPUTS, **overrides})
    out = subprocess.run(
        ["bash", str(LIVE_RUN_SCRIPT), *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 78
    assert reason in out.stderr


def test_live_run_refuses_outside_a_root_host_reviewed_clone():
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **_VALID_LIVE_INPUTS}
    out = subprocess.run(
        ["bash", str(LIVE_RUN_SCRIPT), "check"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Here: a container (ai-agent) or a non-root CI runner; never the Host.
    assert out.returncode == 78
    assert any(
        text in out.stderr
        for text in ("must run as root on the Host", "must run on the Host")
    )
    assert "docker" not in out.stdout
