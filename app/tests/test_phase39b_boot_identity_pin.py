"""Phase 39B boot-identity activation and v2 correlation compatibility."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import LogEntry, Project, Session as SessionModel
from app.services.agents.openclaw_service import OpenClawSessionService
from app.services.observability.log_stream import LogStreamService
from app.services.observability.openclaw_diagnostics import (
    OpenClawDiagnosticEvent,
    build_openclaw_diagnostic_metadata,
    normalize_openclaw_diagnostic_metadata,
)
from app.services.orchestration import worker_identity
from app.services.orchestration.invocation_provenance import (
    CORRELATED,
    INCONCLUSIVE,
    MISMATCH,
    REQUIRED_LOADED_MODULES,
    InvocationProvenanceReference,
    bind_invocation_reference,
    build_invocation_projection,
    persist_invocation_projection,
    reset_invocation_reference,
    validate_invocation_reference,
)
from app.services.orchestration.worker_identity import (
    EXPECTED_TASK_REGISTRATIONS,
    IDENTITY_INCONCLUSIVE,
    IDENTITY_MISMATCH,
    IDENTITY_VERIFIED,
    PIN_ABSENT,
    PIN_INCOMPLETE,
    PIN_INVALID,
    PIN_LOADED,
    PIN_MATCHED,
    PIN_MISMATCHED,
    WorkerBootIdentityManifest,
    create_worker_boot_identity,
    load_candidate_identity_pin,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
FINGERPRINT = "f" * 64
MODULE_DIGEST = "e" * 64
NODE = "celery@pin-test"
ARGV = ["/venv/bin/python3", "-m", "celery", "worker"]
CANARY = "SYNTHETIC_ASSISTANT_CONTENT_CANARY_phase39b_pin"


@pytest.fixture
def candidate(tmp_path: Path):
    source_root = tmp_path / "candidate"
    (source_root / "app").mkdir(parents=True)
    (source_root / "app" / "worker.py").write_text(
        "WORKER_VALUE = 'candidate'\n", encoding="utf-8"
    )
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "config", "user.name", "Pin test"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "candidate"],
    ):
        subprocess.run(args, cwd=source_root, check=True)
    manifest = worker_identity.build_candidate_source_manifest(
        source_root,
        affected_modules=("app.worker",),
        launcher_identity={
            "launcher": "pin-test",
            "executable": "/venv/bin/python3",
            "cwd": str(source_root),
            "argv": ARGV,
        },
    )
    fingerprint = (
        FINGERPRINT,
        {
            "schema_version": worker_identity.CONFIGURATION_PROJECTION_SCHEMA_VERSION,
            "projection_policy_id": worker_identity.CONFIGURATION_POLICY_ID,
            "identity_scope": worker_identity.IDENTITY_SCOPE,
            "invocation_identity": worker_identity.INVOCATION_IDENTITY,
            "invocation_identity_reason": worker_identity.INVOCATION_IDENTITY_REASON,
        },
    )
    master = worker_identity.ProcessIdentity(
        pid=401,
        linux_boot_id="boot-pin",
        start_ticks="4001",
        executable="/venv/bin/python3",
        cwd=str(source_root.resolve()),
        argv=tuple(ARGV),
        ppid=1,
        pgid=401,
        sid=401,
    )
    child = worker_identity.ProcessIdentity(
        pid=402,
        linux_boot_id="boot-pin",
        start_ticks="4002",
        executable="/venv/bin/python3",
        cwd=str(source_root.resolve()),
        argv=tuple(ARGV),
        ppid=401,
        pgid=401,
        sid=401,
    )
    return source_root, manifest, fingerprint, master, child


@pytest.fixture(autouse=True)
def _loaded_required_modules(monkeypatch):
    """Boot provenance for the offline candidate: every required module loaded.

    Observed checkout integrity is stubbed as in the existing boot-path tests;
    pin comparison is independent of it and is exercised for real.
    """

    monkeypatch.setattr(
        worker_identity,
        "verify_candidate_source_manifest",
        lambda *args, **kwargs: worker_identity.CandidateManifestVerification(
            IDENTITY_VERIFIED, ()
        ),
    )
    monkeypatch.setattr(
        worker_identity,
        "capture_loaded_module_provenance",
        lambda _candidate: _boot_provenance(),
    )


def _boot_provenance(evaluator_status="LOADED_MODULE_IDENTITY_VERIFIED"):
    records = []
    for name in REQUIRED_LOADED_MODULES:
        record = {
            "module_name": name,
            "expected_source_sha256": MODULE_DIGEST,
            "source_sha256": MODULE_DIGEST,
            "loaded": True,
        }
        if name == worker_identity.EVALUATOR_MODULE_NAME:
            record["evaluator_identity_status"] = evaluator_status
        records.append(record)
    return tuple(records)


def _pin_payload(manifest, **changes):
    payload = {
        "pin_schema_version": 1,
        "candidate_head": manifest.candidate_head,
        "candidate_tree": manifest.candidate_tree,
        "source_root": manifest.source_root,
        "source_manifest_digest": manifest.source_manifest_digest,
        "launcher_identity": {
            "launcher": "pin-test",
            "executable": "/venv/bin/python3",
            "cwd": manifest.source_root,
            "argv": ARGV,
        },
        "worker_node_identity": NODE,
        "configuration_schema_version": (
            worker_identity.CONFIGURATION_PROJECTION_SCHEMA_VERSION
        ),
        "configuration_fingerprint": FINGERPRINT,
        "configuration_policy_id": worker_identity.CONFIGURATION_POLICY_ID,
        "approval": {
            "operator_identity": "operator@local",
            "approval_id": "approval-0001",
            "approved_at": (NOW - timedelta(hours=1)).isoformat(),
            "expires_at": (NOW + timedelta(days=1)).isoformat(),
        },
    }
    payload.update(changes)
    return payload


def _write_pin(tmp_path: Path, payload, *, mode: int = 0o600) -> Path:
    pin_dir = tmp_path / "operator-pins"
    pin_dir.mkdir(exist_ok=True)
    path = pin_dir / "candidate-pin.json"
    if isinstance(payload, (bytes, str)):
        data = payload.encode() if isinstance(payload, str) else payload
        path.write_bytes(data)
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, mode)
    return path


def _load(path, source_root, tmp_path):
    return load_candidate_identity_pin(
        path,
        source_root=source_root,
        runtime_root=tmp_path / "runtime",
        now=NOW,
    )


def _boot(candidate, tmp_path, pin_load, *, record_dir="identity", **overrides):
    source_root, manifest, fingerprint, master, child = candidate
    kwargs = dict(
        source_root=source_root,
        identity_dir=tmp_path / record_dir,
        candidate_manifest=manifest,
        candidate_pin_load=pin_load,
        worker_node_identity=NODE,
        registered_tasks=EXPECTED_TASK_REGISTRATIONS,
        configuration_fingerprint=fingerprint,
        process_identity=master,
        child_identities=(child,),
        boot_timestamp=NOW,
    )
    kwargs.update(overrides)
    return create_worker_boot_identity(**kwargs)


# 1, 8: valid operator pin is verified and persisted in the boot manifest.
def test_valid_operator_pin_verifies_boot_and_is_persisted(candidate, tmp_path):
    source_root, manifest, *_ = candidate
    pin_path = _write_pin(tmp_path, _pin_payload(manifest))
    pin_load = _load(pin_path, source_root, tmp_path)
    assert pin_load.status == PIN_LOADED

    path, boot = _boot(candidate, tmp_path, pin_load)

    assert boot.identity_status == IDENTITY_VERIFIED
    assert boot.candidate_pin_status == PIN_MATCHED
    assert boot.identity_status_reasons == ()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["candidate_pin_status"] == PIN_MATCHED
    identity = raw["candidate_pin_identity"]
    assert identity["candidate_head"] == manifest.candidate_head
    assert identity["candidate_tree"] == manifest.candidate_tree
    assert identity["source_manifest_digest"] == manifest.source_manifest_digest
    assert identity["approval_id"] == "approval-0001"
    assert len(identity["pin_sha256"]) == 64
    assert "argv" not in json.dumps(identity)
    reloaded = WorkerBootIdentityManifest.from_dict(raw)
    assert reloaded.candidate_pin_status == PIN_MATCHED
    assert reloaded.candidate_pin_identity == identity


# 2: missing pin keeps the existing inconclusive semantics.
@pytest.mark.parametrize("configured", ["", None, "relative/pin.json", "missing"])
def test_missing_pin_remains_inconclusive(candidate, tmp_path, configured):
    source_root, *_ = candidate
    if configured == "missing":
        configured = str(tmp_path / "operator-pins" / "absent.json")
    pin_load = _load(configured, source_root, tmp_path)
    assert pin_load.pin is None

    _path, boot = _boot(candidate, tmp_path, pin_load)

    assert boot.identity_status == IDENTITY_INCONCLUSIVE
    assert boot.candidate_pin_status in {PIN_ABSENT, PIN_INVALID}
    assert boot.candidate_pin_identity is None
    assert boot.identity_status_reasons


def test_unpinned_boot_without_loader_is_inconclusive(candidate, tmp_path):
    _path, boot = _boot(candidate, tmp_path, None)
    assert boot.identity_status == IDENTITY_INCONCLUSIVE
    assert boot.candidate_pin_status == PIN_ABSENT
    assert "candidate_pin_unavailable" in boot.identity_status_reasons


# 3: malformed or unsafe pins never become an expectation.
@pytest.mark.parametrize(
    "case, expected_reason",
    [
        ("not_json", "candidate_pin_malformed"),
        ("unknown_field", "candidate_pin_unknown_fields"),
        ("missing_field", "candidate_pin_missing_fields"),
        ("bad_schema", "candidate_pin_unsupported_schema"),
        ("bad_head", "candidate_pin_revision_invalid"),
        ("expired", "candidate_pin_approval_expired"),
        ("approval_extra", "candidate_pin_approval_invalid"),
        ("launcher_extra", "candidate_pin_launcher_invalid"),
        ("group_writable", "candidate_pin_permissions_unsafe"),
        ("inside_source", "candidate_pin_path_not_independent"),
        ("inside_task_runtime", "candidate_pin_path_not_independent"),
        ("symlink", "candidate_pin_unreadable"),
        ("too_large", "candidate_pin_too_large"),
    ],
)
def test_malformed_or_unsafe_pin_is_invalid_and_inconclusive(
    candidate, tmp_path, case, expected_reason
):
    source_root, manifest, *_ = candidate
    payload = _pin_payload(manifest)
    mode = 0o600
    if case == "not_json":
        payload = "{not json " + CANARY
    elif case == "unknown_field":
        payload["synthetic_override"] = CANARY
    elif case == "missing_field":
        del payload["candidate_tree"]
    elif case == "bad_schema":
        payload["pin_schema_version"] = 2
    elif case == "bad_head":
        payload["candidate_head"] = CANARY
    elif case == "expired":
        payload["approval"]["expires_at"] = (NOW - timedelta(minutes=1)).isoformat()
    elif case == "approval_extra":
        payload["approval"]["token"] = CANARY
    elif case == "launcher_extra":
        payload["launcher_identity"][CANARY] = "x"
    elif case == "group_writable":
        mode = 0o664
    elif case == "too_large":
        payload["configuration_policy_id"] = "x" * (20 * 1024)
    pin_path = _write_pin(tmp_path, payload, mode=mode)
    if case == "inside_source":
        inside = source_root / "candidate-pin.json"
        inside.write_bytes(pin_path.read_bytes())
        os.chmod(inside, 0o600)
        pin_path = inside
    elif case == "inside_task_runtime":
        inside = tmp_path / "runtime" / "tasks" / "1" / "2" / "pin.json"
        inside.parent.mkdir(parents=True)
        inside.write_bytes(pin_path.read_bytes())
        os.chmod(inside, 0o600)
        pin_path = inside
    elif case == "symlink":
        link = tmp_path / "operator-pins" / "linked-pin.json"
        link.symlink_to(pin_path)
        pin_path = link

    pin_load = _load(pin_path, source_root, tmp_path)

    assert pin_load.status == PIN_INVALID
    assert pin_load.pin is None
    assert pin_load.reasons == (expected_reason,)
    _path, boot = _boot(candidate, tmp_path, pin_load)
    assert boot.identity_status == IDENTITY_INCONCLUSIVE
    assert boot.candidate_pin_status == PIN_INVALID
    assert CANARY not in json.dumps(boot.to_dict())


def test_incomplete_configuration_identity_is_inconclusive(candidate, tmp_path):
    source_root, manifest, *_ = candidate
    payload = _pin_payload(manifest)
    del payload["configuration_fingerprint"]
    pin_load = _load(_write_pin(tmp_path, payload), source_root, tmp_path)
    assert pin_load.status == PIN_LOADED

    _path, boot = _boot(candidate, tmp_path, pin_load)

    assert boot.identity_status == IDENTITY_INCONCLUSIVE
    assert boot.candidate_pin_status == PIN_INCOMPLETE
    assert "approved_configuration_digest_unavailable" in boot.identity_status_reasons


# 4, 5, 6: observed identity contradicting the pin is a recorded mismatch.
@pytest.mark.parametrize(
    "field, value, reason",
    [
        ("candidate_head", "1" * 40, "boot_pinned_candidate_head_mismatch"),
        ("candidate_tree", "2" * 40, "boot_pinned_candidate_tree_mismatch"),
        (
            "source_manifest_digest",
            "3" * 64,
            "boot_pinned_source_manifest_mismatch",
        ),
        (
            "configuration_fingerprint",
            "4" * 64,
            "approved_configuration_boot_digest_mismatch",
        ),
        ("worker_node_identity", "celery@other", "boot_worker_node_identity_mismatch"),
    ],
)
def test_pin_mismatch_is_recorded_as_mismatch(
    candidate, tmp_path, field, value, reason
):
    source_root, manifest, *_ = candidate
    pin_load = _load(
        _write_pin(tmp_path, _pin_payload(manifest, **{field: value})),
        source_root,
        tmp_path,
    )
    assert pin_load.status == PIN_LOADED

    path, boot = _boot(candidate, tmp_path, pin_load)

    assert path.exists()
    assert boot.identity_status == IDENTITY_MISMATCH
    assert boot.candidate_pin_status == PIN_MISMATCHED
    assert reason in boot.identity_status_reasons
    # The record describes the observed checkout, never the pin's values.
    assert boot.candidate_head == manifest.candidate_head
    assert boot.source_manifest_digest == manifest.source_manifest_digest


def test_pin_is_never_synthesized_from_the_running_checkout(candidate, tmp_path):
    source_root, manifest, *_ = candidate
    other = _pin_payload(manifest, candidate_head="5" * 40)
    pin_load = _load(_write_pin(tmp_path, other), source_root, tmp_path)
    _path, boot = _boot(candidate, tmp_path, pin_load)
    assert boot.candidate_pin_identity["candidate_head"] == "5" * 40
    assert boot.identity_status == IDENTITY_MISMATCH


def test_unloaded_affected_module_keeps_pinned_boot_inconclusive(
    candidate, tmp_path, monkeypatch
):
    source_root, manifest, *_ = candidate
    monkeypatch.setattr(
        worker_identity,
        "capture_loaded_module_provenance",
        lambda _candidate: ({"module_name": "app.lazy", "loaded": False},),
    )
    pin_load = _load(
        _write_pin(tmp_path, _pin_payload(manifest)), source_root, tmp_path
    )
    _path, boot = _boot(candidate, tmp_path, pin_load)
    assert boot.identity_status == IDENTITY_INCONCLUSIVE
    assert "affected_module_not_loaded_at_boot" in boot.identity_status_reasons


def test_unverified_evaluator_keeps_pinned_boot_inconclusive(
    candidate, tmp_path, monkeypatch
):
    source_root, manifest, *_ = candidate
    monkeypatch.setattr(
        worker_identity,
        "capture_loaded_module_provenance",
        lambda _candidate: _boot_provenance("IDENTITY_INCONCLUSIVE"),
    )
    pin_load = _load(
        _write_pin(tmp_path, _pin_payload(manifest)), source_root, tmp_path
    )
    _path, boot = _boot(candidate, tmp_path, pin_load)
    assert boot.identity_status == IDENTITY_INCONCLUSIVE
    assert "evaluator_identity_unverified" in boot.identity_status_reasons


def test_preload_imports_only_unloaded_modules_and_reports_failures():
    result = worker_identity.preload_affected_modules_for_worker(
        ("json", "app.services.orchestration.phase39b_absent_module")
    )
    assert result == {
        "preloaded": [],
        "failed": ["app.services.orchestration.phase39b_absent_module"],
    }


def test_observed_checkout_failure_still_aborts_boot_record(
    candidate, tmp_path, monkeypatch
):
    source_root, manifest, *_ = candidate
    monkeypatch.setattr(
        worker_identity,
        "verify_candidate_source_manifest",
        lambda *args, **kwargs: worker_identity.CandidateManifestVerification(
            IDENTITY_INCONCLUSIVE, ("dirty_source",)
        ),
    )
    pin_load = _load(
        _write_pin(tmp_path, _pin_payload(manifest)), source_root, tmp_path
    )
    with pytest.raises(worker_identity.WorkerIdentityError):
        _boot(candidate, tmp_path, pin_load)
    assert not (tmp_path / "identity").exists()


# 7: production startup reads the configured operator pin in the master only.
def test_worker_ready_consumes_configured_operator_pin(monkeypatch, tmp_path):
    from app.services.session import recovery_coordinator
    from app.tasks import worker as worker_tasks

    source_root = tmp_path / "source"
    runtime_root = tmp_path / "runtime"
    source_root.mkdir()
    runtime_root.mkdir()
    pin_path = tmp_path / "operator-pins" / "candidate-pin.json"
    monkeypatch.setattr(worker_tasks.settings, "WORKER_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr(worker_tasks.settings, "RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setattr(
        worker_tasks.settings, "WORKER_CANDIDATE_PIN_PATH", str(pin_path)
    )
    loads, creates = [], []
    sentinel = SimpleNamespace(status=PIN_LOADED, pin=object())

    def fake_load(path, **kwargs):
        loads.append((path, kwargs))
        return sentinel

    def fake_create(**kwargs):
        creates.append(kwargs)
        return (
            kwargs["identity_dir"] / "manifest.json",
            SimpleNamespace(
                record_id="record",
                identity_status=IDENTITY_VERIFIED,
                candidate_pin_status=PIN_MATCHED,
            ),
        )

    monkeypatch.setattr(worker_tasks, "_load_candidate_identity_pin", fake_load)
    monkeypatch.setattr(worker_tasks, "_create_worker_boot_identity", fake_create)
    monkeypatch.setattr(
        worker_tasks, "_collect_worker_configuration", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        worker_tasks,
        "get_db_session",
        lambda: SimpleNamespace(close=lambda: None),
    )
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

    worker_tasks.on_worker_ready(
        SimpleNamespace(app=SimpleNamespace(conf={}), hostname="celery@test")
    )

    assert loads == [
        (
            str(pin_path),
            {"source_root": source_root, "runtime_root": str(runtime_root)},
        )
    ]
    assert creates[0]["candidate_pin_load"] is sentinel


def test_worker_init_preloads_affected_modules(monkeypatch):
    from app.tasks import worker as worker_tasks

    calls = []
    monkeypatch.setattr(
        worker_tasks,
        "_preload_evaluator_module_for_worker",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        worker_tasks,
        "_preload_affected_modules_for_worker",
        lambda: calls.append("preload") or {"preloaded": [], "failed": []},
    )
    worker_tasks.preload_worker_evaluator_provenance()
    assert calls == ["preload"]


def test_unpinned_and_historical_manifests_still_load(candidate, tmp_path):
    _path, boot = _boot(candidate, tmp_path, None)
    legacy = boot.to_dict()
    for key in (
        "candidate_pin_status",
        "candidate_pin_identity",
        "identity_status_reasons",
    ):
        legacy.pop(key)
    restored = WorkerBootIdentityManifest.from_dict(legacy)
    assert restored.candidate_pin_status is None
    assert restored.candidate_pin_identity is None
    assert restored.identity_status_reasons == ()


def test_boot_manifest_cannot_be_overwritten_after_boot(candidate, tmp_path):
    source_root, manifest, *_ = candidate
    pin_load = _load(
        _write_pin(tmp_path, _pin_payload(manifest)), source_root, tmp_path
    )
    path, _boot_record = _boot(candidate, tmp_path, pin_load)
    original = path.read_bytes()
    with pytest.raises(worker_identity.ManifestWriteError):
        _boot(candidate, tmp_path, None)
    assert path.read_bytes() == original


# 9, 10, 11: classification follows the real boot record for the child.
def _projection_for(boot, child, *, source=None, retry=0, request_id=None):
    return build_invocation_projection(
        project_id=17,
        task_id=29,
        session_id=31,
        task_execution_id=43,
        celery_request_id=request_id or str(uuid.uuid4()),
        celery_retry_count=retry,
        delivery_redelivered=False,
        invocation_started_at=NOW,
        worker_boot_manifest=boot.to_dict(),
        worker_boot_manifest_digest="d" * 64,
        worker_process=child.to_dict(),
        current_source_manifest=source
        or {
            "candidate_head": boot.candidate_head,
            "candidate_tree": boot.candidate_tree,
            "source_manifest_digest": boot.source_manifest_digest,
        },
        current_loaded_modules=[
            {"module_name": name, "source_sha256": MODULE_DIGEST, "loaded": True}
            for name in REQUIRED_LOADED_MODULES
        ],
    )


def test_pinned_boot_correlates_child_invocation_and_keeps_master_distinct(
    candidate, tmp_path
):
    source_root, manifest, _fingerprint, master, child = candidate
    pin_load = _load(
        _write_pin(tmp_path, _pin_payload(manifest)), source_root, tmp_path
    )
    _path, boot = _boot(candidate, tmp_path, pin_load)

    projection = _projection_for(boot, child)

    assert projection["classification"] == CORRELATED
    assert projection["classification_reasons"] == []
    process = projection["worker_process_identity"]
    assert process["pid"] == child.pid
    assert process["ppid"] == master.pid
    assert projection["worker_boot_manifest_id"] == boot.record_id
    assert projection["source_identity"]["loaded_code_identity_status"] == (
        "UNAVAILABLE"
    )


@pytest.mark.parametrize(
    "pin_change, expected",
    [
        (None, INCONCLUSIVE),
        ({"candidate_tree": "2" * 40}, MISMATCH),
    ],
)
def test_invocation_classification_follows_boot_pin_result(
    candidate, tmp_path, pin_change, expected
):
    source_root, manifest, _fingerprint, _master, child = candidate
    if pin_change is None:
        pin_load = _load("", source_root, tmp_path)
    else:
        pin_load = _load(
            _write_pin(tmp_path, _pin_payload(manifest, **pin_change)),
            source_root,
            tmp_path,
        )
    _path, boot = _boot(candidate, tmp_path, pin_load)
    assert _projection_for(boot, child)["classification"] == expected


def test_pinned_boot_with_source_drift_at_invocation_is_mismatch(candidate, tmp_path):
    source_root, manifest, _fingerprint, _master, child = candidate
    pin_load = _load(
        _write_pin(tmp_path, _pin_payload(manifest)), source_root, tmp_path
    )
    _path, boot = _boot(candidate, tmp_path, pin_load)
    drifted = {
        "candidate_head": "9" * 40,
        "candidate_tree": boot.candidate_tree,
        "source_manifest_digest": boot.source_manifest_digest,
    }
    assert _projection_for(boot, child, source=drifted)["classification"] == MISMATCH


def test_retry_after_pinned_boot_gets_new_attempt_and_stays_correlated(
    candidate, tmp_path
):
    source_root, manifest, _fingerprint, _master, child = candidate
    pin_load = _load(
        _write_pin(tmp_path, _pin_payload(manifest)), source_root, tmp_path
    )
    _path, boot = _boot(candidate, tmp_path, pin_load)
    project_dir = tmp_path / "project"
    request_id = str(uuid.uuid4())
    first = persist_invocation_projection(
        project_dir=project_dir,
        project_id=17,
        session_id=31,
        task_id=29,
        projection=_projection_for(boot, child, request_id=request_id),
    )
    retry = persist_invocation_projection(
        project_dir=project_dir,
        project_id=17,
        session_id=31,
        task_id=29,
        projection=_projection_for(boot, child, retry=1, request_id=request_id),
    )
    assert (first.attempt_number, retry.attempt_number) == (1, 2)
    assert first.invocation_attempt_id != retry.invocation_attempt_id
    assert {first.classification, retry.classification} == {CORRELATED}


# 12, 13, 14: v2 diagnostics keep only a validated compact reference.
def _reference(classification=CORRELATED) -> InvocationProvenanceReference:
    invocation_id = uuid.uuid4().hex
    return InvocationProvenanceReference(
        invocation_attempt_id=invocation_id,
        projection_journal_reference=invocation_id,
        worker_boot_manifest_id="boot-pin-401-4001",
        worker_boot_manifest_digest="d" * 64,
        attempt_number=1,
        classification=classification,
    )


class _CapturingDb:
    def __init__(self):
        self.entries = []

    def add(self, entry):
        self.entries.append(entry)

    def commit(self):
        return None

    def rollback(self):
        return None


def _log_service():
    service = object.__new__(OpenClawSessionService)
    service.db = _CapturingDb()
    service.session_id = 41
    service.task_id = 42
    service.task_execution_id = 43
    service.session_model = None
    service.task_model = None
    return service


@pytest.mark.parametrize("classification", [CORRELATED, INCONCLUSIVE, MISMATCH])
def test_v2_diagnostic_retains_bound_invocation_reference(classification):
    reference = _reference(classification)
    service = _log_service()
    token = bind_invocation_reference(reference)
    try:
        assert service._log_openclaw_diagnostic(
            OpenClawDiagnosticEvent.PROCESS_RESULT,
            {
                "return_code": 0,
                "diagnostic_category": "provider_success",
                "stdout_tail": CANARY,
                "stderr_tail": CANARY,
                "assistant_response": CANARY,
                "provider_payload": {"messages": [CANARY]},
                "error": f"RuntimeError: {CANARY}",
            },
        )
    finally:
        reset_invocation_reference(token)

    (entry,) = service.db.entries
    metadata = json.loads(entry.log_metadata)
    assert metadata["diagnostic_schema_version"] == 2
    assert metadata["content_omitted"] is True
    assert metadata["invocation_provenance"] == reference.to_dict()
    assert CANARY not in entry.message
    assert CANARY not in entry.log_metadata
    for key in ("stdout_tail", "stderr_tail", "assistant_response", "error"):
        assert key not in metadata


def test_v2_diagnostic_without_bound_reference_is_unchanged():
    service = _log_service()
    assert service._log_openclaw_diagnostic(
        OpenClawDiagnosticEvent.PROCESS_RESULT, {"return_code": 0}
    )
    metadata = json.loads(service.db.entries[0].log_metadata)
    assert "invocation_provenance" not in metadata
    assert metadata == build_openclaw_diagnostic_metadata(
        OpenClawDiagnosticEvent.PROCESS_RESULT, {"return_code": 0}
    )


def _with_reference(reference):
    return {
        "diagnostic_schema_version": 2,
        "content_omitted": True,
        "diagnostic_event": "openclaw_process_result",
        "invocation_provenance": reference,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda ref: ref.update(stdout_tail=CANARY),
        lambda ref: ref.pop("classification"),
        lambda ref: ref.update(classification="TRUSTED"),
        lambda ref: ref.update(schema_version=2),
        lambda ref: ref.update(invocation_attempt_id=CANARY),
        lambda ref: ref.update(projection_journal_reference=uuid.uuid4().hex),
        lambda ref: ref.update(projection_journal_reference=None),
        lambda ref: ref.update(worker_boot_manifest_digest=CANARY),
        lambda ref: ref.update(attempt_number=0),
        lambda ref: ref.update(worker_boot_manifest_id={"nested": CANARY}),
    ],
)
def test_invalid_v2_reference_is_rejected(mutate):
    reference = _reference().to_dict()
    mutate(reference)
    assert validate_invocation_reference(reference) is None
    normalized = normalize_openclaw_diagnostic_metadata(_with_reference(reference))
    assert "invocation_provenance" not in normalized
    assert CANARY not in json.dumps(normalized)


def test_non_mapping_v2_reference_is_rejected():
    normalized = normalize_openclaw_diagnostic_metadata(_with_reference(CANARY))
    assert "invocation_provenance" not in normalized


# 12, 17: project-log serialization keeps validated references and history.
class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def join(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self.rows)


def _stream(metadata: str, message: str):
    row = SimpleNamespace(
        id=7,
        session_id=41,
        task_id=42,
        task_execution_id=43,
        level="INFO",
        message=message,
        log_metadata=metadata,
        created_at=datetime.now(UTC),
        session_instance_id="f2be8165-00a1-44ee-a956-da5fec04743a",
    )

    class Db:
        def query(self, *entities):
            if entities[0] is LogEntry:
                return _Query([row])
            if entities[0] is SessionModel.id:
                return _Query([(41,)])
            if entities[0] is Project:
                return _Query([])
            raise AssertionError(entities)

    return next(LogStreamService(Db()).stream_logs(session_id=41))


def test_project_log_serialization_retains_valid_reference_only():
    reference = _reference().to_dict()
    streamed = _stream(json.dumps(_with_reference(reference)), CANARY)
    assert streamed["metadata"]["invocation_provenance"] == reference
    assert CANARY not in json.dumps(streamed)

    bad = dict(reference, response_excerpt=CANARY)
    streamed_bad = _stream(json.dumps(_with_reference(bad)), "message")
    assert "invocation_provenance" not in streamed_bad["metadata"]
    assert CANARY not in json.dumps(streamed_bad)


def test_historical_rows_serialize_unchanged():
    legacy_v2 = {
        "diagnostic_schema_version": 2,
        "content_omitted": True,
        "diagnostic_event": "openclaw_process_result",
        "runtime_status": "completed",
    }
    streamed = _stream(
        json.dumps(legacy_v2), "[OPENCLAW][PROCESS_RESULT] content omitted"
    )
    assert streamed["metadata"] == legacy_v2
    historical = {"stderr_tail": "synthetic historical value"}
    streamed_historical = _stream(json.dumps(historical), "Historical log message")
    assert streamed_historical["metadata"] == historical
    assert streamed_historical["message"] == "Historical log message"
