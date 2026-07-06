"""Tests for HG-P3a — Backend-Aware Human Guidance Activation."""

from __future__ import annotations

from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    GuidanceScope,
    GuidanceStatus,
    HumanGuidance,
    Project,
    Session as SessionModel,
    User,
)
from app.services.human_guidance.service import (
    VALID_BACKENDS,
    _backend_matches,
    _parse_backend_targets,
    collect_active_guidance,
    create_guidance,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(email="p3a@example.com", hashed_password="hashed", is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def project(db_session: Session, user: User) -> Project:
    p = Project(name="p3a-project", workspace_path=None, user_id=user.id)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def client(authenticated_client: TestClient, user: User) -> TestClient:
    return authenticated_client


def _add_guidance(
    db: Session,
    *,
    user_id: int,
    project_id: int,
    message: str,
    backend_targets: Optional[list] = None,
    priority: int = 0,
) -> HumanGuidance:
    entry, _ = create_guidance(
        db,
        user_id=user_id,
        project_id=project_id,
        scope="project",
        message=message,
        priority=priority,
        backend_targets=backend_targets or ["all"],
    )
    return entry


# ---------------------------------------------------------------------------
# Unit tests: _parse_backend_targets
# ---------------------------------------------------------------------------


class TestParseBackendTargets:
    def test_none_returns_all(self):
        assert _parse_backend_targets(None) == ["all"]

    def test_empty_string_returns_all(self):
        assert _parse_backend_targets("") == ["all"]

    def test_json_list_parsed(self):
        assert _parse_backend_targets('["qwen", "claude"]') == ["qwen", "claude"]

    def test_list_passthrough(self):
        assert _parse_backend_targets(["local_openclaw"]) == ["local_openclaw"]

    def test_invalid_json_returns_all(self):
        assert _parse_backend_targets("not-json") == ["all"]


# ---------------------------------------------------------------------------
# Unit tests: _backend_matches
# ---------------------------------------------------------------------------


class TestBackendMatches:
    def _row(self, targets):
        class _R:
            backend_targets = targets

        return _R()

    def test_all_target_matches_any_backend(self):
        row = self._row(["all"])
        assert _backend_matches(row, "qwen")
        assert _backend_matches(row, "local_openclaw")
        assert _backend_matches(row, "claude")

    def test_qwen_only_matches_qwen(self):
        row = self._row(["qwen"])
        assert _backend_matches(row, "qwen")
        assert not _backend_matches(row, "local_openclaw")
        assert not _backend_matches(row, "claude")

    def test_openclaw_only_matches_openclaw(self):
        row = self._row(["local_openclaw"])
        assert _backend_matches(row, "local_openclaw")
        assert not _backend_matches(row, "qwen")

    def test_multiple_targets(self):
        row = self._row(["qwen", "claude"])
        assert _backend_matches(row, "qwen")
        assert _backend_matches(row, "claude")
        assert not _backend_matches(row, "local_openclaw")

    def test_none_targets_defaults_to_all(self):
        row = self._row(None)
        assert _backend_matches(row, "qwen")
        assert _backend_matches(row, "local_openclaw")


# ---------------------------------------------------------------------------
# Integration: collect_active_guidance with backend filtering
# ---------------------------------------------------------------------------


class TestCollectActiveGuidanceBackendFilter:
    def test_default_all_returns_everything(
        self, db_session: Session, user: User, project: Project
    ):
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="all rule",
            backend_targets=["all"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="qwen only",
            backend_targets=["qwen"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="openclaw only",
            backend_targets=["local_openclaw"],
        )

        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="all",
        )
        messages = {r["message"] for r in results}
        assert "all rule" in messages
        assert "qwen only" in messages
        assert "openclaw only" in messages

    def test_qwen_backend_excludes_openclaw(
        self, db_session: Session, user: User, project: Project
    ):
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="all rule",
            backend_targets=["all"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="qwen only",
            backend_targets=["qwen"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="openclaw only",
            backend_targets=["local_openclaw"],
        )

        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="qwen",
        )
        messages = {r["message"] for r in results}
        assert "all rule" in messages
        assert "qwen only" in messages
        assert "openclaw only" not in messages

    def test_openclaw_backend_excludes_qwen(
        self, db_session: Session, user: User, project: Project
    ):
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="all rule",
            backend_targets=["all"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="qwen only",
            backend_targets=["qwen"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="openclaw only",
            backend_targets=["local_openclaw"],
        )

        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="local_openclaw",
        )
        messages = {r["message"] for r in results}
        assert "all rule" in messages
        assert "openclaw only" in messages
        assert "qwen only" not in messages

    def test_mixed_targets_entry(
        self, db_session: Session, user: User, project: Project
    ):
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="qwen and claude",
            backend_targets=["qwen", "claude"],
        )

        qwen_results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="qwen",
        )
        claude_results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="claude",
        )
        openclaw_results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="local_openclaw",
        )
        assert any(r["message"] == "qwen and claude" for r in qwen_results)
        assert any(r["message"] == "qwen and claude" for r in claude_results)
        assert not any(r["message"] == "qwen and claude" for r in openclaw_results)

    def test_backend_targets_in_output_dict(
        self, db_session: Session, user: User, project: Project
    ):
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="qwen rule",
            backend_targets=["qwen"],
        )
        results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="all",
        )
        entry = next(r for r in results if r["message"] == "qwen rule")
        assert entry["backend_targets"] == ["qwen"]

    def test_existing_guidance_without_backend_targets_treated_as_all(
        self, db_session: Session, user: User, project: Project
    ):
        # Simulate a legacy row: backend_targets=None (pre-migration state)
        row = HumanGuidance(
            user_id=user.id,
            project_id=project.id,
            scope=GuidanceScope.PROJECT,
            message="legacy rule no backend",
            status=GuidanceStatus.ACTIVE,
            priority=0,
            revision=1,
            backend_targets=None,
        )
        db_session.add(row)
        db_session.commit()

        qwen_results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="qwen",
        )
        openclaw_results = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            backend="local_openclaw",
        )
        assert any(r["message"] == "legacy rule no backend" for r in qwen_results)
        assert any(r["message"] == "legacy rule no backend" for r in openclaw_results)


# ---------------------------------------------------------------------------
# Integration: create_guidance with backend_targets
# ---------------------------------------------------------------------------


class TestCreateGuidanceBackendTargets:
    def test_default_backend_targets_is_all(
        self, db_session: Session, user: User, project: Project
    ):
        entry, created = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="default backend guidance",
        )
        assert created
        assert _parse_backend_targets(entry.backend_targets) == ["all"]

    def test_explicit_qwen_target(
        self, db_session: Session, user: User, project: Project
    ):
        entry, created = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="qwen explicit",
            backend_targets=["qwen"],
        )
        assert created
        assert _parse_backend_targets(entry.backend_targets) == ["qwen"]

    def test_unknown_backend_target_is_metadata_only(
        self, db_session: Session, user: User, project: Project
    ):
        entry, created = create_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            scope="project",
            message="custom backend metadata",
            backend_targets=["not_a_real_backend"],
        )
        assert created
        assert _parse_backend_targets(entry.backend_targets) == ["not_a_real_backend"]

    def test_all_valid_backends_accepted(
        self, db_session: Session, user: User, project: Project
    ):
        for i, backend in enumerate(sorted(VALID_BACKENDS)):
            entry, _ = create_guidance(
                db_session,
                user_id=user.id,
                project_id=project.id,
                scope="project",
                message=f"rule for {backend} {i}",
                backend_targets=[backend],
            )
            assert _parse_backend_targets(entry.backend_targets) == [backend]


# ---------------------------------------------------------------------------
# Integration: readiness_status backend_statistics
# ---------------------------------------------------------------------------


class TestReadinessBackendStatistics:
    def test_backend_statistics_in_response(
        self, db_session: Session, user: User, project: Project
    ):
        from app.services.human_guidance.activation import readiness_status

        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="all rule",
            backend_targets=["all"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="qwen only",
            backend_targets=["qwen"],
        )

        result = readiness_status(db_session, project_id=project.id, backend="qwen")
        assert "backend_statistics" in result
        stats = result["backend_statistics"]
        assert stats["backend"] == "qwen"
        assert stats["matching_guidance"] == 2  # "all" + "qwen"
        assert stats["filtered_guidance"] == 0  # nothing filtered for qwen

    def test_backend_statistics_filtered_count(
        self, db_session: Session, user: User, project: Project
    ):
        from app.services.human_guidance.activation import readiness_status

        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="all rule",
            backend_targets=["all"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="qwen only",
            backend_targets=["qwen"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="openclaw only",
            backend_targets=["local_openclaw"],
        )

        result = readiness_status(db_session, project_id=project.id, backend="qwen")
        stats = result["backend_statistics"]
        # openclaw-only guidance is filtered out for qwen
        assert stats["matching_guidance"] == 2  # all + qwen
        assert stats["filtered_guidance"] == 1  # openclaw-only excluded

    def test_backend_all_shows_no_filtering(
        self, db_session: Session, user: User, project: Project
    ):
        from app.services.human_guidance.activation import readiness_status

        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="qwen only",
            backend_targets=["qwen"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="openclaw only",
            backend_targets=["local_openclaw"],
        )

        result = readiness_status(db_session, project_id=project.id, backend="all")
        stats = result["backend_statistics"]
        assert stats["backend"] == "all"
        assert stats["filtered_guidance"] == 0
        assert stats["matching_guidance"] == 2


# ---------------------------------------------------------------------------
# API: rendered endpoint with backend param
# ---------------------------------------------------------------------------


class TestRenderedEndpointBackend:
    def test_rendered_default_backend_all(
        self, client: TestClient, db_session: Session, user: User, project: Project
    ):
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="all rule",
            backend_targets=["all"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="qwen only",
            backend_targets=["qwen"],
        )

        r = client.get(f"/api/v1/projects/{project.id}/guidance/rendered")
        assert r.status_code == 200
        data = r.json()
        assert "backend" in data
        assert "filtered_backend_ids" in data
        assert data["backend"] == "all"
        assert data["filtered_backend_ids"] == []

    def test_rendered_qwen_backend_filters_openclaw(
        self, client: TestClient, db_session: Session, user: User, project: Project
    ):
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="all rule",
            backend_targets=["all"],
        )
        openclaw_entry = _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="openclaw only",
            backend_targets=["local_openclaw"],
        )

        r = client.get(f"/api/v1/projects/{project.id}/guidance/rendered?backend=qwen")
        assert r.status_code == 200
        data = r.json()
        assert data["backend"] == "qwen"
        assert openclaw_entry.id in data["filtered_backend_ids"]

    def test_rendered_unknown_backend_is_metadata_filter(
        self, client: TestClient, db_session: Session, user: User, project: Project
    ):
        r = client.get(f"/api/v1/projects/{project.id}/guidance/rendered?backend=bogus")
        assert r.status_code == 200
        assert r.json()["backend"] == "bogus"

    def test_backend_targets_in_create_response(
        self, client: TestClient, db_session: Session, user: User, project: Project
    ):
        r = client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "qwen rule via api",
                "scope": "project",
                "backend_targets": ["qwen"],
            },
        )
        assert r.status_code == 201
        data = r.json()
        assert data["backend_targets"] == ["qwen"]

    def test_backend_targets_defaults_to_all_in_create(
        self, client: TestClient, db_session: Session, user: User, project: Project
    ):
        r = client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={"message": "default backend api rule", "scope": "project"},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["backend_targets"] == ["all"]

    def test_unknown_backend_target_in_create_is_allowed(
        self, client: TestClient, db_session: Session, user: User, project: Project
    ):
        r = client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "bad backend rule",
                "scope": "project",
                "backend_targets": ["unknown_backend"],
            },
        )
        assert r.status_code == 201
        assert r.json()["backend_targets"] == ["unknown_backend"]
