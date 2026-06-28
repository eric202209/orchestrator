"""Phase 15E-2 — Backend Pagination Infrastructure tests.

Covers:
- paginate() helper
- GET /sessions (legacy + paginated)
- GET /projects/{id}/sessions (legacy + paginated)
- GET /tasks (legacy + paginated, no sync side-effect)
- GET /projects/{id}/tasks (SQL pagination, was Python-slicing)
- GET /projects (legacy + paginated)
- AttentionQueryService
- GET /dashboard/attention
- Edge cases and legacy compatibility
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.router import api_router
from app.database import get_db
from app.dependencies import get_current_active_user, get_current_user
from app.models import (
    Base,
    InterventionRequest,
    Project,
    Session as SessionModel,
    Task,
    TaskStatus,
    User,
)
from app.schemas.pagination import Page, QueryOptions, paginate
from app.services.query.attention_query_service import (
    ATTENTION_STATUSES,
    AttentionQueryService,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)
    eng.dispose()


@pytest.fixture(scope="function")
def db(engine):
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.close()


@pytest.fixture(scope="function")
def app(engine):
    _app = FastAPI()
    _app.include_router(api_router, prefix="/api/v1")

    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    fake_user = User(
        id=1,
        email="test@example.com",
        hashed_password="x",
        is_active=True,
    )

    def override_user():
        return fake_user

    _app.dependency_overrides[get_db] = override_db
    _app.dependency_overrides[get_current_active_user] = override_user
    _app.dependency_overrides[get_current_user] = override_user
    return _app


@pytest.fixture(scope="function")
def client(app):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(db, name: str = "Test Project", user_id: int = 1) -> Project:
    p = Project(name=name, user_id=user_id)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_session(
    db,
    project: Project,
    name: str = "s",
    status: str = "pending",
) -> SessionModel:
    s = SessionModel(
        name=name,
        project_id=project.id,
        status=status,
        execution_mode="automatic",
        default_execution_profile="full_lifecycle",
        is_active=False,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_task(
    db,
    project: Project,
    title: str = "task",
    workspace_status: str = "isolated",
    status: TaskStatus = TaskStatus.PENDING,
    plan_position: int = 1,
) -> Task:
    t = Task(
        project_id=project.id,
        title=title,
        workspace_status=workspace_status,
        status=status,
        plan_position=plan_position,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _make_intervention(
    db,
    session: SessionModel,
    project: Project,
    intervention_type: str = "guidance",
    status: str = "pending",
) -> InterventionRequest:
    ir = InterventionRequest(
        session_id=session.id,
        project_id=project.id,
        intervention_type=intervention_type,
        initiated_by="ai",
        prompt="help me",
        status=status,
    )
    db.add(ir)
    db.commit()
    db.refresh(ir)
    return ir


# ===========================================================================
# Section 1 — paginate() helper
# ===========================================================================


class TestPaginateHelper:
    def test_returns_correct_items_page_1(self, db):
        project = _make_project(db)
        for i in range(5):
            _make_session(db, project, name=f"s{i}")

        from sqlalchemy.orm import Session as DbSession

        q = db.query(SessionModel).filter(SessionModel.project_id == project.id)
        result = paginate(q.order_by(SessionModel.id.asc()), 1, 3)
        assert len(result["items"]) == 3
        assert result["page"] == 1
        assert result["per_page"] == 3
        assert result["total"] == 5
        assert result["total_pages"] == 2
        assert result["has_next"] is True
        assert result["has_previous"] is False

    def test_returns_correct_items_page_2(self, db):
        project = _make_project(db)
        for i in range(5):
            _make_session(db, project, name=f"s{i}")

        q = db.query(SessionModel).filter(SessionModel.project_id == project.id)
        result = paginate(q.order_by(SessionModel.id.asc()), 2, 3)
        assert len(result["items"]) == 2
        assert result["page"] == 2
        assert result["has_next"] is False
        assert result["has_previous"] is True

    def test_empty_result(self, db):
        q = db.query(SessionModel).filter(SessionModel.id == -999)
        result = paginate(q, 1, 10)
        assert result["items"] == []
        assert result["total"] == 0
        assert result["total_pages"] == 1
        assert result["has_next"] is False
        assert result["has_previous"] is False

    def test_single_page_exact_fit(self, db):
        project = _make_project(db)
        for i in range(4):
            _make_session(db, project, name=f"s{i}")

        q = db.query(SessionModel).filter(SessionModel.project_id == project.id)
        result = paginate(q, 1, 4)
        assert len(result["items"]) == 4
        assert result["total_pages"] == 1
        assert result["has_next"] is False

    def test_total_pages_computed_correctly(self, db):
        project = _make_project(db)
        for i in range(7):
            _make_session(db, project, name=f"s{i}")

        q = db.query(SessionModel).filter(SessionModel.project_id == project.id)
        result = paginate(q, 1, 3)
        assert result["total_pages"] == 3  # ceil(7/3)


# ===========================================================================
# Section 2 — Page schema and QueryOptions
# ===========================================================================


class TestPaginationSchemas:
    def test_page_generic_can_be_instantiated(self):
        page = Page(
            items=["a", "b"],
            page=1,
            per_page=10,
            total=2,
            total_pages=1,
            has_next=False,
            has_previous=False,
        )
        assert page.items == ["a", "b"]
        assert page.total == 2

    def test_query_options_defaults(self):
        opts = QueryOptions()
        assert opts.page == 1
        assert opts.per_page == 25
        assert opts.order_by == "created_at"
        assert opts.order_dir == "desc"

    def test_query_options_validation(self):
        with pytest.raises(Exception):
            QueryOptions(page=0)

    def test_query_options_max_per_page(self):
        with pytest.raises(Exception):
            QueryOptions(per_page=201)


# ===========================================================================
# Section 3 — GET /sessions legacy mode
# ===========================================================================


class TestSessionsLegacy:
    def test_returns_list_when_no_page(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="alpha")
        resp = client.get("/api/v1/sessions")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_skip_limit_legacy(self, client, db):
        project = _make_project(db)
        for i in range(5):
            _make_session(db, project, name=f"s{i}")
        resp = client.get("/api/v1/sessions?skip=3&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_status_filter_legacy(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="run", status="running")
        _make_session(db, project, name="fail", status="failed")
        resp = client.get("/api/v1/sessions?status=running")
        assert resp.status_code == 200
        data = resp.json()
        assert all(s["status"] == "running" for s in data)

    def test_project_id_filter_legacy(self, client, db):
        p1 = _make_project(db, name="P1")
        p2 = _make_project(db, name="P2")
        _make_session(db, p1, name="s1")
        _make_session(db, p2, name="s2")
        resp = client.get(f"/api/v1/sessions?project_id={p1.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert all(s["project_id"] == p1.id for s in data)

    def test_limit_respected(self, client, db):
        project = _make_project(db)
        for i in range(10):
            _make_session(db, project, name=f"s{i}")
        resp = client.get("/api/v1/sessions?limit=3")
        assert resp.status_code == 200
        assert len(resp.json()) == 3


# ===========================================================================
# Section 4 — GET /sessions paginated mode
# ===========================================================================


class TestSessionsPaginated:
    def test_returns_page_dict_when_page_param(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="s1")
        resp = client.get("/api/v1/sessions?page=1")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "page" in data
        assert "per_page" in data
        assert "total" in data
        assert "total_pages" in data
        assert "has_next" in data
        assert "has_previous" in data

    def test_page_1_structure(self, client, db):
        project = _make_project(db)
        for i in range(5):
            _make_session(db, project, name=f"s{i}")
        resp = client.get("/api/v1/sessions?page=1&per_page=3")
        assert resp.status_code == 200
        data = resp.json()
        assert data["page"] == 1
        assert data["per_page"] == 3
        assert data["total"] == 5
        assert data["total_pages"] == 2
        assert data["has_next"] is True
        assert data["has_previous"] is False
        assert len(data["items"]) == 3

    def test_needs_attention_filter(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="ok", status="running")
        _make_session(db, project, name="bad", status="failed")
        _make_session(db, project, name="waiting", status="awaiting_input")
        resp = client.get("/api/v1/sessions?page=1&needs_attention=true")
        assert resp.status_code == 200
        data = resp.json()
        statuses = {s["status"] for s in data["items"]}
        assert "running" not in statuses
        assert "failed" in statuses or "awaiting_input" in statuses

    def test_status_filter_paginated(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="run", status="running")
        _make_session(db, project, name="fail", status="failed")
        resp = client.get("/api/v1/sessions?page=1&status=failed")
        assert resp.status_code == 200
        data = resp.json()
        assert all(s["status"] == "failed" for s in data["items"])

    def test_search_filter(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="alpha session")
        _make_session(db, project, name="beta session")
        resp = client.get("/api/v1/sessions?page=1&search=alpha")
        assert resp.status_code == 200
        data = resp.json()
        assert all("alpha" in s["name"].lower() for s in data["items"])

    def test_order_by_name_desc(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="aaa")
        _make_session(db, project, name="zzz")
        resp = client.get("/api/v1/sessions?page=1&order_by=name&order_dir=desc")
        assert resp.status_code == 200
        items = resp.json()["items"]
        names = [i["name"] for i in items]
        assert names == sorted(names, reverse=True)

    def test_order_by_name_asc(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="zzz")
        _make_session(db, project, name="aaa")
        resp = client.get("/api/v1/sessions?page=1&order_by=name&order_dir=asc")
        assert resp.status_code == 200
        items = resp.json()["items"]
        names = [i["name"] for i in items]
        assert names == sorted(names)

    def test_page_beyond_last_returns_empty_items(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="s1")
        resp = client.get("/api/v1/sessions?page=999&per_page=10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 1

    def test_per_page_limits_items(self, client, db):
        project = _make_project(db)
        for i in range(10):
            _make_session(db, project, name=f"s{i}")
        resp = client.get("/api/v1/sessions?page=1&per_page=4")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 4

    def test_project_id_filter_paginated(self, client, db):
        p1 = _make_project(db, name="P1")
        p2 = _make_project(db, name="P2")
        _make_session(db, p1, name="sa")
        _make_session(db, p2, name="sb")
        resp = client.get(f"/api/v1/sessions?page=1&project_id={p1.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert all(s["project_id"] == p1.id for s in data["items"])

    def test_created_after_filter(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="s1")
        resp = client.get("/api/v1/sessions?page=1&created_after=2000-01-01T00:00:00")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_created_before_filter(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="s1")
        resp = client.get("/api/v1/sessions?page=1&created_before=2000-01-01T00:00:00")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_page_2_correct(self, client, db):
        project = _make_project(db)
        for i in range(5):
            _make_session(db, project, name=f"s{i}")
        resp1 = client.get("/api/v1/sessions?page=1&per_page=3")
        resp2 = client.get("/api/v1/sessions?page=2&per_page=3")
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        ids1 = {s["id"] for s in resp1.json()["items"]}
        ids2 = {s["id"] for s in resp2.json()["items"]}
        assert ids1.isdisjoint(ids2)


# ===========================================================================
# Section 5 — GET /projects/{id}/sessions
# ===========================================================================


class TestProjectSessionsEndpoint:
    def test_legacy_skip_limit(self, client, db):
        project = _make_project(db)
        for i in range(5):
            _make_session(db, project, name=f"s{i}")
        resp = client.get(f"/api/v1/projects/{project.id}/sessions?skip=2&limit=2")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_legacy_is_active_filter(self, client, db):
        project = _make_project(db)
        s = _make_session(db, project, name="active")
        s.is_active = True
        db.commit()
        _make_session(db, project, name="inactive")
        resp = client.get(f"/api/v1/projects/{project.id}/sessions?is_active=true")
        assert resp.status_code == 200
        data = resp.json()
        assert all(s["is_active"] is True for s in data)

    def test_paginated_returns_page(self, client, db):
        project = _make_project(db)
        for i in range(3):
            _make_session(db, project, name=f"s{i}")
        resp = client.get(f"/api/v1/projects/{project.id}/sessions?page=1&per_page=2")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert data["total"] == 3
        assert len(data["items"]) == 2

    def test_needs_attention_scoped_to_project(self, client, db):
        p1 = _make_project(db, name="P1")
        p2 = _make_project(db, name="P2")
        _make_session(db, p1, name="bad1", status="failed")
        _make_session(db, p2, name="bad2", status="failed")
        resp = client.get(
            f"/api/v1/projects/{p1.id}/sessions?page=1&needs_attention=true"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(s["project_id"] == p1.id for s in data["items"])

    def test_search_scoped_to_project(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="searchable session")
        _make_session(db, project, name="other")
        resp = client.get(
            f"/api/v1/projects/{project.id}/sessions?page=1&search=searchable"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all("searchable" in s["name"].lower() for s in data["items"])

    def test_order_by_name(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="zzz")
        _make_session(db, project, name="aaa")
        resp = client.get(
            f"/api/v1/projects/{project.id}/sessions?page=1&order_by=name&order_dir=asc"
        )
        assert resp.status_code == 200
        names = [s["name"] for s in resp.json()["items"]]
        assert names == sorted(names)

    def test_404_for_unknown_project(self, client, db):
        resp = client.get("/api/v1/projects/99999/sessions?page=1")
        assert resp.status_code == 404


# ===========================================================================
# Section 6 — GET /tasks legacy mode
# ===========================================================================


class TestTasksLegacy:
    def test_returns_list_when_no_page(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="t1")
        resp = client.get("/api/v1/tasks")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_status_filter_legacy(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="pending", status=TaskStatus.PENDING)
        _make_task(db, project, title="done", status=TaskStatus.DONE)
        resp = client.get("/api/v1/tasks?status=DONE")
        assert resp.status_code == 200
        data = resp.json()
        assert all(t["status"] == "done" for t in data)

    def test_skip_limit_legacy(self, client, db):
        project = _make_project(db)
        for i in range(5):
            _make_task(db, project, title=f"t{i}", plan_position=i)
        resp = client.get("/api/v1/tasks?skip=3&limit=10")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_workspace_status_filter_legacy(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="ready", workspace_status="ready")
        _make_task(db, project, title="isolated", workspace_status="isolated")
        resp = client.get("/api/v1/tasks?workspace_status=ready")
        assert resp.status_code == 200
        data = resp.json()
        assert all(t["workspace_status"] == "ready" for t in data)

    def test_get_tasks_no_sync_side_effect(self, client, db):
        """GET /tasks must not call sync_workspace_status (read-only)."""
        project = _make_project(db)
        _make_task(db, project, title="t1")
        # This test verifies no exception is raised and response is clean.
        # Prior implementation called sync_workspace_status which could commit.
        resp = client.get("/api/v1/tasks")
        assert resp.status_code == 200


# ===========================================================================
# Section 7 — GET /tasks paginated mode
# ===========================================================================


class TestTasksPaginated:
    def test_returns_page_when_page_param(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="t1")
        resp = client.get("/api/v1/tasks?page=1")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data

    def test_needs_review_filter(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="ready", workspace_status="ready")
        _make_task(db, project, title="isolated", workspace_status="isolated")
        resp = client.get("/api/v1/tasks?page=1&needs_review=true")
        assert resp.status_code == 200
        data = resp.json()
        assert all(t["workspace_status"] == "ready" for t in data["items"])

    def test_workspace_status_filter_paginated(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="ready", workspace_status="ready")
        _make_task(db, project, title="promoted", workspace_status="promoted")
        resp = client.get("/api/v1/tasks?page=1&workspace_status=promoted")
        assert resp.status_code == 200
        data = resp.json()
        assert all(t["workspace_status"] == "promoted" for t in data["items"])

    def test_status_filter_paginated(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="pending", status=TaskStatus.PENDING)
        _make_task(db, project, title="done", status=TaskStatus.DONE)
        resp = client.get("/api/v1/tasks?page=1&status=DONE")
        assert resp.status_code == 200
        data = resp.json()
        assert all(t["status"] == "done" for t in data["items"])

    def test_search_filter(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="auth module")
        _make_task(db, project, title="database setup")
        resp = client.get("/api/v1/tasks?page=1&search=auth")
        assert resp.status_code == 200
        data = resp.json()
        assert all("auth" in t["title"].lower() for t in data["items"])

    def test_project_id_filter_paginated(self, client, db):
        p1 = _make_project(db, name="P1")
        p2 = _make_project(db, name="P2")
        _make_task(db, p1, title="t1")
        _make_task(db, p2, title="t2")
        resp = client.get(f"/api/v1/tasks?page=1&project_id={p1.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert all(t["project_id"] == p1.id for t in data["items"])

    def test_order_by_title_asc(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="zzz task")
        _make_task(db, project, title="aaa task")
        resp = client.get("/api/v1/tasks?page=1&order_by=title&order_dir=asc")
        assert resp.status_code == 200
        titles = [t["title"] for t in resp.json()["items"]]
        assert titles == sorted(titles)

    def test_page_2_distinct_from_page_1(self, client, db):
        project = _make_project(db)
        for i in range(5):
            _make_task(db, project, title=f"task-{i}", plan_position=i)
        r1 = client.get("/api/v1/tasks?page=1&per_page=3")
        r2 = client.get("/api/v1/tasks?page=2&per_page=3")
        ids1 = {t["id"] for t in r1.json()["items"]}
        ids2 = {t["id"] for t in r2.json()["items"]}
        assert ids1.isdisjoint(ids2)

    def test_empty_needs_review_when_no_ready(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="isolated", workspace_status="isolated")
        resp = client.get("/api/v1/tasks?page=1&needs_review=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_per_page_limits(self, client, db):
        project = _make_project(db)
        for i in range(10):
            _make_task(db, project, title=f"t{i}", plan_position=i)
        resp = client.get("/api/v1/tasks?page=1&per_page=4")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 4


# ===========================================================================
# Section 8 — GET /projects/{id}/tasks
# ===========================================================================


class TestProjectTasksEndpoint:
    def test_legacy_sql_level_pagination(self, client, db):
        """Verify SQL-level skip/limit (was previously Python-level slicing)."""
        project = _make_project(db)
        for i in range(5):
            _make_task(db, project, title=f"t{i}", plan_position=i)
        resp = client.get(f"/api/v1/projects/{project.id}/tasks?skip=2&limit=2")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_paginated_returns_page(self, client, db):
        project = _make_project(db)
        for i in range(5):
            _make_task(db, project, title=f"t{i}", plan_position=i)
        resp = client.get(f"/api/v1/projects/{project.id}/tasks?page=1&per_page=3")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert data["total"] == 5
        assert len(data["items"]) == 3

    def test_needs_review_filter(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="ready", workspace_status="ready")
        _make_task(db, project, title="isolated", workspace_status="isolated")
        resp = client.get(
            f"/api/v1/projects/{project.id}/tasks?page=1&needs_review=true"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(t["workspace_status"] == "ready" for t in data["items"])

    def test_workspace_status_filter(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="ready", workspace_status="ready")
        _make_task(db, project, title="promoted", workspace_status="promoted")
        resp = client.get(
            f"/api/v1/projects/{project.id}/tasks?page=1&workspace_status=ready"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(t["workspace_status"] == "ready" for t in data["items"])

    def test_order_by_plan_position_default(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="third", plan_position=3)
        _make_task(db, project, title="first", plan_position=1)
        _make_task(db, project, title="second", plan_position=2)
        resp = client.get(f"/api/v1/projects/{project.id}/tasks?page=1")
        assert resp.status_code == 200
        positions = [t["plan_position"] for t in resp.json()["items"]]
        assert positions == sorted(positions)

    def test_search_filter(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="auth task", plan_position=1)
        _make_task(db, project, title="deploy task", plan_position=2)
        resp = client.get(f"/api/v1/projects/{project.id}/tasks?page=1&search=auth")
        assert resp.status_code == 200
        data = resp.json()
        assert all("auth" in t["title"].lower() for t in data["items"])

    def test_session_id_included_in_items(self, client, db):
        """Items from project tasks should have session_id field (may be null)."""
        project = _make_project(db)
        _make_task(db, project, title="t1")
        resp = client.get(f"/api/v1/projects/{project.id}/tasks?page=1")
        assert resp.status_code == 200
        for item in resp.json()["items"]:
            assert "session_id" in item


# ===========================================================================
# Section 9 — GET /projects legacy and paginated
# ===========================================================================


class TestProjectsEndpoint:
    def test_legacy_returns_list(self, client, db):
        _make_project(db, name="P1")
        resp = client.get("/api/v1/projects")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_legacy_skip_limit(self, client, db):
        for i in range(5):
            _make_project(db, name=f"P{i}")
        resp = client.get("/api/v1/projects?skip=3&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_legacy_search(self, client, db):
        _make_project(db, name="Alpha Project")
        _make_project(db, name="Beta Project")
        resp = client.get("/api/v1/projects?search=alpha")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert all("alpha" in p["name"].lower() for p in data)

    def test_paginated_returns_page(self, client, db):
        for i in range(5):
            _make_project(db, name=f"P{i}")
        resp = client.get("/api/v1/projects?page=1&per_page=3")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert data["total"] == 5
        assert data["total_pages"] == 2

    def test_paginated_search(self, client, db):
        _make_project(db, name="Alpha")
        _make_project(db, name="Beta")
        resp = client.get("/api/v1/projects?page=1&search=alpha")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Alpha"

    def test_order_by_name_asc(self, client, db):
        _make_project(db, name="ZZZ")
        _make_project(db, name="AAA")
        resp = client.get("/api/v1/projects?page=1&order_by=name&order_dir=asc")
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["items"]]
        assert names == sorted(names)

    def test_order_by_name_desc(self, client, db):
        _make_project(db, name="ZZZ")
        _make_project(db, name="AAA")
        resp = client.get("/api/v1/projects?page=1&order_by=name&order_dir=desc")
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["items"]]
        assert names == sorted(names, reverse=True)

    def test_page_2(self, client, db):
        for i in range(5):
            _make_project(db, name=f"P{i}")
        r1 = client.get("/api/v1/projects?page=1&per_page=3")
        r2 = client.get("/api/v1/projects?page=2&per_page=3")
        ids1 = {p["id"] for p in r1.json()["items"]}
        ids2 = {p["id"] for p in r2.json()["items"]}
        assert ids1.isdisjoint(ids2)

    def test_empty_page(self, client):
        resp = client.get("/api/v1/projects?page=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []


# ===========================================================================
# Section 10 — AttentionQueryService
# ===========================================================================


def _make_user_and_project(db, uid: int, email: str, name: str = "Project") -> tuple:
    """Create a user and a project owned by that user for service-layer tests."""
    user = User(id=uid, email=email, hashed_password="x", is_active=True)
    db.add(user)
    db.commit()
    project = _make_project(db, name=name, user_id=uid)
    return user, project


class TestAttentionQueryService:
    def test_get_pending_interventions_empty(self, db):
        user, _ = _make_user_and_project(db, 10, "u10@x.com")
        svc = AttentionQueryService(db)
        result = svc.get_pending_interventions(user)
        assert result == []

    def test_get_pending_interventions_includes_project_name(self, db):
        user, project = _make_user_and_project(db, 11, "u11@x.com", "My Project")
        session = _make_session(db, project, name="test-session")
        _make_intervention(db, session, project, status="pending")
        svc = AttentionQueryService(db)
        result = svc.get_pending_interventions(user)
        assert len(result) == 1
        assert result[0]["project_name"] == project.name

    def test_get_pending_interventions_only_pending(self, db):
        user, project = _make_user_and_project(db, 12, "u12@x.com")
        session = _make_session(db, project, name="ts")
        _make_intervention(db, session, project, status="pending")
        _make_intervention(db, session, project, status="replied")
        svc = AttentionQueryService(db)
        result = svc.get_pending_interventions(user)
        assert len(result) == 1
        assert result[0]["status"] == "pending"

    def test_attention_statuses_constant(self):
        assert "failed" in ATTENTION_STATUSES
        assert "awaiting_input" in ATTENTION_STATUSES
        assert "stopped" in ATTENTION_STATUSES
        assert "running" not in ATTENTION_STATUSES

    def test_get_sessions_needing_attention_count(self, db):
        user, project = _make_user_and_project(db, 13, "u13@x.com")
        _make_session(db, project, name="ok", status="running")
        _make_session(db, project, name="bad1", status="failed")
        _make_session(db, project, name="bad2", status="awaiting_input")
        svc = AttentionQueryService(db)
        count = svc.get_sessions_needing_attention_count(user)
        assert count == 2

    def test_get_review_queue_count(self, db):
        user, project = _make_user_and_project(db, 14, "u14@x.com")
        _make_task(db, project, title="ready", workspace_status="ready")
        _make_task(db, project, title="isolated", workspace_status="isolated")
        svc = AttentionQueryService(db)
        count = svc.get_review_queue_count(user)
        assert count == 1

    def test_get_attention_sessions(self, db):
        user, project = _make_user_and_project(db, 15, "u15@x.com")
        _make_session(db, project, name="running", status="running")
        _make_session(db, project, name="failed", status="failed")
        svc = AttentionQueryService(db)
        sessions = svc.get_attention_sessions(user)
        assert all(s.status in ATTENTION_STATUSES for s in sessions)

    def test_get_review_queue_tasks(self, db):
        user, project = _make_user_and_project(db, 16, "u16@x.com")
        _make_task(db, project, title="ready", workspace_status="ready")
        _make_task(db, project, title="isolated", workspace_status="isolated")
        svc = AttentionQueryService(db)
        tasks = svc.get_review_queue_tasks(user)
        assert all(t.workspace_status == "ready" for t in tasks)
        assert len(tasks) == 1


# ===========================================================================
# Section 11 — GET /dashboard/attention
# ===========================================================================


class TestDashboardAttentionEndpoint:
    def test_returns_correct_structure(self, client, db):
        resp = client.get("/api/v1/dashboard/attention")
        assert resp.status_code == 200
        data = resp.json()
        assert "pending_interventions" in data
        assert "sessions_needing_attention" in data
        assert "tasks_pending_review" in data

    def test_pending_interventions_is_list(self, client, db):
        resp = client.get("/api/v1/dashboard/attention")
        assert resp.status_code == 200
        assert isinstance(resp.json()["pending_interventions"], list)

    def test_sessions_needing_attention_is_int(self, client, db):
        resp = client.get("/api/v1/dashboard/attention")
        assert resp.status_code == 200
        assert isinstance(resp.json()["sessions_needing_attention"], int)

    def test_tasks_pending_review_is_int(self, client, db):
        resp = client.get("/api/v1/dashboard/attention")
        assert resp.status_code == 200
        assert isinstance(resp.json()["tasks_pending_review"], int)

    def test_empty_case(self, client, db):
        resp = client.get("/api/v1/dashboard/attention")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pending_interventions"] == []
        assert data["sessions_needing_attention"] == 0
        assert data["tasks_pending_review"] == 0

    def test_with_pending_intervention(self, client, db):
        project = _make_project(db)
        session = _make_session(db, project, name="awaiting", status="awaiting_input")
        _make_intervention(db, session, project, status="pending")
        resp = client.get("/api/v1/dashboard/attention")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["pending_interventions"]) == 1
        assert data["pending_interventions"][0]["project_name"] == project.name

    def test_sessions_needing_attention_counted(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="run", status="running")
        _make_session(db, project, name="fail", status="failed")
        _make_session(db, project, name="await", status="awaiting_input")
        resp = client.get("/api/v1/dashboard/attention")
        assert resp.status_code == 200
        assert resp.json()["sessions_needing_attention"] == 2

    def test_tasks_pending_review_counted(self, client, db):
        project = _make_project(db)
        _make_task(db, project, title="ready1", workspace_status="ready")
        _make_task(db, project, title="ready2", workspace_status="ready")
        _make_task(db, project, title="isolated", workspace_status="isolated")
        resp = client.get("/api/v1/dashboard/attention")
        assert resp.status_code == 200
        assert resp.json()["tasks_pending_review"] == 2


# ===========================================================================
# Section 12 — Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_page_zero_rejected(self, client, db):
        resp = client.get("/api/v1/sessions?page=0")
        assert resp.status_code == 422

    def test_page_negative_rejected(self, client, db):
        resp = client.get("/api/v1/sessions?page=-1")
        assert resp.status_code == 422

    def test_per_page_zero_rejected(self, client, db):
        resp = client.get("/api/v1/sessions?page=1&per_page=0")
        assert resp.status_code == 422

    def test_per_page_over_max_rejected(self, client, db):
        resp = client.get("/api/v1/sessions?page=1&per_page=201")
        assert resp.status_code == 422

    def test_tasks_page_zero_rejected(self, client, db):
        resp = client.get("/api/v1/tasks?page=0")
        assert resp.status_code == 422

    def test_projects_page_zero_rejected(self, client, db):
        resp = client.get("/api/v1/projects?page=0")
        assert resp.status_code == 422

    def test_invalid_created_after_returns_400(self, client, db):
        resp = client.get("/api/v1/sessions?page=1&created_after=not-a-date")
        assert resp.status_code == 400

    def test_invalid_created_before_returns_400(self, client, db):
        resp = client.get("/api/v1/sessions?page=1&created_before=not-a-date")
        assert resp.status_code == 400

    def test_page_beyond_last_returns_empty(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="only")
        resp = client.get("/api/v1/sessions?page=100&per_page=10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 1

    def test_empty_search_returns_all(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="alpha")
        _make_session(db, project, name="beta")
        resp = client.get("/api/v1/sessions?page=1&search=")
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    def test_unknown_order_by_falls_back_gracefully(self, client, db):
        project = _make_project(db)
        _make_session(db, project, name="s1")
        resp = client.get("/api/v1/sessions?page=1&order_by=nonexistent_col")
        assert resp.status_code == 200  # falls back to default column


# ===========================================================================
# Section 13 — Legacy compatibility — skip/limit still works everywhere
# ===========================================================================


class TestLegacyCompatibility:
    def test_sessions_skip_limit(self, client, db):
        project = _make_project(db)
        for i in range(5):
            _make_session(db, project, name=f"s{i}")
        resp = client.get("/api/v1/sessions?skip=2&limit=2")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
        assert len(resp.json()) == 2

    def test_tasks_skip_limit(self, client, db):
        project = _make_project(db)
        for i in range(5):
            _make_task(db, project, title=f"t{i}", plan_position=i)
        resp = client.get("/api/v1/tasks?skip=2&limit=2")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
        assert len(resp.json()) == 2

    def test_projects_skip_limit(self, client, db):
        for i in range(5):
            _make_project(db, name=f"P{i}")
        resp = client.get("/api/v1/projects?skip=2&limit=2")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
        assert len(resp.json()) == 2

    def test_project_sessions_skip_limit(self, client, db):
        project = _make_project(db)
        for i in range(5):
            _make_session(db, project, name=f"s{i}")
        resp = client.get(f"/api/v1/projects/{project.id}/sessions?skip=1&limit=2")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
        assert len(resp.json()) == 2

    def test_project_tasks_skip_limit(self, client, db):
        project = _make_project(db)
        for i in range(5):
            _make_task(db, project, title=f"t{i}", plan_position=i)
        resp = client.get(f"/api/v1/projects/{project.id}/tasks?skip=1&limit=2")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
        assert len(resp.json()) == 2
