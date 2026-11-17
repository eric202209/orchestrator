"""P42-V17-R provider-free Review Hold enforcement regressions."""

import json
from pathlib import Path

from app.models import Task, TaskCheckpoint, TaskExecutionChangeSet, TaskStatus
from app.services.orchestration.phases.completion_flow import (
    _resolve_template_review_policy,
)
from app.services.orchestration.validation.candidate_checks import (
    candidate_delta_identity,
)
from app.services.tasks.service import TaskService
from app.tests.test_phase36_maint_gr7_candidate_semantic_verification_authority import (
    SUBSTANTIVE_PASS,
    _Runtime,
    _accepted,
    _seed_auto_publish,
)
from app.tests.test_completion_verification_regressions import (
    _seed_legacy_finalize_ctx,
)


def _seed_held_candidate(db_session, tmp_path, monkeypatch):
    result, ctx, project_root = _seed_auto_publish(
        db_session,
        tmp_path,
        monkeypatch,
        _Runtime({"output": SUBSTANTIVE_PASS}),
        _accepted(),
        template_id="dependency-config-change",
        existing_mutable_paths=("README.md",),
    )
    ctx.project.user_id = 1
    change_set = TaskService(db_session).get_task_execution_change_set(
        task_execution_id=ctx.task_execution_id
    )
    candidate_identity = candidate_delta_identity(
        change_set, project_dir=Path(change_set["artifact_path"])
    )
    db_session.add(
        TaskCheckpoint(
            task_id=ctx.task_id,
            session_id=ctx.session_id,
            checkpoint_type="validation_task_completion",
            state_snapshot=json.dumps(
                {
                    "stage": "task_completion",
                    "status": "accepted",
                    "candidate_identity": candidate_identity,
                }
            ),
        )
    )
    db_session.commit()
    return result, ctx, project_root


def test_p42_v17r_operator_acceptance_publishes_held_candidate_once(
    authenticated_client, db_session, tmp_path, monkeypatch
):
    result, ctx, project_root = _seed_held_candidate(db_session, tmp_path, monkeypatch)

    assert result["status"] == "completed"
    assert not (project_root / "README.md").exists()

    accepted = authenticated_client.post(
        f"/api/v1/tasks/{ctx.task_id}/change-set/accept",
        json={"task_execution_id": ctx.task_execution_id},
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["accepted"] is True
    assert (project_root / "README.md").read_text(encoding="utf-8") == "after\n"

    record = (
        db_session.query(TaskExecutionChangeSet)
        .filter(TaskExecutionChangeSet.task_execution_id == ctx.task_execution_id)
        .one()
    )
    assert record.disposition == "promoted"

    replay = authenticated_client.post(
        f"/api/v1/tasks/{ctx.task_id}/change-set/accept",
        json={"task_execution_id": ctx.task_execution_id},
    )
    assert replay.status_code == 409


def test_p42_v17r_rejection_restores_without_publication(
    authenticated_client, db_session, tmp_path, monkeypatch
):
    result, ctx, project_root = _seed_held_candidate(db_session, tmp_path, monkeypatch)

    assert result["status"] == "completed"
    assert not (project_root / "README.md").exists()

    rejected = authenticated_client.post(
        f"/api/v1/tasks/{ctx.task_id}/change-set/reject",
        json={
            "task_execution_id": ctx.task_execution_id,
            "note": "operator rejected candidate",
        },
    )

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["rejected"] is True
    assert (project_root / "README.md").read_text(encoding="utf-8") == "before\n"

    record = (
        db_session.query(TaskExecutionChangeSet)
        .filter(TaskExecutionChangeSet.task_execution_id == ctx.task_execution_id)
        .one()
    )
    assert record.disposition == "rejected"


def test_p42_v17r_review_template_survives_retry_state_transition(db_session, tmp_path):
    ctx, _execution, _project_root, _workspace_dir = _seed_legacy_finalize_ctx(
        db_session, tmp_path
    )
    ctx.task.template_id = "dependency-config-change"
    db_session.commit()

    for status in (TaskStatus.FAILED, TaskStatus.RUNNING):
        ctx.task.status = status
        db_session.commit()
        db_session.expire_all()
        recovered_task = db_session.get(Task, ctx.task_id)
        policy = _resolve_template_review_policy(recovered_task)
        assert policy["auto_promote_eligible"] is False
        assert policy["hold_if"] == ["always"]


def test_p42_v17r_template_resolution_exception_fails_closed(monkeypatch):
    class FakeTask:
        template_id = "dependency-config-change"

    def _raise(_template_id):
        raise RuntimeError("template store unavailable")

    monkeypatch.setattr(
        "app.services.orchestration.workflow_templates.get_template", _raise
    )

    policy = _resolve_template_review_policy(FakeTask())
    assert policy["auto_promote_eligible"] is False
    assert policy["hold_if"] == ["always"]
