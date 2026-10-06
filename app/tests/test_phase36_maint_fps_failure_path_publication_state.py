"""PHASE36-MAINT FPS — failure-path ChangeSet publication state.

Historical ChangeSets 306-308/310 (failed Planning) and 309 (evaluator-held
Candidate) all persist ``outcome=auto_promote`` and
``publication_eligible=true``.  The single writer is the worker's terminal
re-capture (``app/tasks/worker.py`` ``finally``): it re-ran the content-only
review policy, so a failed execution was projected as publication-eligible
and the authoritative post-evaluator Review projection was overwritten.

Publication authority was never derived from those fields: promotion requires
the accepted Plan authority plus an accepted completion validation bound to
the exact candidate identity.  These cases pin the repaired projection and
that authority is unchanged.  Isolated in-memory DB; no provider is called.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import (
    Project,
    Session as SessionModel,
    Task,
    TaskCheckpoint,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
)
from app.services.orchestration.execution.runtime import workspace_snapshot_key
from app.services.tasks.service import TaskService
from app.api.v1.endpoints.tasks import CHANGE_SET_EXECUTION_NOT_COMPLETED
from app.services.workspace.changeset_service import EXECUTION_NOT_COMPLETED_OUTCOME
from app.tests.test_phase36_maint_gr7_candidate_semantic_verification_authority import (
    _Runtime,
    _accepted,
    _seed_auto_publish,
)
from app.tests.test_project_baseline_regressions import _seed_publication_evidence


def _seed(db_session, tmp_path: Path, *, status: TaskStatus, changed: bool = True):
    root = tmp_path / "fps-product"
    root.mkdir(parents=True)
    project = Project(name="fps-product", workspace_path=str(root))
    db_session.add(project)
    db_session.flush()
    task = Task(
        project_id=project.id,
        title="FPS",
        description="FPS",
        status=status,
        workspace_status="ready" if status == TaskStatus.DONE else "not_created",
    )
    session = SessionModel(project_id=project.id, name="fps-session")
    db_session.add_all([task, session])
    db_session.commit()
    execution = TaskExecution(
        session_id=session.id, task_id=task.id, attempt_number=1, status=status
    )
    db_session.add(execution)
    db_session.commit()
    service = TaskService(db_session)
    key = workspace_snapshot_key(task.id, execution.id)
    (root / "README.md").write_text("before\n", encoding="utf-8")
    service.create_workspace_snapshot(
        project, root, snapshot_key=key, preserve_project_root_rules=True
    )
    if changed:
        (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return project, task, session, execution, service, root, key


def _worker_recapture(service, project, task, session, execution, root, key):
    """The exact argument shape of the worker's terminal ``finally`` capture."""

    return service.persist_task_execution_change_set(
        project,
        task,
        session_id=session.id,
        task_execution_id=execution.id,
        snapshot_key=key,
        target_dir=root,
        preserve_project_root_rules=True,
        status=task.status.value,
        commit=True,
        preserve_review_decision=True,
    )


def _record(db_session, execution_id: int) -> TaskExecutionChangeSet:
    db_session.expire_all()
    return (
        db_session.query(TaskExecutionChangeSet)
        .filter(TaskExecutionChangeSet.task_execution_id == execution_id)
        .one()
    )


def _accept(client, task, execution):
    return client.post(
        f"/api/v1/tasks/{task.id}/change-set/accept",
        json={"task_execution_id": execution.id},
    )


def _assert_not_publishable(decision: dict) -> None:
    assert decision["outcome"] == EXECUTION_NOT_COMPLETED_OUTCOME
    assert decision["publication_eligible"] is False
    assert decision["held_for_review"] is False
    assert decision["reason"] == "task_execution_not_completed"


# --- R1/R3/R9: Planning failure, zero files, policy alone ------------------


@pytest.mark.parametrize("changed", [False, True], ids=["zero_file", "with_files"])
def test_r1_r3_r9_planning_failure_is_not_publishable(
    authenticated_client, db_session, tmp_path, changed
):
    project, task, session, execution, service, root, key = _seed(
        db_session, tmp_path, status=TaskStatus.FAILED, changed=changed
    )
    _worker_recapture(service, project, task, session, execution, root, key)

    decision = _record(db_session, execution.id).review_decision
    _assert_not_publishable(decision)
    # R9: the configured policy is retained, but only as configuration.
    assert decision["configured_outcome"] == "auto_promote"
    assert decision["configured_publication_eligible"] is True
    assert decision["execution_status"] == "failed"

    # Manual accept fails closed before any write (PSC: a not-completed
    # execution's change set is never releasable; the authority loader's own
    # rejection is pinned on completed executions in the PSC suite).
    response = _accept(authenticated_client, task, execution)
    assert response.status_code == 409
    assert CHANGE_SET_EXECUTION_NOT_COMPLETED in response.json()["detail"]
    assert _record(db_session, execution.id).disposition == "captured"


# --- R2/R4: execution or verification failure after an accepted Plan -------


@pytest.mark.parametrize("completion", ["absent", "rejected"])
def test_r2_r4_unvalidated_candidate_cannot_be_accepted(
    authenticated_client, db_session, tmp_path, completion
):
    project, task, session, execution, service, root, key = _seed(
        db_session, tmp_path, status=TaskStatus.FAILED
    )
    _worker_recapture(service, project, task, session, execution, root, key)
    _seed_publication_evidence(
        db_session,
        project,
        task,
        execution,
        service.get_task_execution_change_set(task_execution_id=execution.id),
    )
    checkpoint = (
        db_session.query(TaskCheckpoint)
        .filter(TaskCheckpoint.checkpoint_type == "validation_task_completion")
        .one()
    )
    if completion == "absent":
        db_session.delete(checkpoint)
    else:
        checkpoint.state_snapshot = checkpoint.state_snapshot.replace(
            '"accepted"', '"rejected"'
        )
    db_session.commit()

    _assert_not_publishable(_record(db_session, execution.id).review_decision)
    response = _accept(authenticated_client, task, execution)
    assert response.status_code == 409
    assert CHANGE_SET_EXECUTION_NOT_COMPLETED in response.json()["detail"]
    assert (root / "app.py").exists()
    assert _record(db_session, execution.id).disposition == "captured"


# --- R5: evaluator non-PASS survives the terminal re-capture ----------------


def test_r5_terminal_recapture_keeps_evaluator_hold(db_session, tmp_path, monkeypatch):
    result, ctx, _ = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, _Runtime({"output": ""}), _accepted()
    )
    assert result["status"] == "completed"
    # REENTRY-12/ChangeSet 309 shape: a source change with no warning flags.
    monkeypatch.setattr(
        "app.services.workspace.changeset_service.ChangesetService."
        "change_set_warning_flags",
        lambda self, **kwargs: [],
    )
    TaskService(db_session).persist_task_execution_change_set(
        ctx.project,
        ctx.task,
        session_id=ctx.session_id,
        task_execution_id=ctx.task_execution_id,
        snapshot_key=workspace_snapshot_key(ctx.task_id, ctx.task_execution_id),
        target_dir=Path(ctx.orchestration_state.project_dir),
        preserve_project_root_rules=ctx.runs_in_canonical_baseline,
        status="done",
        commit=True,
        preserve_review_decision=True,
    )

    decision = _record(db_session, ctx.task_execution_id).review_decision
    assert decision["outcome"] == "hold_for_review"
    assert decision["reason"] == "evaluator_assessment_unavailable"
    assert decision["evaluator_verdict"] == "UNKNOWN"
    assert decision["publication_eligible"] is False


# --- R6/R7/R8/R10: pending, rejected, reload --------------------------------


def test_r6_r10_pending_evaluator_marker_survives_failed_recapture(
    authenticated_client, db_session, tmp_path
):
    project, task, session, execution, service, root, key = _seed(
        db_session, tmp_path, status=TaskStatus.FAILED
    )
    _worker_recapture(service, project, task, session, execution, root, key)
    record = _record(db_session, execution.id)
    # The worker died while the evaluator was running (EVPS pending marker).
    record.review_decision = {
        **record.review_decision,
        "outcome": "hold_for_review",
        "held_for_review": True,
        "evaluator_pending": True,
        "publication_eligible": False,
    }
    db_session.commit()

    # R10: a later terminal re-capture / reload keeps it fail-closed.
    for _ in range(2):
        _worker_recapture(service, project, task, session, execution, root, key)
    decision = _record(db_session, execution.id).review_decision
    assert decision["evaluator_pending"] is True
    _assert_not_publishable(decision)
    response = _accept(authenticated_client, task, execution)
    assert response.status_code == 409
    assert "Evaluator assessment is still pending" in response.json()["detail"]


def test_r7_r8_rejected_change_set_cannot_be_accepted(
    authenticated_client, db_session, tmp_path
):
    project, task, session, execution, service, root, key = _seed(
        db_session, tmp_path, status=TaskStatus.DONE
    )
    _worker_recapture(service, project, task, session, execution, root, key)
    service.mark_task_execution_change_set_disposition(
        task_execution_id=execution.id, disposition="rejected", reason="no"
    )

    response = _accept(authenticated_client, task, execution)
    assert response.status_code == 409
    assert "already been rejected" in response.json()["detail"]


# --- Positive: a validated, completed candidate still publishes -------------


def test_positive_completed_validated_candidate_is_published(
    authenticated_client, db_session, tmp_path
):
    project, task, session, execution, service, root, key = _seed(
        db_session, tmp_path, status=TaskStatus.DONE
    )
    service.persist_task_execution_change_set(
        project,
        task,
        session_id=session.id,
        task_execution_id=execution.id,
        snapshot_key=key,
        target_dir=root,
        status=TaskStatus.DONE.value,
    )
    _seed_publication_evidence(
        db_session,
        project,
        task,
        execution,
        service.get_task_execution_change_set(task_execution_id=execution.id),
    )
    first = dict(_record(db_session, execution.id).review_decision)
    _worker_recapture(service, project, task, session, execution, root, key)
    assert _record(db_session, execution.id).review_decision == first
    assert first["publication_eligible"] is True

    (root / "app.py").unlink()
    response = _accept(authenticated_client, task, execution)
    assert response.status_code == 200, response.json()
    assert response.json()["accepted"] is True
    assert (root / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _record(db_session, execution.id).disposition == "promoted"


def test_changed_candidate_recapture_is_recomputed_not_preserved(db_session, tmp_path):
    project, task, session, execution, service, root, key = _seed(
        db_session, tmp_path, status=TaskStatus.DONE
    )
    _worker_recapture(service, project, task, session, execution, root, key)
    record = _record(db_session, execution.id)
    record.review_decision = {**record.review_decision, "reason": "stale_marker"}
    db_session.commit()
    (root / "other.py").write_text("X = 2\n", encoding="utf-8")

    _worker_recapture(service, project, task, session, execution, root, key)
    assert _record(db_session, execution.id).review_decision["reason"] != (
        "stale_marker"
    )
