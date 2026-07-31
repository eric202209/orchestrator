from app.models import (
    ExecutionFailureSummary,
    LogEntry,
    PlanningSession,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskCheckpoint,
    TaskStatus,
    SessionTask,
)


def _task_graph(db_session):
    project = Project(name="Public delete graph")
    db_session.add(project)
    db_session.flush()

    session = SessionModel(project_id=project.id, name="Public delete session")
    task = Task(project_id=project.id, title="Public delete task")
    db_session.add_all([session, task])
    db_session.flush()

    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.FAILED,
    )
    db_session.add(execution)
    db_session.flush()

    change_set = TaskExecutionChangeSet(
        project_id=project.id,
        task_id=task.id,
        session_id=session.id,
        task_execution_id=execution.id,
        base_snapshot_key="base",
    )
    execution_log = LogEntry(
        session_id=session.id,
        task_execution_id=execution.id,
        level="INFO",
        message="execution-only log",
    )
    db_session.add_all([change_set, execution_log])
    db_session.commit()
    return project, session, task, execution, change_set, execution_log


def test_public_task_delete_removes_complete_task_owned_execution_graph(
    authenticated_client, db_session
):
    _, _, task, execution, change_set, execution_log = _task_graph(db_session)
    task_id, execution_id, change_set_id, execution_log_id = (
        task.id,
        execution.id,
        change_set.id,
        execution_log.id,
    )
    db_session.connection().exec_driver_sql("PRAGMA foreign_keys = ON")

    response = authenticated_client.delete(f"/api/v1/tasks/{task_id}")

    assert response.status_code == 204
    db_session.expire_all()
    assert db_session.get(Task, task_id) is None
    assert db_session.get(TaskExecution, execution_id) is None
    assert db_session.get(TaskExecutionChangeSet, change_set_id) is None
    assert db_session.get(LogEntry, execution_log_id) is None


def test_public_project_delete_removes_task_graph_and_retains_soft_history(
    authenticated_client, db_session
):
    project, session, task, execution, change_set, execution_log = _task_graph(
        db_session
    )
    planning_session = PlanningSession(
        project_id=project.id,
        title="Retained planning history",
        prompt="Plan the task",
        status="completed",
    )
    db_session.add(planning_session)
    db_session.flush()
    failure_summary = ExecutionFailureSummary(
        session_id=session.id,
        summary="Retained failure history",
        replan_planning_session_id=planning_session.id,
    )
    db_session.add(failure_summary)
    db_session.commit()
    project_id, session_id, task_id, execution_id, change_set_id, execution_log_id = (
        project.id,
        session.id,
        task.id,
        execution.id,
        change_set.id,
        execution_log.id,
    )
    failure_summary_id, planning_session_id = failure_summary.id, planning_session.id
    db_session.connection().exec_driver_sql("PRAGMA foreign_keys = ON")

    response = authenticated_client.delete(f"/api/v1/projects/{project_id}")

    assert response.status_code == 200
    db_session.expire_all()
    assert db_session.get(Task, task_id) is None
    assert db_session.get(TaskExecution, execution_id) is None
    assert db_session.get(TaskExecutionChangeSet, change_set_id) is None
    assert db_session.get(LogEntry, execution_log_id) is None
    assert db_session.get(Project, project_id).deleted_at is not None
    assert db_session.get(SessionModel, session_id).deleted_at is not None
    assert db_session.get(ExecutionFailureSummary, failure_summary_id) is not None
    assert db_session.get(PlanningSession, planning_session_id) is not None


def test_project_retirement_preserves_graph_and_blocks_new_runtime_work(
    authenticated_client, db_session
):
    project, session, task, execution, change_set, execution_log = _task_graph(
        db_session
    )
    planning_session = PlanningSession(
        project_id=project.id,
        title="Retirement planning history",
        prompt="Retain this planning evidence",
        status="completed",
    )
    checkpoint = TaskCheckpoint(
        task_id=task.id,
        session_id=session.id,
        checkpoint_type="after",
        description="Retained checkpoint",
        state_snapshot='{"retained": true}',
    )
    session_task = SessionTask(
        session_id=session.id, task_id=task.id, status=TaskStatus.DONE
    )
    failure_summary = ExecutionFailureSummary(
        session_id=session.id,
        summary="Retained failure summary",
        replan_planning_session_id=planning_session.id,
    )
    db_session.add_all([planning_session, checkpoint, session_task, failure_summary])
    db_session.commit()
    ids = {
        "project": project.id,
        "session": session.id,
        "task": task.id,
        "execution": execution.id,
        "change_set": change_set.id,
        "log": execution_log.id,
        "planning": planning_session.id,
        "checkpoint": checkpoint.id,
        "session_task": session_task.id,
        "failure": failure_summary.id,
    }

    retired = authenticated_client.post(
        f"/api/v1/projects/{project.id}/retire",
        json={"reason": "legacy_duplicate_workspace_owner"},
    )
    assert retired.status_code == 200
    assert retired.json()["lifecycle_status"] == "retired"
    assert retired.json()["is_launch_eligible"] is False
    assert retired.json()["retirement_reason"] == "legacy_duplicate_workspace_owner"
    retired_at = retired.json()["retired_at"]

    repeat = authenticated_client.post(f"/api/v1/projects/{project.id}/retire")
    assert repeat.status_code == 200
    assert repeat.json()["retired_at"] == retired_at

    db_session.expire_all()
    assert db_session.get(Project, ids["project"]).retired_at is not None
    assert db_session.get(Project, ids["project"]).retired_by_user_id is not None
    assert db_session.get(SessionModel, ids["session"]) is not None
    assert db_session.get(Task, ids["task"]) is not None
    assert db_session.get(TaskExecution, ids["execution"]) is not None
    assert db_session.get(TaskExecutionChangeSet, ids["change_set"]) is not None
    assert db_session.get(LogEntry, ids["log"]) is not None
    assert db_session.get(PlanningSession, ids["planning"]) is not None
    assert db_session.get(TaskCheckpoint, ids["checkpoint"]) is not None
    assert db_session.get(SessionTask, ids["session_task"]) is not None
    assert db_session.get(ExecutionFailureSummary, ids["failure"]) is not None

    assert authenticated_client.get(f"/api/v1/projects/{project.id}").status_code == 200
    assert project.id not in {
        item["id"] for item in authenticated_client.get("/api/v1/projects").json()
    }
    assert (
        authenticated_client.get(f"/api/v1/projects/{project.id}/tasks").status_code
        == 200
    )
    assert authenticated_client.get(f"/api/v1/sessions/{session.id}").status_code == 200
    assert task.id not in {
        item["id"] for item in authenticated_client.get("/api/v1/tasks").json()
    }
    assert session.id not in {
        item["id"] for item in authenticated_client.get("/api/v1/sessions").json()
    }

    session_response = authenticated_client.post(
        "/api/v1/sessions", json={"project_id": project.id, "name": "blocked"}
    )
    task_response = authenticated_client.post(
        "/api/v1/tasks", json={"project_id": project.id, "title": "blocked"}
    )
    planning_response = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={"project_id": project.id, "prompt": "blocked"},
    )
    dogfood_response = authenticated_client.get(
        f"/api/v1/projects/{project.id}/dogfood-admission"
    )
    execution_response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/execute", json={"prompt": "blocked"}
    )
    for response in (
        session_response,
        task_response,
        planning_response,
        dogfood_response,
        execution_response,
    ):
        assert response.status_code == 409
        assert response.json()["detail"]["category"] == "project_retired"

    assert (
        db_session.query(SessionModel)
        .filter(SessionModel.project_id == project.id)
        .count()
        == 1
    )
    assert db_session.query(Task).filter(Task.project_id == project.id).count() == 1
    assert (
        db_session.query(TaskExecution).filter(TaskExecution.task_id == task.id).count()
        == 1
    )
