"""Tests for GET /sessions/{session_id}/knowledge-usage."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from app.models import (
    KnowledgeItem,
    KnowledgeUsageLog,
    Project,
    Session as SessionModel,
)


def _make_project(db):
    project = Project(name="KU Test Project", workspace_path="/tmp/ku_test")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_session(db, project):
    count = db.query(SessionModel).filter(SessionModel.project_id == project.id).count()
    session = SessionModel(
        project_id=project.id,
        name=f"KU Session {count + 1}",
        description="test",
        status="stopped",
        is_active=False,
        execution_mode="manual",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_knowledge_item(db, *, title="Test Item", knowledge_type="format_guide"):
    content = f"{title} content"
    item = KnowledgeItem(
        id=str(uuid.uuid4()),
        title=title,
        content=content,
        knowledge_type=knowledge_type,
        checksum=hashlib.sha256(content.encode()).hexdigest(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_usage_log(db, session, item, *, trigger_phase="planning", task_id=None):
    log = KnowledgeUsageLog(
        session_id=session.id,
        task_id=task_id,
        knowledge_item_id=item.id,
        trigger_phase=trigger_phase,
        retrieval_reason="test retrieval",
        retrieval_query="test query",
        confidence=0.85,
        rank=0,
        used_in_prompt=True,
        was_effective=None,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def test_knowledge_usage_empty(authenticated_client, db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project)

    resp = authenticated_client.get(f"/api/v1/sessions/{session.id}/knowledge-usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == session.id
    assert data["phases"] == {}


def test_knowledge_usage_single_phase(authenticated_client, db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project)
    item = _make_knowledge_item(
        db_session, title="Format Guide", knowledge_type="format_guide"
    )
    _make_usage_log(db_session, session, item, trigger_phase="planning")

    resp = authenticated_client.get(f"/api/v1/sessions/{session.id}/knowledge-usage")
    assert resp.status_code == 200
    data = resp.json()
    assert "planning" in data["phases"]
    entries = data["phases"]["planning"]
    assert len(entries) == 1
    e = entries[0]
    assert e["knowledge_item_id"] == item.id
    assert e["title"] == "Format Guide"
    assert e["knowledge_type"] == "format_guide"
    assert e["confidence"] == pytest.approx(0.85)
    assert e["used_in_prompt"] is True
    assert e["retrieval_reason"] == "test retrieval"
    assert e["task_id"] is None


def test_knowledge_usage_multiple_phases(authenticated_client, db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project)
    item1 = _make_knowledge_item(
        db_session, title="Planning Item", knowledge_type="format_guide"
    )
    item2 = _make_knowledge_item(
        db_session, title="Failure Item", knowledge_type="debug_case"
    )
    _make_usage_log(db_session, session, item1, trigger_phase="planning")
    _make_usage_log(db_session, session, item2, trigger_phase="failure")

    resp = authenticated_client.get(f"/api/v1/sessions/{session.id}/knowledge-usage")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["phases"]["planning"]) == 1
    assert len(data["phases"]["failure"]) == 1
    assert data["phases"]["planning"][0]["title"] == "Planning Item"
    assert data["phases"]["failure"][0]["title"] == "Failure Item"


def test_knowledge_usage_session_not_found(authenticated_client, db_session):
    resp = authenticated_client.get("/api/v1/sessions/99999/knowledge-usage")
    assert resp.status_code == 404


def test_knowledge_usage_isolated_to_session(authenticated_client, db_session):
    project = _make_project(db_session)
    session_a = _make_session(db_session, project)
    session_b = _make_session(db_session, project)
    item = _make_knowledge_item(
        db_session, title="Shared Item", knowledge_type="format_guide"
    )
    _make_usage_log(db_session, session_b, item, trigger_phase="validation")

    resp = authenticated_client.get(f"/api/v1/sessions/{session_a.id}/knowledge-usage")
    assert resp.status_code == 200
    assert resp.json()["phases"] == {}
