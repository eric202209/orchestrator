from app.models import (
    ExecutionFailureSummary,
    LogEntry,
    PlanningSession,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
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
