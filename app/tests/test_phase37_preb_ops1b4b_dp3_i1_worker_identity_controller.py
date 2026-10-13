"""Provider-free Phase 37 DP3-I1 Worker identity/controller coverage."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration import worker_controller, worker_identity
from app.services.orchestration.worker_controller import (
    HOLD_MAINTENANCE_WINDOW_NOT_PROVEN,
    PREFLIGHT_BLOCKED,
    PREFLIGHT_INCONCLUSIVE,
    PREFLIGHT_READY,
    START_REQUESTED,
    STOPPED_VERIFIED,
    STOP_REQUESTED,
    MaintenanceWindowProof,
    OperatorApproval,
    WorkerObservation,
    WorkerOnlyController,
    make_deployment_gate,
)
from app.services.orchestration.worker_identity import (
    EXPECTED_TASK_REGISTRATIONS,
    IDENTITY_INCONCLUSIVE,
    IDENTITY_MISMATCH,
    IDENTITY_UNAVAILABLE,
    IDENTITY_VERIFIED,
    CandidateSourceManifest,
    ProcessIdentity,
    WorkerBootIdentityManifest,
    build_candidate_source_manifest,
    build_non_secret_configuration_fingerprint,
    create_worker_boot_identity,
    sha256_file,
    verify_candidate_source_manifest,
    verify_worker_identity,
)


NOW = datetime(2026, 10, 13, 16, 0, tzinfo=UTC)
TASKS = ("app.tasks.worker.execute_orchestration_task",)


@pytest.fixture
def candidate(tmp_path: Path) -> tuple[Path, CandidateSourceManifest]:
    root = tmp_path / "candidate"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "worker.py").write_text(
        "WORKER_VALUE = 'candidate'\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "DP3 test"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate"], cwd=root, check=True)
    manifest = build_candidate_source_manifest(
        root,
        affected_modules=("app.worker",),
        launcher_identity={
            "launcher": "test-worker",
            "executable": "/venv/bin/python3",
            "cwd": str(root),
            "argv": ["/venv/bin/python3", "-m", "celery", "worker"],
        },
    )
    return root, manifest


def _resign(manifest: CandidateSourceManifest, **changes) -> CandidateSourceManifest:
    updated = replace(manifest, **changes, source_manifest_digest="")
    digest = hashlib.sha256(
        json.dumps(updated.payload(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return replace(updated, source_manifest_digest=digest)


def _identity(pid: int, ticks: str, *, boot: str = "boot-a") -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        linux_boot_id=boot,
        start_ticks=ticks,
        executable="/venv/bin/python3",
        cwd="/srv/orchestrator",
        argv=(
            "/venv/bin/python3",
            "/venv/bin/celery",
            "-A",
            "app.celery_app",
            "worker",
        ),
        ppid=1,
        pgid=pid,
        sid=pid,
    )


def _config() -> tuple[str, dict]:
    return build_non_secret_configuration_fingerprint(
        {
            "queue.identity": "celery",
            "worker.concurrency": 4,
            "runtime.profile": "standard",
            "route.execution_backend": "local_openclaw",
            "beat.schedule": {"orphan": {"task": "sweep", "interval_seconds": 900}},
        },
        secret_presence={"OPENAI_API_KEY": True},
    )


def _module_record(root: Path, manifest: CandidateSourceManifest) -> dict:
    record = manifest.resolved_modules[0]
    source_path = Path(record.expected_source_path)
    return {
        "module_name": record.module_name,
        "resolved_source_path": str(source_path),
        "expected_source_path": record.expected_source_path,
        "source_sha256": record.source_sha256,
        "expected_source_sha256": record.source_sha256,
        "loaded": True,
        "loader_type": "SourceFileLoader",
        "spec_origin": str(source_path),
        "cached_path": None,
        "in_memory_code_attestation": "unavailable",
    }


def _boot(
    root: Path, manifest: CandidateSourceManifest
) -> tuple[
    WorkerBootIdentityManifest, ProcessIdentity, ProcessIdentity, ProcessIdentity
]:
    del root
    worker = _identity(101, "1001")
    child = replace(worker, pid=102, start_ticks="1002", ppid=101)
    beat = replace(worker, pid=201, start_ticks="2001", ppid=1, pgid=201, sid=201)
    api = replace(worker, pid=301, start_ticks="3001", ppid=1, pgid=301, sid=301)
    fingerprint, projection = _config()
    boot = WorkerBootIdentityManifest(
        schema_version=1,
        candidate_head=manifest.candidate_head,
        candidate_tree=manifest.candidate_tree,
        candidate_source_root=manifest.source_root,
        source_manifest_digest=manifest.source_manifest_digest,
        boot_timestamp=NOW.isoformat().replace("+00:00", "Z"),
        linux_boot_id=worker.linux_boot_id,
        worker_pid=worker.pid,
        worker_start_ticks=worker.start_ticks,
        worker_executable=worker.executable,
        worker_cwd=worker.cwd,
        worker_argv=worker.argv,
        worker_node_identity="celery@test",
        expected_task_registrations=TASKS,
        registered_task_registrations=TASKS,
        worker_children=(child.to_dict(),),
        affected_module_provenance=(
            _module_record(Path(manifest.source_root), manifest),
        ),
        configuration_fingerprint=fingerprint,
        configuration_projection=projection,
        identity_status=IDENTITY_VERIFIED,
        record_id="boot-a-101-1001",
    )
    return boot, worker, beat, api


def _observation(
    worker: ProcessIdentity | None,
    beat: ProcessIdentity,
    api: ProcessIdentity,
    *,
    children: tuple[ProcessIdentity, ...] | None = (),
    workers: tuple[ProcessIdentity, ...] | None = None,
    **changes,
) -> WorkerObservation:
    values = dict(
        observed_at=NOW,
        worker=worker,
        worker_children=children,
        worker_identities=(
            workers if workers is not None else ((worker,) if worker else ())
        ),
        beat_identity=beat,
        api_identity=api,
        registered_tasks=TASKS,
        celery_active=0,
        celery_reserved=0,
        celery_scheduled=0,
        redis_queued=0,
        redis_unacked=0,
        active_sessions=0,
        active_task_executions=0,
        active_runtime_leases=0,
        retry_or_redelivery=0,
        active_cli_processes=0,
        managed_bindings=0,
        binding_locks=0,
        binding_construction=0,
        capacity_state="STABLE",
        project_169_integrity=True,
        unknown_ownership=False,
        beat_maintenance_free=True,
        maintenance_window=MaintenanceWindowProof(
            checked_at=NOW,
            free_until=NOW + timedelta(hours=1),
            required_margin_seconds=900,
            schedule_identity=("cleanup", "orphan", "continuation"),
        ),
        notes=(),
    )
    values.update(changes)
    return WorkerObservation(**values)


def _controller(
    candidate: CandidateSourceManifest, boot: WorkerBootIdentityManifest, observation
):
    state = {"observation": observation}
    adapter = SimpleNamespace(stop_calls=[], start_calls=[])

    def stop_worker(identity):
        adapter.stop_calls.append(identity)

    def start_worker(launcher):
        adapter.start_calls.append(launcher)

    adapter.stop_worker = stop_worker
    adapter.start_worker = start_worker
    controller = WorkerOnlyController(
        candidate_manifest=candidate,
        boot_manifest=boot,
        snapshot_provider=lambda: state["observation"],
        process_adapter=adapter,
        clock=lambda: NOW,
    )
    return controller, state, adapter


def _approval(operation: str = "stop") -> OperatorApproval:
    return OperatorApproval(
        operation=operation,
        operator_identity="operator:test",
        approval_id=f"approval-{operation}",
        nonce=f"nonce-{operation}",
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


def _gate(candidate, worker, beat, api, approval, operation="stop", **changes):
    gate = make_deployment_gate(
        operation=operation,
        candidate_manifest=candidate,
        worker=worker,
        beat=beat,
        api=api,
        quiescence_observed_at=NOW,
        maintenance_free_until=NOW + timedelta(hours=1),
        expires_at=NOW + timedelta(minutes=10),
        approval=approval,
        stopped_verified_at=NOW if operation == "start" else None,
    )
    return replace(gate, **changes)


def test_candidate_manifest_correct_and_deterministic(candidate):
    root, manifest = candidate
    rebuilt = build_candidate_source_manifest(
        root,
        affected_modules=("app.worker",),
        launcher_identity=manifest.launcher_identity,
    )
    assert rebuilt.to_dict() == manifest.to_dict()
    assert (
        verify_candidate_source_manifest(manifest).classification == IDENTITY_VERIFIED
    )


@pytest.mark.parametrize("field", ["candidate_head", "candidate_tree"])
def test_candidate_manifest_wrong_git_identity(candidate, field):
    _root, manifest = candidate
    wrong = _resign(manifest, **{field: "f" * 40})
    result = verify_candidate_source_manifest(wrong)
    assert result.classification == IDENTITY_MISMATCH
    assert any("candidate_" in reason for reason in result.reasons)


def test_candidate_manifest_dirty_source(candidate):
    root, manifest = candidate
    (root / "app" / "worker.py").write_text("DIRTY = True\n", encoding="utf-8")
    result = verify_candidate_source_manifest(manifest)
    assert result.classification == IDENTITY_MISMATCH
    assert "dirty_source" in result.reasons


def test_candidate_manifest_wrong_module_path_and_hash(candidate, tmp_path):
    root, manifest = candidate
    alternate = root / "app" / "alternate.py"
    alternate.write_text("ALTERNATE = True\n", encoding="utf-8")
    path_record = replace(
        manifest.resolved_modules[0], expected_source_path=str(alternate.resolve())
    )
    wrong_path = _resign(manifest, resolved_modules=(path_record,))
    path_result = verify_candidate_source_manifest(wrong_path)
    assert path_result.classification == IDENTITY_MISMATCH
    assert any("wrong_module_path" in reason for reason in path_result.reasons)

    hash_record = replace(manifest.resolved_modules[0], source_sha256="0" * 64)
    wrong_hash = _resign(manifest, resolved_modules=(hash_record,))
    hash_result = verify_candidate_source_manifest(wrong_hash)
    assert hash_result.classification == IDENTITY_MISMATCH
    assert any("module_hash_mismatch" in reason for reason in hash_result.reasons)


def test_candidate_manifest_symlink_escape_and_missing_module(candidate, tmp_path):
    root, manifest = candidate
    outside = tmp_path / "outside.py"
    outside.write_text("OUTSIDE = True\n", encoding="utf-8")
    (root / "app" / "link.py").symlink_to(outside)
    with pytest.raises(worker_identity.CandidateManifestError):
        build_candidate_source_manifest(root, affected_modules=("app.link",))
    escape_record = replace(
        manifest.resolved_modules[0], expected_source_path=str(outside.resolve())
    )
    escaped = _resign(manifest, resolved_modules=(escape_record,))
    escaped_result = verify_candidate_source_manifest(escaped)
    assert escaped_result.classification == IDENTITY_MISMATCH
    assert any("path_escape" in reason for reason in escaped_result.reasons)

    (root / "app" / "worker.py").unlink()
    missing_result = verify_candidate_source_manifest(manifest)
    assert missing_result.classification in {IDENTITY_MISMATCH, IDENTITY_UNAVAILABLE}
    assert any(
        "missing_module" in reason or "dirty_source" in reason
        for reason in missing_result.reasons
    )


def test_non_secret_configuration_fingerprint_is_stable_and_omits_values():
    first, payload = _config()
    second, _ = _config()
    changed, _ = build_non_secret_configuration_fingerprint(
        {"queue.identity": "other"}, secret_presence={"OPENAI_API_KEY": True}
    )
    encoded = json.dumps(payload)
    assert first == second
    assert first != changed
    assert "OPENAI_API_KEY" in encoded
    assert "true" in encoded
    assert "synthetic-secret" not in encoded
    with pytest.raises(worker_identity.ConfigurationFingerprintError):
        build_non_secret_configuration_fingerprint(
            {"OPENAI_API_KEY": "synthetic-secret"}
        )


def test_boot_manifest_is_atomic_unique_and_credential_free(candidate, tmp_path):
    root, manifest = candidate
    fingerprint = _config()
    process = _identity(401, "4001")
    path, boot = create_worker_boot_identity(
        source_root=root,
        identity_dir=tmp_path / "identity",
        candidate_manifest=manifest,
        registered_tasks=EXPECTED_TASK_REGISTRATIONS,
        configuration_fingerprint=fingerprint,
        process_identity=process,
        child_identities=(),
    )
    assert path.stat().st_mode & 0o777 == 0o600
    raw = path.read_text(encoding="utf-8")
    assert "synthetic-secret" not in raw
    assert boot.record_id in raw
    with pytest.raises(worker_identity.ManifestWriteError):
        create_worker_boot_identity(
            source_root=root,
            identity_dir=tmp_path / "identity",
            candidate_manifest=manifest,
            registered_tasks=EXPECTED_TASK_REGISTRATIONS,
            configuration_fingerprint=fingerprint,
            process_identity=process,
            child_identities=(),
        )
    real_dir = tmp_path / "real-identity"
    real_dir.mkdir()
    symlink_dir = tmp_path / "symlink-identity"
    symlink_dir.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(worker_identity.ManifestWriteError):
        create_worker_boot_identity(
            source_root=root,
            identity_dir=symlink_dir,
            candidate_manifest=manifest,
            registered_tasks=EXPECTED_TASK_REGISTRATIONS,
            configuration_fingerprint=fingerprint,
            process_identity=_identity(403, "4003"),
            child_identities=(),
        )


def test_manifest_write_failure_is_not_identity_verified(
    candidate, tmp_path, monkeypatch
):
    root, manifest = candidate
    monkeypatch.setattr(
        worker_identity,
        "_write_json_atomic",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            worker_identity.ManifestWriteError("write failed")
        ),
    )
    with pytest.raises(worker_identity.ManifestWriteError):
        create_worker_boot_identity(
            source_root=root,
            identity_dir=tmp_path / "identity",
            candidate_manifest=manifest,
            registered_tasks=EXPECTED_TASK_REGISTRATIONS,
            configuration_fingerprint=_config(),
            process_identity=_identity(402, "4002"),
            child_identities=(),
        )


def _identity_verification_kwargs(boot, worker, beat, api, manifest, **changes):
    values = dict(
        current_process=worker,
        current_registered_tasks=TASKS,
        current_child_identities=(_identity(102, "1002"),),
        current_worker_identities=(worker,),
        current_configuration_fingerprint=_config()[0],
        expected_service_identities={"beat": beat, "api": api},
        current_service_identities={"beat": beat, "api": api},
    )
    values.update(changes)
    return values


def test_independent_verifier_correct_identity_and_in_memory_limit(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    result = verify_worker_identity(
        boot,
        manifest,
        **_identity_verification_kwargs(boot, worker, beat, api, manifest),
    )
    assert result.classification == IDENTITY_VERIFIED
    assert "in_memory_code_attestation_unavailable" in result.reasons


@pytest.mark.parametrize(
    "change,expected",
    [
        (
            lambda worker: replace(worker, start_ticks="9999"),
            "process_start_ticks_mismatch",
        ),
        (
            lambda worker: replace(worker, linux_boot_id="boot-b"),
            "process_linux_boot_id_mismatch",
        ),
        (
            lambda worker: replace(worker, executable="/wrong/python"),
            "process_executable_mismatch",
        ),
    ],
)
def test_verifier_detects_pid_reuse_boot_mismatch_and_process_drift(
    candidate, change, expected
):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    result = verify_worker_identity(
        boot,
        manifest,
        **_identity_verification_kwargs(boot, change(worker), beat, api, manifest),
    )
    assert result.classification == IDENTITY_MISMATCH
    assert expected in result.reasons


def test_verifier_detects_stale_manifest_and_duplication(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    stale = verify_worker_identity(
        boot,
        manifest,
        current_process=None,
        now=NOW + timedelta(days=2),
    )
    assert stale.classification == IDENTITY_MISMATCH
    assert "stale_manifest" in stale.reasons
    duplicate = verify_worker_identity(
        boot,
        manifest,
        **_identity_verification_kwargs(
            boot, worker, beat, api, manifest, current_worker_identities=(worker, beat)
        ),
    )
    assert duplicate.classification == IDENTITY_MISMATCH
    assert "unexpected_worker_duplication" in duplicate.reasons


def test_verifier_detects_registration_config_module_and_source_drift(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    registration = verify_worker_identity(
        boot,
        manifest,
        **_identity_verification_kwargs(
            boot, worker, beat, api, manifest, current_registered_tasks=("wrong.task",)
        ),
    )
    assert registration.classification == IDENTITY_MISMATCH
    assert "registration_drift" in registration.reasons

    config = verify_worker_identity(
        boot,
        manifest,
        **_identity_verification_kwargs(
            boot,
            worker,
            beat,
            api,
            manifest,
            current_configuration_fingerprint="different",
        ),
    )
    assert config.classification == IDENTITY_MISMATCH
    assert "configuration_fingerprint_mismatch" in config.reasons

    module = dict(boot.affected_module_provenance[0])
    module["resolved_source_path"] = "/tmp/wrong.py"
    module_result = verify_worker_identity(
        replace(boot, affected_module_provenance=(module,)),
        manifest,
        **_identity_verification_kwargs(boot, worker, beat, api, manifest),
    )
    assert module_result.classification == IDENTITY_MISMATCH
    assert any("module_path" in reason for reason in module_result.reasons)

    (root / "app" / "worker.py").write_text("SOURCE_DRIFT = True\n", encoding="utf-8")
    drift = verify_worker_identity(
        boot,
        manifest,
        **_identity_verification_kwargs(boot, worker, beat, api, manifest),
    )
    assert drift.classification == IDENTITY_MISMATCH
    assert "dirty_source" in drift.reasons


def test_verifier_returns_unavailable_when_independent_evidence_is_missing(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    del root
    result = verify_worker_identity(
        boot,
        manifest,
        current_process=worker,
        current_registered_tasks=None,
        current_child_identities=None,
        current_worker_identities=None,
        current_configuration_fingerprint=None,
        expected_service_identities={"beat": beat, "api": api},
        current_service_identities={"beat": beat, "api": api},
    )
    assert result.classification == IDENTITY_INCONCLUSIVE
    assert "registrations_unavailable" in result.reasons


class FakeAdapter:
    def __init__(self):
        self.stop_calls = []
        self.start_calls = []

    def stop_worker(self, identity):
        self.stop_calls.append(identity)

    def start_worker(self, launcher):
        self.start_calls.append(launcher)


def test_controller_default_is_read_only_and_stop_requires_approval(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    del root
    controller, _state, adapter = _controller(
        manifest,
        boot,
        _observation(
            worker,
            beat,
            api,
            children=(replace(worker, pid=102, start_ticks="1002", ppid=101),),
        ),
    )
    assert controller.inspect().status == "INSPECTED"
    assert controller.preflight().status == PREFLIGHT_READY
    wrong_approval = _approval("inspect")
    result = controller.stop(
        wrong_approval, _gate(manifest, worker, beat, api, wrong_approval)
    )
    assert result.status == PREFLIGHT_BLOCKED
    assert not adapter.stop_calls


def test_controller_stop_and_replayed_approval_are_separate(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    del root
    controller, _state, adapter = _controller(
        manifest,
        boot,
        _observation(
            worker,
            beat,
            api,
            children=(replace(worker, pid=102, start_ticks="1002", ppid=101),),
        ),
    )
    approval = _approval("stop")
    gate = _gate(manifest, worker, beat, api, approval)
    first = controller.stop(approval, gate)
    second = controller.stop(approval, gate)
    assert first.status == STOP_REQUESTED
    assert second.status == PREFLIGHT_BLOCKED
    assert len(adapter.stop_calls) == 1
    assert not adapter.start_calls


@pytest.mark.parametrize("field", ["worker_pid", "worker_start_ticks"])
def test_controller_rejects_wrong_gate_worker_identity(candidate, field):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    del root
    controller, _state, adapter = _controller(
        manifest, boot, _observation(worker, beat, api)
    )
    approval = _approval("stop")
    gate = _gate(manifest, worker, beat, api, approval, **{field: 9999})
    result = controller.stop(approval, gate)
    assert result.status == PREFLIGHT_BLOCKED
    assert not adapter.stop_calls


@pytest.mark.parametrize(
    "field",
    [
        "celery_active",
        "celery_reserved",
        "celery_scheduled",
        "redis_queued",
        "redis_unacked",
        "active_sessions",
        "active_task_executions",
        "active_runtime_leases",
        "retry_or_redelivery",
        "active_cli_processes",
        "managed_bindings",
        "binding_locks",
        "binding_construction",
    ],
)
def test_controller_blocks_active_or_ambiguous_stop_conditions(candidate, field):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    del root
    controller, _state, adapter = _controller(
        manifest, boot, _observation(worker, beat, api, **{field: 1})
    )
    approval = _approval("stop")
    result = controller.stop(approval, _gate(manifest, worker, beat, api, approval))
    assert result.status == PREFLIGHT_BLOCKED
    assert not adapter.stop_calls


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"capacity_state": "AMBIGUOUS"}, "capacity_state_not_stable"),
        ({"project_169_integrity": False}, "project_169_integrity_unproven"),
        ({"unknown_ownership": True}, "ownership_unknown"),
        ({"beat_maintenance_free": False}, "beat_maintenance_state_unproven"),
    ],
)
def test_controller_blocks_capacity_project_ownership_and_beat_drift(
    candidate, changes, reason
):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    del root
    controller, _state, adapter = _controller(
        manifest, boot, _observation(worker, beat, api, **changes)
    )
    approval = _approval("stop")
    result = controller.stop(approval, _gate(manifest, worker, beat, api, approval))
    assert result.status == PREFLIGHT_BLOCKED
    assert reason in result.reasons
    assert not adapter.stop_calls


def test_controller_holds_when_maintenance_margin_is_not_proven(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    del root
    controller, _state, adapter = _controller(
        manifest, boot, _observation(worker, beat, api, maintenance_window=None)
    )
    approval = _approval("stop")
    result = controller.stop(approval, _gate(manifest, worker, beat, api, approval))
    assert result.status == HOLD_MAINTENANCE_WINDOW_NOT_PROVEN
    assert not adapter.stop_calls


def test_controller_verify_stopped_blocks_old_child_survivor_and_start_is_not_chained(
    candidate,
):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    del root
    child = replace(worker, pid=102, start_ticks="1002", ppid=101)
    controller, state, adapter = _controller(
        manifest, boot, _observation(None, beat, api, children=(child,), workers=())
    )
    stopped = controller.verify_stopped()
    assert stopped.status == PREFLIGHT_BLOCKED
    assert "Worker_child_survivor" in stopped.reasons
    approval = _approval("start")
    result = controller.start(
        approval,
        _gate(manifest, worker, beat, api, approval, operation="start"),
    )
    assert result.status == PREFLIGHT_BLOCKED
    assert "old_Worker_child_survivor" in result.reasons
    assert not adapter.start_calls
    assert state["observation"].worker is None


def test_controller_expired_approval_and_no_default_observer_are_safe(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    del root
    controller = WorkerOnlyController(
        candidate_manifest=manifest, boot_manifest=boot, clock=lambda: NOW
    )
    assert controller.inspect().status == PREFLIGHT_INCONCLUSIVE
    controller, _state, adapter = _controller(
        manifest, boot, _observation(worker, beat, api)
    )
    expired = replace(_approval("stop"), expires_at=NOW - timedelta(seconds=1))
    result = controller.stop(expired, _gate(manifest, worker, beat, api, expired))
    assert result.status == PREFLIGHT_BLOCKED
    assert "approval_or_gate_expired" in result.reasons
    assert not adapter.stop_calls


def test_controller_source_no_forbidden_lifecycle_controls_and_no_sigkill():
    source = Path(worker_controller.__file__).read_text(encoding="utf-8")
    assert "SIGKILL" not in source
    assert "killpg" not in source
    assert "pkill" not in source
    assert "killall" not in source


def test_worker_ready_manifest_failure_is_nonfatal_and_not_verified(
    monkeypatch, caplog
):
    from app.services.session import recovery_coordinator
    from app.tasks import worker as worker_tasks

    class FakeDb:
        def close(self):
            return None

    monkeypatch.setattr(
        worker_tasks,
        "_collect_worker_configuration",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("manifest setup failed")
        ),
    )
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
    with caplog.at_level(logging.ERROR):
        worker_tasks.on_worker_ready(sender)
    assert any("not identity-verified" in record.message for record in caplog.records)
