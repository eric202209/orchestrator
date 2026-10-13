"""Provider-free Phase 37 DP3-I1 Worker identity/controller coverage."""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import os
import socket
import stat
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration import worker_controller, worker_identity
from app.services.orchestration.worker_controller import (
    ApprovalVerification,
    ControllerSafetyError,
    FileApprovalClaimStore,
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
    CONFIG_PROJECTION_INCOMPLETE,
    CandidateIdentityPin,
    CONFIGURATION_POLICY_ID,
    CONFIGURATION_PROJECTION_SCHEMA_VERSION,
    DEFAULT_AFFECTED_MODULES,
    EXPECTED_TASK_REGISTRATIONS,
    IDENTITY_INCONCLUSIVE,
    IDENTITY_MISMATCH,
    IDENTITY_UNAVAILABLE,
    IDENTITY_VERIFIED,
    CandidateSourceManifest,
    ProcessIdentity,
    WorkerBootIdentityManifest,
    build_candidate_source_manifest,
    build_effective_task_policy_projection,
    build_non_secret_configuration_fingerprint,
    capture_loaded_module_provenance,
    collect_worker_configuration,
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
            "argv": [
                "/venv/bin/python3",
                "/venv/bin/celery",
                "-A",
                "app.celery_app",
                "worker",
            ],
        },
    )
    return root, manifest


def _resign(manifest: CandidateSourceManifest, **changes) -> CandidateSourceManifest:
    updated = replace(manifest, **changes, source_manifest_digest="")
    digest = hashlib.sha256(
        json.dumps(updated.payload(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return replace(updated, source_manifest_digest=digest)


def _identity(
    pid: int,
    ticks: str,
    *,
    boot: str = "boot-a",
    cwd: str = "/srv/orchestrator",
) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        linux_boot_id=boot,
        start_ticks=ticks,
        executable="/venv/bin/python3",
        cwd=cwd,
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
            "worker.prefetch_multiplier": 1,
            "worker.acks_late": True,
            "worker.reject_on_worker_lost": True,
            "worker.acks_on_failure_or_timeout": True,
            "task.soft_time_limit_seconds": 120,
            "task.time_limit_seconds": 180,
            "runtime.profile": "standard",
            "route.execution_backend": "local_openclaw",
            "celery.task_serializer": "json",
            "celery.accept_content": ["json"],
            "celery.result_serializer": "json",
            "celery.timezone": "UTC",
            "celery.enable_utc": True,
            "celery.task_default_queue": "celery",
            "celery.task_routes": {},
            "celery.broker_route_identity": "redis://127.0.0.1:6379/0",
            "celery.result_backend_route_identity": "redis://127.0.0.1:6379/1",
            "retry.max_retries": 3,
            "retry.countdown_seconds": 60,
            "retry.redelivery_policy": "reject_on_worker_lost",
            "task.decorator_time_limits": {"execute": {"soft": 120, "hard": 180}},
            "policy.timeout_policy": {"planning": 60, "repair": 30},
            "maintenance.task_identity": {"orphan": "sweep"},
            "openclaw.gateway_route": "http://127.0.0.1:8000",
            "openclaw.model_identity": {"execution": "local-model"},
            "openclaw.fallback_policy": {"agent_backend": "local_openclaw"},
            "openclaw.cli.executable": "/usr/local/bin/openclaw",
            "openclaw.cli.args": ["--json"],
            "d37.binding.route_behavior": "ephemeral-runtime-binding",
            "d37.binding.provider_base_url_identity": "http://127.0.0.1:8000",
            "d37.binding.model_selection": "openai/qwen-local",
            "d37.binding.auth_schema_identity": "api-key",
            "beat.schedule": {"orphan": {"task": "sweep", "interval_seconds": 900}},
            "task.effective_policies": build_effective_task_policy_projection(
                _task_registry(), _task_policy_celery_config()
            ),
        },
        secret_presence={"OPENAI_API_KEY": True},
    )


def _pin(manifest: CandidateSourceManifest) -> CandidateIdentityPin:
    fingerprint, projection = _config()
    return CandidateIdentityPin(
        candidate_head=manifest.candidate_head,
        candidate_tree=manifest.candidate_tree,
        source_root=manifest.source_root,
        source_manifest_digest=manifest.source_manifest_digest,
        launcher_identity=manifest.launcher_identity,
        worker_node_identity="celery@test",
        configuration_schema_version=CONFIGURATION_PROJECTION_SCHEMA_VERSION,
        configuration_fingerprint=fingerprint,
        configuration_policy_id=projection["projection_policy_id"],
    )


def _transition_in_process(root: str, claim_id: str, result_queue) -> None:
    store = FileApprovalClaimStore(root)
    try:
        store.transition(claim_id, expected="CLAIMED", state="OPERATION_ATTEMPTED")
    except ControllerSafetyError:
        result_queue.put(False)
    else:
        result_queue.put(True)


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
    worker = _identity(101, "1001", cwd=manifest.source_root)
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
        worker_node_identity="celery@test",
        observer_provenance="test-trusted-observer",
        observer_candidate_source_manifest_digest=None,
        observer_worker_key=worker.key() if worker else None,
        beat_maintenance_identity="test-beat-maintenance",
    )
    values.update(changes)
    return WorkerObservation(**values)


def _controller(
    candidate: CandidateSourceManifest,
    boot: WorkerBootIdentityManifest,
    observation,
    *,
    claim_store=None,
    fail_stop: bool = False,
):
    adapter = SimpleNamespace(stop_calls=[], start_calls=[])

    def stop_worker(identity):
        if fail_stop:
            raise ControllerSafetyError("synthetic adapter uncertainty")
        adapter.stop_calls.append(identity)

    def start_worker(launcher):
        adapter.start_calls.append(launcher)

    adapter.stop_worker = stop_worker
    adapter.start_worker = start_worker
    observation = replace(
        observation,
        observer_candidate_source_manifest_digest=candidate.source_manifest_digest,
    )
    state = {"observation": observation}

    class TestApprovalVerifier:
        def verify(self, approval, *, operation, candidate_pin, gate, now):
            del operation, candidate_pin, gate, now
            return ApprovalVerification(
                approval.operator_identity == "operator:test",
                principal=(
                    "operator:test"
                    if approval.operator_identity == "operator:test"
                    else None
                ),
            )

    class TestObserverVerifier:
        def verify(self, observation, *, candidate_pin, expected_worker, now):
            del now
            reasons = []
            if observation.observer_provenance != "test-trusted-observer":
                reasons.append("observer_provenance_missing")
            if (
                observation.observer_candidate_source_manifest_digest
                != candidate_pin.source_manifest_digest
            ):
                reasons.append("observer_candidate_binding_mismatch")
            if observation.observer_worker_key != (
                expected_worker.key() if expected_worker else None
            ):
                reasons.append("observer_worker_binding_mismatch")
            return not reasons, reasons

    controller = WorkerOnlyController(
        candidate_manifest=candidate,
        candidate_pin=_pin(candidate),
        boot_manifest=boot,
        snapshot_provider=lambda: state["observation"],
        process_adapter=adapter,
        approval_verifier=TestApprovalVerifier(),
        observer_verifier=TestObserverVerifier(),
        claim_store=claim_store
        or FileApprovalClaimStore(Path(tempfile.mkdtemp(prefix="dp3-i3-claims-"))),
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
    object.__setattr__(approval, "candidate_head", candidate.candidate_head)
    object.__setattr__(approval, "candidate_tree", candidate.candidate_tree)
    object.__setattr__(
        approval, "source_manifest_digest", candidate.source_manifest_digest
    )
    object.__setattr__(approval, "worker_pid", worker.pid)
    object.__setattr__(approval, "worker_start_ticks", worker.start_ticks)
    object.__setattr__(approval, "linux_boot_id", worker.linux_boot_id)
    object.__setattr__(approval, "gate_id", gate.gate_id)
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
    changed_values = dict(payload["allowlisted_values"])
    changed_values["queue.identity"] = "other"
    changed, _ = build_non_secret_configuration_fingerprint(
        changed_values, secret_presence={"OPENAI_API_KEY": True}
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
    assert boot.identity_status == IDENTITY_INCONCLUSIVE
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
        current_configuration_schema_version=CONFIGURATION_PROJECTION_SCHEMA_VERSION,
        current_configuration_policy_id=CONFIGURATION_POLICY_ID,
        expected_service_identities={"beat": beat, "api": api},
        current_service_identities={"beat": beat, "api": api},
        candidate_pin=_pin(manifest),
        current_worker_node_identity="celery@test",
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
        candidate_manifest=manifest,
        candidate_pin=_pin(manifest),
        boot_manifest=boot,
        clock=lambda: NOW,
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


@pytest.mark.parametrize(
    "pin_change,reason",
    [
        (
            lambda pin, worker: replace(
                pin,
                launcher_identity={
                    **pin.launcher_identity,
                    "executable": "/wrong/python",
                },
            ),
            "current_launcher_executable_mismatch",
        ),
        (
            lambda pin, worker: replace(
                pin,
                launcher_identity={**pin.launcher_identity, "cwd": "/wrong/cwd"},
            ),
            "current_launcher_cwd_mismatch",
        ),
        (
            lambda pin, worker: replace(
                pin,
                launcher_identity={
                    **pin.launcher_identity,
                    "argv": [*pin.launcher_identity["argv"], "--wrong"],
                },
            ),
            "current_launcher_argv_mismatch",
        ),
    ],
)
def test_i3_external_launcher_pin_rejects_boot_and_current_drift(
    candidate, pin_change, reason
):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    pin = _identity_verification_kwargs(boot, worker, beat, api, manifest)[
        "candidate_pin"
    ]
    result = verify_worker_identity(
        boot,
        manifest,
        **_identity_verification_kwargs(
            boot,
            worker,
            beat,
            api,
            manifest,
            candidate_pin=pin_change(pin, worker),
        ),
    )
    assert result.classification == IDENTITY_MISMATCH
    assert reason in result.reasons


def test_i3_missing_or_wrong_node_pin_never_verifies(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    missing = verify_worker_identity(
        boot,
        manifest,
        **_identity_verification_kwargs(
            boot, worker, beat, api, manifest, candidate_pin=None
        ),
    )
    assert missing.classification == IDENTITY_INCONCLUSIVE
    assert "candidate_pin_unavailable" in missing.reasons

    wrong_node = replace(
        _identity_verification_kwargs(boot, worker, beat, api, manifest)[
            "candidate_pin"
        ],
        worker_node_identity="celery@other",
    )
    result = verify_worker_identity(
        boot,
        manifest,
        **_identity_verification_kwargs(
            boot,
            worker,
            beat,
            api,
            manifest,
            candidate_pin=wrong_node,
        ),
    )
    assert result.classification == IDENTITY_MISMATCH
    assert "boot_worker_node_identity_mismatch" in result.reasons


def test_i3_configuration_projection_covers_policy_and_excludes_secrets():
    settings_object = SimpleNamespace(
        OPENCLAW_GATEWAY_URL="http://127.0.0.1:8000",
        CELERY_BROKER_URL="redis://127.0.0.1:6379/0",
        CELERY_RESULT_BACKEND="redis://127.0.0.1:6379/1",
        OPENCLAW_CLI_PATH="/usr/local/bin/openclaw",
        OPENCLAW_CLI_ARGS="--profile local --json",
        EXECUTION_MODEL="local-model-a",
        PLANNER_MODEL="planner-a",
        PLANNING_REPAIR_MODEL="repair-a",
        AGENT_BACKEND="local_openclaw",
        AGENT_SECONDARY_BACKEND=None,
        OPENCLAW_API_KEY="synthetic-secret-value",
        OPENAI_API_KEY="another-secret-value",
        PLANNING_DIRECT_API_KEY="third-secret-value",
    )
    celery_config = {
        "task_default_queue": "celery",
        "task_default_exchange": None,
        "task_default_routing_key": None,
        "task_serializer": "json",
        "accept_content": ["json"],
        "result_serializer": "json",
        "timezone": "UTC",
        "enable_utc": True,
        "task_acks_late": True,
        "task_reject_on_worker_lost": True,
        "task_acks_on_failure_or_timeout": True,
        "worker_prefetch_multiplier": 1,
        "task_routes": {"app.tasks.worker.*": {"queue": "celery"}},
        "beat_schedule": {
            "cleanup": {
                "task": "app.tasks.maintenance.cleanup_old_logs",
                "schedule": timedelta(days=1),
                "kwargs": {"days": 30},
            }
        },
    }
    first, projection = collect_worker_configuration(
        settings_object,
        celery_config,
        worker_runtime=SimpleNamespace(concurrency=4),
        task_registry=_task_registry(),
        operational_values={
            "task.soft_time_limit_seconds": 120,
            "task.time_limit_seconds": 180,
            "retry.max_retries": 3,
            "retry.countdown_seconds": 60,
            "retry.redelivery_policy": "reject_on_worker_lost",
            "task.decorator_time_limits": {"execute": {"soft": 120, "hard": 180}},
            "policy.timeout_policy": {"planning": 60, "repair": 30},
            "d37.binding.route_behavior": "ephemeral-runtime-binding",
            "d37.binding.provider_base_url_identity": "http://127.0.0.1:8000",
            "d37.binding.model_selection": "openai/qwen-local",
            "d37.binding.auth_schema_identity": "api-key",
        },
    )
    changed_schedule = dict(celery_config)
    changed_schedule["beat_schedule"] = {
        **celery_config["beat_schedule"],
        "cleanup": {
            **celery_config["beat_schedule"]["cleanup"],
            "kwargs": {"days": 31},
        },
    }
    second, _ = collect_worker_configuration(
        settings_object,
        changed_schedule,
        worker_runtime=SimpleNamespace(concurrency=4),
        task_registry=_task_registry(),
        operational_values={
            "task.soft_time_limit_seconds": 120,
            "task.time_limit_seconds": 180,
            "retry.max_retries": 3,
            "retry.countdown_seconds": 60,
            "retry.redelivery_policy": "reject_on_worker_lost",
            "task.decorator_time_limits": {"execute": {"soft": 120, "hard": 180}},
            "policy.timeout_policy": {"planning": 60, "repair": 30},
            "d37.binding.route_behavior": "ephemeral-runtime-binding",
            "d37.binding.provider_base_url_identity": "http://127.0.0.1:8000",
            "d37.binding.model_selection": "openai/qwen-local",
            "d37.binding.auth_schema_identity": "api-key",
        },
    )
    assert first != second
    assert projection["allowlisted_values"]["openclaw.gateway_route"]
    encoded = json.dumps(projection)
    assert "synthetic-secret-value" not in encoded
    assert "another-secret-value" not in encoded
    assert "OPENAI_API_KEY" in encoded

    unsupported_schedule = dict(celery_config)
    unsupported_schedule["beat_schedule"] = {
        "cleanup": {"task": "task", "schedule": timedelta(days=1), "unknown": 1}
    }
    with pytest.raises(
        worker_identity.ConfigurationFingerprintError,
        match=CONFIG_PROJECTION_INCOMPLETE,
    ):
        collect_worker_configuration(settings_object, unsupported_schedule)


def test_i3_candidate_pin_and_reviewed_module_boundary_are_explicit(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    pin = _identity_verification_kwargs(boot, worker, beat, api, manifest)[
        "candidate_pin"
    ]
    wrong_digest = replace(pin, source_manifest_digest="0" * 64)
    result = verify_worker_identity(
        boot,
        manifest,
        **_identity_verification_kwargs(
            boot, worker, beat, api, manifest, candidate_pin=wrong_digest
        ),
    )
    assert result.classification == IDENTITY_MISMATCH
    assert "expected_source_manifest_digest_mismatch" in result.reasons
    assert {
        "app.services.session.orphan_ownership",
        "app.services.session.recovery_coordinator",
        "app.services.orchestration.execution.runtime_context",
        "app.services.agents.runtime_configuration",
        "app.services.orchestration.lifecycle.worker_bootstrap",
        "app.services.orchestration.lifecycle.worker_capacity",
        "app.services.orchestration.execution.runtime",
        "app.services.agents.agent_runtime",
        "app.services.agents.agent_backends",
        "app.services.agents.runtime_adapters.openclaw_adapter",
        "app.services.orchestration.policy",
        "app.services.orchestration.coordinators.completion_coordinator",
        "app.services.orchestration.coordinators.failure_coordinator",
        "app.services.orchestration.phases.execution_loop",
        "app.services.orchestration.phases.planning_flow",
        "app.services.orchestration.state.persistence",
        "app.services.orchestration.state.execution_states",
        "app.services.orchestration.state.session_state",
        "app.services.workspace.control_state_paths",
        "app.services.workspace.checkpoint_service",
        "app.services.workspace.project_isolation_service",
        "app.services.workspace.task_sandbox_allocator",
        "app.services.workspace.system_settings",
        "app.services.workspace.project_mutation_lock",
        "app.services.session.execution_policy",
        "app.services.session.session_execution_service",
        "app.services.session.session_runtime_service",
        "app.services.execution.process_identity",
        "app.tasks.worker_support.workspace",
    }.issubset(DEFAULT_AFFECTED_MODULES)


def test_i7_reviewed_boundary_pins_all_load_bearing_worker_and_celery_modules():
    required = {
        "app.models",
        "app.services.agents.backend_concurrency",
        "app.services.agents.backend_slot_reconciliation",
        "app.services.human_guidance.activation",
        "app.services.human_guidance.conflicts",
        "app.services.integrations.github",
        "app.services.observability.maintenance_observability",
        "app.services.orchestration",
        "app.services.orchestration.execution.binding_reconciliation",
        "app.services.orchestration.lifecycle.continuation_recovery",
        "app.services.orchestration.phases.incremental_flow",
        "app.services.orchestration.phases.planning_guidance_enforcement",
        "app.services.orchestration.planning.incremental_classifier",
        "app.services.orchestration.planning.planner_contract_registry",
        "app.services.orchestration.repo_memory",
        "app.services.orchestration.run_state",
        "app.services.orchestration.worker_identity",
        "app.services.orchestration.working_memory",
        "app.services.planning.planning_dispatch",
        "app.services.planning.planning_session_service",
        "app.services.project.state_summary",
        "app.tasks.github_tasks",
        "app.tasks.planning_tasks",
        "app.tasks.worker_support.worker_helpers",
    }
    assert required.issubset(DEFAULT_AFFECTED_MODULES)
    assert len(DEFAULT_AFFECTED_MODULES) == len(set(DEFAULT_AFFECTED_MODULES))


def test_i7_import_origin_drift_is_not_verified(candidate):
    root, manifest = candidate
    alternate = root.parent / "alternate.py"
    alternate.write_text("ALTERNATE = True\n", encoding="utf-8")
    boot, worker, beat, api = _boot(root, manifest)
    record = dict(boot.affected_module_provenance[0])
    record["spec_origin"] = str(alternate)
    result = verify_worker_identity(
        replace(boot, affected_module_provenance=(record,)),
        manifest,
        **_identity_verification_kwargs(boot, worker, beat, api, manifest),
    )
    assert result.classification == IDENTITY_MISMATCH
    assert "module_import_origin_mismatch:app.worker" in result.reasons


def test_i7_missing_required_module_remains_inconclusive(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    lazy = capture_loaded_module_provenance(manifest, modules={})
    result = verify_worker_identity(
        replace(boot, affected_module_provenance=lazy),
        manifest,
        **_identity_verification_kwargs(boot, worker, beat, api, manifest),
    )
    assert result.classification == IDENTITY_INCONCLUSIVE
    assert "lazy_module_provenance_unavailable:app.worker" in result.reasons


def test_i7_unsupported_dynamic_module_evidence_is_inconclusive(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    dynamic = dict(boot.affected_module_provenance[0])
    dynamic.update(
        resolved_source_path=None,
        source_sha256=None,
        loaded=False,
        load_timing="dynamic_unresolved",
    )
    result = verify_worker_identity(
        replace(boot, affected_module_provenance=(dynamic,)),
        manifest,
        **_identity_verification_kwargs(boot, worker, beat, api, manifest),
    )
    assert result.classification == IDENTITY_INCONCLUSIVE
    assert "dynamic_module_provenance_unavailable:app.worker" in result.reasons


def test_i7_package_and_module_source_paths_are_not_interchangeable(candidate):
    root, _manifest = candidate
    package = root / "app" / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 'package'\n", encoding="utf-8")
    subprocess.run(["git", "add", "app/pkg"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "package"], cwd=root, check=True)
    manifest = build_candidate_source_manifest(
        root,
        affected_modules=("app.pkg",),
        launcher_identity={
            "launcher": "test-worker",
            "executable": "/venv/bin/python3",
            "cwd": str(root),
            "argv": [
                "/venv/bin/python3",
                "/venv/bin/celery",
                "-A",
                "app.celery_app",
                "worker",
            ],
        },
    )
    assert manifest.resolved_modules[0].expected_source_path.endswith(
        "app/pkg/__init__.py"
    )
    wrong = replace(
        manifest.resolved_modules[0],
        expected_source_path=str((root / "app" / "pkg.py").resolve()),
    )
    result = verify_candidate_source_manifest(
        _resign(manifest, resolved_modules=(wrong,))
    )
    assert result.classification == IDENTITY_MISMATCH
    assert any("module_path" in reason for reason in result.reasons)


def _task_registry():
    return {
        name: SimpleNamespace(
            name=name,
            max_retries=3,
            default_retry_delay=60,
            time_limit=None,
            soft_time_limit=None,
            acks_late=True,
            reject_on_worker_lost=True,
            acks_on_failure_or_timeout=True,
            queue="celery",
            exchange=None,
            routing_key=None,
        )
        for name in sorted(EXPECTED_TASK_REGISTRATIONS)
    }


def _task_policy_celery_config():
    return {
        "task_default_queue": "celery",
        "task_default_exchange": None,
        "task_default_routing_key": None,
        "task_routes": {},
    }


def test_i7_task_policy_projection_is_complete_deterministic_and_fail_closed():
    registry = _task_registry()
    config = _task_policy_celery_config()
    first = build_effective_task_policy_projection(registry, config)
    second = build_effective_task_policy_projection(
        dict(reversed(list(registry.items()))), config
    )
    assert first == second
    assert first["reviewed_task_names"] == sorted(EXPECTED_TASK_REGISTRATIONS)
    assert [item["task_name"] for item in first["policies"]] == sorted(
        EXPECTED_TASK_REGISTRATIONS
    )

    missing = dict(registry)
    missing.pop(sorted(EXPECTED_TASK_REGISTRATIONS)[0])
    with pytest.raises(
        worker_identity.ConfigurationFingerprintError,
        match="missing registered task policy",
    ):
        build_effective_task_policy_projection(missing, config)

    unexpected = dict(registry)
    unexpected["app.tasks.unsafe_task"] = registry[
        sorted(EXPECTED_TASK_REGISTRATIONS)[0]
    ]
    with pytest.raises(
        worker_identity.ConfigurationFingerprintError,
        match="unexpected safety-critical task",
    ):
        build_effective_task_policy_projection(unexpected, config)

    with pytest.raises(
        worker_identity.ConfigurationFingerprintError,
        match=CONFIG_PROJECTION_INCOMPLETE,
    ):
        build_effective_task_policy_projection(None, config)


@pytest.mark.parametrize(
    "field,value,group,key",
    [
        ("max_retries", 99, "retry", "max_retries"),
        ("time_limit", 999, "time_limits", "hard"),
        ("acks_late", False, "acknowledgements", "acks_late"),
        ("queue", "unsafe", "route", "queue"),
    ],
)
def test_i7_task_policy_projection_detects_effective_policy_drift(
    field, value, group, key
):
    baseline = build_effective_task_policy_projection(
        _task_registry(), _task_policy_celery_config()
    )
    registry = _task_registry()
    task = registry[sorted(registry)[0]]
    setattr(task, field, value)
    changed = build_effective_task_policy_projection(
        registry, _task_policy_celery_config()
    )
    assert baseline != changed
    assert changed["policies"][0]["policy"][group][key] == value


def test_i7_task_policy_projection_rejects_unsupported_types_and_secrets():
    registry = _task_registry()
    registry[sorted(registry)[0]].max_retries = object()
    with pytest.raises(
        worker_identity.ConfigurationFingerprintError,
        match=CONFIG_PROJECTION_INCOMPLETE,
    ):
        build_effective_task_policy_projection(registry, _task_policy_celery_config())

    config = _task_policy_celery_config()
    config["task_routes"] = {
        sorted(EXPECTED_TASK_REGISTRATIONS)[0]: {
            "queue": "celery",
            "token": "synthetic",
        }
    }
    with pytest.raises(
        worker_identity.ConfigurationFingerprintError,
        match=CONFIG_PROJECTION_INCOMPLETE,
    ):
        build_effective_task_policy_projection(_task_registry(), config)


def test_i7_task_policy_projection_changes_approved_configuration_digest():
    settings = SimpleNamespace(
        OPENCLAW_GATEWAY_URL="http://127.0.0.1:8000",
        CELERY_BROKER_URL="redis://127.0.0.1:6379/0",
        CELERY_RESULT_BACKEND="redis://127.0.0.1:6379/1",
        OPENCLAW_CLI_PATH="/usr/local/bin/openclaw",
        OPENCLAW_CLI_ARGS="--json",
        EXECUTION_MODEL="local-model",
        PLANNER_MODEL="planner",
        PLANNING_REPAIR_MODEL="repair",
        AGENT_BACKEND="local_openclaw",
        AGENT_SECONDARY_BACKEND=None,
    )
    config = {
        **_task_policy_celery_config(),
        "task_serializer": "json",
        "accept_content": ["json"],
        "result_serializer": "json",
        "timezone": "UTC",
        "enable_utc": True,
        "worker_prefetch_multiplier": 1,
        "task_acks_late": True,
        "task_reject_on_worker_lost": True,
        "task_acks_on_failure_or_timeout": True,
        "beat_schedule": {},
    }
    operational = {
        "task.soft_time_limit_seconds": 120,
        "task.time_limit_seconds": 180,
        "retry.max_retries": 3,
        "retry.countdown_seconds": 60,
        "retry.redelivery_policy": "reject_on_worker_lost",
        "task.decorator_time_limits": {"execute": {"soft": 120, "hard": 180}},
        "policy.timeout_policy": {"planning": 60},
        "d37.binding.route_behavior": "ephemeral",
        "d37.binding.provider_base_url_identity": "http://127.0.0.1:8000",
        "d37.binding.model_selection": "local-model",
        "d37.binding.auth_schema_identity": "api-key",
    }
    baseline, _ = collect_worker_configuration(
        settings,
        config,
        worker_runtime=SimpleNamespace(concurrency=1),
        operational_values=operational,
        task_registry=_task_registry(),
    )
    changed_registry = _task_registry()
    changed_registry[sorted(changed_registry)[0]].max_retries = 7
    changed, _ = collect_worker_configuration(
        settings,
        config,
        worker_runtime=SimpleNamespace(concurrency=1),
        operational_values=operational,
        task_registry=changed_registry,
    )
    assert baseline != changed


def test_i3_claim_store_rejects_replay_across_controller_reconstruction(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    shared_store = FileApprovalClaimStore(root.parent / "shared-claims")
    observation = _observation(worker, beat, api)
    first_controller, _state, first_adapter = _controller(
        manifest, boot, observation, claim_store=shared_store
    )
    second_controller, _state, second_adapter = _controller(
        manifest, boot, observation, claim_store=shared_store
    )
    approval = _approval("stop")
    gate = _gate(manifest, worker, beat, api, approval)
    assert first_controller.stop(approval, gate).status == STOP_REQUESTED
    replay = second_controller.stop(approval, gate)
    assert replay.status == PREFLIGHT_BLOCKED
    assert "approval replay rejected" in replay.reasons
    assert len(first_adapter.stop_calls) == 1
    assert not second_adapter.stop_calls


def test_i3_claim_store_rejects_concurrent_replay_and_uncertain_retry(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    shared_store = FileApprovalClaimStore(root.parent / "concurrent-claims")
    approval = _approval("stop")
    gate = _gate(manifest, worker, beat, api, approval)
    controllers = [
        _controller(
            manifest,
            boot,
            _observation(worker, beat, api),
            claim_store=shared_store,
        )[0]
        for _ in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda item: item.stop(approval, gate), controllers)
        )
    assert sorted(result.status for result in results) == [
        PREFLIGHT_BLOCKED,
        STOP_REQUESTED,
    ]

    uncertain_store = FileApprovalClaimStore(root.parent / "uncertain-claims")
    failing, _state, adapter = _controller(
        manifest,
        boot,
        _observation(worker, beat, api),
        claim_store=uncertain_store,
        fail_stop=True,
    )
    failed = failing.stop(approval, gate)
    assert failed.status == PREFLIGHT_BLOCKED
    retrying, _state, retry_adapter = _controller(
        manifest,
        boot,
        _observation(worker, beat, api),
        claim_store=uncertain_store,
    )
    replay = retrying.stop(approval, gate)
    assert replay.status == PREFLIGHT_BLOCKED
    assert "approval replay rejected" in replay.reasons
    assert not retry_adapter.stop_calls
    assert not adapter.stop_calls


def test_i3_trusted_observer_is_required_and_fail_closed(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    controller, _state, adapter = _controller(
        manifest, boot, _observation(worker, beat, api)
    )
    controller.observer_verifier = None
    result = controller.stop(
        _approval("stop"),
        _gate(manifest, worker, beat, api, _approval("stop")),
    )
    assert result.status == PREFLIGHT_BLOCKED
    assert "trusted_observer_unavailable" in result.reasons
    assert not adapter.stop_calls


def test_i3_pidfd_rechecks_full_provenance_before_signal(monkeypatch):
    expected = _identity(501, "5001")
    observed = [expected, replace(expected, cwd="/changed")]
    signals = []
    adapter = worker_controller.PidfdWorkerProcessAdapter(
        lambda _pid: observed.pop(0) if observed else expected
    )
    monkeypatch.setattr(worker_controller.os, "pidfd_open", lambda _pid, _flags: 99)
    monkeypatch.setattr(
        worker_controller.os,
        "pidfd_send_signal",
        lambda *args: signals.append(args),
        raising=False,
    )
    monkeypatch.setattr(worker_controller.os, "close", lambda _fd: None)
    with pytest.raises(ControllerSafetyError):
        adapter.stop_worker(expected)
    assert not signals


@pytest.mark.parametrize(
    "pin_change,reason",
    [
        (
            {"executable": "/synthetic/unapproved-launcher"},
            "approved_launcher_candidate_manifest_mismatch",
        ),
        (
            {"cwd": "/synthetic/unapproved-cwd"},
            "approved_launcher_candidate_manifest_mismatch",
        ),
        (
            {"argv": ["/venv/bin/python3", "--unapproved"]},
            "approved_launcher_candidate_manifest_mismatch",
        ),
        (
            {"worker_node_identity": "celery@other"},
            "start_worker_node_identity_mismatch",
        ),
    ],
)
def test_i5_start_rejects_launcher_pin_mismatch_before_claim_or_adapter(
    candidate, pin_change, reason
):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    del worker
    launcher = dict(manifest.launcher_identity)
    worker_node_identity = "celery@test"
    if "worker_node_identity" in pin_change:
        worker_node_identity = pin_change["worker_node_identity"]
    else:
        launcher.update(pin_change)
    wrong_pin = replace(
        _pin(manifest),
        launcher_identity=launcher,
        worker_node_identity=worker_node_identity,
    )
    claim_root = root.parent / "i5-launcher-claims"
    controller, state, adapter = _controller(
        manifest,
        boot,
        _observation(None, beat, api, workers=()),
        claim_store=FileApprovalClaimStore(claim_root),
    )
    controller.candidate_pin = wrong_pin
    approval = _approval("start")
    result = controller.start(
        approval,
        _gate(
            manifest,
            _identity(101, "1001", cwd=manifest.source_root),
            beat,
            api,
            approval,
            operation="start",
        ),
    )
    assert result.status == PREFLIGHT_BLOCKED
    assert reason in result.reasons
    assert not adapter.start_calls
    assert not list(claim_root.glob("*.json"))
    assert state["observation"].worker is None


def test_i5_start_passes_only_canonically_approved_launcher(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    controller, _state, adapter = _controller(
        manifest,
        boot,
        _observation(None, beat, api, workers=()),
    )
    approval = _approval("start")
    result = controller.start(
        approval,
        _gate(manifest, worker, beat, api, approval, operation="start"),
    )
    assert result.status == START_REQUESTED
    assert adapter.start_calls == [
        {
            **manifest.launcher_identity,
            "worker_node_identity": "celery@test",
        }
    ]


@pytest.mark.parametrize(
    "changed_key,changed_value",
    [
        ("openclaw.gateway_route", "http://127.0.0.1:9999"),
        ("openclaw.model_identity", {"execution": "unapproved-model"}),
        ("openclaw.fallback_policy", {"agent_backend": "unapproved"}),
        ("d37.binding.provider_base_url_identity", "http://127.0.0.1:9999"),
        ("celery.task_serializer", "pickle"),
        ("task.decorator_time_limits", {"execute": {"hard": 999}}),
        ("beat.schedule", {"orphan": {"kwargs": {"days": 99}}}),
        ("retry.redelivery_policy", "ack_on_failure"),
    ],
)
def test_i5_approved_configuration_rejects_self_consistent_unapproved_drift(
    candidate, changed_key, changed_value
):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    changed_values = dict(_config()[1]["allowlisted_values"])
    changed_values[changed_key] = changed_value
    wrong_fingerprint, _projection = build_non_secret_configuration_fingerprint(
        changed_values,
        secret_presence={"OPENAI_API_KEY": True},
    )
    result = verify_worker_identity(
        boot,
        manifest,
        **_identity_verification_kwargs(
            boot,
            worker,
            beat,
            api,
            manifest,
            candidate_pin=replace(
                _pin(manifest), configuration_fingerprint=wrong_fingerprint
            ),
            current_configuration_fingerprint=wrong_fingerprint,
        ),
    )
    assert result.classification == IDENTITY_MISMATCH
    assert "approved_configuration_boot_digest_mismatch" in result.reasons


def test_i5_missing_or_unsupported_configuration_pin_never_verifies(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    missing = verify_worker_identity(
        boot,
        manifest,
        **_identity_verification_kwargs(
            boot,
            worker,
            beat,
            api,
            manifest,
            candidate_pin=replace(
                _pin(manifest),
                configuration_fingerprint=None,
                configuration_policy_id=None,
            ),
        ),
    )
    assert missing.classification == IDENTITY_INCONCLUSIVE
    assert "approved_configuration_digest_unavailable" in missing.reasons
    unsupported = verify_worker_identity(
        boot,
        manifest,
        **_identity_verification_kwargs(
            boot,
            worker,
            beat,
            api,
            manifest,
            candidate_pin=replace(_pin(manifest), configuration_schema_version=99),
        ),
    )
    assert unsupported.classification != IDENTITY_VERIFIED
    assert "unsupported_configuration_projection_schema" in unsupported.reasons


def test_i5_secret_bearing_route_is_rejected_without_serialization():
    settings_object = SimpleNamespace(
        OPENCLAW_GATEWAY_URL="https://user:synthetic-secret@example.invalid/gateway",
        CELERY_BROKER_URL="redis://127.0.0.1:6379/0",
        CELERY_RESULT_BACKEND="redis://127.0.0.1:6379/1",
        OPENCLAW_CLI_PATH="/usr/local/bin/openclaw",
        OPENCLAW_CLI_ARGS="--json",
    )
    with pytest.raises(
        worker_identity.ConfigurationFingerprintError,
        match=CONFIG_PROJECTION_INCOMPLETE,
    ):
        collect_worker_configuration(
            settings_object,
            {
                "task_default_queue": "celery",
                "task_serializer": "json",
                "accept_content": ["json"],
                "result_serializer": "json",
                "timezone": "UTC",
                "enable_utc": True,
                "worker_prefetch_multiplier": 1,
                "task_acks_late": True,
                "task_reject_on_worker_lost": True,
                "task_acks_on_failure_or_timeout": True,
                "task_routes": {},
                "beat_schedule": {},
            },
            worker_runtime=SimpleNamespace(concurrency=1),
            operational_values={},
        )


def test_i5_claim_store_legal_cas_graph_and_terminal_replay_rejection(tmp_path):
    store = FileApprovalClaimStore(tmp_path / "claims")
    claim_id = "a" * 64
    now = NOW
    store.claim(
        claim_id=claim_id,
        binding={"operation": "start", "candidate": "approved"},
        expires_at=now + timedelta(minutes=5),
        now=now,
    )
    store.transition(claim_id, expected="CLAIMED", state="OPERATION_ATTEMPTED")
    store.transition(claim_id, expected="OPERATION_ATTEMPTED", state="COMPLETED")
    with pytest.raises(
        ControllerSafetyError, match="illegal approval claim transition"
    ):
        store.transition(claim_id, expected="COMPLETED", state="CLAIMED")
    record = json.loads((tmp_path / "claims" / f"{claim_id}.json").read_text())
    assert [item["state"] for item in record["state_history"]] == [
        "CLAIMED",
        "OPERATION_ATTEMPTED",
        "COMPLETED",
    ]


def test_i5_claim_store_serializes_competing_transitions(tmp_path):
    store = FileApprovalClaimStore(tmp_path / "claims")
    claim_id = "b" * 64
    store.claim(
        claim_id=claim_id,
        binding={"operation": "stop"},
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )

    def transition():
        try:
            store.transition(claim_id, expected="CLAIMED", state="OPERATION_ATTEMPTED")
            return True
        except ControllerSafetyError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _item: transition(), (1, 2)))
    assert sorted(results) == [False, True]


def test_i5_claim_store_serializes_competing_process_transitions(tmp_path):
    store = FileApprovalClaimStore(tmp_path / "claims")
    claim_id = "d" * 64
    store.claim(
        claim_id=claim_id,
        binding={"operation": "stop"},
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_transition_in_process,
            args=(str(store.root), claim_id, result_queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
    assert all(process.exitcode == 0 for process in processes)
    assert sorted(result_queue.get(timeout=2) for _ in processes) == [False, True]


def test_i5_claim_store_rejects_symlink_ancestor_and_unsafe_permissions(tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    symlink_parent = tmp_path / "symlink-parent"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ControllerSafetyError, match="symlinked ancestor"):
        FileApprovalClaimStore(symlink_parent / "claims")

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    os.chmod(unsafe, 0o755)
    with pytest.raises(ControllerSafetyError, match="permissions unsafe"):
        FileApprovalClaimStore(unsafe)

    store = FileApprovalClaimStore(tmp_path / "secure")
    claim_id = "e" * 64
    store.claim(
        claim_id=claim_id,
        binding={"operation": "stop"},
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    os.chmod(store.root / f"{claim_id}.json", 0o644)
    with pytest.raises(ControllerSafetyError, match="claim file permissions unsafe"):
        store.transition(claim_id, expected="CLAIMED", state="OPERATION_ATTEMPTED")


@pytest.mark.parametrize("object_kind", ["fifo", "directory", "socket"])
def test_i7_claim_lock_rejects_non_regular_objects(tmp_path, object_kind):
    store = FileApprovalClaimStore(tmp_path / f"claims-{object_kind}")
    claim_id = "f" * 64
    lock_path = store.root / f".{claim_id}.lock"
    sock = None
    if object_kind == "fifo":
        os.mkfifo(lock_path)
    elif object_kind == "directory":
        lock_path.mkdir()
    else:
        short_root = Path(tempfile.mkdtemp(prefix="dp3", dir="/tmp"))
        store = FileApprovalClaimStore(short_root / "c")
        lock_path = store.root / f".{claim_id}.lock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(lock_path))
    try:
        with pytest.raises(ControllerSafetyError, match="lock .*regular"):
            store.transition(claim_id, expected="CLAIMED", state="OPERATION_ATTEMPTED")
    finally:
        if sock is not None:
            sock.close()
            lock_path.unlink(missing_ok=True)
            store.root.rmdir()
            short_root.rmdir()


def test_i7_claim_lock_regular_file_remains_accepted(tmp_path):
    store = FileApprovalClaimStore(tmp_path / "regular-claims")
    claim_id = "1" * 64
    store.claim(
        claim_id=claim_id,
        binding={"operation": "stop"},
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    lock_path = store.root / f".{claim_id}.lock"
    assert stat.S_ISREG(lock_path.stat().st_mode)
    store.transition(claim_id, expected="CLAIMED", state="OPERATION_ATTEMPTED")


def test_i7_claim_lock_path_replacement_is_checked_after_open(tmp_path, monkeypatch):
    store = FileApprovalClaimStore(tmp_path / "replacement-claims")
    claim_id = "2" * 64
    store.claim(
        claim_id=claim_id,
        binding={"operation": "stop"},
        expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    lock_path = store.root / f".{claim_id}.lock"
    original_open = worker_controller.os.open
    replaced = {"done": False}

    def replace_before_open(path, flags, mode=0o777, *, dir_fd=None):
        if Path(path) == lock_path and not replaced["done"]:
            lock_path.unlink()
            os.mkfifo(lock_path)
            replaced["done"] = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(worker_controller.os, "open", replace_before_open)
    with pytest.raises(ControllerSafetyError, match="lock .*regular"):
        store.transition(claim_id, expected="CLAIMED", state="OPERATION_ATTEMPTED")


def test_i5_claim_persistence_failure_is_uncertain_and_not_released(
    tmp_path, monkeypatch
):
    store = FileApprovalClaimStore(tmp_path / "claims")
    claim_id = "c" * 64
    original = store._fsync_directory
    calls = {"count": 0}

    def fail_after_claim():
        calls["count"] += 1
        if calls["count"] > 0:
            raise ControllerSafetyError("synthetic fsync failure")
        return original()

    monkeypatch.setattr(store, "_fsync_directory", fail_after_claim)
    with pytest.raises(ControllerSafetyError, match="fsync failure"):
        store.claim(
            claim_id=claim_id,
            binding={"operation": "start"},
            expires_at=NOW + timedelta(minutes=5),
            now=NOW,
        )
    assert (tmp_path / "claims" / f"{claim_id}.json").exists()
    with pytest.raises(ControllerSafetyError, match="approval replay rejected"):
        store.claim(
            claim_id=claim_id,
            binding={"operation": "start"},
            expires_at=NOW + timedelta(minutes=5),
            now=NOW,
        )


def test_i5_completion_persistence_failure_is_not_reported_as_success(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)

    class CompletionFailureStore(FileApprovalClaimStore):
        def transition(self, claim_id, *, expected, state):
            if state == "COMPLETED":
                raise ControllerSafetyError("synthetic completion fsync failure")
            return super().transition(claim_id, expected=expected, state=state)

    store = CompletionFailureStore(root.parent / "completion-failure")
    controller, _state, adapter = _controller(
        manifest, boot, _observation(worker, beat, api), claim_store=store
    )
    approval = _approval("stop")
    gate = _gate(manifest, worker, beat, api, approval)
    result = controller.stop(approval, gate)
    assert result.status == PREFLIGHT_INCONCLUSIVE
    assert "claim_completion_persistence_uncertain" in result.reasons
    assert len(adapter.stop_calls) == 1
    replay = controller.stop(approval, gate)
    assert replay.status == PREFLIGHT_BLOCKED
    assert "START_REQUESTED" not in replay.reasons


def test_i5_lazy_module_provenance_is_inconclusive_not_attested(candidate):
    root, manifest = candidate
    boot, worker, beat, api = _boot(root, manifest)
    lazy = dict(boot.affected_module_provenance[0])
    lazy.update(
        resolved_source_path=None,
        source_sha256=None,
        loaded=False,
        load_timing="lazy_or_unloaded",
    )
    result = verify_worker_identity(
        replace(boot, affected_module_provenance=(lazy,)),
        manifest,
        **_identity_verification_kwargs(boot, worker, beat, api, manifest),
    )
    assert result.classification == IDENTITY_INCONCLUSIVE
    assert "lazy_module_provenance_unavailable:app.worker" in result.reasons
