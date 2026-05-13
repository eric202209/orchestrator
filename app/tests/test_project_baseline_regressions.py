from __future__ import annotations

from pathlib import Path

from app.models import Project, Session as SessionModel, Task, TaskStatus
from app.services.task_service import TaskService


def test_rebuild_project_baseline_uses_only_promoted_workspaces(
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "baseline-regression"
    project_root.mkdir(parents=True)

    project = Project(
        name="baseline-regression",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    promoted_dir = project_root / "task-promoted"
    ready_dir = project_root / "task-ready"
    promoted_dir.mkdir()
    ready_dir.mkdir()
    (promoted_dir / "accepted.txt").write_text("accepted", encoding="utf-8")
    (ready_dir / "unreviewed.txt").write_text("unreviewed", encoding="utf-8")

    db_session.add_all(
        [
            Task(
                project_id=project.id,
                title="Promoted task",
                description="Accepted work",
                status=TaskStatus.DONE,
                workspace_status="promoted",
                task_subfolder="task-promoted",
            ),
            Task(
                project_id=project.id,
                title="Ready task",
                description="Done but not accepted",
                status=TaskStatus.DONE,
                workspace_status="ready",
                task_subfolder="task-ready",
            ),
        ]
    )
    db_session.commit()

    result = TaskService(db_session).rebuild_project_baseline(project)

    assert result["promoted_task_count"] == 1
    assert result["merged_task_count"] == 1
    assert result["files_copied"] == 1
    assert (project_root / "accepted.txt").read_text(encoding="utf-8") == "accepted"
    assert not (project_root / "unreviewed.txt").exists()


def test_project_gitignore_guard_creates_runtime_exclusions(
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "gitignore-guard"
    project = Project(
        name="gitignore-guard",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    result = TaskService(db_session).ensure_project_gitignore_guard(project)

    gitignore = project_root / ".gitignore"
    assert result["changed"] is True
    assert gitignore.exists()
    contents = gitignore.read_text(encoding="utf-8")
    assert "# BEGIN OpenClaw workspace guard" in contents
    assert ".openclaw/" in contents
    assert "node_modules/" in contents
    assert "__pycache__/" in contents


def test_project_gitignore_guard_preserves_existing_rules_and_is_idempotent(
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "gitignore-preserve"
    project_root.mkdir(parents=True)
    gitignore = project_root / ".gitignore"
    gitignore.write_text("dist/\n.env\n", encoding="utf-8")
    project = Project(
        name="gitignore-preserve",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task_service = TaskService(db_session)
    first = task_service.ensure_project_gitignore_guard(project)
    second = task_service.ensure_project_gitignore_guard(project)

    contents = gitignore.read_text(encoding="utf-8")
    assert first["changed"] is True
    assert second["changed"] is False
    assert contents.startswith("dist/\n.env\n")
    assert contents.count("# BEGIN OpenClaw workspace guard") == 1
    assert contents.count(".openclaw/") == 1


def test_rebuild_project_baseline_preserves_project_gitignore_guard(
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "rebuild-gitignore"
    project_root.mkdir(parents=True)
    gitignore = project_root / ".gitignore"
    gitignore.write_text("dist/\n", encoding="utf-8")
    project = Project(
        name="rebuild-gitignore",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task_dir = project_root / "task-promoted"
    task_dir.mkdir()
    (task_dir / "README.md").write_text("accepted", encoding="utf-8")
    task = Task(
        project_id=project.id,
        title="Promoted task",
        description="Accepted work",
        status=TaskStatus.DONE,
        workspace_status="promoted",
        task_subfolder="task-promoted",
    )
    db_session.add(task)
    db_session.commit()

    result = TaskService(db_session).rebuild_project_baseline(project)

    assert result["files_copied"] == 1
    contents = gitignore.read_text(encoding="utf-8")
    assert contents.startswith("dist/\n")
    assert ".openclaw/" in contents
    assert "__pycache__/" in contents


def test_promoted_workspace_archive_removes_visible_task_folder_but_preserves_rebuild_source(
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "promoted-archive"
    project_root.mkdir(parents=True)

    project = Project(
        name="promoted-archive",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task_dir = project_root / "task-accepted"
    task_dir.mkdir()
    (task_dir / "README.md").write_text("accepted", encoding="utf-8")
    task = Task(
        project_id=project.id,
        title="Accepted task",
        description="Accepted work",
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder="task-accepted",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    task_service = TaskService(db_session)
    task_service.promote_task_into_baseline(project, task)
    archive_result = task_service.archive_promoted_task_workspace(project, task)
    db_session.commit()
    db_session.refresh(task)

    assert archive_result["archived"] is True
    assert not task_dir.exists()
    archived_dir = Path(archive_result["archive_path"])
    assert archived_dir.exists()
    assert task.task_subfolder.startswith(".openclaw/promoted-workspace-archive/")
    assert task.workspace_status == "promoted"
    assert task.promoted_at is not None

    audit = task_service.audit_project_workspace_shape(project)
    assert audit["retained_task_workspace_count"] == 0
    assert audit["duplicated_scaffold_artifacts"] == {}

    (project_root / "README.md").unlink()
    result = task_service.rebuild_project_baseline(project)

    assert result["promoted_task_count"] == 1
    assert result["files_copied"] == 1
    assert (project_root / "README.md").read_text(encoding="utf-8") == "accepted"


def test_manual_promote_endpoint_archives_visible_task_workspace(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "manual-promote-archive"
    project_root.mkdir(parents=True)
    project = Project(
        name="manual-promote-archive",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task_dir = project_root / "task-manual"
    task_dir.mkdir()
    (task_dir / "README.md").write_text("manual", encoding="utf-8")
    task = Task(
        project_id=project.id,
        title="Manual promote",
        description="Accepted manually",
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder="task-manual",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/promote",
        json={"note": "accepted"},
    )

    assert response.status_code == 200
    db_session.refresh(task)
    assert not task_dir.exists()
    assert task.workspace_status == "promoted"
    assert task.task_subfolder.startswith(".openclaw/promoted-workspace-archive/")
    assert (project_root / "README.md").read_text(encoding="utf-8") == "manual"
    assert (project_root / task.task_subfolder / "README.md").read_text(
        encoding="utf-8"
    ) == "manual"


def test_workspace_shape_audit_distinguishes_baseline_from_retained_sandboxes(
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "workspace-audit"
    project_root.mkdir(parents=True)

    project = Project(
        name="workspace-audit",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    promoted_dir = project_root / "task-promoted"
    ready_dir = project_root / "task-ready"
    promoted_dir.mkdir()
    ready_dir.mkdir()
    (project_root / "README.md").write_text("baseline", encoding="utf-8")
    (promoted_dir / "package.json").write_text('{"scripts": {}}', encoding="utf-8")
    (promoted_dir / "README.md").write_text("accepted", encoding="utf-8")
    (ready_dir / "package.json").write_text('{"scripts": {}}', encoding="utf-8")
    (ready_dir / "README.md").write_text("pending change", encoding="utf-8")
    (ready_dir / "tests").mkdir()
    (ready_dir / "tests" / "test_app.py").write_text(
        "def test_app(): pass\n", encoding="utf-8"
    )
    (ready_dir / ".openclaw").mkdir()
    (ready_dir / ".openclaw" / "events.jsonl").write_text("{}", encoding="utf-8")

    db_session.add_all(
        [
            Task(
                project_id=project.id,
                title="Promoted task",
                description="Accepted work",
                status=TaskStatus.DONE,
                workspace_status="promoted",
                task_subfolder="task-promoted",
            ),
            Task(
                project_id=project.id,
                title="Ready task",
                description="Done but not accepted",
                status=TaskStatus.DONE,
                workspace_status="ready",
                task_subfolder="task-ready",
            ),
        ]
    )
    db_session.commit()

    task_service = TaskService(db_session)
    promoted_task = db_session.query(Task).filter_by(title="Promoted task").one()
    task_service.promote_task_into_baseline(project, promoted_task)

    audit = task_service.audit_project_workspace_shape(project)

    assert audit["baseline"]["file_count"] == 2
    assert audit["retained_task_workspace_count"] == 2
    assert audit["unpromoted_done_workspace_count"] == 1
    assert audit["duplicated_scaffold_artifacts"] == {
        "README.md": 2,
        "package.json": 2,
    }
    assert audit["transient_artifact_names"] == [".openclaw"]
    assert any("completed task workspace" in issue for issue in audit["issues"])
    ready_workspace = next(
        item
        for item in audit["retained_task_workspaces"]
        if item["task_subfolder"] == "task-ready"
    )
    assert ready_workspace["baseline_diff"]["added_count"] == 1
    assert ready_workspace["baseline_diff"]["modified_count"] == 1
    assert "README.md" in ready_workspace["baseline_diff"]["modified_files"]


def test_update_task_rejects_lowering_current_step_on_promoted_workspace(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "lower-step-safety"
    project_root.mkdir(parents=True)
    project = Project(
        name="lower-step-safety",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Promoted task",
        description="Accepted work",
        status=TaskStatus.DONE,
        workspace_status="promoted",
        task_subfolder="task-promoted",
        current_step=3,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    response = authenticated_client.put(
        f"/api/v1/tasks/{task.id}",
        json={"current_step": 1},
    )

    assert response.status_code == 409
    assert "promoted task workspace" in response.json()["detail"]
    db_session.refresh(task)
    assert task.current_step == 3


def test_update_task_allows_lowering_current_step_before_promotion(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "lower-step-unpromoted"
    project_root.mkdir(parents=True)
    project = Project(
        name="lower-step-unpromoted",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Ready task",
        description="Unaccepted work",
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder="task-ready",
        current_step=3,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    response = authenticated_client.put(
        f"/api/v1/tasks/{task.id}",
        json={"current_step": 1},
    )

    assert response.status_code == 200
    db_session.refresh(task)
    assert task.current_step == 1


def test_cleanup_retained_task_workspaces_preserves_promoted_and_running(
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "cleanup-workspaces"
    project_root.mkdir(parents=True)
    project = Project(
        name="cleanup-workspaces",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    for folder in ("task-promoted", "task-ready", "task-blocked", "task-running"):
        workspace = project_root / folder
        workspace.mkdir()
        (workspace / "artifact.txt").write_text(folder, encoding="utf-8")

    promoted_task = Task(
        project_id=project.id,
        title="Promoted task",
        description="Accepted",
        status=TaskStatus.DONE,
        workspace_status="promoted",
        task_subfolder="task-promoted",
    )
    ready_task = Task(
        project_id=project.id,
        title="Ready task",
        description="Unaccepted done",
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder="task-ready",
    )
    blocked_task = Task(
        project_id=project.id,
        title="Blocked task",
        description="Failed",
        status=TaskStatus.FAILED,
        workspace_status="blocked",
        task_subfolder="task-blocked",
    )
    running_task = Task(
        project_id=project.id,
        title="Running task",
        description="Active",
        status=TaskStatus.RUNNING,
        workspace_status="in_progress",
        task_subfolder="task-running",
    )
    db_session.add_all([promoted_task, ready_task, blocked_task, running_task])
    db_session.commit()

    task_service = TaskService(db_session)
    preview = task_service.cleanup_retained_task_workspaces(project)

    assert preview["dry_run"] is True
    assert preview["candidate_count"] == 1
    assert preview["candidates"][0]["task_subfolder"] == "task-blocked"
    assert (project_root / "task-blocked").exists()

    result = task_service.cleanup_retained_task_workspaces(project, dry_run=False)

    assert result["deleted_count"] == 1
    archived_blocked = Path(result["deleted"][0]["archive_path"])
    assert not (project_root / "task-blocked").exists()
    assert archived_blocked.exists()
    assert (archived_blocked / "artifact.txt").read_text(
        encoding="utf-8"
    ) == "task-blocked"
    assert (project_root / "task-promoted").exists()
    assert (project_root / "task-ready").exists()
    assert (project_root / "task-running").exists()
    db_session.refresh(blocked_task)
    assert blocked_task.task_subfolder is None
    assert blocked_task.workspace_status == "not_created"
    assert (
        blocked_task.promotion_note
        == f"Archived retained workspace at {archived_blocked}"
    )


def test_workspace_cleanup_endpoint_defaults_to_preview(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "cleanup-endpoint"
    project_root.mkdir(parents=True)
    project = Project(
        name="cleanup-endpoint",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    blocked_dir = project_root / "task-blocked"
    blocked_dir.mkdir()
    (blocked_dir / "artifact.txt").write_text("blocked", encoding="utf-8")
    task = Task(
        project_id=project.id,
        title="Blocked task",
        description="Failed",
        status=TaskStatus.FAILED,
        workspace_status="blocked",
        task_subfolder="task-blocked",
    )
    db_session.add(task)
    db_session.commit()

    response = authenticated_client.post(
        f"/api/v1/projects/{project.id}/workspace-cleanup",
        json={},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["candidate_count"] == 1
    assert body["deleted_count"] == 0
    assert blocked_dir.exists()


def test_workspace_overview_endpoint_includes_audit_payload(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "overview-audit"
    project_root.mkdir(parents=True)
    project = Project(
        name="overview-audit",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    ready_dir = project_root / "task-ready"
    ready_dir.mkdir()
    (ready_dir / "package.json").write_text("{}", encoding="utf-8")
    task = Task(
        project_id=project.id,
        title="Ready task",
        description="Done but unpromoted",
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder="task-ready",
    )
    db_session.add(task)
    db_session.commit()

    response = authenticated_client.get(
        f"/api/v1/projects/{project.id}/workspace-overview"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["audit"]["retained_task_workspace_count"] == 1
    assert body["audit"]["unpromoted_done_workspace_count"] == 1
    assert (
        body["audit"]["retained_task_workspaces"][0]["task_subfolder"] == "task-ready"
    )
    assert (
        body["audit"]["retained_task_workspaces"][0]["baseline_diff"]["added_count"]
        == 1
    )
    assert body["audit"]["duplicated_scaffold_artifacts"] == {}


def test_workspace_cleanup_endpoint_deletes_when_explicitly_requested(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "cleanup-endpoint-delete"
    project_root.mkdir(parents=True)
    project = Project(
        name="cleanup-endpoint-delete",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    blocked_dir = project_root / "task-blocked"
    promoted_dir = project_root / "task-promoted"
    blocked_dir.mkdir()
    promoted_dir.mkdir()
    (blocked_dir / "artifact.txt").write_text("blocked", encoding="utf-8")
    (promoted_dir / "artifact.txt").write_text("promoted", encoding="utf-8")
    blocked_task = Task(
        project_id=project.id,
        title="Blocked task",
        description="Failed",
        status=TaskStatus.FAILED,
        workspace_status="blocked",
        task_subfolder="task-blocked",
    )
    promoted_task = Task(
        project_id=project.id,
        title="Promoted task",
        description="Accepted",
        status=TaskStatus.DONE,
        workspace_status="promoted",
        task_subfolder="task-promoted",
    )
    db_session.add_all([blocked_task, promoted_task])
    db_session.commit()

    response = authenticated_client.post(
        f"/api/v1/projects/{project.id}/workspace-cleanup",
        json={"dry_run": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is False
    assert body["deleted_count"] == 1
    assert body["deleted"][0]["task_subfolder"] == "task-blocked"
    archive_path = Path(body["deleted"][0]["archive_path"])
    assert not blocked_dir.exists()
    assert archive_path.exists()
    assert promoted_dir.exists()
    db_session.refresh(blocked_task)
    assert blocked_task.task_subfolder is None
    assert blocked_task.workspace_status == "not_created"


def test_workspace_archive_restore_endpoint_restores_archived_workspace(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "restore-archive"
    project_root.mkdir(parents=True)
    project = Project(
        name="restore-archive",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    blocked_dir = project_root / "task-blocked"
    blocked_dir.mkdir()
    (blocked_dir / "artifact.txt").write_text("blocked", encoding="utf-8")
    task = Task(
        project_id=project.id,
        title="Blocked task",
        description="Failed",
        status=TaskStatus.FAILED,
        workspace_status="blocked",
        task_subfolder="task-blocked",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    cleanup_response = authenticated_client.post(
        f"/api/v1/projects/{project.id}/workspace-cleanup",
        json={"dry_run": False},
    )
    assert cleanup_response.status_code == 200
    archive_path = cleanup_response.json()["deleted"][0]["archive_path"]

    restore_response = authenticated_client.post(
        f"/api/v1/projects/{project.id}/workspace-archive/restore",
        json={"task_id": task.id, "archive_path": archive_path},
    )

    assert restore_response.status_code == 200
    body = restore_response.json()
    restored_dir = Path(body["workspace_path"])
    assert restored_dir.exists()
    assert (restored_dir / "artifact.txt").read_text(encoding="utf-8") == "blocked"
    db_session.refresh(task)
    assert task.task_subfolder == restored_dir.name
    assert task.workspace_status == "blocked"


def test_workspace_archive_restore_rejects_archive_outside_project(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "restore-archive-guard"
    project_root.mkdir(parents=True)
    outside_archive = tmp_path / "outside-archive"
    outside_archive.mkdir()
    project = Project(
        name="restore-archive-guard",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="No workspace",
        description="Failed",
        status=TaskStatus.FAILED,
        workspace_status="not_created",
    )
    db_session.add(task)
    db_session.commit()

    response = authenticated_client.post(
        f"/api/v1/projects/{project.id}/workspace-archive/restore",
        json={"task_id": task.id, "archive_path": str(outside_archive)},
    )

    assert response.status_code == 409
    assert "outside this project's workspace archive" in response.json()["detail"]


def test_changes_requested_new_session_retry_archives_old_workspace(
    authenticated_client,
    db_session,
    monkeypatch,
    tmp_path: Path,
):
    from app.tests.test_task_execution_transaction_regressions import (
        _stub_retry_dispatch,
    )

    project_root = tmp_path / "repair-rerun"
    project_root.mkdir(parents=True)
    old_workspace = project_root / "task-old"
    old_workspace.mkdir()
    (old_workspace / "stale.txt").write_text("old", encoding="utf-8")

    project = Project(
        name="repair-rerun",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    workflow_session = SessionModel(
        project_id=project.id,
        name="Project workflow",
        status="stopped",
        is_active=False,
    )
    task = Task(
        project_id=project.id,
        title="Repair rerun task",
        description="repair prompt",
        status=TaskStatus.FAILED,
        workspace_status="blocked",
        task_subfolder="task-old",
    )
    db_session.add_all([workflow_session, task])
    db_session.commit()
    db_session.refresh(task)

    request_response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes",
        json={"note": "needs repair"},
    )
    assert request_response.status_code == 200

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)
    retry_response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"execution_scope": "new_session", "create_new_session": True},
    )

    assert retry_response.status_code == 200
    payload = retry_response.json()
    archive_result = payload["repair_archive_result"]
    assert archive_result["archived"] is True
    archived_path = Path(archive_result["archive_path"])
    assert archived_path.exists()
    assert (archived_path / "stale.txt").read_text(encoding="utf-8") == "old"
    assert not old_workspace.exists()

    db_session.refresh(task)
    assert task.task_subfolder is None
    assert task.status == TaskStatus.PENDING
    assert "needs repair" in (task.promotion_note or "")
    assert "Archived previous workspace" in (task.promotion_note or "")
    assert payload["execution_scope"] == "isolated_session"
    assert captured_kwargs["task_id"] == task.id
