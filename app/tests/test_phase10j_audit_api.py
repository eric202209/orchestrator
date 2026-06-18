"""Phase 10J-d — Structured Audit Events API tests.

Covers:
- admin can list structured events
- non-admin rejected (401/403)
- event_type filter: PERMISSION_APPROVED (with and without brackets)
- session_id / task_id / level filters
- since / until datetime filters
- pagination: limit / offset
- order: asc / desc
- invalid JSON metadata does not 500
- unstructured LogEntry excluded by default
- rows with metadata-only (no bracket message) included
- PERMISSION_APPROVED / PERMISSION_DENIED events appear correctly
- guidance warning events appear correctly
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    Task,
    TaskStatus,
)


# ── helpers ────────────────────────────────────────────────────────────────────

URL = "/api/v1/ops/audit-events"


def _add_log(
    db: Session,
    message: str,
    *,
    level: str = "INFO",
    session_id: int | None = None,
    task_id: int | None = None,
    session_instance_id: str | None = None,
    log_metadata: str | None = None,
    created_at: datetime | None = None,
) -> LogEntry:
    entry = LogEntry(
        message=message,
        level=level,
        session_id=session_id,
        task_id=task_id,
        session_instance_id=session_instance_id,
        log_metadata=log_metadata,
    )
    if created_at is not None:
        entry.created_at = created_at
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def project(db_session: Session) -> Project:
    p = Project(name="audit-project", workspace_path=None)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def session(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="audit-session",
        status="running",
        is_active=True,
        instance_id="audit-instance-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def task(db_session: Session, project: Project) -> Task:
    t = Task(project_id=project.id, title="audit-task", status=TaskStatus.RUNNING)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


# ── auth tests ─────────────────────────────────────────────────────────────────


class TestAuditEventsAuth:
    def test_admin_can_access(self, authenticated_client: TestClient):
        resp = authenticated_client.get(URL)
        assert resp.status_code == 200

    def test_non_admin_rejected(self, api_client: TestClient):
        resp = api_client.get(URL)
        assert resp.status_code in (401, 403)

    def test_response_shape(self, authenticated_client: TestClient):
        body = authenticated_client.get(URL).json()
        assert "total" in body
        assert "limit" in body
        assert "offset" in body
        assert "items" in body
        assert isinstance(body["items"], list)


# ── default filter (structured only) ──────────────────────────────────────────


class TestDefaultStructuredFilter:
    def test_unstructured_log_excluded(
        self, authenticated_client: TestClient, db_session: Session
    ):
        _add_log(db_session, "plain text log — not structured")
        resp = authenticated_client.get(URL)
        body = resp.json()
        messages = [i["message"] for i in body["items"]]
        assert "plain text log — not structured" not in messages

    def test_bracketed_message_included(
        self, authenticated_client: TestClient, db_session: Session
    ):
        _add_log(db_session, "[PERMISSION_APPROVED]")
        resp = authenticated_client.get(URL)
        body = resp.json()
        messages = [i["message"] for i in body["items"]]
        assert "[PERMISSION_APPROVED]" in messages

    def test_metadata_only_row_included(
        self, authenticated_client: TestClient, db_session: Session
    ):
        _add_log(
            db_session,
            "non-bracketed message with metadata",
            log_metadata=json.dumps({"key": "value"}),
        )
        resp = authenticated_client.get(URL)
        body = resp.json()
        messages = [i["message"] for i in body["items"]]
        assert "non-bracketed message with metadata" in messages

    def test_empty_db_returns_zero(self, authenticated_client: TestClient):
        resp = authenticated_client.get(URL)
        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []


# ── event_type filter ──────────────────────────────────────────────────────────


class TestEventTypeFilter:
    def test_event_type_without_brackets(
        self, authenticated_client: TestClient, db_session: Session
    ):
        _add_log(db_session, "[PERMISSION_APPROVED]")
        _add_log(db_session, "[PERMISSION_DENIED]")
        resp = authenticated_client.get(
            URL, params={"event_type": "PERMISSION_APPROVED"}
        )
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["event_type"] == "PERMISSION_APPROVED"

    def test_event_type_with_brackets_normalized(
        self, authenticated_client: TestClient, db_session: Session
    ):
        _add_log(db_session, "[PERMISSION_APPROVED]")
        _add_log(db_session, "[PERMISSION_DENIED]")
        resp = authenticated_client.get(
            URL, params={"event_type": "[PERMISSION_APPROVED]"}
        )
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["event_type"] == "PERMISSION_APPROVED"

    def test_event_type_no_match_returns_empty(
        self, authenticated_client: TestClient, db_session: Session
    ):
        _add_log(db_session, "[PERMISSION_APPROVED]")
        resp = authenticated_client.get(URL, params={"event_type": "NONEXISTENT_EVENT"})
        body = resp.json()
        assert body["total"] == 0

    def test_event_type_extracts_label_in_response(
        self, authenticated_client: TestClient, db_session: Session
    ):
        _add_log(db_session, "[GUIDANCE_POST_WRITE_WARNING] some extra text")
        resp = authenticated_client.get(
            URL, params={"event_type": "GUIDANCE_POST_WRITE_WARNING"}
        )
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["event_type"] == "GUIDANCE_POST_WRITE_WARNING"


# ── session / task / level filters ────────────────────────────────────────────


class TestFieldFilters:
    def test_session_id_filter(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        session: SessionModel,
    ):
        _add_log(db_session, "[PERMISSION_APPROVED]", session_id=session.id)
        _add_log(db_session, "[PERMISSION_DENIED]", session_id=999)
        resp = authenticated_client.get(URL, params={"session_id": session.id})
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["session_id"] == session.id

    def test_task_id_filter(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        task: Task,
    ):
        _add_log(db_session, "[PERMISSION_APPROVED]", task_id=task.id)
        _add_log(db_session, "[PERMISSION_DENIED]", task_id=999)
        resp = authenticated_client.get(URL, params={"task_id": task.id})
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["task_id"] == task.id

    def test_level_filter(self, authenticated_client: TestClient, db_session: Session):
        _add_log(db_session, "[PERMISSION_APPROVED]", level="INFO")
        _add_log(db_session, "[PERMISSION_DENIED]", level="WARN")
        resp = authenticated_client.get(URL, params={"level": "WARN"})
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["level"] == "WARN"

    def test_level_filter_case_insensitive(
        self, authenticated_client: TestClient, db_session: Session
    ):
        _add_log(db_session, "[PERMISSION_APPROVED]", level="INFO")
        resp = authenticated_client.get(URL, params={"level": "info"})
        body = resp.json()
        assert body["total"] == 1


# ── since / until filters ──────────────────────────────────────────────────────


class TestDatetimeFilters:
    def test_since_filter_excludes_older(
        self, authenticated_client: TestClient, db_session: Session
    ):
        old_ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        new_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)
        _add_log(db_session, "[PERMISSION_APPROVED]", created_at=old_ts)
        _add_log(db_session, "[PERMISSION_DENIED]", created_at=new_ts)
        since = "2026-01-01T00:00:00Z"
        resp = authenticated_client.get(URL, params={"since": since})
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["message"] == "[PERMISSION_DENIED]"

    def test_until_filter_excludes_newer(
        self, authenticated_client: TestClient, db_session: Session
    ):
        old_ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        new_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)
        _add_log(db_session, "[PERMISSION_APPROVED]", created_at=old_ts)
        _add_log(db_session, "[PERMISSION_DENIED]", created_at=new_ts)
        until = "2026-01-01T00:00:00Z"
        resp = authenticated_client.get(URL, params={"until": until})
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["message"] == "[PERMISSION_APPROVED]"

    def test_invalid_since_returns_422(
        self, authenticated_client: TestClient, db_session: Session
    ):
        _add_log(db_session, "[PERMISSION_APPROVED]")
        resp = authenticated_client.get(URL, params={"since": "not-a-date"})
        assert resp.status_code == 422
        assert "since" in resp.json()["detail"].lower()

    def test_invalid_until_returns_422(
        self, authenticated_client: TestClient, db_session: Session
    ):
        _add_log(db_session, "[PERMISSION_APPROVED]")
        resp = authenticated_client.get(URL, params={"until": "garbage-value"})
        assert resp.status_code == 422
        assert "until" in resp.json()["detail"].lower()


# ── pagination ─────────────────────────────────────────────────────────────────


class TestPagination:
    def test_limit_reduces_items(
        self, authenticated_client: TestClient, db_session: Session
    ):
        for i in range(5):
            _add_log(db_session, f"[EVENT_{i}]")
        resp = authenticated_client.get(URL, params={"limit": 2})
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["total"] == 5
        assert body["limit"] == 2

    def test_offset_pages(self, authenticated_client: TestClient, db_session: Session):
        for i in range(4):
            _add_log(db_session, f"[EVT]")
        resp_p1 = authenticated_client.get(URL, params={"limit": 2, "offset": 0})
        resp_p2 = authenticated_client.get(URL, params={"limit": 2, "offset": 2})
        ids_p1 = {i["id"] for i in resp_p1.json()["items"]}
        ids_p2 = {i["id"] for i in resp_p2.json()["items"]}
        assert ids_p1.isdisjoint(ids_p2)

    def test_offset_beyond_total_returns_empty_items(
        self, authenticated_client: TestClient, db_session: Session
    ):
        _add_log(db_session, "[EVT]")
        resp = authenticated_client.get(URL, params={"limit": 10, "offset": 999})
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 1

    def test_limit_default_100(self, authenticated_client: TestClient):
        resp = authenticated_client.get(URL)
        assert resp.json()["limit"] == 100

    def test_limit_max_500(self, authenticated_client: TestClient):
        resp = authenticated_client.get(URL, params={"limit": 501})
        assert resp.status_code == 422


# ── ordering ───────────────────────────────────────────────────────────────────


class TestOrdering:
    def test_desc_order_newest_first(
        self, authenticated_client: TestClient, db_session: Session
    ):
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        _add_log(db_session, "[OLDER]", created_at=t1)
        _add_log(db_session, "[NEWER]", created_at=t2)
        resp = authenticated_client.get(URL, params={"order": "desc"})
        items = resp.json()["items"]
        assert items[0]["message"] == "[NEWER]"
        assert items[1]["message"] == "[OLDER]"

    def test_asc_order_oldest_first(
        self, authenticated_client: TestClient, db_session: Session
    ):
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        _add_log(db_session, "[OLDER]", created_at=t1)
        _add_log(db_session, "[NEWER]", created_at=t2)
        resp = authenticated_client.get(URL, params={"order": "asc"})
        items = resp.json()["items"]
        assert items[0]["message"] == "[OLDER]"
        assert items[1]["message"] == "[NEWER]"

    def test_invalid_order_rejected(self, authenticated_client: TestClient):
        resp = authenticated_client.get(URL, params={"order": "sideways"})
        assert resp.status_code == 422


# ── metadata safety ────────────────────────────────────────────────────────────


class TestMetadataSafety:
    def test_valid_metadata_parsed(
        self, authenticated_client: TestClient, db_session: Session
    ):
        meta = json.dumps({"permission_id": 7, "action": "approved"})
        _add_log(db_session, "[PERMISSION_APPROVED]", log_metadata=meta)
        resp = authenticated_client.get(URL)
        item = resp.json()["items"][0]
        assert item["metadata"]["permission_id"] == 7
        assert item["metadata"]["action"] == "approved"

    def test_invalid_metadata_does_not_500(
        self, authenticated_client: TestClient, db_session: Session
    ):
        _add_log(db_session, "[PERMISSION_APPROVED]", log_metadata="NOT_JSON{{{{")
        resp = authenticated_client.get(URL)
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["metadata"] is None

    def test_null_metadata_returns_null(
        self, authenticated_client: TestClient, db_session: Session
    ):
        _add_log(db_session, "[PERMISSION_APPROVED]", log_metadata=None)
        resp = authenticated_client.get(URL)
        item = resp.json()["items"][0]
        assert item["metadata"] is None


# ── known event type correctness ──────────────────────────────────────────────


class TestKnownEventTypes:
    def test_permission_approved_event(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        session: SessionModel,
        task: Task,
    ):
        meta = json.dumps(
            {
                "permission_id": 42,
                "session_id": session.id,
                "task_id": task.id,
                "action": "approved",
            }
        )
        _add_log(
            db_session,
            "[PERMISSION_APPROVED]",
            session_id=session.id,
            task_id=task.id,
            session_instance_id=session.instance_id,
            log_metadata=meta,
        )
        resp = authenticated_client.get(
            URL, params={"event_type": "PERMISSION_APPROVED"}
        )
        body = resp.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["event_type"] == "PERMISSION_APPROVED"
        assert item["session_id"] == session.id
        assert item["task_id"] == task.id
        assert item["session_instance_id"] == session.instance_id
        assert item["metadata"]["action"] == "approved"
        assert item["metadata"]["permission_id"] == 42

    def test_permission_denied_event(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        session: SessionModel,
    ):
        meta = json.dumps(
            {
                "permission_id": 9,
                "session_id": session.id,
                "task_id": None,
                "action": "denied",
            }
        )
        _add_log(
            db_session,
            "[PERMISSION_DENIED]",
            session_id=session.id,
            log_metadata=meta,
        )
        resp = authenticated_client.get(URL, params={"event_type": "PERMISSION_DENIED"})
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["metadata"]["action"] == "denied"

    def test_guidance_warning_event(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        session: SessionModel,
    ):
        meta = json.dumps({"file": "app/models.py", "guidance_id": 3})
        _add_log(
            db_session,
            "[GUIDANCE_POST_WRITE_WARNING] violated constraint in app/models.py",
            level="WARN",
            session_id=session.id,
            log_metadata=meta,
        )
        resp = authenticated_client.get(
            URL, params={"event_type": "GUIDANCE_POST_WRITE_WARNING"}
        )
        body = resp.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["event_type"] == "GUIDANCE_POST_WRITE_WARNING"
        assert item["level"] == "WARN"
        assert item["metadata"]["guidance_id"] == 3
