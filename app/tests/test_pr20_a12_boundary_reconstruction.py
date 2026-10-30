"""Offline characterizations of the PR20-A11 identity and verdict boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.v1.endpoints import tasks as task_endpoints
from app.models import TaskCheckpoint, TaskStatus
from app.services.orchestration import worker_identity
from app.services.orchestration.state import persistence
from app.services.orchestration.types import ValidationVerdict
from app.tasks import worker as worker_tasks
from scripts import dev_runtime


def test_development_runtime_default_layout_is_disjoint_and_requires_pin_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    config = dev_runtime.RuntimeConfig.for_repo(repo_root)
    runtime_root = config.root / "runtime"
    runtime_root.mkdir(parents=True)

    assert config.root != repo_root / ".dev-runtime" / "phase40b-r1"
    assert repo_root not in config.root.parents
    assert config.root not in repo_root.parents
    assert config.repo_root.resolve() == repo_root.resolve()
    paths = worker_identity.resolve_worker_identity_paths(
        config.repo_root, runtime_root
    )
    assert paths.source_root == repo_root.resolve()
    assert paths.runtime_root == runtime_root.resolve()

    monkeypatch.delenv(dev_runtime.ACCEPTANCE_VERIFICATION_OPT_IN_ENV, raising=False)
    env = dev_runtime.build_environment(config, secret_key="test-only")
    pin = worker_identity.load_candidate_identity_pin(
        env["WORKER_CANDIDATE_PIN_PATH"],
        source_root=env["WORKER_SOURCE_ROOT"],
        runtime_root=env["RUNTIME_ROOT"],
    )
    assert pin.status == worker_identity.PIN_ABSENT


def test_missing_boot_manifest_becomes_inconclusive_invocation_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "checkout"
    source_root.mkdir()
    runtime_root = source_root / ".dev-runtime" / "phase40b-r1" / "runtime"
    runtime_root.mkdir(parents=True)
    monkeypatch.setattr(worker_tasks.settings, "WORKER_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr(worker_tasks.settings, "RUNTIME_ROOT", str(runtime_root))

    evidence = worker_tasks._invocation_worker_evidence()
    assert evidence[0] is None
    assert evidence[1] is None
    assert evidence[3] is None
    assert evidence[4] is None

    projection = worker_tasks._build_invocation_projection(
        project_id=45,
        task_id=52,
        session_id=43,
        task_execution_id=52,
        celery_request_id="offline-characterization",
        celery_retry_count=0,
        delivery_redelivered=False,
        invocation_started_at=datetime.now(UTC),
        worker_boot_manifest=evidence[0],
        worker_boot_manifest_digest=evidence[1],
        worker_process=evidence[2],
        current_source_manifest=evidence[3],
        current_loaded_modules=evidence[4],
    )
    assert projection["classification"] == "INCONCLUSIVE"
    assert projection["worker_boot_manifest_id"] is None
    assert projection["worker_boot_manifest_digest"] is None


def test_launcher_reports_responsive_but_identity_unverified_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    config = dev_runtime.RuntimeConfig.for_repo(repo_root)
    config.root.mkdir(parents=True)
    dev_runtime._owner_path(config).touch()

    state = {
        "status": "ready",
        "processes": {
            name: {"pid": index}
            for index, name in enumerate(("api", "redis", "worker"), start=1)
        },
        "task_acceptance_verification": {"configured": "true", "opt_in_value": "1"},
    }
    monkeypatch.setattr(dev_runtime, "_require_runtime_root", lambda _config: None)
    monkeypatch.setattr(dev_runtime, "_owner_record", lambda _config: {})
    monkeypatch.setattr(dev_runtime, "_load_state", lambda _config, _owner: state)
    monkeypatch.setattr(
        dev_runtime, "_process_identity", lambda _record: ("owned", "fixture")
    )
    monkeypatch.setattr(dev_runtime, "_proc_environ_value", lambda *_args: "true")
    monkeypatch.setattr(dev_runtime, "_api_ready", lambda _config: True)
    monkeypatch.setattr(dev_runtime, "_redis_ping", lambda _port: True)
    monkeypatch.setattr(dev_runtime, "_worker_ping", lambda *_args: True)
    monkeypatch.setattr(
        dev_runtime,
        "_worker_identity_report",
        lambda *_args: {
            "worker_identity_verified": False,
            "identity_status": "IDENTITY_INCONCLUSIVE",
            "manifest_id": None,
            "manifest_digest": None,
            "reasons": ["candidate_pin_unavailable"],
        },
    )
    monkeypatch.setattr(dev_runtime, "_qdrant_ready", lambda _config: False)
    monkeypatch.setattr(dev_runtime, "build_environment", lambda _config: {})
    monkeypatch.setattr(dev_runtime, "provider_summary", lambda: "offline fixture")

    report = dev_runtime.status_report(config)
    assert report["status"] == "ready"
    assert report["process_ready"] is True
    assert report["worker_responsive"] is True
    assert report["worker_identity_verified"] is False
    assert report["worker_ready"] is False
    assert report["worker_identity"]["reasons"] == ["candidate_pin_unavailable"]


def test_controlled_api_dispatch_rejects_unverified_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        task_endpoints.settings, "CONTROLLED_WORKER_IDENTITY_REQUIRED", True
    )
    monkeypatch.setattr(
        task_endpoints,
        "_current_controlled_worker_identity_binding",
        lambda: (_ for _ in ()).throw(
            task_endpoints.ControlledWorkerIdentityError("identity unavailable")
        ),
    )

    with pytest.raises(
        Exception, match="controlled Worker identity verification failed"
    ):
        task_endpoints._attach_controlled_worker_identity(None)


def test_validation_event_drops_details_that_checkpoint_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_events: list[dict[str, object]] = []
    checkpoints: list[TaskCheckpoint] = []
    db = SimpleNamespace(add=checkpoints.append)
    state = SimpleNamespace(validation_history=[], project_dir=tmp_path)
    acceptance = {
        "section_found": True,
        "extracted": ["python -m pytest -q"],
        "admitted": ["python -m pytest -q"],
        "refused": [],
        "ignored": [],
        "results": [
            {
                "command": "python -m pytest -q",
                "executed": True,
                "returncode": 0,
                "outcome": "passed",
            }
        ],
    }
    monkeypatch.setattr(persistence, "control_state_of", lambda _state: tmp_path)
    monkeypatch.setattr(
        persistence,
        "append_orchestration_event",
        lambda **kwargs: captured_events.append(kwargs["details"]) or {"event_id": "1"},
    )
    monkeypatch.setattr(
        persistence, "write_orchestration_state_snapshot", lambda **_kwargs: {}
    )
    verdict = ValidationVerdict(
        stage="task_completion",
        status="accepted",
        profile="mutation",
        details={"task_acceptance_verification": acceptance},
    )

    persistence.record_validation_verdict(
        db,
        session_id=43,
        task_id=52,
        orchestration_state=state,
        verdict=verdict,
    )

    stored = json.loads(checkpoints[0].state_snapshot)
    assert stored["details"]["task_acceptance_verification"] == acceptance
    assert "task_acceptance_verification" not in captured_events[0]
    assert state.validation_history[0]["details"]["task_acceptance_verification"] == (
        acceptance
    )
