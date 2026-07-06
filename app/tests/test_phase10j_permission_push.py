"""Phase 10J-a — Permission Push: LogEntry emission on approve/deny.

Covers:
- approve_permission() writes [PERMISSION_APPROVED] LogEntry
- deny_permission() writes [PERMISSION_DENIED] LogEntry
- metadata correctness (permission_id, session_id, task_id, action)
- no LogEntry written when permission request has no session_id
- existing permission flow (status transitions) unchanged
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from app.models import (
    LogEntry,
    PermissionRequest,
    Project,
    Session as SessionModel,
    Task,
    TaskStatus,
)
from app.services.permissions.approval import (
    PermissionApprovalService,
    PermissionStatus,
)


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def project(db_session: Session) -> Project:
    p = Project(name="perm-push-project", workspace_path=None)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def session_with_instance(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="perm-push-session",
        status="running",
        is_active=True,
        instance_id="perm-test-instance-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def task(db_session: Session, project: Project) -> Task:
    t = Task(
        project_id=project.id,
        title="perm-task",
        status=TaskStatus.RUNNING,
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


def _make_permission(
    db: Session,
    project: Project,
    session: SessionModel | None = None,
    task: Task | None = None,
) -> PermissionRequest:
    req = PermissionRequest(
        project_id=project.id,
        session_id=session.id if session else None,
        task_id=task.id if task else None,
        operation_type="shell_command",
        command="rm -rf /tmp/test",
        description="Execute shell command",
        status=PermissionStatus.PENDING.value,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _log_entries(db: Session, session_id: int) -> list[LogEntry]:
    return (
        db.query(LogEntry)
        .filter(LogEntry.session_id == session_id)
        .order_by(LogEntry.id.asc())
        .all()
    )


# ── tests ─────────────────────────────────────────────────────────────────────


class TestApprovePermissionEmitsLogEntry:
    def test_log_entry_created(
        self,
        db_session: Session,
        project: Project,
        session_with_instance: SessionModel,
        task: Task,
    ):
        req = _make_permission(db_session, project, session_with_instance, task)
        svc = PermissionApprovalService(db_session)

        svc.approve_permission(req.id, approved_by="operator@example.com")

        logs = _log_entries(db_session, session_with_instance.id)
        assert len(logs) == 1
        assert logs[0].message == "[PERMISSION_APPROVED]"

    def test_log_entry_level_is_info(
        self,
        db_session: Session,
        project: Project,
        session_with_instance: SessionModel,
        task: Task,
    ):
        req = _make_permission(db_session, project, session_with_instance, task)
        svc = PermissionApprovalService(db_session)

        svc.approve_permission(req.id, approved_by="operator@example.com")

        logs = _log_entries(db_session, session_with_instance.id)
        assert logs[0].level == "INFO"

    def test_metadata_correctness(
        self,
        db_session: Session,
        project: Project,
        session_with_instance: SessionModel,
        task: Task,
    ):
        req = _make_permission(db_session, project, session_with_instance, task)
        svc = PermissionApprovalService(db_session)

        svc.approve_permission(req.id, approved_by="operator@example.com")

        logs = _log_entries(db_session, session_with_instance.id)
        meta = json.loads(logs[0].log_metadata)
        assert meta["permission_id"] == req.id
        assert meta["session_id"] == session_with_instance.id
        assert meta["task_id"] == task.id
        assert meta["action"] == "approved"

    def test_session_instance_id_propagated(
        self,
        db_session: Session,
        project: Project,
        session_with_instance: SessionModel,
        task: Task,
    ):
        req = _make_permission(db_session, project, session_with_instance, task)
        svc = PermissionApprovalService(db_session)

        svc.approve_permission(req.id, approved_by="operator@example.com")

        logs = _log_entries(db_session, session_with_instance.id)
        assert logs[0].session_instance_id == session_with_instance.instance_id

    def test_permission_status_still_approved(
        self,
        db_session: Session,
        project: Project,
        session_with_instance: SessionModel,
        task: Task,
    ):
        req = _make_permission(db_session, project, session_with_instance, task)
        svc = PermissionApprovalService(db_session)

        updated = svc.approve_permission(req.id, approved_by="operator@example.com")

        assert updated.status == PermissionStatus.APPROVED.value
        assert updated.approved_by == "operator@example.com"

    def test_no_log_entry_when_no_session(self, db_session: Session, project: Project):
        req = _make_permission(db_session, project, session=None, task=None)
        svc = PermissionApprovalService(db_session)

        svc.approve_permission(req.id, approved_by="operator@example.com")

        total_logs = db_session.query(LogEntry).count()
        assert total_logs == 0


class TestDenyPermissionEmitsLogEntry:
    def test_log_entry_created(
        self,
        db_session: Session,
        project: Project,
        session_with_instance: SessionModel,
        task: Task,
    ):
        req = _make_permission(db_session, project, session_with_instance, task)
        svc = PermissionApprovalService(db_session)

        svc.deny_permission(req.id, reason="Too risky")

        logs = _log_entries(db_session, session_with_instance.id)
        assert len(logs) == 1
        assert logs[0].message == "[PERMISSION_DENIED]"

    def test_log_entry_level_is_info(
        self,
        db_session: Session,
        project: Project,
        session_with_instance: SessionModel,
        task: Task,
    ):
        req = _make_permission(db_session, project, session_with_instance, task)
        svc = PermissionApprovalService(db_session)

        svc.deny_permission(req.id, reason="Too risky")

        logs = _log_entries(db_session, session_with_instance.id)
        assert logs[0].level == "INFO"

    def test_metadata_correctness(
        self,
        db_session: Session,
        project: Project,
        session_with_instance: SessionModel,
        task: Task,
    ):
        req = _make_permission(db_session, project, session_with_instance, task)
        svc = PermissionApprovalService(db_session)

        svc.deny_permission(req.id, reason="Too risky")

        logs = _log_entries(db_session, session_with_instance.id)
        meta = json.loads(logs[0].log_metadata)
        assert meta["permission_id"] == req.id
        assert meta["session_id"] == session_with_instance.id
        assert meta["task_id"] == task.id
        assert meta["action"] == "denied"

    def test_session_instance_id_propagated(
        self,
        db_session: Session,
        project: Project,
        session_with_instance: SessionModel,
        task: Task,
    ):
        req = _make_permission(db_session, project, session_with_instance, task)
        svc = PermissionApprovalService(db_session)

        svc.deny_permission(req.id, reason="Too risky")

        logs = _log_entries(db_session, session_with_instance.id)
        assert logs[0].session_instance_id == session_with_instance.instance_id

    def test_permission_status_still_denied(
        self,
        db_session: Session,
        project: Project,
        session_with_instance: SessionModel,
        task: Task,
    ):
        req = _make_permission(db_session, project, session_with_instance, task)
        svc = PermissionApprovalService(db_session)

        updated = svc.deny_permission(req.id, reason="Too risky")

        assert updated.status == PermissionStatus.DENIED.value
        assert updated.denied_reason == "Too risky"

    def test_no_log_entry_when_no_session(self, db_session: Session, project: Project):
        req = _make_permission(db_session, project, session=None, task=None)
        svc = PermissionApprovalService(db_session)

        svc.deny_permission(req.id, reason="No session")

        total_logs = db_session.query(LogEntry).count()
        assert total_logs == 0
