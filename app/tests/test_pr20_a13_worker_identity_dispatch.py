"""Offline security regressions for the controlled Worker identity boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.services.orchestration import worker_identity
from app.services.orchestration import controlled_worker_identity
from scripts import dev_runtime


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
NODE = "phase40b-r1@identity-test"
FINGERPRINT = "f" * 64
ARGV = ("/venv/bin/celery", "-A", "app.celery_app", "worker")


def _pin_payload(candidate, launcher_identity):
    return {
        "pin_schema_version": worker_identity.CANDIDATE_PIN_SCHEMA_VERSION,
        "candidate_head": candidate.candidate_head,
        "candidate_tree": candidate.candidate_tree,
        "source_root": candidate.source_root,
        "source_manifest_digest": candidate.source_manifest_digest,
        "launcher_identity": dict(launcher_identity),
        "worker_node_identity": NODE,
        "configuration_schema_version": (
            worker_identity.CONFIGURATION_PROJECTION_SCHEMA_VERSION
        ),
        "configuration_fingerprint": FINGERPRINT,
        "configuration_policy_id": worker_identity.CONFIGURATION_POLICY_ID,
        "approval": {
            "operator_identity": "fixture-operator",
            "approval_id": "fixture-approval-001",
            "approved_at": (NOW - timedelta(hours=1)).isoformat(),
            "expires_at": (NOW + timedelta(days=1)).isoformat(),
        },
    }


@pytest.fixture
def identity_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source_root = tmp_path / "candidate-source"
    (source_root / "app").mkdir(parents=True)
    (source_root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (source_root / "app" / "worker.py").write_text(
        "VALUE = 'pinned candidate'\n", encoding="utf-8"
    )
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "fixture@example.invalid"],
        ["git", "config", "user.name", "A13 fixture"],
        ["git", "add", "app"],
        ["git", "commit", "-qm", "fixture candidate"],
    ):
        subprocess.run(args, cwd=source_root, check=True)

    launcher_identity = {
        "launcher": "celery-worker",
        "executable": "/venv/bin/python3",
        "cwd": str(source_root.resolve()),
        "argv": list(ARGV),
    }
    candidate = worker_identity.build_candidate_source_manifest(
        source_root,
        affected_modules=("app.worker",),
        launcher_identity=launcher_identity,
    )
    monkeypatch.setattr(
        worker_identity,
        "verify_candidate_source_manifest",
        lambda *_args, **_kwargs: worker_identity.CandidateManifestVerification(
            worker_identity.IDENTITY_VERIFIED, ()
        ),
    )
    runtime_root = tmp_path / "separate-runtime"
    identity_dir = runtime_root / worker_identity.WORKER_IDENTITY_DIRECTORY_NAME
    identity_dir.mkdir(parents=True)
    pin_path = tmp_path / "operator-approved" / "candidate-pin.json"
    pin_path.parent.mkdir()
    pin_path.write_text(
        json.dumps(_pin_payload(candidate, launcher_identity)), encoding="utf-8"
    )
    pin_path.chmod(0o600)
    pin_load = worker_identity.load_candidate_identity_pin(
        pin_path,
        source_root=source_root,
        runtime_root=runtime_root,
        now=NOW,
    )
    assert pin_load.status == worker_identity.PIN_LOADED

    process = worker_identity.ProcessIdentity(
        pid=424242,
        linux_boot_id="a13-fixture-boot",
        start_ticks="4242001",
        executable="/venv/bin/python3",
        cwd=str(source_root.resolve()),
        argv=ARGV,
        ppid=1,
        pgid=424242,
        sid=424242,
    )
    projection = {
        "schema_version": worker_identity.CONFIGURATION_PROJECTION_SCHEMA_VERSION,
        "projection_policy_id": worker_identity.CONFIGURATION_POLICY_ID,
        "identity_scope": worker_identity.IDENTITY_SCOPE,
        "invocation_identity": worker_identity.INVOCATION_IDENTITY,
        "invocation_identity_reason": worker_identity.INVOCATION_IDENTITY_REASON,
    }
    record_id = f"{process.linux_boot_id}-{process.pid}-{process.start_ticks}"
    manifest = {
        "schema_version": worker_identity.SCHEMA_VERSION,
        "candidate_head": candidate.candidate_head,
        "candidate_tree": candidate.candidate_tree,
        "candidate_source_root": str(source_root.resolve()),
        "source_manifest_digest": candidate.source_manifest_digest,
        "boot_timestamp": NOW.isoformat(),
        "linux_boot_id": process.linux_boot_id,
        "worker_pid": process.pid,
        "worker_start_ticks": process.start_ticks,
        "worker_executable": process.executable,
        "worker_cwd": process.cwd,
        "worker_argv": list(process.argv),
        "worker_node_identity": NODE,
        "expected_task_registrations": [],
        "registered_task_registrations": [],
        "worker_children": [],
        "affected_module_provenance": [{"module_name": "app.worker", "loaded": True}],
        "configuration_fingerprint": FINGERPRINT,
        "configuration_projection": projection,
        "identity_status": worker_identity.IDENTITY_VERIFIED,
        "record_id": record_id,
        "configuration_schema_version": (
            worker_identity.CONFIGURATION_PROJECTION_SCHEMA_VERSION
        ),
        "configuration_policy_id": worker_identity.CONFIGURATION_POLICY_ID,
        "identity_scope": worker_identity.IDENTITY_SCOPE,
        "invocation_identity": worker_identity.INVOCATION_IDENTITY,
        "invocation_identity_reason": worker_identity.INVOCATION_IDENTITY_REASON,
        "candidate_pin_status": worker_identity.PIN_MATCHED,
        "candidate_pin_identity": dict(pin_load.identity),
        "identity_status_reasons": [],
    }
    manifest_path = identity_dir / f"worker-{record_id}.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)
    monkeypatch.setattr(
        worker_identity,
        "capture_process_identity",
        lambda pid=None: process if pid in (None, process.pid) else None,
    )
    return SimpleNamespace(
        source_root=source_root,
        runtime_root=runtime_root,
        identity_dir=identity_dir,
        pin_path=pin_path,
        pin_load=pin_load,
        process=process,
        manifest=manifest,
        manifest_path=manifest_path,
    )


def test_dev_runtime_root_is_external_and_exact(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    config = dev_runtime.RuntimeConfig.for_repo(repo)

    assert config.root != repo / ".dev-runtime" / "phase40b-r1"
    assert repo not in config.root.parents
    assert config.root not in repo.parents
    dev_runtime._require_runtime_root(config)

    substituted = dev_runtime.RuntimeConfig(
        repo_root=repo,
        root=tmp_path / "unrelated-runtime",
    )
    with pytest.raises(dev_runtime.RuntimeToolError, match="trusted.*runtime root"):
        dev_runtime._require_runtime_root(substituted)


def test_runtime_symlink_alias_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    trusted = dev_runtime.RuntimeConfig.for_repo(repo)
    alias = tmp_path / "runtime-alias"
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(trusted.root, target_is_directory=True)
    config = dev_runtime.RuntimeConfig(repo_root=repo, root=alias)

    with pytest.raises(dev_runtime.RuntimeToolError, match="symlink|canonical"):
        dev_runtime._require_runtime_root(config)


def test_runtime_contract_rejects_overlapping_source_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    overlapping = repo / "runtime"
    monkeypatch.setattr(dev_runtime, "_trusted_runtime_root", lambda _repo: overlapping)
    substituted = dev_runtime.RuntimeConfig(repo_root=repo, root=overlapping)

    with pytest.raises(dev_runtime.RuntimeToolError, match="overlaps"):
        dev_runtime._require_runtime_root(substituted)

    nested_source = overlapping / "source"
    nested_source.mkdir(parents=True)
    with pytest.raises(
        worker_identity.WorkerIdentityPathError,
        match="source/runtime root overlap",
    ):
        worker_identity.resolve_worker_identity_paths(nested_source, overlapping)


def test_source_symlink_alias_uses_canonical_checkout_root(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    alias = tmp_path / "checkout-alias"
    alias.symlink_to(repo, target_is_directory=True)

    config = dev_runtime.RuntimeConfig.for_repo(alias)

    assert config.repo_root == repo.resolve()
    assert config.root == dev_runtime._trusted_runtime_root(repo.resolve())
    assert repo not in config.root.parents


def test_stale_owner_contract_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    config = dev_runtime.RuntimeConfig.for_repo(repo)
    config.root.mkdir(parents=True)
    dev_runtime._owner_path(config).write_text(
        json.dumps(
            {
                "schema": dev_runtime.SCHEMA_VERSION,
                "instance_id": "old-owner",
                "repo_root": str(repo),
                "runtime_root": str(config.root),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(dev_runtime.RuntimeToolError, match="ownership evidence"):
        dev_runtime._owner_record(config)


def test_preflight_refuses_missing_operator_candidate_pin(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    config = dev_runtime.RuntimeConfig.for_repo(repo)

    with pytest.raises(
        dev_runtime.RuntimeToolError,
        match="controlled Worker candidate pin refused: candidate_pin_unavailable",
    ):
        dev_runtime.preflight(config)


def test_cleanup_of_new_root_preserves_historical_a11_runtime(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    legacy = repo / ".dev-runtime" / "phase40b-r1"
    legacy.mkdir(parents=True)
    sentinel = legacy / "database" / "a11-preserved.txt"
    sentinel.parent.mkdir()
    sentinel.write_text("historical evidence", encoding="utf-8")

    config = dev_runtime.RuntimeConfig.for_repo(repo)
    dev_runtime._ensure_owner(config)
    dev_runtime._make_directories(config)
    result = dev_runtime.cleanup_runtime(config)

    assert result["status"] == "cleaned"
    assert sentinel.read_text(encoding="utf-8") == "historical evidence"


def test_candidate_pin_must_be_outside_entire_controlled_runtime(
    identity_fixture, tmp_path: Path
) -> None:
    inside = identity_fixture.runtime_root / "operator-pin.json"
    inside.write_bytes(identity_fixture.pin_path.read_bytes())
    inside.chmod(0o600)

    result = worker_identity.load_candidate_identity_pin(
        inside,
        source_root=identity_fixture.source_root,
        runtime_root=identity_fixture.runtime_root,
        require_outside_runtime=True,
        now=NOW,
    )

    assert result.status == worker_identity.PIN_INVALID
    assert "candidate_pin_path_not_independent" in result.reasons


@pytest.mark.parametrize(
    "path_kind, expected_reason",
    [
        ("empty", "candidate_pin_unavailable"),
        ("missing", "candidate_pin_missing"),
        ("stale", "candidate_pin_approval_expired"),
    ],
)
def test_candidate_pin_absent_or_stale_fails_closed(
    identity_fixture, path_kind: str, expected_reason: str
) -> None:
    fixture = identity_fixture
    path = "" if path_kind == "empty" else fixture.pin_path
    now = NOW + timedelta(days=2) if path_kind == "stale" else NOW
    if path_kind == "missing":
        fixture.pin_path.unlink()

    result = worker_identity.load_candidate_identity_pin(
        path,
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        require_outside_runtime=True,
        now=now,
    )

    assert result.status != worker_identity.PIN_LOADED
    assert expected_reason in result.reasons


def test_candidate_pin_path_and_file_permissions_fail_closed(identity_fixture) -> None:
    fixture = identity_fixture
    alias = fixture.pin_path.parent / "pin-alias.json"
    alias.symlink_to(fixture.pin_path)
    symlink_result = worker_identity.load_candidate_identity_pin(
        alias,
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        require_outside_runtime=True,
        now=NOW,
    )
    assert "candidate_pin_path_symlinked" in symlink_result.reasons

    fixture.pin_path.chmod(0o666)
    permissions_result = worker_identity.load_candidate_identity_pin(
        fixture.pin_path,
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        require_outside_runtime=True,
        now=NOW,
    )
    assert "candidate_pin_permissions_unsafe" in permissions_result.reasons

    fixture.pin_path.chmod(0o600)
    fixture.pin_path.parent.chmod(0o777)
    parent_result = worker_identity.load_candidate_identity_pin(
        fixture.pin_path,
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        require_outside_runtime=True,
        now=NOW,
    )
    assert "candidate_pin_parent_permissions_unsafe" in parent_result.reasons


def test_candidate_pin_source_mismatch_fails_closed(identity_fixture) -> None:
    fixture = identity_fixture
    payload = json.loads(fixture.pin_path.read_text(encoding="utf-8"))
    payload["source_root"] = str(fixture.runtime_root)
    fixture.pin_path.write_text(json.dumps(payload), encoding="utf-8")
    fixture.pin_path.chmod(0o600)

    result = controlled_worker_identity.verify_controlled_worker_identity(
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        pin_path=fixture.pin_path,
        expected_node_identity=NODE,
        now=NOW,
    )

    assert result.identity_verified is False
    assert "candidate_pin_source_root_mismatch" in result.reasons


def test_live_boot_identity_produces_binding_and_rejects_worker_restart(
    identity_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = identity_fixture
    result = controlled_worker_identity.verify_controlled_worker_identity(
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        pin_path=fixture.pin_path,
        expected_node_identity=NODE,
        now=NOW,
    )

    assert result.process_ready is True
    assert result.identity_verified is True, result.reasons
    assert result.binding["record_id"] == fixture.manifest["record_id"]

    restarted = dict(result.binding)
    restarted["worker_start_ticks"] = "4242002"
    mismatch = controlled_worker_identity.verify_controlled_worker_identity(
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        pin_path=fixture.pin_path,
        expected_node_identity=NODE,
        expected_binding=restarted,
        now=NOW,
    )
    assert mismatch.identity_verified is False
    assert "dispatch_worker_binding_mismatch" in mismatch.reasons

    monkeypatch.setattr(
        worker_identity,
        "capture_process_identity",
        lambda pid=None: replace(fixture.process, start_ticks="4242002"),
    )
    restarted_process = controlled_worker_identity.verify_controlled_worker_identity(
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        pin_path=fixture.pin_path,
        expected_node_identity=NODE,
        expected_binding=result.binding,
        now=NOW,
    )
    assert restarted_process.identity_verified is False
    assert restarted_process.process_ready is False
    assert "worker_process_identity_changed" in restarted_process.reasons


def test_manifest_replacement_pin_replacement_and_dirty_source_fail_closed(
    identity_fixture,
) -> None:
    fixture = identity_fixture
    original = controlled_worker_identity.verify_controlled_worker_identity(
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        pin_path=fixture.pin_path,
        expected_node_identity=NODE,
        now=NOW,
    )
    assert original.identity_verified, original.reasons

    fixture.manifest_path.write_text(
        json.dumps({**fixture.manifest, "identity_status_reasons": ["replaced"]}),
        encoding="utf-8",
    )
    replaced_manifest = controlled_worker_identity.verify_controlled_worker_identity(
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        pin_path=fixture.pin_path,
        expected_node_identity=NODE,
        expected_binding=original.binding,
        now=NOW,
    )
    assert replaced_manifest.identity_verified is False

    fixture.manifest_path.write_text(json.dumps(fixture.manifest), encoding="utf-8")
    payload = json.loads(fixture.pin_path.read_text(encoding="utf-8"))
    payload["approval"]["approval_id"] = "fixture-approval-replaced"
    fixture.pin_path.write_text(json.dumps(payload), encoding="utf-8")
    fixture.pin_path.chmod(0o600)
    replaced_pin = controlled_worker_identity.verify_controlled_worker_identity(
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        pin_path=fixture.pin_path,
        expected_node_identity=NODE,
        expected_binding=original.binding,
        now=NOW,
    )
    assert replaced_pin.identity_verified is False

    (fixture.source_root / "app" / "worker.py").write_text(
        "VALUE = 'changed after boot'\n", encoding="utf-8"
    )
    dirty_source = controlled_worker_identity.verify_controlled_worker_identity(
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        pin_path=fixture.pin_path,
        expected_node_identity=NODE,
        now=NOW,
    )
    assert dirty_source.identity_verified is False


def test_missing_manifest_is_not_identity_ready(identity_fixture) -> None:
    fixture = identity_fixture
    fixture.manifest_path.unlink()

    result = controlled_worker_identity.verify_controlled_worker_identity(
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        pin_path=fixture.pin_path,
        expected_node_identity=NODE,
        now=NOW,
    )

    assert result.process_ready is False
    assert result.identity_verified is False


def test_inconclusive_manifest_and_digest_mismatch_fail_closed(
    identity_fixture,
) -> None:
    fixture = identity_fixture
    fixture.manifest["identity_status"] = worker_identity.IDENTITY_INCONCLUSIVE
    fixture.manifest_path.write_text(json.dumps(fixture.manifest), encoding="utf-8")
    inconclusive = controlled_worker_identity.verify_controlled_worker_identity(
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        pin_path=fixture.pin_path,
        expected_node_identity=NODE,
        now=NOW,
    )
    assert inconclusive.process_ready is True
    assert inconclusive.identity_verified is False

    fixture.manifest["identity_status"] = worker_identity.IDENTITY_VERIFIED
    fixture.manifest_path.write_text(json.dumps(fixture.manifest), encoding="utf-8")
    verified = controlled_worker_identity.verify_controlled_worker_identity(
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        pin_path=fixture.pin_path,
        expected_node_identity=NODE,
        now=NOW,
    )
    wrong_digest = {**verified.binding, "manifest_sha256": "0" * 64}
    mismatch = controlled_worker_identity.verify_controlled_worker_identity(
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        pin_path=fixture.pin_path,
        expected_node_identity=NODE,
        expected_binding=wrong_digest,
        now=NOW,
    )
    assert mismatch.identity_verified is False
    assert "dispatch_worker_binding_mismatch" in mismatch.reasons


def test_api_dispatch_identity_binding_is_opt_in_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.v1.endpoints import tasks as task_endpoints

    calls: list[str] = []
    monkeypatch.setattr(
        task_endpoints.settings, "CONTROLLED_WORKER_IDENTITY_REQUIRED", False
    )
    monkeypatch.setattr(
        task_endpoints,
        "_current_controlled_worker_identity_binding",
        lambda: calls.append("checked") or {"record_id": "fixture"},
    )
    assert task_endpoints._attach_controlled_worker_identity(
        {"planner_contract": {}}
    ) == {"planner_contract": {}}
    assert calls == []

    monkeypatch.setattr(
        task_endpoints.settings, "CONTROLLED_WORKER_IDENTITY_REQUIRED", True
    )
    bound = task_endpoints._attach_controlled_worker_identity({"planner_contract": {}})
    assert bound[controlled_worker_identity.CONTROLLED_WORKER_IDENTITY_CONTEXT_KEY] == {
        "record_id": "fixture"
    }

    def unavailable():
        raise controlled_worker_identity.ControlledWorkerIdentityError("no identity")

    monkeypatch.setattr(
        task_endpoints, "_current_controlled_worker_identity_binding", unavailable
    )
    with pytest.raises(Exception, match="controlled Worker identity"):
        task_endpoints._attach_controlled_worker_identity(None)


def test_api_binding_requires_the_configured_worker_to_pong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.v1.endpoints import tasks as task_endpoints
    from app.celery_app import celery_app

    evidence = controlled_worker_identity.ControlledWorkerIdentityEvidence(
        process_ready=True,
        identity_verified=True,
        identity_status=worker_identity.IDENTITY_VERIFIED,
        binding={"record_id": "fixture-worker-boot"},
        manifest=SimpleNamespace(worker_pid=424242),
    )
    monkeypatch.setattr(
        task_endpoints.settings, "WORKER_SOURCE_ROOT", "/fixture/source"
    )
    monkeypatch.setattr(
        task_endpoints.settings, "RUNTIME_ROOT", "/fixture/runtime/runtime"
    )
    monkeypatch.setattr(
        task_endpoints.settings, "WORKER_CANDIDATE_PIN_PATH", "/trusted/pin.json"
    )
    monkeypatch.setattr(
        task_endpoints.settings,
        "WORKER_CANDIDATE_PIN_PROTECTED_ROOT",
        "/fixture/runtime",
    )
    monkeypatch.setattr(
        task_endpoints.settings, "CONTROLLED_WORKER_NODE_IDENTITY", NODE
    )
    monkeypatch.setattr(
        controlled_worker_identity,
        "verify_controlled_worker_identity",
        lambda **_kwargs: evidence,
    )
    ping_calls: list[dict[str, object]] = []

    def inspect(*, destination, timeout):
        ping_calls.append({"destination": destination, "timeout": timeout})
        return SimpleNamespace(ping=lambda: {NODE: {"ok": "pong"}})

    monkeypatch.setattr(celery_app.control, "inspect", inspect)

    binding = task_endpoints._current_controlled_worker_identity_binding()

    assert binding == {"record_id": "fixture-worker-boot"}
    assert ping_calls == [{"destination": [NODE], "timeout": 1.0}]


def test_celery_publish_boundary_rechecks_binding_and_preserves_default_policy(
    identity_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import celery_app as celery_module

    fixture = identity_fixture
    evidence = controlled_worker_identity.verify_controlled_worker_identity(
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        pin_path=fixture.pin_path,
        expected_node_identity=NODE,
        now=NOW,
    )
    monkeypatch.setattr(
        celery_module.settings, "CONTROLLED_WORKER_IDENTITY_REQUIRED", False
    )
    celery_module._require_controlled_worker_identity_for_publish(
        sender=celery_module.CONTROLLED_EXECUTION_TASK_NAME,
        body=((), {}, {}),
    )

    monkeypatch.setattr(
        celery_module.settings, "CONTROLLED_WORKER_IDENTITY_REQUIRED", True
    )
    monkeypatch.setattr(
        celery_module.settings, "WORKER_SOURCE_ROOT", str(fixture.source_root)
    )
    monkeypatch.setattr(
        celery_module.settings, "RUNTIME_ROOT", str(fixture.runtime_root)
    )
    monkeypatch.setattr(
        celery_module.settings, "WORKER_CANDIDATE_PIN_PATH", str(fixture.pin_path)
    )
    monkeypatch.setattr(
        celery_module.settings,
        "WORKER_CANDIDATE_PIN_PROTECTED_ROOT",
        str(fixture.runtime_root),
    )
    monkeypatch.setattr(celery_module.settings, "CONTROLLED_WORKER_NODE_IDENTITY", NODE)
    monkeypatch.setattr(
        celery_module.celery_app.control,
        "inspect",
        lambda **_kwargs: SimpleNamespace(ping=lambda: {NODE: {"ok": "pong"}}),
    )
    body = (
        (),
        {
            "context": {
                controlled_worker_identity.CONTROLLED_WORKER_IDENTITY_CONTEXT_KEY: dict(
                    evidence.binding
                )
            }
        },
        {},
    )

    celery_module._require_controlled_worker_identity_for_publish(
        sender=celery_module.CONTROLLED_EXECUTION_TASK_NAME,
        body=body,
    )
    unbound_body = ((), {}, {})
    celery_module._require_controlled_worker_identity_for_publish(
        sender=celery_module.CONTROLLED_EXECUTION_TASK_NAME,
        body=unbound_body,
    )
    assert (
        unbound_body[1]["context"][
            controlled_worker_identity.CONTROLLED_WORKER_IDENTITY_CONTEXT_KEY
        ]
        == evidence.binding
    )

    changed_binding = {**evidence.binding, "worker_start_ticks": "stale"}
    stale_body = (
        (),
        {
            "context": {
                controlled_worker_identity.CONTROLLED_WORKER_IDENTITY_CONTEXT_KEY: changed_binding
            }
        },
        {},
    )
    with pytest.raises(RuntimeError, match="dispatch refused"):
        celery_module._require_controlled_worker_identity_for_publish(
            sender=celery_module.CONTROLLED_EXECUTION_TASK_NAME,
            body=stale_body,
        )

    def unavailable(**_kwargs):
        raise controlled_worker_identity.ControlledWorkerIdentityError(
            "candidate_pin_unavailable"
        )

    monkeypatch.setattr(
        controlled_worker_identity,
        "current_controlled_worker_identity_binding",
        unavailable,
    )
    with pytest.raises(RuntimeError, match="candidate_pin_unavailable"):
        celery_module._require_controlled_worker_identity_for_publish(
            sender=celery_module.CONTROLLED_EXECUTION_TASK_NAME,
            body=((), {}, {}),
        )


def test_one_shot_uses_the_same_controlled_identity_binding(
    identity_fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.tasks import worker as worker_tasks

    fixture = identity_fixture
    binding = controlled_worker_identity.verify_controlled_worker_identity(
        source_root=fixture.source_root,
        runtime_root=fixture.runtime_root,
        pin_path=fixture.pin_path,
        expected_node_identity=NODE,
        now=NOW,
    ).binding
    manifest = worker_identity.WorkerBootIdentityManifest.from_dict(fixture.manifest)
    verifier_calls: list[dict[str, object]] = []

    def verify(**kwargs):
        verifier_calls.append(kwargs)
        return controlled_worker_identity.ControlledWorkerIdentityEvidence(
            process_ready=True,
            identity_verified=True,
            identity_status=worker_identity.IDENTITY_VERIFIED,
            binding=binding,
            manifest=manifest,
            manifest_digest=binding["manifest_sha256"],
        )

    monkeypatch.setattr(
        worker_tasks.settings, "CONTROLLED_WORKER_IDENTITY_REQUIRED", True
    )
    monkeypatch.setattr(
        worker_tasks.settings, "WORKER_SOURCE_ROOT", str(fixture.source_root)
    )
    monkeypatch.setattr(
        worker_tasks.settings, "RUNTIME_ROOT", str(fixture.runtime_root)
    )
    monkeypatch.setattr(
        worker_tasks.settings, "WORKER_CANDIDATE_PIN_PATH", str(fixture.pin_path)
    )
    monkeypatch.setattr(
        worker_tasks.settings,
        "WORKER_CANDIDATE_PIN_PROTECTED_ROOT",
        str(fixture.runtime_root),
    )
    monkeypatch.setattr(worker_tasks.settings, "CONTROLLED_WORKER_NODE_IDENTITY", NODE)
    monkeypatch.setattr(worker_tasks, "verify_controlled_worker_identity", verify)

    attestation = worker_tasks._one_shot_worker_identity_attestation(binding)

    assert attestation is not None
    assert verifier_calls[0]["expected_binding"] == binding
    assert verifier_calls[0]["expected_pid"] == os.getpid()
