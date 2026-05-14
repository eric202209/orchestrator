from __future__ import annotations

import json
from pathlib import Path

from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
)
from app.services.orchestration.execution.runtime import workspace_snapshot_key
from app.services.task_service import TASK_CHANGE_SET_LOG_MESSAGE, TaskService
from app.services.workspace.baseline_promotion_service import BaselinePromotionService
from app.services.workspace.changeset_service import ChangesetService
from app.services.workspace.project_mutation_lock import project_mutation_lock
from app.services.workspace.workspace_paths import (
    AUTO_SNAPSHOT_ROOT,
    resolve_project_root,
)
from app.services.workspace.workspace_snapshot_service import WorkspaceSnapshotService


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


def test_workspace_services_share_project_root_contract(db_session, tmp_path: Path):
    project_root = tmp_path / "shared-root-contract"
    project_root.mkdir(parents=True)
    project = Project(
        name="shared-root-contract",
        workspace_path=str(tmp_path),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    expected_root = project_root.resolve()

    assert resolve_project_root(project, db_session) == expected_root
    assert TaskService(db_session).get_project_root(project) == expected_root
    assert ChangesetService(db_session).get_project_root(project) == expected_root
    assert (
        WorkspaceSnapshotService(db_session).get_project_root(project) == expected_root
    )
    assert (
        BaselinePromotionService(db_session).get_project_root(project) == expected_root
    )
    assert (
        (expected_root / AUTO_SNAPSHOT_ROOT)
        .as_posix()
        .endswith(".openclaw/auto-snapshots")
    )


def test_task_execution_change_set_captures_added_modified_and_deleted_files(
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "change-set-capture"
    project_root.mkdir(parents=True)
    (project_root / "README.md").write_text("before\n", encoding="utf-8")
    (project_root / "old.txt").write_text("remove me\n", encoding="utf-8")

    project = Project(
        name="change-set-capture",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Capture change set",
        description="Mutate canonical root",
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder="task-capture-change-set",
    )
    session = SessionModel(project_id=project.id, name="capture-session")
    db_session.add_all([task, session])
    db_session.commit()
    db_session.refresh(task)
    db_session.refresh(session)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)

    task_service = TaskService(db_session)
    snapshot_key = workspace_snapshot_key(task.id, execution.id)
    task_service.create_workspace_snapshot(
        project,
        project_root,
        snapshot_key=snapshot_key,
        preserve_project_root_rules=True,
    )

    (project_root / "README.md").write_text("after\n", encoding="utf-8")
    (project_root / "old.txt").unlink()
    (project_root / "package.json").write_text('{"scripts": {}}\n', encoding="utf-8")
    (project_root / ".openclaw" / "runtime").mkdir(parents=True)
    (project_root / ".openclaw" / "runtime" / "ignored.txt").write_text(
        "ignored\n", encoding="utf-8"
    )

    change_set = task_service.persist_task_execution_change_set(
        project,
        task,
        session_id=session.id,
        task_execution_id=execution.id,
        snapshot_key=snapshot_key,
        target_dir=project_root,
        status=TaskStatus.DONE.value,
    )

    assert change_set["added_files"] == ["package.json"]
    assert change_set["modified_files"] == ["README.md"]
    assert change_set["deleted_files"] == ["old.txt"]
    assert change_set["changed_count"] == 3
    assert "deleted_files" in change_set["warning_flags"]
    assert "dependency_files_changed" in change_set["warning_flags"]
    assert all(".openclaw" not in path for path in change_set["added_files"])

    durable_change_set = (
        db_session.query(TaskExecutionChangeSet)
        .filter(TaskExecutionChangeSet.task_execution_id == execution.id)
        .one()
    )
    assert durable_change_set.project_id == project.id
    assert durable_change_set.task_id == task.id
    assert durable_change_set.session_id == session.id
    assert durable_change_set.base_snapshot_key == snapshot_key
    assert durable_change_set.added_files == ["package.json"]
    assert durable_change_set.modified_files == ["README.md"]
    assert durable_change_set.deleted_files == ["old.txt"]
    assert durable_change_set.review_decision["held_for_review"] is True
    assert durable_change_set.review_reason == "nontrivial_change_set_review_required"
    assert durable_change_set.disposition == "captured"

    read_back = task_service.get_task_execution_change_set(
        task_execution_id=execution.id
    )
    assert read_back["changed_count"] == 3
    assert read_back["added_files"] == ["package.json"]
    assert read_back["review_decision"]["reason"] == (
        "nontrivial_change_set_review_required"
    )
    assert read_back["disposition"] == "captured"

    log_entry = (
        db_session.query(LogEntry)
        .filter(
            LogEntry.task_execution_id == execution.id,
            LogEntry.message == TASK_CHANGE_SET_LOG_MESSAGE,
        )
        .one()
    )
    assert '"changed_count": 3' in log_entry.log_metadata


def test_reject_task_execution_change_set_archives_candidate_and_restores_snapshot(
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "change-set-reject"
    project_root.mkdir(parents=True)
    (project_root / "README.md").write_text("accepted\n", encoding="utf-8")
    (project_root / "keep.txt").write_text("keep\n", encoding="utf-8")

    project = Project(
        name="change-set-reject",
        workspace_path=str(project_root),
    )
    task = Task(
        project_id=1,
        title="Reject change set",
        description="Reject canonical changes",
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder="task-reject-change-set",
    )
    session = SessionModel(project_id=1, name="reject-session")
    db_session.add(project)
    db_session.flush()
    task.project_id = project.id
    session.project_id = project.id
    db_session.add_all([task, session])
    db_session.commit()
    db_session.refresh(project)
    db_session.refresh(task)
    db_session.refresh(session)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)

    task_service = TaskService(db_session)
    task_service.ensure_project_gitignore_guard(project)
    snapshot_key = workspace_snapshot_key(task.id, execution.id)
    task_service.create_workspace_snapshot(
        project,
        project_root,
        snapshot_key=snapshot_key,
        preserve_project_root_rules=True,
    )

    (project_root / "README.md").write_text("candidate\n", encoding="utf-8")
    (project_root / "keep.txt").unlink()
    (project_root / "new.txt").write_text("candidate file\n", encoding="utf-8")
    preserved_snapshot_marker = (
        project_root / ".openclaw" / "auto-snapshots" / "manual-sentinel"
    )
    preserved_snapshot_marker.mkdir(parents=True)
    (preserved_snapshot_marker / "marker.txt").write_text(
        "snapshot history\n", encoding="utf-8"
    )
    preserved_archive = project_root / ".openclaw" / "rejected-change-archive" / "prior"
    preserved_archive.mkdir(parents=True)
    (preserved_archive / "manifest.json").write_text("{}", encoding="utf-8")
    task_service.persist_task_execution_change_set(
        project,
        task,
        session_id=session.id,
        task_execution_id=execution.id,
        snapshot_key=snapshot_key,
        target_dir=project_root,
    )

    result = task_service.reject_task_execution_change_set(
        project,
        task,
        task_execution_id=execution.id,
        snapshot_key=snapshot_key,
    )

    assert result["rejected"] is True
    assert result["change_set_disposition"]["disposition"] == "rejected"
    assert result["change_set_disposition"]["disposition_reason"] == (
        "operator_rejected_change_set"
    )
    assert result["restore_result"]["restored"] is True
    assert (project_root / "README.md").read_text(encoding="utf-8") == "accepted\n"
    assert (project_root / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert not (project_root / "new.txt").exists()
    assert ".openclaw/" in (project_root / ".gitignore").read_text(encoding="utf-8")
    assert (preserved_snapshot_marker / "marker.txt").read_text(
        encoding="utf-8"
    ) == "snapshot history\n"
    assert (preserved_archive / "manifest.json").exists()

    archive_dir = Path(result["archive_path"])
    assert (archive_dir / "README.md").read_text(encoding="utf-8") == "candidate\n"
    assert (archive_dir / "new.txt").read_text(encoding="utf-8") == "candidate file\n"
    assert (archive_dir / "manifest.json").exists()
    db_session.refresh(task)
    assert task.workspace_status == "changes_requested"
    assert "Rejected task execution" in task.promotion_note


def test_change_set_endpoints_show_and_reject_recorded_candidate(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "change-set-endpoints"
    project_root.mkdir(parents=True)
    (project_root / "README.md").write_text("accepted\n", encoding="utf-8")
    project = Project(
        name="change-set-endpoints",
        workspace_path=str(project_root),
    )
    task = Task(
        project_id=1,
        title="Endpoint change set",
        description="Review candidate",
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder="task-endpoint-change-set",
    )
    session = SessionModel(project_id=1, name="endpoint-session")
    db_session.add(project)
    db_session.flush()
    task.project_id = project.id
    session.project_id = project.id
    db_session.add_all([task, session])
    db_session.commit()
    db_session.refresh(project)
    db_session.refresh(task)
    db_session.refresh(session)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)

    task_service = TaskService(db_session)
    snapshot_key = workspace_snapshot_key(task.id, execution.id)
    task_service.create_workspace_snapshot(
        project,
        project_root,
        snapshot_key=snapshot_key,
        preserve_project_root_rules=True,
    )
    (project_root / "README.md").write_text("candidate\n", encoding="utf-8")
    (project_root / "notes.md").write_text("new\n", encoding="utf-8")
    task_service.persist_task_execution_change_set(
        project,
        task,
        session_id=session.id,
        task_execution_id=execution.id,
        snapshot_key=snapshot_key,
        target_dir=project_root,
    )

    show_response = authenticated_client.get(f"/api/v1/tasks/{task.id}/change-set")

    assert show_response.status_code == 200
    body = show_response.json()
    assert body["task_execution_id"] == execution.id
    assert body["change_set"]["added_files"] == ["notes.md"]
    assert body["change_set"]["modified_files"] == ["README.md"]
    assert body["review_decision"]["changed_count"] == 2
    assert "workspace_review_policy" in body["review_decision"]

    overview_response = authenticated_client.get(
        f"/api/v1/projects/{project.id}/workspace-overview"
    )

    assert overview_response.status_code == 200
    pending_change_sets = overview_response.json()["pending_change_sets"]
    assert pending_change_sets[0]["task_id"] == task.id
    assert pending_change_sets[0]["change_set"]["changed_count"] == 2
    assert pending_change_sets[0]["review_decision"]["changed_count"] == 2

    reject_response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/change-set/reject",
        json={"task_execution_id": execution.id, "note": "needs review"},
    )

    assert reject_response.status_code == 200
    reject_body = reject_response.json()
    assert reject_body["rejected"] is True
    assert reject_body["change_set_disposition"]["disposition"] == "rejected"
    assert reject_body["change_set_disposition"]["disposition_reason"] == "needs review"
    assert (project_root / "README.md").read_text(encoding="utf-8") == "accepted\n"
    assert not (project_root / "notes.md").exists()
    db_session.refresh(task)
    assert task.workspace_status == "changes_requested"


def test_change_set_reject_requires_explicit_task_execution_id(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "reject-explicit-execution"
    project_root.mkdir(parents=True)
    project = Project(
        name="reject-explicit-execution",
        workspace_path=str(project_root),
    )
    task = Task(
        project_id=1,
        title="Reject explicit execution",
        description="Review candidate",
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder="task-reject-explicit",
    )
    db_session.add(project)
    db_session.flush()
    task.project_id = project.id
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/change-set/reject",
        json={"note": "no implicit latest"},
    )

    assert response.status_code == 400
    assert "task_execution_id is required" in response.json()["detail"]


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
    session = SessionModel(project_id=project.id, name="manual-promote-session")
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)
    task_service = TaskService(db_session)
    task_service.persist_task_execution_change_set(
        project,
        task,
        session_id=session.id,
        task_execution_id=execution.id,
        snapshot_key=workspace_snapshot_key(task.id, execution.id),
        target_dir=task_dir,
    )

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/promote",
        json={"note": "accepted", "task_execution_id": execution.id},
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
    promotion_log = (
        db_session.query(LogEntry)
        .filter(LogEntry.task_id == task.id)
        .filter(LogEntry.message.like("Workspace promoted into project baseline%"))
        .one()
    )
    promotion_metadata = json.loads(promotion_log.log_metadata)
    assert (
        promotion_metadata["baseline_result"]["accepted_change_set"][
            "task_execution_id"
        ]
        == execution.id
    )
    assert (
        promotion_metadata["baseline_result"]["accepted_change_set"]["disposition"]
        == "promoted"
    )


def test_manual_promote_endpoint_requires_execution_id_for_recorded_change_set(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "manual-promote-requires-id"
    project_root.mkdir(parents=True)
    project = Project(
        name="manual-promote-requires-id",
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
        title="Manual promote requires id",
        description="Accepted manually",
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder="task-manual",
    )
    session = SessionModel(project_id=project.id, name="manual-promote-session")
    db_session.add_all([task, session])
    db_session.commit()
    db_session.refresh(task)
    db_session.refresh(session)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)
    db_session.add(
        TaskExecutionChangeSet(
            project_id=project.id,
            task_id=task.id,
            session_id=session.id,
            task_execution_id=execution.id,
            base_snapshot_key="manual-promote-snapshot",
            added_files=["README.md"],
            modified_files=[],
            deleted_files=[],
            warning_flags=[],
            disposition="captured",
        )
    )
    db_session.add(
        LogEntry(
            session_id=session.id,
            task_id=task.id,
            task_execution_id=execution.id,
            level="INFO",
            message=TASK_CHANGE_SET_LOG_MESSAGE,
            log_metadata=json.dumps(
                {
                    "schema": "openclaw.task_execution_change_set.v1",
                    "task_id": task.id,
                    "task_execution_id": execution.id,
                    "changed_count": 1,
                    "added_files": ["README.md"],
                    "modified_files": [],
                    "deleted_files": [],
                    "warning_flags": [],
                }
            ),
        )
    )
    db_session.commit()

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/promote",
        json={"note": "accepted"},
    )

    assert response.status_code == 400
    assert "task_execution_id is required" in response.json()["detail"]


def test_manual_promote_rejects_active_project_mutation_lock(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "manual-promote-lock"
    project_root.mkdir(parents=True)
    project = Project(name="manual-promote-lock", workspace_path=str(project_root))
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
    session = SessionModel(project_id=project.id, name="manual-promote-lock-session")
    db_session.add_all([task, session])
    db_session.commit()
    db_session.refresh(task)
    db_session.refresh(session)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)
    TaskService(db_session).persist_task_execution_change_set(
        project,
        task,
        session_id=session.id,
        task_execution_id=execution.id,
        snapshot_key=workspace_snapshot_key(task.id, execution.id),
        target_dir=task_dir,
    )

    with project_mutation_lock(
        project_id=project.id,
        project_root=project_root,
        operation="test_conflict",
        owner="test",
    ):
        response = authenticated_client.post(
            f"/api/v1/tasks/{task.id}/promote",
            json={"note": "accepted", "task_execution_id": execution.id},
        )

    assert response.status_code == 409
    assert "active canonical-root writer" in response.json()["detail"]
    db_session.refresh(task)
    assert task.workspace_status == "ready"
    assert task_dir.exists()


def test_manual_promote_endpoint_rejects_stale_task_execution_id(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "manual-promote-stale-id"
    project_root.mkdir(parents=True)
    project = Project(
        name="manual-promote-stale-id",
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
        title="Manual promote stale id",
        description="Accepted manually",
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder="task-manual",
    )
    session = SessionModel(project_id=project.id, name="manual-promote-session")
    db_session.add_all([task, session])
    db_session.commit()
    db_session.refresh(task)
    db_session.refresh(session)
    first_execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    latest_execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=2,
        status=TaskStatus.DONE,
    )
    db_session.add_all([first_execution, latest_execution])
    db_session.commit()
    db_session.refresh(first_execution)
    db_session.refresh(latest_execution)

    for execution, filename in (
        (first_execution, "old.md"),
        (latest_execution, "latest.md"),
    ):
        db_session.add(
            TaskExecutionChangeSet(
                project_id=project.id,
                task_id=task.id,
                session_id=session.id,
                task_execution_id=execution.id,
                base_snapshot_key=f"manual-promote-{filename}",
                added_files=[filename],
                modified_files=[],
                deleted_files=[],
                warning_flags=[],
                disposition="captured",
            )
        )
        db_session.add(
            LogEntry(
                session_id=session.id,
                task_id=task.id,
                task_execution_id=execution.id,
                level="INFO",
                message=TASK_CHANGE_SET_LOG_MESSAGE,
                log_metadata=json.dumps(
                    {
                        "schema": "openclaw.task_execution_change_set.v1",
                        "task_id": task.id,
                        "task_execution_id": execution.id,
                        "changed_count": 1,
                        "added_files": [filename],
                        "modified_files": [],
                        "deleted_files": [],
                        "warning_flags": [],
                    }
                ),
            )
        )
    db_session.commit()

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/promote",
        json={"note": "accepted", "task_execution_id": first_execution.id},
    )

    assert response.status_code == 409
    assert "latest pending change set" in response.json()["detail"]
    db_session.refresh(task)
    assert task.workspace_status == "ready"
    assert task_dir.exists()


def test_change_set_actions_reject_task_execution_id_from_another_task(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project_root = tmp_path / "change-set-cross-task"
    project_root.mkdir(parents=True)
    project = Project(
        name="change-set-cross-task",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Target task",
        description="Review candidate",
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder="task-target",
    )
    other_task = Task(
        project_id=project.id,
        title="Other task",
        description="Wrong candidate",
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder="task-other",
    )
    session = SessionModel(project_id=project.id, name="cross-task-session")
    db_session.add_all([task, other_task, session])
    db_session.commit()
    db_session.refresh(task)
    db_session.refresh(other_task)
    db_session.refresh(session)

    other_execution = TaskExecution(
        session_id=session.id,
        task_id=other_task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(other_execution)
    db_session.commit()
    db_session.refresh(other_execution)
    db_session.add(
        LogEntry(
            session_id=session.id,
            task_id=other_task.id,
            task_execution_id=other_execution.id,
            level="INFO",
            message=TASK_CHANGE_SET_LOG_MESSAGE,
            log_metadata=json.dumps(
                {
                    "schema": "openclaw.task_execution_change_set.v1",
                    "task_id": other_task.id,
                    "task_execution_id": other_execution.id,
                    "changed_count": 1,
                    "added_files": ["wrong.md"],
                    "modified_files": [],
                    "deleted_files": [],
                    "warning_flags": [],
                }
            ),
        )
    )
    db_session.commit()

    promote_response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/promote",
        json={"note": "accepted", "task_execution_id": other_execution.id},
    )
    reject_response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/change-set/reject",
        json={"task_execution_id": other_execution.id, "note": "wrong task"},
    )

    assert promote_response.status_code == 409
    assert "different task" in promote_response.json()["detail"]
    assert reject_response.status_code == 409
    assert "different task" in reject_response.json()["detail"]
    db_session.refresh(task)
    assert task.workspace_status == "ready"


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
