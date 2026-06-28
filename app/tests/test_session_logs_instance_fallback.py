from __future__ import annotations

from datetime import UTC, datetime

from app.models import LogEntry, Project, Session as SessionModel
from app.services.session.session_inspection_service import (
    get_session_logs_payload,
    get_sorted_logs_payload,
)


def test_session_logs_include_legacy_null_instance_rows(db_session):
    project = Project(name="Log Fallback", workspace_path="log-fallback")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="Run with legacy logs",
        status="completed",
        instance_id="current-instance",
    )
    db_session.add(session)
    db_session.flush()
    db_session.add_all(
        [
            LogEntry(
                session_id=session.id,
                level="INFO",
                message="legacy visible log",
                session_instance_id=None,
                created_at=datetime.now(UTC),
            ),
            LogEntry(
                session_id=session.id,
                level="INFO",
                message="current visible log",
                session_instance_id="current-instance",
                created_at=datetime.now(UTC),
            ),
            LogEntry(
                session_id=session.id,
                level="INFO",
                message="stale hidden log",
                session_instance_id="old-instance",
                created_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()

    payload = get_session_logs_payload(db_session, session.id)
    messages = {log.message for log in payload["logs"]}

    assert "legacy visible log" in messages
    assert "current visible log" in messages
    assert "stale hidden log" not in messages


def test_sorted_session_logs_include_legacy_null_instance_rows(db_session):
    project = Project(name="Sorted Log Fallback", workspace_path="sorted-log-fallback")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="Sorted run with legacy logs",
        status="completed",
        instance_id="current-instance",
    )
    db_session.add(session)
    db_session.flush()
    db_session.add(
        LogEntry(
            session_id=session.id,
            level="INFO",
            message="legacy sorted log",
            session_instance_id=None,
            created_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    payload = get_sorted_logs_payload(db_session, session.id)

    assert [log["message"] for log in payload["logs"]] == ["legacy sorted log"]
