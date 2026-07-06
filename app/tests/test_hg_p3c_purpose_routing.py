"""Tests for HG-P3c — Guidance Purpose Routing."""

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
    User,
)
from app.services.human_guidance.service import (
    VALID_PURPOSES,
    _parse_purpose_targets,
    _purpose_matches,
    collect_active_guidance,
    create_guidance,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(email="p3c@example.com", hashed_password="hashed", is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def project(db_session: Session, user: User) -> Project:
    p = Project(name="p3c-project", workspace_path=None, user_id=user.id)
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
    purpose_targets: Optional[list] = None,
    priority: int = 0,
) -> HumanGuidance:
    entry, _ = create_guidance(
        db,
        user_id=user_id,
        project_id=project_id,
        scope="project",
        message=message,
        priority=priority,
        purpose_targets=purpose_targets,
    )
    return entry


# ---------------------------------------------------------------------------
# Unit: _parse_purpose_targets
# ---------------------------------------------------------------------------


class TestParsePurposeTargets:
    def test_none_returns_all(self):
        assert _parse_purpose_targets(None) == ["all"]

    def test_empty_list_returns_all(self):
        assert _parse_purpose_targets([]) == ["all"]

    def test_empty_string_returns_all(self):
        assert _parse_purpose_targets("") == ["all"]

    def test_list_passthrough(self):
        assert _parse_purpose_targets(["planning", "repair"]) == ["planning", "repair"]

    def test_json_string(self):
        assert _parse_purpose_targets('["execution"]') == ["execution"]

    def test_invalid_json_returns_all(self):
        assert _parse_purpose_targets("not-json") == ["all"]

    def test_single_all_value(self):
        assert _parse_purpose_targets(["all"]) == ["all"]


# ---------------------------------------------------------------------------
# Unit: _purpose_matches
# ---------------------------------------------------------------------------


class TestPurposeMatches:
    def _row(self, targets):
        class _R:
            purpose_targets = targets

        return _R()

    def test_purpose_all_includes_everything(self):
        row = self._row(["planning"])
        assert _purpose_matches(row, "all")

    def test_all_target_matches_any_purpose(self):
        row = self._row(["all"])
        assert _purpose_matches(row, "planning")
        assert _purpose_matches(row, "repair")
        assert _purpose_matches(row, "execution")
        assert _purpose_matches(row, "validation")

    def test_planning_only_matches_planning(self):
        row = self._row(["planning"])
        assert _purpose_matches(row, "planning")
        assert not _purpose_matches(row, "repair")
        assert not _purpose_matches(row, "execution")
        assert not _purpose_matches(row, "validation")

    def test_repair_only_matches_repair(self):
        row = self._row(["repair"])
        assert _purpose_matches(row, "repair")
        assert not _purpose_matches(row, "planning")

    def test_validation_only_matches_validation(self):
        row = self._row(["validation"])
        assert _purpose_matches(row, "validation")
        assert not _purpose_matches(row, "execution")

    def test_multiple_purposes(self):
        row = self._row(["planning", "repair"])
        assert _purpose_matches(row, "planning")
        assert _purpose_matches(row, "repair")
        assert not _purpose_matches(row, "execution")
        assert not _purpose_matches(row, "validation")

    def test_none_target_treated_as_all(self):
        row = self._row(None)
        assert _purpose_matches(row, "planning")
        assert _purpose_matches(row, "repair")

    def test_empty_purpose_treated_as_all(self):
        row = self._row(["planning"])
        assert _purpose_matches(row, "")


# ---------------------------------------------------------------------------
# Integration: collect_active_guidance with purpose param
# ---------------------------------------------------------------------------


class TestCollectActiveGuidancePurpose:
    def test_default_all_returns_everything(self, db_session, user, project):
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="global guidance",
            purpose_targets=["all"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="planning only",
            purpose_targets=["planning"],
        )
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        messages = [e["message"] for e in entries]
        assert "global guidance" in messages
        assert "planning only" in messages

    def test_planning_purpose_includes_all_and_planning(
        self, db_session, user, project
    ):
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="all guidance",
            purpose_targets=["all"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="planning guidance",
            purpose_targets=["planning"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="repair only",
            purpose_targets=["repair"],
        )
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            purpose="planning",
        )
        messages = [e["message"] for e in entries]
        assert "all guidance" in messages
        assert "planning guidance" in messages
        assert "repair only" not in messages

    def test_repair_purpose_includes_all_and_repair(self, db_session, user, project):
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="all guidance",
            purpose_targets=["all"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="repair guidance",
            purpose_targets=["repair"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="planning only",
            purpose_targets=["planning"],
        )
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            purpose="repair",
        )
        messages = [e["message"] for e in entries]
        assert "all guidance" in messages
        assert "repair guidance" in messages
        assert "planning only" not in messages

    def test_validation_purpose(self, db_session, user, project):
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="validation guidance",
            purpose_targets=["validation"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="execution only",
            purpose_targets=["execution"],
        )
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            purpose="validation",
        )
        messages = [e["message"] for e in entries]
        assert "validation guidance" in messages
        assert "execution only" not in messages

    def test_execution_purpose(self, db_session, user, project):
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="execution guidance",
            purpose_targets=["execution"],
        )
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="repair only",
            purpose_targets=["repair"],
        )
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            purpose="execution",
        )
        messages = [e["message"] for e in entries]
        assert "execution guidance" in messages
        assert "repair only" not in messages

    def test_mixed_purpose_targets(self, db_session, user, project):
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="planning and repair",
            purpose_targets=["planning", "repair"],
        )
        planning_entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            purpose="planning",
        )
        repair_entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            purpose="repair",
        )
        validation_entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            purpose="validation",
        )
        assert any(e["message"] == "planning and repair" for e in planning_entries)
        assert any(e["message"] == "planning and repair" for e in repair_entries)
        assert not any(
            e["message"] == "planning and repair" for e in validation_entries
        )

    def test_null_purpose_targets_treated_as_all(self, db_session, user, project):
        # Simulate a legacy row where purpose_targets is NULL (becomes ["all"])
        entry = HumanGuidance(
            user_id=user.id,
            project_id=project.id,
            scope=GuidanceScope.PROJECT,
            message="legacy no purpose",
            status=GuidanceStatus.ACTIVE,
            priority=0,
            revision=1,
            purpose_targets=None,
        )
        db_session.add(entry)
        db_session.commit()

        for purpose in ("planning", "execution", "repair", "validation"):
            entries = collect_active_guidance(
                db_session,
                user_id=user.id,
                project_id=project.id,
                session_id=None,
                task_id=None,
                purpose=purpose,
            )
            assert any(
                e["message"] == "legacy no purpose" for e in entries
            ), f"Legacy row missing for purpose={purpose}"

    def test_purpose_targets_in_output(self, db_session, user, project):
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="annotated",
            purpose_targets=["planning"],
        )
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        annotated = next(e for e in entries if e["message"] == "annotated")
        assert annotated["purpose_targets"] == ["planning"]

    def test_backward_compat_no_purpose_arg(self, db_session, user, project):
        # Calling without purpose= must return all entries, unchanged behavior
        _add_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            message="compat check",
            purpose_targets=["repair"],
        )
        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        assert any(e["message"] == "compat check" for e in entries)


# ---------------------------------------------------------------------------
# API: create guidance with purpose_targets
# ---------------------------------------------------------------------------


class TestCreateGuidanceWithPurpose:
    def test_create_with_purpose_targets(self, client, db_session, user, project):
        resp = client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "Never use mutable defaults",
                "scope": "project",
                "purpose_targets": ["planning", "repair"],
            },
        )
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["purpose_targets"] == ["planning", "repair"]

    def test_create_default_purpose_targets(self, client, db_session, user, project):
        resp = client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "default purpose entry",
                "scope": "project",
            },
        )
        assert resp.status_code in (200, 201)
        data = resp.json()
        assert data["purpose_targets"] == ["all"]

    def test_get_guidance_includes_purpose_targets(
        self, client, db_session, user, project
    ):
        create_resp = client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "serialization check",
                "scope": "project",
                "purpose_targets": ["validation"],
            },
        )
        assert create_resp.status_code in (200, 201)
        gid = create_resp.json()["id"]

        get_resp = client.get(f"/api/v1/guidance/{gid}")
        assert get_resp.status_code == 200
        assert get_resp.json()["purpose_targets"] == ["validation"]

    def test_list_guidance_includes_purpose_targets(
        self, client, db_session, user, project
    ):
        client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "list serialization",
                "scope": "project",
                "purpose_targets": ["execution"],
            },
        )
        resp = client.get(f"/api/v1/projects/{project.id}/guidance")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert any(i["purpose_targets"] == ["execution"] for i in items)


# ---------------------------------------------------------------------------
# API: preview rendered endpoint with purpose param
# ---------------------------------------------------------------------------


class TestRenderedGuidancePurpose:
    def test_rendered_no_purpose_returns_all(self, client, db_session, user, project):
        client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "planning note",
                "scope": "project",
                "purpose_targets": ["planning"],
            },
        )
        client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "repair note",
                "scope": "project",
                "purpose_targets": ["repair"],
            },
        )
        resp = client.get(f"/api/v1/projects/{project.id}/guidance/rendered")
        assert resp.status_code == 200
        data = resp.json()
        assert data["purpose"] == "all"
        # Both entries must appear
        assert data["selected_count"] >= 2

    def test_rendered_planning_purpose_filters(self, client, db_session, user, project):
        client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "planning note p3c",
                "scope": "project",
                "purpose_targets": ["planning"],
            },
        )
        client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "repair note p3c",
                "scope": "project",
                "purpose_targets": ["repair"],
            },
        )
        resp = client.get(
            f"/api/v1/projects/{project.id}/guidance/rendered?purpose=planning"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["purpose"] == "planning"
        assert "filtered_purpose_ids" in data
        # repair-only entry must be absent from the block
        assert "repair note p3c" not in (data.get("block") or "")
        assert "planning note p3c" in (data.get("block") or "")

    def test_rendered_response_includes_filtered_purpose_ids(
        self, client, db_session, user, project
    ):
        repair_resp = client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "repair only fpi",
                "scope": "project",
                "purpose_targets": ["repair"],
            },
        )
        repair_id = repair_resp.json()["id"]

        resp = client.get(
            f"/api/v1/projects/{project.id}/guidance/rendered?purpose=planning"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert repair_id in data["filtered_purpose_ids"]


# ---------------------------------------------------------------------------
# API: readiness purpose_statistics
# ---------------------------------------------------------------------------


class TestReadinessPurposeStatistics:
    def test_readiness_includes_purpose_statistics(
        self, client, db_session, user, project
    ):
        client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "readiness planning",
                "scope": "project",
                "purpose_targets": ["planning"],
            },
        )
        client.post(
            f"/api/v1/projects/{project.id}/guidance",
            json={
                "message": "readiness all",
                "scope": "project",
                "purpose_targets": ["all"],
            },
        )
        resp = client.get(f"/api/v1/projects/{project.id}/guidance/readiness")
        assert resp.status_code == 200
        data = resp.json()
        assert "purpose_statistics" in data
        ps = data["purpose_statistics"]
        assert ps["planning"] == 1
        assert ps["all"] == 1
        assert ps["repair"] == 0
        assert ps["execution"] == 0
        assert ps["validation"] == 0

    def test_readiness_purpose_statistics_zero_when_no_guidance(
        self, client, db_session, user, project
    ):
        resp = client.get(f"/api/v1/projects/{project.id}/guidance/readiness")
        assert resp.status_code == 200
        data = resp.json()
        ps = data["purpose_statistics"]
        for key in ("planning", "execution", "repair", "validation", "all"):
            assert ps[key] == 0


# ---------------------------------------------------------------------------
# Migration backfill: NULL purpose_targets behaves as ["all"]
# ---------------------------------------------------------------------------


class TestMigrationBackfill:
    def test_null_purpose_treated_as_all_in_service(self, db_session, user, project):
        entry = HumanGuidance(
            user_id=user.id,
            project_id=project.id,
            scope=GuidanceScope.PROJECT,
            message="pre-migration row",
            status=GuidanceStatus.ACTIVE,
            priority=0,
            revision=1,
            purpose_targets=None,
        )
        db_session.add(entry)
        db_session.commit()

        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
            purpose="planning",
        )
        assert any(e["message"] == "pre-migration row" for e in entries)

    def test_null_purpose_serialized_as_all(self, db_session, user, project):
        entry = HumanGuidance(
            user_id=user.id,
            project_id=project.id,
            scope=GuidanceScope.PROJECT,
            message="serialized null",
            status=GuidanceStatus.ACTIVE,
            priority=0,
            revision=1,
            purpose_targets=None,
        )
        db_session.add(entry)
        db_session.commit()

        entries = collect_active_guidance(
            db_session,
            user_id=user.id,
            project_id=project.id,
            session_id=None,
            task_id=None,
        )
        row = next(e for e in entries if e["message"] == "serialized null")
        assert row["purpose_targets"] == ["all"]


# ---------------------------------------------------------------------------
# VALID_PURPOSES constant
# ---------------------------------------------------------------------------


class TestValidPurposes:
    def test_valid_purposes_set(self):
        assert "all" in VALID_PURPOSES
        assert "planning" in VALID_PURPOSES
        assert "execution" in VALID_PURPOSES
        assert "repair" in VALID_PURPOSES
        assert "validation" in VALID_PURPOSES
