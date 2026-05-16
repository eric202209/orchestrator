"""Phase 10F workflow template tests.

Covers:
- Template loading from the YAML directory
- get() / list() / known_ids()
- WorkflowTemplate.evaluate_review_policy()
- decide_change_set_review() with template_review_policy
- dependency-config-change always holds for review
- verification-only has read_file-only allowed_ops
- Invalid/valid template_id at real FastAPI task creation endpoint
- auto_promote_eligible=false enforced in review policy
- Unknown condition names fail-closed
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Set
from unittest.mock import MagicMock, patch

import pytest

from app.services.orchestration.review_policy.change_sets import (
    decide_change_set_review,
)
from app.services.orchestration.workflow_templates import (
    WorkflowTemplate,
    WorkflowTemplateLoader,
    get_template_loader,
    known_template_ids,
    list_templates,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "docs" / "workflow-templates"

_EXPECTED_IDS = {
    "docs-update",
    "static-site-change",
    "python-bug-fix",
    "fastapi-endpoint-change",
    "react-ui-change",
    "dependency-config-change",
    "verification-only",
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_template_dir_exists():
    assert TEMPLATE_DIR.is_dir(), f"Template dir missing: {TEMPLATE_DIR}"


def test_all_expected_templates_load():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    loaded = loader.known_ids()
    assert (
        loaded == _EXPECTED_IDS
    ), f"Missing: {_EXPECTED_IDS - loaded}, Extra: {loaded - _EXPECTED_IDS}"


def test_loader_returns_correct_type():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("python-bug-fix")
    assert isinstance(tmpl, WorkflowTemplate)
    assert tmpl.id == "python-bug-fix"
    assert tmpl.display_name == "Python Bug Fix"


def test_loader_list_returns_all():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    assert len(loader.list()) == len(_EXPECTED_IDS)


def test_loader_get_unknown_returns_none():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    assert loader.get("nonexistent-template") is None


def test_loader_missing_dir_is_silent():
    loader = WorkflowTemplateLoader(Path("/nonexistent/path"))
    assert loader.list() == []


# ---------------------------------------------------------------------------
# Template field correctness
# ---------------------------------------------------------------------------


def test_dependency_config_change_is_never_auto_promote_eligible():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("dependency-config-change")
    assert tmpl is not None
    assert tmpl.auto_promote_eligible is False
    assert "always" in tmpl.review_policy.get("hold_if", [])


def test_verification_only_allowed_ops_read_only():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("verification-only")
    assert tmpl is not None
    assert tmpl.allowed_ops == ["read_file"]


def test_verification_only_enforced_when_mutations_detected():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("verification-only")
    assert tmpl is not None
    # Simulate a change_set with mutations: changed_count > 0
    policy = {
        **tmpl.review_policy,
        "allowed_ops": tmpl.allowed_ops,
        "auto_promote_eligible": tmpl.auto_promote_eligible,
    }
    decision = decide_change_set_review(
        {"changed_count": 3, "warning_flags": []},
        workspace_review_policy="hold_nontrivial",
        template_review_policy=policy,
    )
    assert decision["held_for_review"] is True
    assert decision["reason"] == "template_allowed_ops_violation"
    assert decision["template_signal"].get("allowed_ops_violation") is True


def test_verification_only_passes_when_no_mutations():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("verification-only")
    assert tmpl is not None
    policy = {
        **tmpl.review_policy,
        "allowed_ops": tmpl.allowed_ops,
        "auto_promote_eligible": tmpl.auto_promote_eligible,
    }
    decision = decide_change_set_review(
        {"changed_count": 0, "warning_flags": []},
        workspace_review_policy="hold_nontrivial",
        template_review_policy=policy,
    )
    assert decision["held_for_review"] is False


def test_verification_only_violation_surfaces_under_hold_all():
    """Violation reason must not be hidden by a prior generic hold (hold_all)."""
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("verification-only")
    assert tmpl is not None
    policy = {
        **tmpl.review_policy,
        "allowed_ops": tmpl.allowed_ops,
        "auto_promote_eligible": tmpl.auto_promote_eligible,
    }
    decision = decide_change_set_review(
        {"changed_count": 2, "warning_flags": ["deleted_files"]},
        workspace_review_policy="hold_all",
        template_review_policy=policy,
    )
    assert decision["held_for_review"] is True
    assert decision["reason"] == "template_allowed_ops_violation"
    assert decision["template_signal"].get("allowed_ops_violation") is True


def test_verification_only_violation_surfaces_with_warning_flags():
    """Violation reason surfaces even when warning flags triggered a hold first."""
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("verification-only")
    assert tmpl is not None
    policy = {
        **tmpl.review_policy,
        "allowed_ops": tmpl.allowed_ops,
        "auto_promote_eligible": tmpl.auto_promote_eligible,
    }
    decision = decide_change_set_review(
        {"changed_count": 1, "warning_flags": ["config_files_changed"]},
        workspace_review_policy="hold_nontrivial",
        template_review_policy=policy,
    )
    assert decision["held_for_review"] is True
    assert decision["reason"] == "template_allowed_ops_violation"
    assert decision["template_signal"].get("allowed_ops_violation") is True


def test_python_bug_fix_allowed_ops_include_shell():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("python-bug-fix")
    assert tmpl is not None
    assert "shell_command" in tmpl.allowed_ops
    assert "write_file" in tmpl.allowed_ops


def test_docs_update_workflow_profile():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("docs-update")
    assert tmpl is not None
    assert tmpl.workflow_profile == "review_only"
    assert tmpl.verification == "docs"


# ---------------------------------------------------------------------------
# evaluate_review_policy()
# ---------------------------------------------------------------------------


def test_evaluate_dependency_config_always_holds():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("dependency-config-change")
    assert tmpl is not None
    result = tmpl.evaluate_review_policy(set())
    assert result["forced_hold"] is True
    # auto_promote_eligible=false short-circuits before hold_if conditions
    assert "auto_promote_eligible_false" in result["triggered_hold_conditions"]


def test_evaluate_python_bug_fix_auto_promote_no_flags():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("python-bug-fix")
    assert tmpl is not None
    result = tmpl.evaluate_review_policy(set())
    assert result["forced_hold"] is False
    assert result["auto_promote_ok"] is True


def test_evaluate_python_bug_fix_hold_on_deletes():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("python-bug-fix")
    assert tmpl is not None
    result = tmpl.evaluate_review_policy({"deleted_files"})
    assert result["forced_hold"] is True
    assert "has_deletes" in result["triggered_hold_conditions"]


def test_evaluate_python_bug_fix_fails_auto_promote_with_deps():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("python-bug-fix")
    assert tmpl is not None
    result = tmpl.evaluate_review_policy({"dependency_files_changed"})
    assert result["forced_hold"] is False
    assert result["auto_promote_ok"] is False
    assert "no_dependency_changes" in result["failed_auto_promote_conditions"]


# ---------------------------------------------------------------------------
# decide_change_set_review() integration
# ---------------------------------------------------------------------------


def _cs(warning_flags=None, changed_count=1):
    return {"warning_flags": warning_flags or [], "changed_count": changed_count}


def test_template_forced_hold_overrides_auto_publish_all():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("dependency-config-change")
    # Use full policy dict so auto_promote_eligible is carried through
    full_policy = {
        **tmpl.review_policy,
        "auto_promote_eligible": tmpl.auto_promote_eligible,
        "allowed_ops": tmpl.allowed_ops,
    }
    decision = decide_change_set_review(
        _cs(),
        workspace_review_policy="auto_publish_all",
        template_review_policy=full_policy,
    )
    assert decision["held_for_review"] is True
    assert decision["reason"] == "template_auto_promote_not_eligible"
    assert decision["outcome"] == "hold_for_review"


def test_auto_promote_eligible_false_enforced_in_decide_change_set_review():
    """decide_change_set_review enforces auto_promote_eligible without hold_if: always."""
    decision = decide_change_set_review(
        _cs(),
        workspace_review_policy="auto_publish_all",
        template_review_policy={
            "auto_promote_if": [],
            "hold_if": [],  # no hold_if: [always] — enforcement must not rely on it
            "auto_promote_eligible": False,
            "allowed_ops": ["write_file"],
        },
    )
    assert decision["held_for_review"] is True
    assert decision["reason"] == "template_auto_promote_not_eligible"


def test_template_auto_promote_releases_hold_nontrivial():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("python-bug-fix")
    # Non-source-risk warning flag shouldn't block auto-promote for this template
    decision = decide_change_set_review(
        _cs(warning_flags=["some_minor_flag"]),
        workspace_review_policy="hold_nontrivial",
        template_review_policy=tmpl.review_policy,
    )
    assert decision["held_for_review"] is False
    assert decision["reason"] == "template_auto_promote_conditions_met"


def test_template_hold_condition_triggered_by_deletes():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("python-bug-fix")
    decision = decide_change_set_review(
        _cs(warning_flags=["deleted_files"]),
        workspace_review_policy="hold_nontrivial",
        template_review_policy=tmpl.review_policy,
    )
    assert decision["held_for_review"] is True
    assert decision["reason"] == "template_hold_condition_triggered"


def test_template_signal_included_in_decision():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("python-bug-fix")
    decision = decide_change_set_review(
        _cs(),
        workspace_review_policy="hold_nontrivial",
        template_review_policy=tmpl.review_policy,
    )
    assert "template_signal" in decision
    assert isinstance(decision["template_signal"], dict)


def test_no_template_review_policy_preserves_existing_behavior():
    decision = decide_change_set_review(
        _cs(warning_flags=["deleted_files"]),
        workspace_review_policy="hold_nontrivial",
        template_review_policy=None,
    )
    assert decision["held_for_review"] is True
    assert decision["reason"] == "nontrivial_change_set_review_required"
    assert decision["template_signal"] == {}


def test_hold_all_still_holds_even_with_auto_promote_template():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("docs-update")
    decision = decide_change_set_review(
        _cs(),
        workspace_review_policy="hold_all",
        template_review_policy=tmpl.review_policy,
    )
    # hold_all → holds; template auto_promote_if doesn't release hold_all
    assert decision["held_for_review"] is True


# ---------------------------------------------------------------------------
# Singleton / module-level helpers
# ---------------------------------------------------------------------------


def test_known_template_ids_returns_expected_set():
    ids = known_template_ids()
    assert _EXPECTED_IDS.issubset(ids)


def test_list_templates_returns_non_empty():
    templates = list_templates()
    assert len(templates) >= len(_EXPECTED_IDS)


def test_get_template_loader_is_singleton():
    loader_a = get_template_loader()
    loader_b = get_template_loader()
    assert loader_a is loader_b


# ---------------------------------------------------------------------------
# Task creation — real FastAPI route via TestClient
# ---------------------------------------------------------------------------


@pytest.fixture()
def _project(db_session_factory):
    from app.models import Project

    db = db_session_factory()
    p = Project(name="template-test-project", workspace_path=None)
    db.add(p)
    db.commit()
    db.refresh(p)
    project_id = p.id
    db.close()
    return project_id


def test_task_creation_valid_template_id_accepted(authenticated_client, _project):
    resp = authenticated_client.post(
        "/api/v1/tasks",
        json={
            "title": "Fix authentication bug",
            "project_id": _project,
            "template_id": "python-bug-fix",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["template_id"] == "python-bug-fix"


def test_task_creation_invalid_template_id_rejected(authenticated_client, _project):
    resp = authenticated_client.post(
        "/api/v1/tasks",
        json={
            "title": "Some task",
            "project_id": _project,
            "template_id": "nonexistent-template",
        },
    )
    assert resp.status_code == 422
    assert "nonexistent-template" in resp.text


def test_task_creation_no_template_id_accepted(authenticated_client, _project):
    resp = authenticated_client.post(
        "/api/v1/tasks",
        json={"title": "No template task", "project_id": _project},
    )
    assert resp.status_code == 201
    assert resp.json()["template_id"] is None


# ---------------------------------------------------------------------------
# auto_promote_eligible enforced
# ---------------------------------------------------------------------------


def test_evaluate_auto_promote_eligible_false_always_holds():
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("dependency-config-change")
    assert tmpl is not None
    assert tmpl.auto_promote_eligible is False
    result = tmpl.evaluate_review_policy(set())
    assert result["forced_hold"] is True
    assert "auto_promote_eligible_false" in result["triggered_hold_conditions"]


def test_evaluate_auto_promote_eligible_false_ignores_conditions():
    """auto_promote_eligible=false holds even with empty warning_flags and no hold_if."""
    loader = WorkflowTemplateLoader(TEMPLATE_DIR)
    tmpl = loader.get("dependency-config-change")
    assert tmpl is not None
    # Even with no warning flags, must hold
    result = tmpl.evaluate_review_policy(set())
    assert result["forced_hold"] is True


def test_resolve_template_carries_auto_promote_eligible():
    from app.services.orchestration.phases.completion_flow import (
        _resolve_template_review_policy,
    )

    class FakeTask:
        template_id = "dependency-config-change"

    policy = _resolve_template_review_policy(FakeTask())
    assert policy is not None
    assert policy.get("auto_promote_eligible") is False
    assert "allowed_ops" in policy


# ---------------------------------------------------------------------------
# Unknown condition names fail-closed
# ---------------------------------------------------------------------------


def test_unknown_hold_condition_fails_closed():
    tmpl = WorkflowTemplate(
        id="test-unknown",
        display_name="Test",
        workflow_profile="default",
        review_policy={"auto_promote_if": [], "hold_if": ["typo_condition"]},
        allowed_ops=[],
        verification="mutation",
        auto_promote_eligible=True,
    )
    result = tmpl.evaluate_review_policy(set())
    # Unknown hold_if condition must trigger hold (fail-closed)
    assert result["forced_hold"] is True


def test_unknown_auto_promote_condition_fails_closed():
    tmpl = WorkflowTemplate(
        id="test-unknown-ap",
        display_name="Test",
        workflow_profile="default",
        review_policy={"auto_promote_if": ["typo_no_stuff"], "hold_if": []},
        allowed_ops=[],
        verification="mutation",
        auto_promote_eligible=True,
    )
    result = tmpl.evaluate_review_policy(set())
    # Unknown auto_promote_if condition must block auto-promote (fail-closed)
    assert result["auto_promote_ok"] is False


def test_loader_logs_unknown_conditions(caplog):
    import tempfile
    import yaml

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad-template.yaml"
        p.write_text(
            yaml.dump(
                {
                    "id": "bad-template",
                    "display_name": "Bad",
                    "workflow_profile": "default",
                    "review_policy": {
                        "auto_promote_if": ["typo_condition"],
                        "hold_if": ["another_typo"],
                    },
                    "allowed_ops": [],
                    "verification": "mutation",
                    "auto_promote_eligible": True,
                    "risk_flags": [],
                }
            )
        )
        import logging

        with caplog.at_level(logging.WARNING):
            loader = WorkflowTemplateLoader(Path(td))
        assert "unknown condition" in caplog.text.lower()
        # Template still loads (warn, don't skip)
        assert "bad-template" in loader.known_ids()
