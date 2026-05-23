"""Tests for GET /sessions/{session_id}/knowledge-usage."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models import (
    KnowledgeItem,
    KnowledgeUsageLog,
    Project,
    Session as SessionModel,
)
from app.config import settings


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


def _make_usage_log(
    db,
    session,
    item,
    *,
    trigger_phase="planning",
    task_id=None,
    retrieval_reason="test retrieval",
    confidence=0.85,
    used_in_prompt=True,
    created_at=None,
):
    log = KnowledgeUsageLog(
        session_id=session.id,
        task_id=task_id,
        knowledge_item_id=item.id,
        trigger_phase=trigger_phase,
        retrieval_reason=retrieval_reason,
        retrieval_query="test query",
        confidence=confidence,
        rank=0,
        used_in_prompt=used_in_prompt,
        was_effective=None,
        created_at=created_at,
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
    assert data["knowledge_used"] is False
    assert (
        data["omission_reason"] == "no_retrieval_attempts_or_no_matching_items_logged"
    )
    assert data["phase_summaries"]["planning"]["knowledge_used"] is False


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
    assert e["confidence_avg"] == pytest.approx(0.85)
    assert e["confidence_max"] == pytest.approx(0.85)
    assert e["used_in_prompt"] is True
    assert e["retrieval_reason"] == "test retrieval"
    assert e["usage_count"] == 1
    assert e["first_used_at"] is not None
    assert e["last_used_at"] is not None
    assert data["knowledge_used"] is True
    assert data["phase_summaries"]["planning"]["knowledge_used"] is True
    assert data["phase_summaries"]["planning"]["top_item_titles"] == ["Format Guide"]
    assert data["top_items"][0]["title"] == "Format Guide"
    assert data["top_items"][0]["phase"] == "planning"


def test_mobile_knowledge_usage_uses_mobile_gateway_auth(
    api_client, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", "mobile-ku-test-key")
    project = _make_project(db_session)
    session = _make_session(db_session, project)
    item = _make_knowledge_item(
        db_session, title="Mobile Format Guide", knowledge_type="format_guide"
    )
    _make_usage_log(db_session, session, item, trigger_phase="planning")

    resp = api_client.get(
        f"/api/v1/mobile/sessions/{session.id}/knowledge-usage",
        headers={"X-OpenClaw-API-Key": "mobile-ku-test-key"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == session.id
    entries = data["phases"]["planning"]
    assert len(entries) == 1
    assert entries[0]["title"] == "Mobile Format Guide"
    assert entries[0]["confidence_max"] == pytest.approx(0.85)


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


def test_knowledge_usage_groups_duplicate_logs_without_deleting_rows(
    authenticated_client, db_session
):
    project = _make_project(db_session)
    session = _make_session(db_session, project)
    item = _make_knowledge_item(
        db_session, title="Planning Output Format Guide", knowledge_type="format_guide"
    )
    created_at_1 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    created_at_2 = created_at_1 + timedelta(minutes=5)

    _make_usage_log(
        db_session,
        session,
        item,
        trigger_phase="planning",
        retrieval_reason="sqlite_fallback_qdrant_or_embedding_unavailable",
        confidence=0.2,
        used_in_prompt=True,
        created_at=created_at_1,
    )
    _make_usage_log(
        db_session,
        session,
        item,
        trigger_phase="planning",
        retrieval_reason="sqlite_fallback_qdrant_or_embedding_unavailable",
        confidence=0.4,
        used_in_prompt=True,
        created_at=created_at_2,
    )

    resp = authenticated_client.get(f"/api/v1/sessions/{session.id}/knowledge-usage")
    assert resp.status_code == 200
    entries = resp.json()["phases"]["planning"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["knowledge_item_id"] == item.id
    assert entry["usage_count"] == 2
    assert entry["used_in_prompt"] is True
    assert (
        entry["retrieval_reason"] == "sqlite_fallback_qdrant_or_embedding_unavailable"
    )
    assert entry["first_used_at"] == created_at_1.isoformat()
    assert entry["last_used_at"] == created_at_2.isoformat()
    assert entry["confidence_max"] == pytest.approx(0.4)
    assert entry["confidence_avg"] == pytest.approx(0.3)

    raw_rows = (
        db_session.query(KnowledgeUsageLog)
        .filter(KnowledgeUsageLog.session_id == session.id)
        .all()
    )
    assert len(raw_rows) == 2


def test_knowledge_usage_keeps_distinct_used_in_prompt_groups_separate(
    authenticated_client, db_session
):
    project = _make_project(db_session)
    session = _make_session(db_session, project)
    item = _make_knowledge_item(
        db_session, title="Failure Case", knowledge_type="debug_case"
    )

    _make_usage_log(
        db_session,
        session,
        item,
        trigger_phase="failure",
        retrieval_reason="semantic_retrieval",
        used_in_prompt=False,
    )
    _make_usage_log(
        db_session,
        session,
        item,
        trigger_phase="failure",
        retrieval_reason="semantic_retrieval",
        used_in_prompt=True,
    )

    resp = authenticated_client.get(f"/api/v1/sessions/{session.id}/knowledge-usage")
    assert resp.status_code == 200
    entries = resp.json()["phases"]["failure"]
    assert len(entries) == 2
    assert sorted(entry["used_in_prompt"] for entry in entries) == [False, True]


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
