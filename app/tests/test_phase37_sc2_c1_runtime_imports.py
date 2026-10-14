"""Phase 37 SC2-C1 runtime import regressions (real imports, sqlite only)."""

from __future__ import annotations

import importlib

import pytest

from app.models import ExecutionTaskRuntimeLease
from app.tasks import worker as worker_tasks

from test_phase29c2_execution_eligibility import _build_context
from test_phase29c3_scheduler_claim import _ready_root
from test_phase29c5_runtime_ownership import _submitted

FORMER_EXPORT_TARGETS = (
    "input_manifest",
    "operator_review",
    "operator_review_persistence",
    "plan_commit_service",
    "planner_service",
    "planning_brief",
    "planning_brief_stage",
    "planning_session_service",
    "protocol_persistence",
    "providers",
    "structured_task_plan",
    "structured_task_plan_stage",
)


def test_planning_package_has_no_lazy_exports():
    package = importlib.import_module("app.services.planning")
    for name in ("__getattr__", "_EXPORTS", "__all__", "PlanCommitService"):
        assert not hasattr(package, name), name


@pytest.mark.parametrize("module", FORMER_EXPORT_TARGETS)
def test_former_export_target_modules_import_concretely(module):
    imported = importlib.import_module(f"app.services.planning.{module}")
    assert imported.__name__ == f"app.services.planning.{module}"


def test_worker_dispatch_task_is_registered():
    task = worker_tasks.receive_execution_task_dispatch
    assert task.name == "app.tasks.worker.receive_execution_task_dispatch"
    assert worker_tasks.celery_app.tasks[task.name].name == task.name
    assert task.acks_late is True
    assert task.reject_on_worker_lost is True


def test_worker_entry_rejects_tampered_payload_via_ownership_error(
    monkeypatch, db_session
):
    context = _build_context(db_session)
    task = _ready_root(context)
    _, _, created = _submitted(db_session, task)
    monkeypatch.setattr(worker_tasks, "get_db_session", lambda: db_session)
    payload = dict(created.intent.worker_command_payload)
    payload["execution_task_id"] = task.id + 1

    result = worker_tasks.receive_execution_task_dispatch.__wrapped__(payload)

    assert result == {"status": "rejected", "error_code": "worker_payload_mismatch"}
    assert db_session.query(ExecutionTaskRuntimeLease).count() == 0
