from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration import worker_identity


def _minimal_candidate(tmp_path: Path):
    source_root = tmp_path / "candidate"
    (source_root / "app").mkdir(parents=True)
    (source_root / "app" / "worker.py").write_text(
        "WORKER_VALUE = 'candidate'\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=source_root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=source_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "C1 test"], cwd=source_root, check=True
    )
    subprocess.run(["git", "add", "."], cwd=source_root, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate"], cwd=source_root, check=True)
    manifest = worker_identity.build_candidate_source_manifest(
        source_root,
        affected_modules=("app.worker",),
        launcher_identity={
            "launcher": "c1-test",
            "executable": "/venv/bin/python3",
            "cwd": str(source_root),
            "argv": ["/venv/bin/python3", "-m", "celery", "worker"],
        },
    )
    fingerprint = (
        "c1",
        {
            "schema_version": worker_identity.CONFIGURATION_PROJECTION_SCHEMA_VERSION,
            "projection_policy_id": worker_identity.CONFIGURATION_POLICY_ID,
        },
    )
    process = worker_identity.ProcessIdentity(
        pid=401,
        linux_boot_id="boot-c1",
        start_ticks="4001",
        executable="/venv/bin/python3",
        cwd=str(source_root),
        argv=("/venv/bin/python3", "-m", "celery", "worker"),
        ppid=1,
        pgid=401,
        sid=401,
    )
    return source_root, manifest, fingerprint, process


def test_worker_ready_identity_paths_are_cwd_independent(monkeypatch, tmp_path):
    from app.services.session import recovery_coordinator
    from app.tasks import worker as worker_tasks

    source_root = tmp_path / "source"
    runtime_root = tmp_path / "runtime"
    source_root.mkdir()
    runtime_root.mkdir()
    cwd_a = tmp_path / "cwd-a"
    cwd_b = tmp_path / "cwd-b"
    cwd_a.mkdir()
    cwd_b.mkdir()
    monkeypatch.setattr(worker_tasks.settings, "WORKER_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr(worker_tasks.settings, "RUNTIME_ROOT", str(runtime_root))

    calls = []

    def fake_create_worker_boot_identity(**kwargs):
        calls.append(kwargs)
        return (
            kwargs["identity_dir"] / "manifest.json",
            SimpleNamespace(
                record_id="record", identity_status="IDENTITY_INCONCLUSIVE"
            ),
        )

    monkeypatch.setattr(
        worker_tasks, "_collect_worker_configuration", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        worker_tasks, "_create_worker_boot_identity", fake_create_worker_boot_identity
    )

    class FakeDb:
        def close(self):
            return None

    monkeypatch.setattr(worker_tasks, "get_db_session", lambda: FakeDb())
    monkeypatch.setattr(
        recovery_coordinator,
        "inspect_startup_running_executions",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        worker_tasks, "reconcile_backend_slots_on_boot", lambda db: None
    )
    monkeypatch.setattr(
        worker_tasks, "report_openclaw_binding_residue_on_boot", lambda: None
    )
    sender = SimpleNamespace(app=SimpleNamespace(conf={}), hostname="celery@test")

    monkeypatch.chdir(cwd_a)
    worker_tasks.on_worker_ready(sender)
    monkeypatch.chdir(cwd_b)
    worker_tasks.on_worker_ready(sender)

    assert [call["source_root"] for call in calls] == [source_root, source_root]
    assert [call["identity_dir"] for call in calls] == [
        runtime_root / "worker-identities",
        runtime_root / "worker-identities",
    ]
    assert not (cwd_a / "run" / "worker-identities").exists()
    assert not (cwd_b / "run" / "worker-identities").exists()


def test_identity_manifest_is_written_under_configured_runtime_root(
    monkeypatch, tmp_path
):
    source_root, candidate, fingerprint, process = _minimal_candidate(tmp_path)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    paths = worker_identity.resolve_worker_identity_paths(source_root, runtime_root)
    monkeypatch.setattr(
        worker_identity,
        "verify_candidate_source_manifest",
        lambda *args, **kwargs: worker_identity.CandidateManifestVerification(
            worker_identity.IDENTITY_VERIFIED, ()
        ),
    )

    path, manifest = worker_identity.create_worker_boot_identity(
        source_root=paths.source_root,
        identity_dir=paths.identity_dir,
        candidate_manifest=candidate,
        registered_tasks=worker_identity.EXPECTED_TASK_REGISTRATIONS,
        configuration_fingerprint=fingerprint,
        process_identity=process,
        child_identities=(),
        boot_timestamp=datetime(2026, 10, 15, tzinfo=UTC),
    )

    assert path.parent == runtime_root / "worker-identities"
    assert path.is_relative_to(runtime_root)
    assert (
        worker_identity.WorkerBootIdentityManifest.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        ).schema_version
        == worker_identity.SCHEMA_VERSION
    )
    assert manifest.candidate_source_root == str(source_root.resolve())
    assert not (tmp_path / "cwd" / "run" / "worker-identities").exists()


@pytest.mark.parametrize(
    "source_value, runtime_value, message",
    [
        ("", "/tmp/runtime", "source root is required"),
        ("relative/source", "/tmp/runtime", "source root must be absolute"),
        ("/does/not/exist", "/tmp/runtime", "source root does not exist"),
    ],
)
def test_worker_identity_paths_reject_invalid_source_configuration(
    source_value, runtime_value, message
):
    with pytest.raises(worker_identity.WorkerIdentityPathError, match=message):
        worker_identity.resolve_worker_identity_paths(source_value, runtime_value)


def test_worker_identity_paths_reject_invalid_runtime_configuration(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    missing_runtime = tmp_path / "missing-runtime"
    runtime_file = tmp_path / "runtime-file"
    runtime_file.write_text("not a directory", encoding="utf-8")

    for runtime_value, message in (
        (missing_runtime, "runtime root does not exist"),
        (runtime_file, "runtime root is not a directory"),
    ):
        with pytest.raises(worker_identity.WorkerIdentityPathError, match=message):
            worker_identity.resolve_worker_identity_paths(source_root, runtime_value)


def test_worker_identity_paths_reject_overlap_and_identity_file(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    overlapping_runtime = source_root / "runtime"
    overlapping_runtime.mkdir()
    with pytest.raises(worker_identity.WorkerIdentityPathError, match="overlap"):
        worker_identity.resolve_worker_identity_paths(source_root, overlapping_runtime)

    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    (runtime_root / "worker-identities").write_text("not a directory", encoding="utf-8")
    with pytest.raises(
        worker_identity.WorkerIdentityPathError, match="identity directory"
    ):
        worker_identity.resolve_worker_identity_paths(source_root, runtime_root)


def test_worker_identity_paths_canonicalize_symlink_aliases(tmp_path):
    source_root = tmp_path / "source"
    runtime_root = tmp_path / "runtime"
    source_root.mkdir()
    runtime_root.mkdir()
    source_alias = tmp_path / "source-alias"
    runtime_alias = tmp_path / "runtime-alias"
    source_alias.symlink_to(source_root, target_is_directory=True)
    runtime_alias.symlink_to(runtime_root, target_is_directory=True)

    direct = worker_identity.resolve_worker_identity_paths(source_root, runtime_root)
    aliased = worker_identity.resolve_worker_identity_paths(source_alias, runtime_alias)

    assert aliased == direct
