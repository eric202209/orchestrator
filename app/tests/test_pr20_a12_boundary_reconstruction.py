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


def test_development_runtime_default_layout_overlaps_worker_source_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    config = dev_runtime.RuntimeConfig.for_repo(repo_root)
    runtime_root = config.root / "runtime"
    runtime_root.mkdir(parents=True)

    assert config.root == repo_root / ".dev-runtime" / "phase40b-r1"
    assert config.repo_root.resolve() == repo_root.resolve()
    with pytest.raises(
        worker_identity.WorkerIdentityPathError,
        match="source/runtime root overlap",
    ):
        worker_identity.resolve_worker_identity_paths(config.repo_root, runtime_root)

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


def test_launcher_readiness_uses_worker_ping_without_boot_identity(
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
    monkeypatch.setattr(dev_runtime, "_qdrant_ready", lambda _config: False)
    monkeypatch.setattr(dev_runtime, "build_environment", lambda _config: {})
    monkeypatch.setattr(dev_runtime, "provider_summary", lambda: "offline fixture")

    report = dev_runtime.status_report(config)
    assert report["status"] == "ready"
    assert report["worker_ready"] is True
    assert "worker_boot_manifest_id" not in report
    assert "worker_identity_verified" not in report


def test_api_execute_route_queues_without_worker_attestation_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = SimpleNamespace(
        id=52,
        status=TaskStatus.PENDING,
        title="offline task",
        description="offline acceptance task",
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        task_endpoints,
        "require_task_runtime_eligibility",
        lambda _db, _task_id, _user: task,
    )

    def fake_queue(_db, queued_task, *, retry_request, prompt_override):
        calls.append(
            {
                "task_id": queued_task.id,
                "prompt": prompt_override,
                "session_id": retry_request.session_id,
            }
        )
        return {
            "task_id": queued_task.id,
            "session_id": 43,
            "task_execution_id": 52,
            "celery_task_id": "offline-only",
        }

    monkeypatch.setattr(task_endpoints, "_queue_task_retry", fake_queue)
    response = task_endpoints.queue_task_with_canonical_execution(
        task_id=52,
        payload=task_endpoints.DirectExecuteRequest(),
        db=object(),
        current_user=object(),
    )

    assert response["status"] == "queued"
    assert calls == [{"task_id": 52, "prompt": task.description, "session_id": None}]


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
