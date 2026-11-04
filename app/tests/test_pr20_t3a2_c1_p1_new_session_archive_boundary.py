"""PR20 T3-A2a C1-P1: new-Session request-changes archive/issuance boundary.

The repair-workspace archive must finish before any retry TaskExecution exists,
must not touch request-changes authority inputs, and must leave no publishable
attempt when it fails. Publication is recording-only; databases are offline.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models import Session as SessionModel
from app.models import Task, TaskExecution, TaskStatus
from app.services.execution.workflow_lifecycle import (
    dispatch_value_sha256,
    reconcile_task_execution_dispatches,
)
from app.services.tasks.service import TaskService
from app.services.workspace.baseline_promotion_service import (
    REQUESTED_CHANGES_ARCHIVE_ROOT,
)
from app.tests.test_pr20_t3a2_request_changes_boundary import (
    OPERATOR_DESCRIPTION,
    _add_prior_change_set,
    _create_task,
    _run_worker_completion_seam,
    _stub_retry_publication,
    _worker_seam_failure_diagnostics,
)

NOTE = "Review note."
NEW_SESSION = {
    "retry_intent": "request_changes",
    "create_new_session": True,
    "execution_scope": "new_session",
}
SAME_SESSION = {"retry_intent": "request_changes"}


def _review_task(authenticated_client, db_session, tmp_path: Path):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    prior = _add_prior_change_set(db_session, task)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": NOTE}
    )
    assert response.status_code == 200, response.text
    project_root = tmp_path / "project"
    return task, prior, project_root, project_root / "task-review"


def _archive_entries(project_root: Path) -> list[Path]:
    root = project_root / REQUESTED_CHANGES_ARCHIVE_ROOT
    return sorted(root.glob("*/*")) if root.exists() else []


def _fresh(db_session_factory, model, ident):
    observer = db_session_factory()
    try:
        row = observer.get(model, ident)
        if row is not None:
            observer.expunge(row)
        return row
    finally:
        observer.close()


def _executions(db_session_factory, task_id: int) -> list[TaskExecution]:
    observer = db_session_factory()
    try:
        rows = (
            observer.query(TaskExecution)
            .filter(TaskExecution.task_id == task_id)
            .order_by(TaskExecution.id)
            .all()
        )
        for row in rows:
            observer.expunge(row)
        return rows
    finally:
        observer.close()


def _beat(db_session_factory, *, task_execution_id=None, make_due=False):
    """Run the real reconciler with a recording-only publisher."""

    recorded = []

    def _publisher(*, kwargs, task_id, retries=0):
        recorded.append({"kwargs": dict(kwargs), "task_id": task_id})

    db = db_session_factory()
    try:
        if make_due and task_execution_id is not None:
            execution = db.get(TaskExecution, task_execution_id)
            execution.dispatch_next_attempt_at = datetime.now(timezone.utc).replace(
                tzinfo=None
            ) - timedelta(minutes=5)
            db.commit()
        result = reconcile_task_execution_dispatches(
            db, task_execution_id=task_execution_id, publisher=_publisher
        )
    finally:
        db.close()
    return result, recorded


def _assert_review_intact(task: Task, *, subfolder):
    assert task.status == TaskStatus.DONE
    assert task.workspace_status == "changes_requested"
    assert task.promotion_note == NOTE
    assert task.description == OPERATOR_DESCRIPTION
    assert task.task_subfolder == subfolder


# --------------------------------------------------------------------- positive


def test_original_defect_new_session_retry_now_publishes_with_intact_authority(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior, project_root, old_workspace = _review_task(
        authenticated_client, db_session, tmp_path
    )
    (old_workspace / "candidate.txt").write_text("old", encoding="utf-8")
    published = _stub_retry_publication(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["execution_scope"] == "isolated_session"
    assert body["session_id"] != prior.session_id
    assert body["repair_archive_result"]["archived"] is True
    archived = Path(body["repair_archive_result"]["archive_path"])
    assert (archived / "candidate.txt").read_text(encoding="utf-8") == "old"
    assert not old_workspace.exists()
    assert _archive_entries(project_root) == [archived]

    persisted = _fresh(db_session_factory, Task, task.id)
    assert persisted.workspace_status == "changes_requested"
    assert persisted.promotion_note == NOTE
    assert persisted.description == OPERATOR_DESCRIPTION
    assert persisted.status == TaskStatus.PENDING
    # This fixture Task runs in the canonical project root, so workspace
    # preparation allocates no replacement subfolder.
    assert persisted.task_subfolder is None
    assert project_root.is_dir()

    execution = _fresh(db_session_factory, TaskExecution, body["task_execution_id"])
    new_session = _fresh(db_session_factory, SessionModel, body["session_id"])
    assert new_session is not None and new_session.project_id == task.project_id
    assert execution.session_id == new_session.id
    assert execution.attempt_number == 1
    assert execution.dispatch_kind == "ROOT"
    assert execution.dispatch_parent_task_execution_id is None
    assert execution.dispatch_state == "READY"
    assert execution.dispatch_refusal_reason is None
    invocation = execution.dispatch_invocation
    assert execution.dispatch_invocation_sha256 == dispatch_value_sha256(invocation)
    assert invocation["prompt_source"] == "description"
    authority = invocation["acceptance_authority"]
    assert authority["acceptance_mode"] == "request_changes"
    assert authority["task_description_sha256"] == dispatch_value_sha256(
        persisted.description
    )
    assert authority["promotion_note_sha256"] == dispatch_value_sha256(
        persisted.promotion_note
    )
    fields = TaskService.request_changes_change_set_fields(
        TaskService(db_session).get_task_execution_change_set(
            task_execution_id=prior.task_execution_id
        )
    )
    assert authority["prior_change_set"] == {
        "change_set_id": prior.id,
        "task_execution_id": prior.task_execution_id,
        "bounded_fields_sha256": dispatch_value_sha256(fields),
    }
    assert published["prompt"] == OPERATOR_DESCRIPTION
    assert published["published_task_id"] == execution.dispatch_celery_task_id

    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / "worker-new-session",
        db_session_factory=db_session_factory,
        task=task,
        prompt=published["prompt"],
        context=None,
        dispatch_kind="ROOT",
        persisted_task_execution_id=execution.id,
        expected_attempt_number=1,
        celery_task_id=published["published_task_id"],
    )
    diagnostics = _worker_seam_failure_diagnostics(result)
    assert len(result.worker_contexts) == 1, diagnostics
    context = result.worker_contexts[0]
    assert context.acceptance_authority_status == "verified_request_changes"
    assert context.acceptance_task_prompt == OPERATOR_DESCRIPTION
    assert NOTE in context.prompt
    assert result.prior_change_set_reference == authority["prior_change_set"]
    assert result.provider_calls == [], diagnostics
    # Recording-only stub: only the Operator acceptance command is selected.
    assert len(result.executed_commands) == 1, diagnostics
    assert "test_operator.py" in result.executed_commands[0]


def test_archive_completes_before_any_retry_execution_exists_and_beat_sees_nothing(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior, _project_root, old_workspace = _review_task(
        authenticated_client, db_session, tmp_path
    )
    original_archive = TaskService.archive_task_workspace_for_repair_rerun
    observed = {}

    def _archive_with_beat_probe(self, project, archived_task, **kwargs):
        observed["executions_before"] = [
            row.id for row in _executions(db_session_factory, task.id)
        ]
        observed["beat_before"] = _beat(db_session_factory)
        result = original_archive(self, project, archived_task, **kwargs)
        observed["executions_after"] = [
            row.id for row in _executions(db_session_factory, task.id)
        ]
        observed["beat_after"] = _beat(db_session_factory)
        return result

    monkeypatch.setattr(
        TaskService, "archive_task_workspace_for_repair_rerun", _archive_with_beat_probe
    )
    published = _stub_retry_publication(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
    )

    assert response.status_code == 200, response.text
    assert observed["executions_before"] == [prior.task_execution_id]
    assert observed["executions_after"] == [prior.task_execution_id]
    for key in ("beat_before", "beat_after"):
        beat_result, beat_published = observed[key]
        assert beat_result["inspected_count"] == 0
        assert beat_published == []
    assert not old_workspace.exists()
    assert published["published_task_id"]


def test_same_session_retry_is_unchanged_and_never_archives(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior, project_root, old_workspace = _review_task(
        authenticated_client, db_session, tmp_path
    )
    calls = []
    original_archive = TaskService.archive_task_workspace_for_repair_rerun
    monkeypatch.setattr(
        TaskService,
        "archive_task_workspace_for_repair_rerun",
        lambda *a, **kw: calls.append(1) or original_archive(*a, **kw),
    )
    published = _stub_retry_publication(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=SAME_SESSION
    )

    assert response.status_code == 200, response.text
    assert calls == []
    assert response.json()["repair_archive_result"] is None
    assert old_workspace.is_dir()
    assert _archive_entries(project_root) == []
    persisted = _fresh(db_session_factory, Task, task.id)
    assert persisted.task_subfolder == "task-review"
    assert persisted.promotion_note == NOTE
    execution = _fresh(
        db_session_factory, TaskExecution, response.json()["task_execution_id"]
    )
    assert execution.session_id == prior.session_id
    assert execution.dispatch_kind == "ROOT"
    assert execution.dispatch_state == "READY"
    assert published["prompt"] == OPERATOR_DESCRIPTION
    assert published["published_task_id"] == execution.dispatch_celery_task_id


def test_duplicate_publication_and_beat_replay_reuse_identity_without_rearchive(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior, project_root, _old = _review_task(
        authenticated_client, db_session, tmp_path
    )
    calls = []
    original_archive = TaskService.archive_task_workspace_for_repair_rerun
    monkeypatch.setattr(
        TaskService,
        "archive_task_workspace_for_repair_rerun",
        lambda *a, **kw: calls.append(1) or original_archive(*a, **kw),
    )
    published = _stub_retry_publication(monkeypatch)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
    )
    assert response.status_code == 200, response.text
    execution_id = response.json()["task_execution_id"]
    before = _fresh(db_session_factory, TaskExecution, execution_id)

    beat_result, beat_published = _beat(
        db_session_factory, task_execution_id=execution_id, make_due=True
    )

    assert beat_result["published_count"] == 1
    assert beat_published[0]["task_id"] == before.dispatch_celery_task_id
    assert beat_published[0]["task_id"] == published["published_task_id"]
    assert beat_published[0]["kwargs"]["task_execution_id"] == execution_id
    assert beat_published[0]["kwargs"]["prompt"] == published["prompt"]
    after = _fresh(db_session_factory, TaskExecution, execution_id)
    assert after.dispatch_invocation == before.dispatch_invocation
    assert after.dispatch_invocation_sha256 == before.dispatch_invocation_sha256
    assert after.dispatch_celery_task_id == before.dispatch_celery_task_id
    assert after.dispatch_publish_attempts == before.dispatch_publish_attempts + 1
    assert [row.id for row in _executions(db_session_factory, task.id)] == [
        prior.task_execution_id,
        execution_id,
    ]
    assert calls == [1]
    assert len(_archive_entries(project_root)) == 1


# --------------------------------------------------------------------- negative


def test_archive_filesystem_failure_creates_no_execution_and_is_retryable(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    import app.services.workspace.baseline_promotion_service as baseline_module

    task, prior, project_root, old_workspace = _review_task(
        authenticated_client, db_session, tmp_path
    )
    published = _stub_retry_publication(monkeypatch)

    def _failing_move(*_args, **_kwargs):
        raise OSError("offline archive move failure")

    with monkeypatch.context() as patch:
        patch.setattr(baseline_module.shutil, "move", _failing_move)
        response = authenticated_client.post(
            f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
        )

    assert response.status_code == 409, response.text
    assert "no retry execution was created" in response.json()["detail"]
    _assert_review_intact(
        _fresh(db_session_factory, Task, task.id), subfolder="task-review"
    )
    assert [row.id for row in _executions(db_session_factory, task.id)] == [
        prior.task_execution_id
    ]
    assert old_workspace.is_dir()
    assert _archive_entries(project_root) == []
    assert published == {}
    assert _beat(db_session_factory)[0]["inspected_count"] == 0

    recovered = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["repair_archive_result"]["archived"] is True
    assert published["published_task_id"]


def test_archive_metadata_commit_failure_creates_no_execution_and_recovers(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior, project_root, old_workspace = _review_task(
        authenticated_client, db_session, tmp_path
    )
    published = _stub_retry_publication(monkeypatch)
    original_archive = TaskService.archive_task_workspace_for_repair_rerun

    def _archive_then_break_commit(self, project, archived_task, **kwargs):
        result = original_archive(self, project, archived_task, **kwargs)

        def _fail_commit():
            raise RuntimeError("offline archive metadata commit failure")

        monkeypatch.setattr(self.db, "commit", _fail_commit)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(
            TaskService,
            "archive_task_workspace_for_repair_rerun",
            _archive_then_break_commit,
        )
        with pytest.raises(RuntimeError, match="archive metadata commit failure"):
            authenticated_client.post(
                f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
            )

    # Filesystem moved; database still points at the old (now missing) folder.
    assert not old_workspace.exists()
    assert len(_archive_entries(project_root)) == 1
    _assert_review_intact(
        _fresh(db_session_factory, Task, task.id), subfolder="task-review"
    )
    assert [row.id for row in _executions(db_session_factory, task.id)] == [
        prior.task_execution_id
    ]
    assert published == {}
    assert _beat(db_session_factory)[0]["inspected_count"] == 0

    recovered = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["repair_archive_result"]["reason"] == "workspace_missing"
    assert len(_archive_entries(project_root)) == 1
    execution = _fresh(
        db_session_factory, TaskExecution, recovered.json()["task_execution_id"]
    )
    assert execution.dispatch_state == "READY"
    assert published["published_task_id"] == execution.dispatch_celery_task_id


def test_invocation_commit_failure_keeps_durable_archive_and_review_active(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    import app.api.v1.endpoints.tasks as tasks_endpoint

    task, prior, project_root, old_workspace = _review_task(
        authenticated_client, db_session, tmp_path
    )
    published = _stub_retry_publication(monkeypatch)
    configure = tasks_endpoint.configure_task_execution_dispatch

    def _configure_then_break_commit(db, **kwargs):
        result = configure(db, **kwargs)

        def _fail_commit():
            raise RuntimeError("offline invocation commit failure")

        monkeypatch.setattr(db, "commit", _fail_commit)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(
            tasks_endpoint,
            "configure_task_execution_dispatch",
            _configure_then_break_commit,
        )
        with pytest.raises(RuntimeError, match="offline invocation commit failure"):
            authenticated_client.post(
                f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
            )

    assert not old_workspace.exists()
    assert len(_archive_entries(project_root)) == 1
    _assert_review_intact(_fresh(db_session_factory, Task, task.id), subfolder=None)
    assert [row.id for row in _executions(db_session_factory, task.id)] == [
        prior.task_execution_id
    ]
    assert published == {}
    assert _beat(db_session_factory)[0]["inspected_count"] == 0

    recovered = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["repair_archive_result"] == {
        "archived": False,
        "reason": "task_has_no_workspace",
    }
    assert len(_archive_entries(project_root)) == 1
    assert published["published_task_id"]


def test_workspace_preparation_failure_leaves_valid_authority_for_reconciler(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    import app.api.v1.endpoints.tasks as tasks_endpoint

    task, prior, project_root, _old = _review_task(
        authenticated_client, db_session, tmp_path
    )
    published = _stub_retry_publication(monkeypatch)

    def _failing_prepare(*_args, **_kwargs):
        raise OSError("offline workspace preparation failure")

    with monkeypatch.context() as patch:
        patch.setattr(tasks_endpoint, "ensure_task_workspace", _failing_prepare)
        with pytest.raises(OSError, match="workspace preparation failure"):
            authenticated_client.post(
                f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
            )

    assert published == {}
    assert len(_archive_entries(project_root)) == 1
    executions = _executions(db_session_factory, task.id)
    assert [row.id for row in executions[:-1]] == [prior.task_execution_id]
    execution = executions[-1]
    assert execution.status == TaskStatus.PENDING
    assert execution.dispatch_state == "PENDING_BINDING"
    assert execution.dispatch_invocation_sha256 == dispatch_value_sha256(
        execution.dispatch_invocation
    )
    persisted = _fresh(db_session_factory, Task, task.id)
    assert persisted.workspace_status == "changes_requested"
    assert persisted.promotion_note == NOTE
    assert persisted.task_subfolder is None

    # The independent reconciler publishes the durable attempt (the Worker
    # allocates the missing subfolder itself); authority still verifies.
    beat_result, beat_published = _beat(
        db_session_factory, task_execution_id=execution.id
    )
    assert beat_result["published_count"] == 1, beat_result
    assert beat_published[0]["task_id"] == execution.dispatch_celery_task_id
    assert len(_archive_entries(project_root)) == 1


def test_publication_failure_keeps_archive_and_authority_recoverable(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, _prior, project_root, _old = _review_task(
        authenticated_client, db_session, tmp_path
    )

    def _fail(_kwargs, _options):
        raise RuntimeError("offline publisher failure")

    published = _stub_retry_publication(monkeypatch, on_publish=_fail)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
    )

    assert response.status_code == 200, response.text
    execution_id = response.json()["task_execution_id"]
    execution = _fresh(db_session_factory, TaskExecution, execution_id)
    assert execution.dispatch_state == "READY"
    assert execution.dispatch_last_error == "publisher_error:RuntimeError"
    assert published["published_task_id"] == execution.dispatch_celery_task_id
    beat_result, beat_published = _beat(
        db_session_factory, task_execution_id=execution_id, make_due=True
    )
    assert beat_result["published_count"] == 1
    assert beat_published[0]["task_id"] == execution.dispatch_celery_task_id
    assert len(_archive_entries(project_root)) == 1


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("description", "Operator rewrote the task.", "task_prompt_reference_changed"),
        (
            "promotion_note",
            "Different feedback.",
            "acceptance_request_changes_source_changed",
        ),
    ],
)
def test_authority_input_mutation_after_issuance_is_refused(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path,
    monkeypatch,
    field,
    value,
    reason,
):
    task, _prior, _root, _old = _review_task(authenticated_client, db_session, tmp_path)
    _stub_retry_publication(monkeypatch)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
    )
    assert response.status_code == 200, response.text
    execution_id = response.json()["task_execution_id"]

    mutator = db_session_factory()
    try:
        row = mutator.get(Task, task.id)
        setattr(row, field, value)
        mutator.commit()
    finally:
        mutator.close()

    beat_result, beat_published = _beat(
        db_session_factory, task_execution_id=execution_id, make_due=True
    )
    assert beat_published == []
    assert beat_result["results"][0]["state"] == "refused"
    assert beat_result["results"][0]["reason"] == reason
    refused = _fresh(db_session_factory, TaskExecution, execution_id)
    assert refused.dispatch_state == "REFUSED"


@pytest.mark.parametrize(
    "mutation",
    ["task_not_done", "blank_promotion_note", "blank_description"],
)
def test_invalid_review_state_or_source_data_refuses_before_archive(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path,
    monkeypatch,
    mutation,
):
    task, prior, project_root, old_workspace = _review_task(
        authenticated_client, db_session, tmp_path
    )
    row = db_session.get(Task, task.id)
    if mutation == "task_not_done":
        row.status = TaskStatus.FAILED
    elif mutation == "blank_promotion_note":
        row.promotion_note = "   "
    else:
        row.description = "   "
    db_session.commit()
    published = _stub_retry_publication(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
    )

    assert response.status_code == 409, response.text
    assert old_workspace.is_dir()
    assert _archive_entries(project_root) == []
    assert [row.id for row in _executions(db_session_factory, task.id)] == [
        prior.task_execution_id
    ]
    assert published == {}


def test_request_changes_intent_without_active_review_never_archives(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    old_workspace = tmp_path / "project" / "task-review"
    published = _stub_retry_publication(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
    )

    assert response.status_code == 409, response.text
    assert old_workspace.is_dir()
    assert _archive_entries(tmp_path / "project") == []
    assert published == {}


# ---------------------------------------------------------------- compatibility


def test_ordinary_new_session_retry_outside_review_does_not_archive(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    old_workspace = tmp_path / "project" / "task-review"
    published = _stub_retry_publication(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"create_new_session": True, "execution_scope": "new_session"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["repair_archive_result"] is None
    assert old_workspace.is_dir()
    execution = _fresh(
        db_session_factory, TaskExecution, response.json()["task_execution_id"]
    )
    authority = execution.dispatch_invocation["acceptance_authority"]
    assert authority["acceptance_mode"] == "ordinary"
    assert execution.dispatch_kind == "ROOT"
    assert published["published_task_id"] == execution.dispatch_celery_task_id
