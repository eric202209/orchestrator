from __future__ import annotations

import json

import pytest

from app.models import PlanningSession, Project
from app.services.planning.planning_session_service import PlanningSessionService


REQUIRED_ARTIFACT_KEYS = (
    "requirements",
    "design",
    "implementation_plan",
    "planner_markdown",
)


def _candidate_compact_retry_prompt(base_prompt: str) -> str:
    return "\n\n".join(
        [
            base_prompt,
            "COMPACT RETRY OUTPUT CONTRACT:",
            (
                "Return exactly one top-level JSON object. The first non-whitespace "
                "character must be { and the last non-whitespace character must be }."
            ),
            (
                "The object must contain exactly these artifact keys: "
                "requirements, design, implementation_plan, planner_markdown."
            ),
            (
                "Do not return a top-level array. Do not return step objects, "
                "task-plan arrays, implementation-plan arrays, or objects with "
                "top-level step/title/description fields."
            ),
            (
                "TASK_START lines are allowed only inside the planner_markdown "
                "string value. They must not appear as top-level array items or "
                "top-level step objects."
            ),
            "Do not include prose outside the JSON object.",
        ]
    )


def _build_current_compact_prompt(db_session) -> str:
    project = Project(
        name="Candidate Prompt Probe",
        description="Small React dashboard",
        project_rules="Keep scope narrow.",
        workspace_path="candidate-prompt-probe",
    )
    session = PlanningSession(
        project=project,
        title="Probe",
        prompt="Create a plan for a settings form with tests.",
        status="active",
        source_brain="local",
    )
    service = PlanningSessionService(db_session)
    service._add_message(
        session,
        "user",
        session.prompt,
        metadata={"kind": "prompt", "skip_clarification": True},
    )
    synthesis_prompt = service._build_synthesis_prompt(session, project)
    return service._build_compact_synthesis_prompt(synthesis_prompt)


def _valid_artifact_output() -> str:
    return json.dumps(
        {
            "requirements": "# Requirements",
            "design": "# Design",
            "implementation_plan": "# Implementation Plan",
            "planner_markdown": "\n".join(
                [
                    "## Task List",
                    "- [ ] TASK_START: Implement | Add focused change | order=1 | P1 | effort=medium | profile=full_lifecycle",
                    "- [ ] TASK_START: Test | Add focused tests | order=2 | P1 | effort=small | profile=test_only",
                    "- [ ] TASK_START: Verify | Run checks | order=3 | P1 | effort=small | profile=test_only",
                ]
            ),
        }
    )


def _step_array_output() -> str:
    return json.dumps(
        [
            {
                "step": 1,
                "title": "Requirements",
                "description": "# Requirements",
            },
            {
                "step": 2,
                "title": "planner_markdown",
                "description": (
                    "## Task List\n"
                    "- [ ] TASK_START: Implement | Add focused change | order=1 | P1 | effort=medium | profile=full_lifecycle"
                ),
            },
        ]
    )


def test_candidate_prompt_contains_required_artifact_keys(db_session):
    candidate = _candidate_compact_retry_prompt(
        _build_current_compact_prompt(db_session)
    )

    for key in REQUIRED_ARTIFACT_KEYS:
        assert key in candidate


def test_candidate_prompt_explicitly_requires_top_level_object(db_session):
    candidate = _candidate_compact_retry_prompt(
        _build_current_compact_prompt(db_session)
    )

    assert "top-level JSON object" in candidate
    assert "first non-whitespace character must be {" in candidate
    assert "last non-whitespace character must be }" in candidate


def test_candidate_prompt_explicitly_forbids_top_level_arrays(db_session):
    candidate = _candidate_compact_retry_prompt(
        _build_current_compact_prompt(db_session)
    )

    assert "Do not return a top-level array" in candidate


def test_candidate_prompt_explicitly_forbids_step_task_plan_top_level_response(
    db_session,
):
    candidate = _candidate_compact_retry_prompt(
        _build_current_compact_prompt(db_session)
    )

    assert "Do not return step objects" in candidate
    assert "task-plan arrays" in candidate
    assert "implementation-plan arrays" in candidate
    assert "top-level step/title/description fields" in candidate


def test_candidate_prompt_scopes_task_start_to_planner_markdown_string(db_session):
    candidate = _candidate_compact_retry_prompt(
        _build_current_compact_prompt(db_session)
    )

    assert (
        "TASK_START lines are allowed only inside the planner_markdown string value"
        in candidate
    )
    assert "must not appear as top-level array items" in candidate
    assert "top-level step objects" in candidate


def test_current_prompt_contains_anti_array_and_step_plan_constraints(db_session):
    current = _build_current_compact_prompt(db_session)

    assert "JSON object" in current
    assert "TASK_START" in current
    assert "Return exactly one top-level JSON object" in current
    assert "first non-whitespace character must be {" in current
    assert "last non-whitespace character must be }" in current
    assert "Do not return a top-level array" in current
    assert "Do not return step objects" in current
    assert "task-plan arrays" in current
    assert "implementation-plan arrays" in current
    assert "top-level step/title/description fields" in current
    assert (
        "TASK_START lines are allowed only inside the planner_markdown string value"
        in current
    )
    assert "Do not include prose outside the JSON object" in current


def test_candidate_prompt_does_not_change_artifact_parser_behavior(db_session):
    service = PlanningSessionService(db_session)
    parsed = service._parse_finalization_payload(
        {"status": "completed", "output": _valid_artifact_output()}
    )

    assert set(parsed) == set(REQUIRED_ARTIFACT_KEYS)
    assert "TASK_START" in parsed["planner_markdown"]


def test_candidate_prompt_does_not_make_step_arrays_acceptable(db_session):
    service = PlanningSessionService(db_session)

    with pytest.raises(
        RuntimeError, match="Planning synthesis returned malformed artifact payload"
    ):
        service._parse_finalization_payload(
            {"status": "completed", "output": _step_array_output()}
        )
