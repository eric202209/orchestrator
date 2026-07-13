"""Phase 25A-2 regressions for Task Execution Sandbox metadata containment."""

from pathlib import Path
from types import SimpleNamespace

from app.models import Project, Session as SessionModel, Task, TaskExecution, TaskStatus
from app.services.tasks.service import TaskService
from app.services.workspace.task_sandbox_allocator import (
    allocate_task_sandbox,
    dispose_task_sandbox,
)
from app.services.workspace.workspace_paths import (
    AUTO_SNAPSHOT_ROOT,
    HYDRATION_EXCLUDED_NAMES,
    RUNTIME_METADATA_FILENAME,
)
from app.services.workspace.workspace_snapshot_service import WorkspaceSnapshotService


def _snapshot_service(project_root: Path) -> WorkspaceSnapshotService:
    service = WorkspaceSnapshotService(None)
    service.get_project_root = lambda _project: project_root
    return service


def test_sandbox_snapshot_does_not_retain_runtime_metadata(tmp_path: Path):
    project_root = tmp_path / "project"
    sandbox = tmp_path / "runtime" / "tasks" / "1" / "1"
    project_root.mkdir()
    sandbox.mkdir(parents=True)
    (sandbox / RUNTIME_METADATA_FILENAME).write_text("{}\n", encoding="utf-8")
    (sandbox / "user.txt").write_text("user content\n", encoding="utf-8")

    result = _snapshot_service(project_root).create_workspace_snapshot(
        SimpleNamespace(id=1),
        sandbox,
        snapshot_key="task-1-execution-1-pre-run",
    )

    snapshot = Path(result["snapshot_path"])
    assert (snapshot / "user.txt").exists()
    assert not (snapshot / RUNTIME_METADATA_FILENAME).exists()


def test_legacy_snapshot_restore_excludes_runtime_metadata(tmp_path: Path):
    project_root = tmp_path / "project"
    retained_root = tmp_path / "retained"
    project_root.mkdir()
    snapshot = retained_root / AUTO_SNAPSHOT_ROOT / "legacy"
    snapshot.mkdir(parents=True)
    (snapshot / RUNTIME_METADATA_FILENAME).write_text("legacy\n", encoding="utf-8")
    (snapshot / "user.txt").write_text("restore me\n", encoding="utf-8")

    result = _snapshot_service(project_root).restore_workspace_snapshot_unlocked(
        SimpleNamespace(id=1),
        project_root,
        snapshot_key="legacy",
        snapshot_root=retained_root,
    )

    assert result["restored"] is True
    assert (project_root / "user.txt").read_text(encoding="utf-8") == "restore me\n"
    assert not (project_root / RUNTIME_METADATA_FILENAME).exists()


def test_reject_restores_user_files_while_excluding_runtime_metadata(
    db_session, tmp_path: Path
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "user.txt").write_text("before\n", encoding="utf-8")

    project = Project(name="phase25a2-disposable", workspace_path=str(project_root))
    task = Task(
        project_id=1,
        title="Reject metadata regression",
        description="Keep runtime metadata in the Task Execution Sandbox",
        status=TaskStatus.DONE,
        workspace_status="ready",
        task_subfolder="task-phase25a2",
    )
    execution_session = SessionModel(project_id=1, name="phase25a2-session")
    db_session.add(project)
    db_session.flush()
    task.project_id = project.id
    execution_session.project_id = project.id
    db_session.add_all([task, execution_session])
    db_session.commit()
    db_session.refresh(project)
    db_session.refresh(task)
    db_session.refresh(execution_session)
    execution = TaskExecution(
        session_id=execution_session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)

    runtime_root = tmp_path / "runtime"
    sandbox = allocate_task_sandbox(
        project_root,
        project_id=project.id,
        task_execution_id=execution.id,
        runtime_root=runtime_root,
        executor="openclaw",
    )
    snapshot_key = "task-1-execution-1-pre-run"
    task_service = TaskService(db_session)
    task_service.create_workspace_snapshot(
        project,
        sandbox.path,
        snapshot_key=snapshot_key,
        snapshot_root=runtime_root,
    )
    assert (sandbox.path / RUNTIME_METADATA_FILENAME).exists()
    task_service.retain_workspace_snapshot(
        project,
        source_root=runtime_root,
        snapshot_key=snapshot_key,
    )

    (project_root / "user.txt").write_text("candidate\n", encoding="utf-8")
    (project_root / "new.txt").write_text("candidate file\n", encoding="utf-8")

    try:
        result = task_service.reject_task_execution_change_set(
            project,
            task,
            task_execution_id=execution.id,
            snapshot_key=snapshot_key,
        )

        assert result["rejected"] is True
        assert result["restore_result"]["restored"] is True
        assert (project_root / "user.txt").read_text(encoding="utf-8") == "before\n"
        assert not (project_root / "new.txt").exists()
        assert not (project_root / RUNTIME_METADATA_FILENAME).exists()
        assert result["snapshot_cleanup"]["existed"] is True
        assert not (project_root / AUTO_SNAPSHOT_ROOT / snapshot_key).exists()
    finally:
        dispose_task_sandbox(sandbox)


def test_runtime_metadata_filename_remains_in_exclusion_contract():
    assert RUNTIME_METADATA_FILENAME in HYDRATION_EXCLUDED_NAMES
