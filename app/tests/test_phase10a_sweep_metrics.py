from __future__ import annotations

from app.models import Project, Task, TaskStatus
from scripts.phase10a_operational_stability_sweep import (
    _delete_stale_probe_records,
    _task_outcome_metrics_from_rows,
)


def test_task_outcome_metrics_split_final_first_pass_and_recovered_success():
    metrics = _task_outcome_metrics_from_rows(
        [
            {"id": 287, "status": "done"},
            {"id": 288, "status": "done"},
            {"id": 289, "status": "done"},
            {"id": 290, "status": "done"},
        ],
        [
            {"id": 422, "task_id": 287, "attempt_number": 1, "status": "done"},
            {"id": 423, "task_id": 288, "attempt_number": 1, "status": "done"},
            {"id": 424, "task_id": 289, "attempt_number": 1, "status": "done"},
            {"id": 425, "task_id": 290, "attempt_number": 1, "status": "failed"},
            {"id": 426, "task_id": 290, "attempt_number": 2, "status": "done"},
        ],
    )

    assert metrics["final_done"] == 4
    assert metrics["first_pass_success"] == 3
    assert metrics["recovered_success"] == 1
    assert metrics["execution_attempts"] == 5
    assert metrics["execution_attempts_done"] == 4
    assert metrics["final_success_rate"] == 1.0
    assert metrics["first_pass_success_rate"] == 0.75
    assert metrics["recovered_success_rate"] == 0.25
    assert metrics["attempt_success_rate"] == 0.8
    assert metrics["first_pass_task_ids"] == [287, 288, 289]
    assert metrics["recovered_task_ids"] == [290]


def test_stale_probe_cleanup_removes_probe_task_workspace(db_session, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    probe_subfolder = "task-phase-10a-stale-running-session-probe"
    probe_workspace = project_root / probe_subfolder
    probe_workspace.mkdir()
    (probe_workspace / "probe.txt").write_text("synthetic probe", encoding="utf-8")

    project = Project(name="Probe Project", workspace_path=str(project_root))
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Phase 10A stale running session probe",
        status=TaskStatus.RUNNING,
        task_subfolder=probe_subfolder,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    task_id = task.id

    _delete_stale_probe_records(
        db_session,
        {"task_id": task_id, "task_subfolder": probe_subfolder},
    )

    assert not probe_workspace.exists()
    assert db_session.query(Task).filter(Task.id == task_id).first() is None
