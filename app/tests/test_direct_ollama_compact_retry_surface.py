from __future__ import annotations

import json

import pytest

from app.models import PlanningArtifact, PlanningSession, Project
from app.services.planning.planning_session_service import PlanningSessionService


def _artifact_payload() -> dict[str, str]:
    return {
        "requirements": "# Requirements\n\n- Build the requested feature.",
        "design": "# Design\n\nUse a narrow implementation surface.",
        "implementation_plan": "# Implementation Plan\n\n1. Implement\n2. Test",
        "planner_markdown": "\n".join(
            [
                "## Task List",
                "- [ ] TASK_START: Implement feature | Add the focused change | order=1 | P1 | effort=medium | profile=full_lifecycle",
                "- [ ] TASK_START: Add tests | Cover expected behavior | order=2 | P1 | effort=medium | profile=test_only",
                "- [ ] TASK_START: Verify | Run focused checks | order=3 | P1 | effort=small | profile=test_only",
            ]
        ),
    }


def _step_array_payload() -> list[dict[str, str | int]]:
    return [
        {
            "step": 1,
            "title": "Requirements",
            "description": "# Requirements\n\n- Build the requested feature.",
        },
        {
            "step": 2,
            "title": "planner_markdown",
            "description": (
                "## Task List\n"
                "- [ ] TASK_START: Implement feature | Add the focused change | order=1 | P1 | effort=medium | profile=full_lifecycle"
            ),
        },
    ]


def _create_active_skip_session(
    db_session,
    *,
    name: str = "Compact Retry Surface Project",
    prompt: str = "Plan a compact feature with tests.",
) -> tuple[PlanningSessionService, PlanningSession]:
    project = Project(
        name=name,
        description="Small planning synthesis characterization project.",
        workspace_path="compact-retry-surface-project",
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = PlanningSession(
        project_id=project.id,
        title="Compact retry surface",
        prompt=prompt,
        status="active",
        source_brain="local",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    service = PlanningSessionService(db_session)
    service._add_message(
        session,
        "user",
        session.prompt,
        metadata={"kind": "prompt", "skip_clarification": True},
    )
    db_session.commit()
    return service, session


def _diagnostic_payload(db_session, session_id: int) -> dict:
    diagnostic = (
        db_session.query(PlanningArtifact)
        .filter(
            PlanningArtifact.planning_session_id == session_id,
            PlanningArtifact.artifact_type
            == "planning_synthesis_parse_failure_diagnostic",
        )
        .one()
    )
    return json.loads(diagnostic.content)


def test_compact_retry_valid_artifact_object_parses_successfully(db_session):
    service = PlanningSessionService(db_session)

    parsed = service._parse_finalization_payload(
        {
            "status": "completed",
            "output": json.dumps(_artifact_payload()),
            "backend": "direct_ollama",
            "model_family": "qwen3:8b-hybrid",
        }
    )

    assert parsed["requirements"].startswith("# Requirements")
    assert parsed["design"].startswith("# Design")
    assert parsed["implementation_plan"].startswith("# Implementation Plan")
    assert "TASK_START" in parsed["planner_markdown"]


def test_compact_retry_valid_json_array_of_steps_is_rejected(db_session):
    service = PlanningSessionService(db_session)

    with pytest.raises(
        RuntimeError, match="Planning synthesis returned malformed artifact payload"
    ):
        service._parse_finalization_payload(
            {
                "status": "completed",
                "output": json.dumps(_step_array_payload()),
                "backend": "direct_ollama",
                "model_family": "qwen3:8b-hybrid",
            }
        )


def test_compact_retry_malformed_json_array_of_steps_is_rejected(db_session):
    service = PlanningSessionService(db_session)
    malformed_array = (
        '[{"step": 1, "title": "Requirements", '
        '"description" "missing colon before value"}]'
    )

    with pytest.raises(json.JSONDecodeError, match="Expecting ':' delimiter"):
        service._parse_finalization_payload(
            {
                "status": "completed",
                "output": malformed_array,
                "backend": "direct_ollama",
                "model_family": "qwen3:8b-hybrid",
            }
        )


def test_terminal_compact_retry_wrong_shape_writes_diagnostic_artifact(
    db_session, monkeypatch
):
    service, session = _create_active_skip_session(db_session)
    compact_output = json.dumps(_step_array_payload())
    outputs = [
        '{"requirements": "# Requirements", "design" "missing colon"}',
        compact_output,
    ]

    def fake_run_openclaw(
        self,
        prompt,
        *,
        source_brain="local",
        timeout_seconds=None,
    ):
        return {
            "status": "completed",
            "output": outputs.pop(0),
            "backend": "direct_ollama",
            "model_family": "qwen3:8b-hybrid",
        }

    monkeypatch.setattr(PlanningSessionService, "_run_openclaw", fake_run_openclaw)

    updated = service.process_session(session.id)

    assert updated is not None
    assert updated.status == "failed"
    assert (
        updated.last_error == "Planning synthesis returned malformed artifact payload"
    )
    payload = _diagnostic_payload(db_session, updated.id)
    assert payload["attempt"] == "compact_retry"
    assert payload["backend"] == "direct_ollama"
    assert payload["model_family"] == "qwen3:8b-hybrid"
    assert payload["classification"] == "malformed_artifact_payload"
    assert payload["response_chars"] == len(compact_output)
    assert payload["cleaned_chars"] == len(compact_output)
    assert len(payload["raw_sha256"]) == 64
    assert "json_error_line" not in payload
    assert payload["raw_excerpt_head"].lstrip().startswith("[")


def test_terminal_compact_retry_malformed_step_array_records_parse_location(
    db_session, monkeypatch
):
    service, session = _create_active_skip_session(
        db_session,
        name="Malformed Compact Retry Surface Project",
    )
    compact_output = (
        "[\n"
        '  {"step": 1, "title": "Requirements"},\n'
        '  {"step": 2, "title" "missing colon"}\n'
        "]"
    )
    outputs = [
        json.dumps(_step_array_payload()),
        compact_output,
    ]

    def fake_run_openclaw(
        self,
        prompt,
        *,
        source_brain="local",
        timeout_seconds=None,
    ):
        return {
            "status": "completed",
            "output": outputs.pop(0),
            "backend": "direct_ollama",
            "model_family": "qwen3:8b-hybrid",
        }

    monkeypatch.setattr(PlanningSessionService, "_run_openclaw", fake_run_openclaw)

    updated = service.process_session(session.id)

    assert updated is not None
    assert updated.status == "failed"
    assert "Expecting ':' delimiter" in (updated.last_error or "")
    payload = _diagnostic_payload(db_session, updated.id)
    assert payload["attempt"] == "compact_retry"
    assert payload["classification"] == "malformed_json_syntax"
    assert payload["json_error_message"] == "Expecting ':' delimiter"
    assert payload["json_error_line"] == 3
    assert payload["json_error_column"] > 0
    assert payload["json_error_position"] > 0
    assert payload["response_chars"] == len(compact_output)
    assert len(payload["raw_sha256"]) == 64


def test_compact_retry_prompt_contains_artifact_keys_and_array_prohibition(db_session):
    project = Project(
        name="Compact Prompt Probe",
        description="React dashboard",
        project_rules="Keep scope narrow.",
        workspace_path="compact-prompt-probe",
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

    prompt = service._build_synthesis_prompt(session, project)
    compact = service._build_compact_synthesis_prompt(prompt)

    for key in ("requirements", "design", "implementation_plan", "planner_markdown"):
        assert key in compact
    assert "JSON object" in compact
    assert "TASK_START" in compact
    assert "Return exactly one top-level JSON object" in compact
    assert "first non-whitespace character must be {" in compact
    assert "last non-whitespace character must be }" in compact
    assert "Do not return a top-level array" in compact
    assert "Do not return step objects" in compact
    assert "task-plan arrays" in compact
    assert "implementation-plan arrays" in compact
    assert "top-level step/title/description fields" in compact
    assert (
        "TASK_START lines are allowed only inside the planner_markdown string value"
        in compact
    )
    assert "Do not include prose outside the JSON object" in compact
