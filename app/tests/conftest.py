from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.router import api_router
from app.config import settings
from app.database import get_db
from app.dependencies import get_current_active_user, get_current_user
from app.models import Base, User
from app.services.auth_rate_limit import clear_auth_rate_limits

SEMANTIC_TEST_MODULES = {
    "test_decision_timeline_endpoint.py",
    "test_orchestration_event_journal.py",
    "test_orchestration_replay.py",
    "test_orchestration_replay_fixtures.py",
    "test_policy_simulation_regressions.py",
    "test_session_events_endpoint.py",
    "test_session_replay_endpoint.py",
}

SLOW_TEST_NODEIDS = {
    "test_admin_diagnostics.py::test_diagnostics_endpoint_returns_expected_shape",
    "test_admin_diagnostics.py::test_diagnostics_backends_list_includes_registered_providers",
    "test_admin_diagnostics.py::test_diagnostics_overall_status_is_valid_string",
    "test_admin_diagnostics.py::test_diagnostics_queue_shape",
    "test_admin_diagnostics.py::test_diagnostics_streaming_shape",
    "test_admin_diagnostics.py::test_diagnostics_streaming_recent_errors_are_reported",
    "test_admin_diagnostics.py::test_diagnostics_sessions_shape",
    "test_admin_diagnostics.py::test_diagnostics_recent_audit_events_only_returns_structured",
    "test_planning_knowledge_logging.py::test_malformed_planning_output_repair_timeout_does_not_leave_session_running",
    "test_planning_knowledge_logging.py::test_planning_repair_timeout_records_failure_knowledge",
    "test_planning_knowledge_logging.py::test_oversized_planning_repair_prompt_skips_repair_and_records_failure_knowledge",
}


def pytest_configure(config):
    # Marker definitions live in pytest.ini so CI, local pytest, and future
    # Codex sessions share one visible test-tier contract.
    return None


INTEGRATION_TEST_MODULE_KEYWORDS = (
    "_api",
    "_endpoint",
    "_endpoints",
    "admin_diagnostics",
    "canonical_workspace",
    "execution_reliability",
    "github",
    "knowledge_service",
    "knowledge_usage",
    "langfuse",
    "operator_controls",
    "orchestration_without_langfuse",
    "phase6a_runtime",
    "planner_recovery",
    "planning_background",
    "planning_knowledge",
    "planning_sessions",
    "project_tasks",
    "replan_flow",
    "schema_migrations",
    "security",
    "session_auth",
    "session_execution_service",
    "session_lifecycle",
    "stopped_session",
    "task_execution_transaction",
    "workspace_restore",
)


def pytest_collection_modifyitems(config, items):
    for item in items:
        module_name = item.path.name
        module_nodeid = f"{module_name}::{item.name}"
        if module_nodeid in SLOW_TEST_NODEIDS:
            item.add_marker(pytest.mark.slow)
        if module_name in SEMANTIC_TEST_MODULES:
            item.add_marker(pytest.mark.semantic)
        elif any(
            keyword in module_name for keyword in INTEGRATION_TEST_MODULE_KEYWORDS
        ):
            item.add_marker(pytest.mark.integration)
        else:
            item.add_marker(pytest.mark.unit)


@pytest.fixture(autouse=True)
def reset_runtime_flags():
    original_force_inline = settings.INLINE_PLANNING
    original_keypair_flag = settings.ALLOW_TEST_KEYPAIR_ENDPOINT
    original_rate_limit_window = settings.AUTH_RATE_LIMIT_WINDOW_SECONDS
    original_rate_limit_attempts = settings.AUTH_RATE_LIMIT_MAX_ATTEMPTS

    settings.INLINE_PLANNING = True
    clear_auth_rate_limits()

    try:
        yield
    finally:
        settings.INLINE_PLANNING = original_force_inline
        settings.ALLOW_TEST_KEYPAIR_ENDPOINT = original_keypair_flag
        settings.AUTH_RATE_LIMIT_WINDOW_SECONDS = original_rate_limit_window
        settings.AUTH_RATE_LIMIT_MAX_ATTEMPTS = original_rate_limit_attempts
        clear_auth_rate_limits()


@pytest.fixture()
def qdrant_memory(monkeypatch):
    """Set QDRANT_URL to ':memory:' for tests that instantiate KnowledgeService via settings."""
    from app.config import settings

    monkeypatch.setattr(settings, "QDRANT_URL", ":memory:")


@pytest.fixture
def db_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    try:
        yield TestingSessionLocal
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def db_session(db_session_factory) -> Generator[Session, None, None]:
    db = db_session_factory()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def api_app(db_session_factory) -> Generator[FastAPI, None, None]:
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")

    def override_get_db():
        db = db_session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def api_client(api_app) -> Generator[TestClient, None, None]:
    client = TestClient(api_app)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def authenticated_client(api_app) -> Generator[TestClient, None, None]:
    fake_user = User(
        id=1,
        email="regression@example.com",
        hashed_password="not-used",
        is_active=True,
    )

    def override_current_user():
        return fake_user

    api_app.dependency_overrides[get_current_user] = override_current_user
    api_app.dependency_overrides[get_current_active_user] = override_current_user

    client = TestClient(api_app)
    try:
        yield client
    finally:
        client.close()
