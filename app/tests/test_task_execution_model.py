from datetime import datetime, timezone

from app.models import Project, Session as SessionModel, Task, TaskExecution, TaskStatus
from app.services.tasks.execution import (
    executions_for_session,
    executions_for_task,
    latest_execution_for_session_task,
    next_attempt_number,
)


def _create_project_session_task(db_session):
    project = Project(name="Execution Project")
    db_session.add(project)
    db_session.flush()

    session = SessionModel(
        project_id=project.id,
        name="Canonical Session",
        status="pending",
    )
    task = Task(
        project_id=project.id,
        title="Implement feature",
        status=TaskStatus.PENDING,
    )
    db_session.add_all([session, task])
    db_session.flush()
    return project, session, task


def test_task_execution_model_creation_links_session_and_task(db_session):
    _, session, task = _create_project_session_task(db_session)

    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)

    assert execution.id is not None
    assert execution.created_at is not None
    assert execution.session.id == session.id
    assert execution.task.id == task.id
    assert session.task_executions == [execution]
    assert task.executions == [execution]


def test_next_attempt_number_is_scoped_to_session_and_task(db_session):
    project, session, task = _create_project_session_task(db_session)
    other_session = SessionModel(
        project_id=project.id,
        name="Other Session",
        status="pending",
    )
    other_task = Task(
        project_id=project.id,
        title="Other task",
        status=TaskStatus.PENDING,
    )
    db_session.add_all([other_session, other_task])
    db_session.flush()

    db_session.add_all(
        [
            TaskExecution(
                session_id=session.id,
                task_id=task.id,
                attempt_number=1,
                status=TaskStatus.FAILED,
            ),
            TaskExecution(
                session_id=session.id,
                task_id=task.id,
                attempt_number=2,
                status=TaskStatus.PENDING,
            ),
            TaskExecution(
                session_id=other_session.id,
                task_id=task.id,
                attempt_number=7,
                status=TaskStatus.PENDING,
            ),
            TaskExecution(
                session_id=session.id,
                task_id=other_task.id,
                attempt_number=4,
                status=TaskStatus.PENDING,
            ),
        ]
    )
    db_session.commit()

    assert next_attempt_number(db_session, session.id, task.id) == 3
    assert next_attempt_number(db_session, other_session.id, task.id) == 8
    assert next_attempt_number(db_session, session.id, other_task.id) == 5


def test_next_attempt_number_is_idempotent_without_insert(db_session):
    _, session, task = _create_project_session_task(db_session)

    first = next_attempt_number(db_session, session.id, task.id)
    second = next_attempt_number(db_session, session.id, task.id)

    assert first == 1
    assert second == 1
    assert db_session.query(TaskExecution).count() == 0


def test_task_execution_read_helpers(db_session):
    project, session, task = _create_project_session_task(db_session)
    other_session = SessionModel(
        project_id=project.id,
        name="Other Session",
        status="pending",
    )
    db_session.add(other_session)
    db_session.flush()

    attempt_1 = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.FAILED,
    )
    attempt_2 = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=2,
        status=TaskStatus.RUNNING,
    )
    other_execution = TaskExecution(
        session_id=other_session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.PENDING,
    )
    db_session.add_all([attempt_1, attempt_2, other_execution])
    db_session.commit()

    assert (
        latest_execution_for_session_task(db_session, session.id, task.id) == attempt_2
    )
    assert executions_for_session(db_session, session.id) == [attempt_1, attempt_2]
    assert executions_for_task(db_session, task.id) == [
        attempt_1,
        attempt_2,
        other_execution,
    ]
