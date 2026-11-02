"""PR20 T3-A1 recognition-only acceptance declarations.

These tests compare the complete acceptance-extraction result with a
SHA-pinned export of committed HEAD. All extracted commands remain data; no
pytest command from a task description is executed here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services.orchestration.state.persistence import _task_acceptance_evidence
from app.services.orchestration.validation import candidate_checks
from app.services.orchestration.validation.candidate_checks import (
    declared_acceptance_forms,
    task_acceptance_pytest_commands,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.planning.planner_service import PlannerService

FIXTURE = Path(__file__).parent / "fixtures" / "pr20_t3a1_head_compatibility_cases.json"
FIXTURE_SHA = FIXTURE.with_suffix(".sha256")

FORM_HEADINGS = (
    ("planner", "PLANNER ACCEPTANCE CRITERIA:"),
    ("markdown", "## Acceptance Criteria"),
    ("bold", "**Acceptance criteria:**"),
    ("bullet", "- Acceptance criteria:"),
    ("no_colon", "Acceptance criteria"),
    ("parenthetical", "Acceptance criteria (must pass):"),
    ("singular", "Acceptance criterion:"),
    ("inline", "Implement this. Acceptance criteria:"),
)


def _extraction_projection(extraction):
    return {
        "section_found": extraction.section_found,
        "extracted": list(extraction.extracted),
        "admitted": [
            {"text": command.text, "pytest_args": list(command.pytest_args)}
            for command in extraction.admitted
        ],
        "refused": [dict(item) for item in extraction.refused],
        "ignored": [dict(item) for item in extraction.ignored],
    }


def _head_fixture():
    expected_sha, _name = FIXTURE_SHA.read_text(encoding="ascii").split()
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == expected_sha
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("form", "heading"), FORM_HEADINGS)
def test_each_declared_form_counts_pytest_spans_without_authority(form, heading):
    prompt = (
        f"{heading}\n"
        "- `pytest tests/test_one.py` passes.\n"
        "- `python -m pytest -q tests/test_two.py` passes.\n"
    )

    assert declared_acceptance_forms(prompt) == (
        {"form": form, "pytest_span_count": 2},
    )

    extraction = task_acceptance_pytest_commands(prompt)
    assert extraction.section_found is False
    assert extraction.extracted == ()
    assert extraction.admitted == ()
    assert extraction.refused == ()


def test_multiple_declarations_aggregate_duplicate_forms_in_stable_order():
    prompt = (
        "Planner acceptance criteria:\n- `pytest tests/test_one.py`\n\n"
        "## Acceptance criteria:\n- `pytest tests/test_two.py`\n\n"
        "Planner Acceptance criteria:\n"
        "- `python -m pytest -q tests/test_three.py`\n"
    )

    assert declared_acceptance_forms(prompt) == (
        {"form": "planner", "pytest_span_count": 2},
        {"form": "markdown", "pytest_span_count": 1},
    )


def test_empty_malformed_and_ordinary_prose_boundaries():
    assert declared_acceptance_forms("**Acceptance criteria:**\n") == (
        {"form": "bold", "pytest_span_count": 0},
    )

    prose_and_malformed = "\n".join(
        (
            "The acceptance criteria are discussed below.",
            "I reviewed the acceptance criteria yesterday.",
            "Fix it. Acceptance criteria are listed in the ticket.",
            "####### Acceptance criteria:",
            "Acceptance criterion (must pass):",
            "Planner acceptance criteria",
            "- `pytest tests/test_ordinary_prose.py`",
        )
    )
    assert declared_acceptance_forms(prose_and_malformed) == ()


def test_request_changes_feedback_boundary_is_not_an_acceptance_declaration():
    feedback = (
        "Operator request-changes feedback (not acceptance criteria):\n"
        "- `pytest tests/test_feedback.py`\n"
    )
    assert declared_acceptance_forms(feedback) == ()

    after_planner_declaration = (
        "Planner acceptance criteria:\n- `pytest tests/test_planner.py`\n"
        "Operator request-changes feedback (not acceptance criteria):\n"
        "- `pytest tests/test_feedback.py`\n"
    )
    assert declared_acceptance_forms(after_planner_declaration) == (
        {"form": "planner", "pytest_span_count": 1},
    )


@pytest.mark.parametrize(
    "prompt",
    (
        "Acceptance criteria:\n- `pytest tests/test_operator.py`\n\n"
        "Planner acceptance criteria:\n- `pytest tests/test_planner.py`\n",
        "Planner acceptance criteria:\n- `pytest tests/test_planner.py`\n\n"
        "Acceptance criteria:\n- `pytest tests/test_operator.py`\n",
    ),
)
def test_mixed_operator_and_planner_forms_preserve_operator_authority(prompt):
    extraction = task_acceptance_pytest_commands(prompt)

    assert extraction.section_found is True
    assert extraction.extracted == ("pytest tests/test_operator.py",)
    assert [item.text for item in extraction.admitted] == [
        "pytest tests/test_operator.py"
    ]
    assert extraction.refused == ()
    assert declared_acceptance_forms(prompt) == (
        {"form": "planner", "pytest_span_count": 1},
    )


def test_real_t3b2_parser_output_is_observed_but_not_authoritative():
    markdown = (
        "## Task List\n"
        "- [ ] TASK_START: Export CSV | Add CSV export | order=1 | P1 | "
        "AC: `pytest tests/test_export.py` passes | AC: header row is present\n"
    )
    parsed = PlannerService.parse_markdown_result(markdown)

    assert parsed.rejection_reason is None
    (task,) = parsed.tasks
    assert task.description == (
        "Add CSV export | order=1 | P1\n\nPlanner acceptance criteria:\n"
        "- `pytest tests/test_export.py` passes\n"
        "- header row is present"
    )
    extraction = task_acceptance_pytest_commands(task.description)
    assert extraction.section_found is False
    assert extraction.extracted == ()
    assert extraction.admitted == ()
    assert extraction.refused == ()
    assert declared_acceptance_forms(task.description) == (
        {"form": "planner", "pytest_span_count": 1},
    )


def test_b06_markdown_heading_overcapture_matches_committed_head():
    fixture = _head_fixture()
    case = next(
        item
        for item in fixture["cases"]
        if item["case_id"] == "B06_md_heading_does_not_close"
    )
    actual = _extraction_projection(
        task_acceptance_pytest_commands(case["descriptions"][0])
    )

    assert actual == case["expected_head_extractions"][0]
    assert actual["section_found"] is True
    assert actual["admitted"][0]["text"] == "pytest tests/test_a.py"
    assert actual["refused"] == [
        {"command": "pytest -k slow", "reason": "unsupported_pytest_option"}
    ]


def test_t1_and_t2_cases_match_complete_head_extraction_outputs():
    fixture = _head_fixture()
    planner_cases = 0
    compared_descriptions = 0

    for case in fixture["cases"]:
        if case["kind"] == "planner":
            planner_cases += 1
            parsed = PlannerService.parse_markdown_result(case["input"])
            assert parsed.rejection_reason == case["parser_rejection_reason"], case[
                "case_id"
            ]
            descriptions = [task.description for task in parsed.tasks]
            assert descriptions == case["descriptions"], case["case_id"]
        else:
            descriptions = case["descriptions"]

        assert len(descriptions) == len(case["expected_head_extractions"]), case[
            "case_id"
        ]
        for description, expected in zip(
            descriptions, case["expected_head_extractions"]
        ):
            actual = _extraction_projection(
                task_acceptance_pytest_commands(description)
            )
            assert actual == expected, case["case_id"]
            compared_descriptions += 1

    assert len(fixture["cases"]) == 53
    assert planner_cases > 0
    assert compared_descriptions > planner_cases


def test_run_task_acceptance_adds_only_raw_declared_forms(tmp_path):
    task_prompt = "Planner acceptance criteria:\n- `pytest tests/test_planner.py`\n"
    findings = []
    commands_run = []

    result = candidate_checks._run_task_acceptance(
        project_dir=tmp_path,
        task_prompt=task_prompt,
        focused_command="",
        focused_returncode=None,
        timeout_seconds=1,
        findings=findings,
        commands_run=commands_run,
    )

    assert result == {
        "section_found": False,
        "extracted": [],
        "admitted": [],
        "refused": [],
        "ignored": [],
        "results": [],
        "declared_forms": [{"form": "planner", "pytest_span_count": 1}],
    }
    assert findings == []
    assert commands_run == []


@pytest.mark.parametrize("severity", ("standard", "high"))
def test_observe_only_forms_leave_standard_and_strict_statuses_unchanged(
    tmp_path, severity
):
    findings = []
    raw = candidate_checks._run_task_acceptance(
        project_dir=tmp_path,
        task_prompt="## Acceptance criteria\n- `pytest tests/test_a.py`\n",
        focused_command="",
        focused_returncode=None,
        timeout_seconds=1,
        findings=findings,
        commands_run=[],
    )
    warnings = [item.message for item in findings if item.severity != "error"]
    repairable = [item.message for item in findings if item.repairable]
    rejected = [
        item.message
        for item in findings
        if item.severity == "error" and not item.repairable
    ]

    assert raw["declared_forms"] == [{"form": "markdown", "pytest_span_count": 1}]
    assert findings == []
    assert (
        ValidatorService._select_status(
            warnings=warnings,
            repairable=repairable,
            rejected=rejected,
            severity=severity,
            stage="task_completion",
        )
        == "accepted"
    )
    assert ValidatorService._select_status(
        warnings=["existing warning"],
        repairable=[],
        rejected=[],
        severity=severity,
        stage="task_completion",
    ) == ("warning" if severity == "standard" else "rejected")


def test_v1_acceptance_evidence_projection_excludes_raw_declared_forms(tmp_path):
    raw = candidate_checks._run_task_acceptance(
        project_dir=tmp_path,
        task_prompt="Planner acceptance criteria:\n- `pytest tests/test_a.py`\n",
        focused_command="",
        focused_returncode=None,
        timeout_seconds=1,
        findings=[],
        commands_run=[],
    )
    projected = _task_acceptance_evidence(
        {
            "stage": "task_completion",
            "status": "accepted",
            "details": {"task_acceptance_verification": raw},
        },
        session_id=1,
        task_id=2,
        task_execution_id=None,
    )

    assert projected is not None
    assert projected["section_found"] is False
    assert projected["admitted_count"] == 0
    assert projected["refused_count"] == 0
    assert "declared_forms" not in projected
