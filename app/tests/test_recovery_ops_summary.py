"""Tests for Phase 13B recovery ops summary reporting."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.ops import ops_recovery_summary
from app.database import get_db
from app.models import Base, Project, Session as SessionModel, Task
from app.services.orchestration.events.event_types import EventType


@pytest.fixture()
def mem_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _seed(mem_db, tmp_path: Path):
    project = Project(name="recovery-ops", workspace_path=str(tmp_path))
    mem_db.add(project)
    mem_db.flush()
    session = SessionModel(
        project_id=project.id,
        name="session-a",
        status="running",
        is_active=True,
        model_lane_label="local_openclaw",
        created_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
    )
    mem_db.add(session)
    mem_db.flush()
    task = Task(project_id=project.id, title="task-a", description="x")
    mem_db.add(task)
    mem_db.commit()

    event_dir = tmp_path / ".agent" / "events"
    event_dir.mkdir(parents=True, exist_ok=True)
    event_path = event_dir / f"session_{session.id}_task_{task.id}.jsonl"
    event_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_type": EventType.EXECUTION_RECOVERY_ATTEMPTED,
                        "details": {
                            "scope": "step",
                            "failure_class": "import_error",
                        },
                    }
                ),
                json.dumps(
                    {
                        "event_type": EventType.EXECUTION_RECOVERY_SUCCEEDED,
                        "details": {
                            "scope": "step",
                            "failure_class": "import_error",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return project, session


def test_ops_recovery_summary_endpoint_returns_rollup(
    authenticated_client: TestClient, mem_db, tmp_path: Path, monkeypatch
):
    project, session = _seed(mem_db, tmp_path)

    def _override_db():
        try:
            yield mem_db
        finally:
            pass

    app = authenticated_client.app
    app.dependency_overrides[get_db] = _override_db
    try:
        resp = authenticated_client.get(
            "/api/v1/ops/recovery-summary",
            params={"project_id": project.id, "model": "local_openclaw"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["filters"]["project_id"] == project.id
    assert body["filters"]["model"] == "local_openclaw"
    assert body["metrics"]["recovery_attempted_count"] == 1
    assert body["metrics"]["recovery_succeeded_count"] == 1
    assert body["metrics"]["by_project"]
    assert body["metrics"]["by_session"]
    assert body["metrics"]["by_model"]
    assert body["metrics"]["by_day"]


def test_ops_recovery_summary_endpoint_rejects_unauthenticated(api_client: TestClient):
    resp = api_client.get("/api/v1/ops/recovery-summary")
    assert resp.status_code in (401, 403)
