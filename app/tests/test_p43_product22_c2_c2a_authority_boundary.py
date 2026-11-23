"""Offline C2-C2A authority and disposable Worker boundary contracts."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.orchestration.worker_boundary import (
    DISPOSABLE_WORKER_QUEUE,
    DISPOSABLE_WORKER_TASK_ALLOWLIST,
    WorkerBoundaryViolation,
    WorkerTaskAdmissionError,
    model_environment_authority_violations,
    validate_disposable_task_admission,
    validate_structured_http_provider_contract,
    validate_worker_authority_paths,
)


def _allowlist() -> str:
    return ",".join(sorted(DISPOSABLE_WORKER_TASK_ALLOWLIST))


def _authority_paths(tmp_path):
    return {
        "database": tmp_path / "db",
        "runtime": tmp_path / "runtime",
        "checkpoints": tmp_path / "checkpoints",
        "identity": tmp_path / "identity",
        "workspace": tmp_path / "authoritative-workspace",
    }


def test_model_environment_cannot_receive_worker_authority_inputs():
    violations = model_environment_authority_violations(
        {
            "CELERY_BROKER_URL": "redis://worker-only:6379/9",
            "DATABASE_URL": "sqlite:////worker-only/orchestrator.db",
            "WORKER_CANDIDATE_PIN_PATH": "/worker-only/pin.json",
            "OPENAI_BASE_URL": "http://model-provider:11434/v1",
        }
    )

    assert violations == (
        "CELERY_BROKER_URL",
        "DATABASE_URL",
        "WORKER_CANDIDATE_PIN_PATH",
    )


def test_worker_authority_paths_are_disjoint_from_model_workspace(tmp_path):
    paths = _authority_paths(tmp_path)
    validate_worker_authority_paths(
        application_root=tmp_path / "release",
        authority_paths=paths,
        model_workspace_root=tmp_path / "model-workspace",
    )

    with pytest.raises(WorkerBoundaryViolation, match="model workspace overlaps"):
        validate_worker_authority_paths(
            application_root=tmp_path / "release",
            authority_paths=paths,
            model_workspace_root=paths["workspace"],
        )


def test_worker_authority_records_cannot_share_the_model_application_root(tmp_path):
    paths = _authority_paths(tmp_path)
    with pytest.raises(WorkerBoundaryViolation, match="application root"):
        validate_worker_authority_paths(
            application_root=paths["workspace"],
            authority_paths=paths,
        )


def test_unauthorized_celery_task_names_are_rejected():
    with pytest.raises(WorkerTaskAdmissionError, match="not admitted"):
        validate_disposable_task_admission(
            task_name="app.tasks.maintenance.cleanup_old_logs",
            queue_name=DISPOSABLE_WORKER_QUEUE,
            isolation_mode="disposable",
            task_allowlist=_allowlist(),
        )


def test_default_and_live_queues_are_rejected_for_disposable_worker():
    for queue in ("celery", "orchestrator-live", ""):
        with pytest.raises(
            WorkerTaskAdmissionError, match="disposable execution queue"
        ):
            validate_disposable_task_admission(
                task_name="app.tasks.worker.execute_orchestration_task",
                queue_name=queue,
                isolation_mode="disposable",
                task_allowlist=_allowlist(),
            )


def test_celery_publication_guard_rejects_unauthorized_task_and_queue(monkeypatch):
    celery_module = importlib.import_module("app.celery_app")
    monkeypatch.setenv("WORKER_ISOLATION_MODE", "disposable")
    monkeypatch.setenv("CELERY_QUEUE_NAME", DISPOSABLE_WORKER_QUEUE)
    monkeypatch.setenv("WORKER_TASK_ALLOWLIST", _allowlist())

    with pytest.raises(WorkerTaskAdmissionError, match="not admitted"):
        celery_module._validate_disposable_publication(
            task_name="app.tasks.github_tasks.process_github_push_event",
            queue_name=DISPOSABLE_WORKER_QUEUE,
        )
    with pytest.raises(WorkerTaskAdmissionError, match="disposable execution queue"):
        celery_module._validate_disposable_publication(
            task_name="app.tasks.worker.execute_orchestration_task",
            queue_name="celery",
        )


def test_disposable_publication_guard_keeps_existing_lifecycle_inert(monkeypatch):
    celery_module = importlib.import_module("app.celery_app")
    monkeypatch.delenv("WORKER_ISOLATION_MODE", raising=False)

    # The new guard is opt-in to the disposable image contract. Existing
    # normal TaskExecution publication remains governed by its current gate.
    celery_module._validate_disposable_publication(
        task_name="app.tasks.maintenance.cleanup_old_logs",
        queue_name="celery",
    )


def test_stale_worker_identity_is_rejected_before_disposable_task_run(monkeypatch):
    celery_module = importlib.import_module("app.celery_app")
    controlled_identity = importlib.import_module(
        "app.services.orchestration.controlled_worker_identity"
    )
    monkeypatch.setenv("WORKER_ISOLATION_MODE", "disposable")
    monkeypatch.setenv("CELERY_QUEUE_NAME", DISPOSABLE_WORKER_QUEUE)
    monkeypatch.setenv("WORKER_TASK_ALLOWLIST", _allowlist())
    monkeypatch.setattr(settings, "CONTROLLED_WORKER_IDENTITY_REQUIRED", True)
    monkeypatch.setattr(
        celery_module, "_validate_disposable_runtime_contract", lambda: None
    )
    monkeypatch.setattr(
        controlled_identity,
        "verify_controlled_worker_identity",
        lambda **_kwargs: SimpleNamespace(
            identity_verified=False,
            reasons=("worker_boot_manifest_unavailable_or_stale",),
        ),
    )

    with pytest.raises(WorkerBoundaryViolation, match="stale"):
        celery_module._admit_disposable_task_before_run(
            task=SimpleNamespace(
                name="app.tasks.worker.execute_orchestration_task",
                request=SimpleNamespace(
                    delivery_info={"routing_key": DISPOSABLE_WORKER_QUEUE}
                ),
            )
        )


@pytest.mark.parametrize(
    "backend",
    ["local_openclaw", "remote_openclaw_gateway", "claude_code", "codex"],
)
def test_structured_provider_contract_fails_closed(backend):
    with pytest.raises(WorkerBoundaryViolation, match="structured HTTP"):
        validate_structured_http_provider_contract(
            provider_name="direct_openai_compatible",
            backend_values={"EXECUTION_BACKEND": backend},
        )


def test_structured_provider_contract_accepts_only_selected_http_topology():
    validate_structured_http_provider_contract(
        provider_name="direct_openai_compatible",
        backend_values={
            "AGENT_BACKEND": "direct_ollama",
            "PLANNING_BACKEND": "openai_chat_completions",
            "EXECUTION_BACKEND": "direct_ollama",
        },
    )
