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


@pytest.fixture(autouse=True)
def reset_runtime_flags():
    original_force_inline = settings.ORCHESTRATOR_FORCE_INLINE_PLANNING
    original_keypair_flag = settings.ALLOW_TEST_KEYPAIR_ENDPOINT
    original_rate_limit_window = settings.AUTH_RATE_LIMIT_WINDOW_SECONDS
    original_rate_limit_attempts = settings.AUTH_RATE_LIMIT_MAX_ATTEMPTS

    settings.ORCHESTRATOR_FORCE_INLINE_PLANNING = True
    clear_auth_rate_limits()

    try:
        yield
    finally:
        settings.ORCHESTRATOR_FORCE_INLINE_PLANNING = original_force_inline
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
