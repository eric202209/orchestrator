"""PR20-RESET-03B-T3-B2-I1: Planner ``| AC:`` decoding into a TAB-1 block.

Planner criteria on list-form TASK_START rows become a final, classification
only ``Planner acceptance criteria:`` block.  They never carry operator
acceptance authority and never drive Task metadata.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app.models import PlanningMessage, PlanningSession, Task
from app.services.orchestration.validation.candidate_checks import (
    task_acceptance_pytest_commands,
)
from app.services.planning.planner_service import PlannerService
from app.services.planning.planning_session_service import PlanningSessionService

TL = "## Task List\n"
ROW = "- [ ] TASK_START: "
BLOCK = "\n\nPlanner acceptance criteria:\n"


def _result(markdown: str):
    return PlannerService.parse_markdown_result(markdown)


def _only(markdown: str):
    result = _result(markdown)
    assert result.rejection_reason is None, result.rejection_reason
    (task,) = result.tasks
    return task


def _metadata(task) -> tuple:
    return (
        task.title,
        task.execution_profile,
        task.workflow_stage,
        task.priority,
        task.plan_position,
        task.estimated_effort,
    )


def _assert_not_authoritative(description: str) -> None:
    extraction = task_acceptance_pytest_commands(description)
    assert extraction.section_found is False
    assert extraction.admitted == ()
    assert extraction.refused == ()


# --------------------------------------------------------------------------
# Decoding and rendering
# --------------------------------------------------------------------------


def test_single_inline_ac_renders_planner_block_after_description():
    task = _only(
        TL
        + ROW
        + "Add retry handling | Retry failed requests | order=1 | P1 | "
        + "AC: Retry failed requests up to 3 times\n"
    )
    assert task.description == (
        "Retry failed requests | order=1 | P1"
        + BLOCK
        + "- Retry failed requests up to 3 times"
    )
    _assert_not_authoritative(task.description)


def test_multiple_inline_criteria_keep_emitted_order():
    task = _only(
        TL
        + ROW
        + "Fix | Fix the bug | AC: GET /items returns 404 | order=1 | "
        + "AC: response shape unchanged\n"
    )
    assert task.description == (
        "Fix the bug | order=1"
        + BLOCK
        + "- GET /items returns 404\n- response shape unchanged"
    )


def test_each_task_receives_only_its_own_criteria():
    first, second = PlannerService.parse_markdown(
        TL
        + ROW
        + "First | one | order=1 | AC: first criterion\n"
        + ROW
        + "Second | two | order=2 | AC: second criterion\n"
    )
    assert first.description == "one | order=1" + BLOCK + "- first criterion"
    assert second.description == "two | order=2" + BLOCK + "- second criterion"


def test_empty_criteria_are_dropped_and_no_empty_block_is_rendered():
    with_empty = _only(TL + ROW + "Fix | Fix the bug | AC: | AC:    | order=1\n")
    without = _only(TL + ROW + "Fix | Fix the bug | order=1\n")
    assert asdict(with_empty) == asdict(without)
    assert "Planner acceptance criteria:" not in with_empty.description

    mixed = _only(TL + ROW + "Fix | Fix the bug | AC: | AC: kept\n")
    assert mixed.description == "Fix the bug" + BLOCK + "- kept"


def test_duplicate_and_conflicting_criteria_are_preserved_in_order():
    task = _only(
        TL
        + ROW
        + "Fix | Fix | AC: same | AC: same | AC: returns 200 | AC: returns 404\n"
    )
    assert task.description == (
        "Fix" + BLOCK + "- same\n- same\n- returns 200\n- returns 404"
    )


def test_backtick_pipes_and_escaped_pipes_decode_only_inside_ac_values():
    task = _only(
        TL
        + ROW
        + r"Fix | Keep a \| b literal | AC: `pytest a \| cat` and x \| y | "
        + "AC: `cmd | other` stays one criterion | order=1\n"
    )
    assert task.description == (
        r"Keep a \| b literal | order=1"
        + BLOCK
        + "- `pytest a | cat` and x | y\n- `cmd | other` stays one criterion"
    )


def test_criterion_whitespace_is_normalized_and_markdown_kept_verbatim():
    task = _only(
        TL
        + ROW
        + "Fix | Fix |  AC:   **bold**\t\tand  `code`   and - dash  | order=1\n"
    )
    assert task.description == (
        "Fix | order=1" + BLOCK + "- **bold** and `code` and - dash"
    )


def test_ac_only_row_uses_existing_description_fallback():
    task = _only(TL + ROW + "Fix Parser | AC: parser accepts empty input\n")
    assert task.description == (
        "Implement fix parser" + BLOCK + "- parser accepts empty input"
    )


def test_title_colon_delimited_ac_description_is_decoded():
    task = _only(TL + ROW + "Fix: AC: decoded\n")
    assert task.description == "Implement fix" + BLOCK + "- decoded"


# --------------------------------------------------------------------------
# Metadata isolation (U-3)
# --------------------------------------------------------------------------


def test_r0_profile_loss_explicit_profile_after_ac_is_retained():
    row = "Implement export | Build CSV export | order=1 | P1 | effort=medium"
    plain = _only(TL + ROW + row + " | profile=full_lifecycle\n")
    decoded = _only(
        TL
        + ROW
        + row
        + " | profile=full_lifecycle | AC: `pytest tests/test_export.py` passes\n"
    )
    assert plain.execution_profile == "full_lifecycle"
    assert _metadata(decoded) == _metadata(plain)
    assert decoded.description == (
        plain.description + BLOCK + "- `pytest tests/test_export.py` passes"
    )


@pytest.mark.parametrize(
    "scope", ["profile=test_only", "stage=review", "profile=review_only | stage=plan"]
)
def test_explicit_scope_fields_survive_ac_fields_in_any_position(scope):
    plain = _only(TL + ROW + f"Fix | Fix the bug | order=2 | {scope}\n")
    after = _only(TL + ROW + f"Fix | Fix the bug | order=2 | {scope} | AC: done\n")
    before = _only(TL + ROW + f"Fix | Fix the bug | AC: done | order=2 | {scope}\n")
    assert _metadata(after) == _metadata(plain) == _metadata(before)


def test_r0_inference_leak_ac_text_does_not_set_position_or_priority():
    plain = _only(TL + ROW + "Fix | Fix the bug\n")
    leaked = _only(TL + ROW + "Fix | Fix the bug | AC: step 7 completes; P0 critical\n")
    assert _metadata(leaked) == _metadata(plain)
    assert (leaked.plan_position, leaked.priority) == (1, plain.priority)
    assert leaked.priority != 100


@pytest.mark.parametrize(
    "criterion",
    [
        "step 7 completes",
        "order=9 holds",
        "task 4 is done",
        "phase 3 rollout",
        "P0 urgent critical blocker",
        "profile=test_only",
        "stage=review",
        "mode=debug_only",
        "run tests, review, audit and debug",
        "large refactor across many modules",
        "1. numbered criterion",
    ],
)
def test_no_ac_text_drives_any_metadata(criterion):
    plain = _only(TL + ROW + "Document parser | Write docs | order=2\n")
    decoded = _only(
        TL + ROW + f"Document parser | Write docs | order=2 | AC: {criterion}\n"
    )
    assert _metadata(decoded) == _metadata(plain)


def test_position_comes_from_remaining_text_and_r7_duplicates_still_reject():
    tasks = PlannerService.parse_markdown(
        TL + ROW + "First | one | AC: order=2\n" + ROW + "Second | two | AC: step 1\n"
    )
    assert [task.plan_position for task in tasks] == [1, 2]

    duplicate = _result(
        TL + ROW + "First | one | order=1 | AC: a\n" + ROW + "Second | two | order=1\n"
    )
    assert (duplicate.mode, duplicate.rejection_reason, duplicate.tasks) == (
        "rejected",
        "invalid_task_order",
        [],
    )
    assert (
        PlannerService.parse_markdown(
            TL + ROW + "A | a | order=1 | AC: x\n" + ROW + "B | b | order=1\n"
        )
        == []
    )


def test_invalid_scope_metadata_still_rejects_with_ac_fields():
    result = _result(TL + ROW + "Fix | Fix | profile=bogus | AC: anything\n")
    assert (result.mode, result.rejection_reason, result.tasks) == (
        "rejected",
        "invalid_scope_metadata",
        [],
    )


# --------------------------------------------------------------------------
# Malformed backticks (U-2) and unsupported syntax (U-4, U-6)
# --------------------------------------------------------------------------


def test_m19_unbalanced_backticks_keep_existing_behavior_undecoded():
    markdown = (
        TL
        + ROW
        + "Broken | Do it | AC: `pytest tests/test_a.py passes | profile=test_only\n"
    )
    with_ac = _only(markdown)
    assert with_ac.description == "Do it | AC: `pytest tests/test_a.py passes"
    assert with_ac.execution_profile == "test_only"
    _assert_not_authoritative(with_ac.description)
    # No partial decoding: even a later well-formed AC field stays raw.
    assert PlannerService._split_acceptance_fields("Do it | AC: `pytest a | AC: b") == (
        "Do it | AC: `pytest a | AC: b",
        [],
    )


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        pytest.param(
            TL + "### TASK_START: Fix | Fix the bug | AC: foo passes\n",
            [("Fix | Fix the bug | AC", "foo passes")],
            id="heading-form-task-start",
        ),
        pytest.param(
            TL + "- Fix | Fix the bug | AC: foo passes\n",
            [("Fix", "Fix the bug | AC: foo passes")],
            id="m3-legacy-row",
        ),
        pytest.param(
            TL + ROW + "Fix | Fix the bug | ac: lower | order=1\n",
            [("Fix", "Fix the bug | ac: lower | order=1")],
            id="lowercase-ac",
        ),
        pytest.param(
            TL + ROW + "Fix | Fix the bug | Ac: mixed | AC : spaced | xAC: glued\n",
            [("Fix", "Fix the bug | Ac: mixed | AC : spaced | xAC: glued")],
            id="non-canonical-ac-prefixes",
        ),
        pytest.param(
            TL + ROW + "Fix | Mention AC: inline in prose\n",
            [("Fix", "Mention AC: inline in prose")],
            id="mid-field-ac",
        ),
        pytest.param(
            TL
            + "### Fix normalize\nNormalize input. | AC: not a field here\n\n"
            + "Planner acceptance criteria:\n- `pytest tests/test_n.py`\n",
            [
                (
                    "Fix normalize",
                    "Normalize input. | AC: not a field here\n\n"
                    "Planner acceptance criteria:\n- `pytest tests/test_n.py`",
                )
            ],
            id="m2-body-with-existing-planner-block",
        ),
    ],
)
def test_unsupported_forms_are_not_decoded(markdown, expected):
    tasks = PlannerService.parse_markdown(markdown)
    assert [(task.title, task.description) for task in tasks] == expected


def test_mixed_list_and_heading_task_start_still_rejects_with_ac():
    result = _result(
        TL + ROW + "List | a | AC: x\n" + "### TASK_START: Heading | b | AC: y\n"
    )
    assert (result.mode, result.rejection_reason, result.tasks) == (
        "rejected",
        "mixed_task_syntax",
        [],
    )


# --------------------------------------------------------------------------
# Rows without AC are unchanged on every field (differential against the
# decoder disabled, which is the pre-T3-B2 code path).
# --------------------------------------------------------------------------


def _recovery_markdown() -> str:
    service = object.__new__(PlanningSessionService)
    artifacts = service._build_replan_recovery_artifacts(
        SimpleNamespace(
            prompt="Replan\n- Broken test: AssertionError: expected `value` | AC: x"
        ),
        SimpleNamespace(name="Recovery | Project"),
    )
    return artifacts["planner_markdown"]


NO_AC_MARKDOWNS = [
    TL
    + ROW
    + "Fix | Fix the bug | order=1 | P1 | effort=small | profile=full_lifecycle\n",
    TL + ROW + "Fix | Keep `a|b` and a \\| b | stage=review\n",
    TL + ROW + "Fix | Acceptance criteria: `pytest tests/test_a.py`\n",
    TL + ROW + "Only title\n" + ROW + "Second | step 3 | P0\n",
    TL + "  - [ ] TASK_START: Indented | row | order=4\n",
    TL + "### Heading task\nBody line | with pipe\n",
    TL + "- Legacy | row | profile=test_only\n",
    PlannerService.generate_markdown("Build login and reporting", "Project"),
]


@pytest.mark.parametrize("markdown", NO_AC_MARKDOWNS + ["RECOVERY"])
def test_rows_without_ac_are_identical_to_decoder_disabled(markdown, monkeypatch):
    if markdown == "RECOVERY":
        markdown = _recovery_markdown()
    enabled = _result(markdown)
    monkeypatch.setattr(
        PlannerService, "_split_acceptance_fields", staticmethod(lambda raw: (raw, []))
    )
    disabled = _result(markdown)
    assert enabled.mode == disabled.mode
    assert enabled.rejection_reason == disabled.rejection_reason
    assert [asdict(t) for t in enabled.tasks] == [asdict(t) for t in disabled.tasks]
    assert all(
        "Planner acceptance criteria:" not in t.description for t in enabled.tasks
    )


def test_recovery_rows_are_unchanged_literals():
    tasks = PlannerService.parse_markdown(_recovery_markdown())
    assert [
        (t.title, t.execution_profile, t.workflow_stage, t.plan_position) for t in tasks
    ] == [
        ("Diagnose recovered failure", "review_only", "diagnose", 1),
        ("Plan bounded recovery approach", "review_only", "plan", 2),
        ("Apply targeted recovery fix", "debug_only", "debug", 3),
        ("Validate recovery path", "test_only", "validate", 4),
        ("Review recovery outcome", "review_only", "complete", 5),
    ]
    assert all("\n" not in t.description for t in tasks)


# --------------------------------------------------------------------------
# Determinism and idempotence
# --------------------------------------------------------------------------


def test_parse_is_deterministic_and_neutralizer_stays_idempotent():
    markdown = (
        TL
        + ROW
        + "Fix | body | AC: Acceptance criteria: `pytest tests/test_a.py` | AC: two\n"
    )
    first = [asdict(t) for t in PlannerService.parse_markdown(markdown)]
    second = [asdict(t) for t in PlannerService.parse_markdown(markdown)]
    assert first == second
    description = first[0]["description"]
    assert PlannerService._neutralize_acceptance_headings(description) == description


def test_decoder_is_a_no_op_on_rendered_output():
    task = _only(TL + ROW + "Fix | body | order=1 | AC: one | AC: `two`\n")
    assert PlannerService._split_acceptance_fields(task.description) == (
        task.description,
        [],
    )


# --------------------------------------------------------------------------
# Acceptance authority with the real extractor
# --------------------------------------------------------------------------


AUTHORITY_ROWS = [
    "Fix | body | AC: Retry up to 3 times",
    "Fix | body | AC: `pytest tests/test_a.py` passes",
    "Fix | body | AC: `python -m pytest tests/test_a.py -q`",
    "Fix | body | AC: `pytest tests/a.py && true`",
    "Fix | body | AC: Acceptance criteria: `pytest tests/test_a.py`",
    "Fix | body | AC: ACCEPTANCE CRITERIA: `pytest tests/test_a.py`",
    "Fix | body | AC: acceptance criteria:`pytest tests/test_a.py`",
    "Fix | body | AC: Acceptance criteria: `pytest tests/test_a.py`",
    "Fix | body | AC: \tAcceptance criteria: `pytest tests/test_a.py`",
    "Fix | body | AC: ### Acceptance criteria: `pytest tests/test_a.py`",
    "Fix | body | AC: **Acceptance criteria:** `pytest tests/test_a.py`",
    "Fix | body | AC: ```pytest tests/test_a.py```",
    "Fix | body | AC: Planner acceptance criteria: `pytest tests/test_a.py`",
    "Fix | AC: x | Acceptance criteria: `pytest tests/test_a.py`",
    "Fix | AC: `pytest tests/test_a.py` | AC: Acceptance criteria:",
    "Fix | body | AC: ok\u0085Acceptance criteria: `pytest tests/test_a.py`",
]


@pytest.mark.parametrize("row", AUTHORITY_ROWS)
def test_planner_only_descriptions_are_never_operator_authority(row):
    tasks = PlannerService.parse_markdown(TL + ROW + row + "\n")
    assert tasks
    for task in tasks:
        _assert_not_authoritative(task.description)


def test_ac_removal_cannot_expose_an_operator_heading_at_line_start():
    task = _only(
        TL + ROW + "Fix | AC: x | Acceptance criteria: `pytest tests/test_a.py`\n"
    )
    assert task.description == (
        "Planner Acceptance criteria: `pytest tests/test_a.py`" + BLOCK + "- x"
    )


def test_operator_block_with_planner_block_admits_only_operator_commands():
    planner = _only(
        TL + ROW + "Fix | body | AC: `pytest tests/test_planner.py` passes\n"
    ).description
    for description in (
        "Operator body\n\nAcceptance criteria:\n- `pytest tests/test_operator.py`"
        + "\n\n"
        + planner,
        planner + "\n\nAcceptance criteria:\n- `pytest tests/test_operator.py`",
    ):
        extraction = task_acceptance_pytest_commands(description)
        assert extraction.section_found is True
        assert [c.text for c in extraction.admitted] == [
            "pytest tests/test_operator.py"
        ]
        assert extraction.refused == ()


# --------------------------------------------------------------------------
# Synthesis instruction (U-5)
# --------------------------------------------------------------------------


AC_INSTRUCTION = (
    "On each TASK_START line, add one | AC: <criterion> field per acceptance "
    "criterion of that task, put test commands in single backticks, and write "
    "a literal | in prose as \\|."
)


def _synthesis_prompt(long: bool) -> str:
    service = PlanningSessionService(db=None)  # type: ignore[arg-type]
    session = PlanningSession(
        id=1,
        project_id=1,
        title="Planning session",
        prompt="Build an execution-ready plan for a dashboard rewrite with auth.",
        status="active",
        source_brain="local",
    )
    count = 8 if long else 1
    session.messages = [
        PlanningMessage(
            role="user" if index % 2 == 0 else "assistant",
            content=(("very long planning detail " * 40) if long else "detail")
            + f" message {index}",
        )
        for index in range(count)
    ]
    project = SimpleNamespace(
        name="Project",
        description=("existing project " * 80) if long else "small",
        project_rules=None,
    )
    return service._build_synthesis_prompt(session, project)


@pytest.mark.parametrize("long", [False, True])
def test_synthesis_prompt_carries_ac_instruction_within_budget(long):
    prompt = _synthesis_prompt(long)
    assert AC_INSTRUCTION in prompt
    assert len(prompt) <= PlanningSessionService.SYNTHESIS_PROMPT_CHAR_BUDGET
    compact = PlanningSessionService(db=None)._build_compact_synthesis_prompt(prompt)  # type: ignore[arg-type]
    assert "COMPACT RETRY OUTPUT CONTRACT:" in compact
    assert "TASK_START: Title | Description" in compact


def test_instructed_row_shape_decodes_as_specified():
    task = _only(
        TL
        + "- [ ] TASK_START: Title | Description | order=1 | P1 | effort=medium | "
        + "profile=full_lifecycle | AC: `pytest tests/test_x.py` passes | "
        + "AC: prose with a literal \\| pipe\n"
    )
    assert task.execution_profile == "full_lifecycle"
    assert task.plan_position == 1
    assert task.description == (
        "Description | order=1 | P1 | effort=medium"
        + BLOCK
        + "- `pytest tests/test_x.py` passes\n- prose with a literal | pipe"
    )


# --------------------------------------------------------------------------
# Persistence and parser-bypass paths (SQLite fixtures)
# --------------------------------------------------------------------------


PLANNER_MARKDOWN = "\n".join(
    [
        "# Project: Planner AC",
        "",
        "## Task List",
        "- [ ] TASK_START: First task | Build it | order=1 | P1 | "
        "profile=full_lifecycle | AC: `pytest tests/test_first.py` passes | "
        "AC: step 7 completes",
        "- [ ] TASK_START: Second task | Verify it | order=2 | P1",
    ]
)
FIRST_DESCRIPTION = (
    "Build it | order=1 | P1"
    + BLOCK
    + "- `pytest tests/test_first.py` passes\n- step 7 completes"
)


def _create_project(client, name: str) -> dict:
    response = client.post(
        "/api/v1/projects", json={"name": name, "description": "T3-B2 tests"}
    )
    assert response.status_code == 201
    return response.json()


def _complete_session(client, project: dict, monkeypatch) -> dict:
    payload = {
        "requirements": "# Requirements\n\n- Keep criteria",
        "design": "# Design\n\n- Parser boundary",
        "implementation_plan": "# Implementation Plan\n\n1. Decode",
        "planner_markdown": PLANNER_MARKDOWN,
    }
    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt, *args, **kwargs: {
            "status": "completed",
            "output": json.dumps(payload),
        },
    )
    response = client.post(
        "/api/v1/planning/sessions",
        json={
            "project_id": project["id"],
            "prompt": "Plan with planner acceptance criteria",
            "skip_clarification": True,
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "completed"
    return response.json()


def test_session_preview_and_commit_persist_the_same_planner_block(
    authenticated_client, db_session, monkeypatch
):
    project = _create_project(authenticated_client, "Planner AC session")
    session = _complete_session(authenticated_client, project, monkeypatch)

    preview = authenticated_client.get(f"/api/v1/planning/sessions/{session['id']}")
    assert preview.status_code == 200
    previews = preview.json()["tasks_preview"]
    assert [p["description"] for p in previews] == [
        FIRST_DESCRIPTION,
        "Verify it | order=2 | P1",
    ]
    assert previews[0]["execution_profile"] == "full_lifecycle"

    committed = authenticated_client.post(
        f"/api/v1/planning/sessions/{session['id']}/commit", json={}
    )
    assert committed.status_code == 200
    assert [t["description"] for t in committed.json()["tasks"]] == [
        p["description"] for p in previews
    ]

    db_session.expire_all()
    rows = (
        db_session.query(Task)
        .filter(Task.project_id == project["id"])
        .order_by(Task.plan_position)
        .all()
    )
    assert [(t.description, t.plan_position, t.execution_profile) for t in rows] == [
        (p["description"], p["plan_position"], p["execution_profile"]) for p in previews
    ]
    assert [(t.plan_position, t.execution_profile) for t in rows][0] == (
        1,
        "full_lifecycle",
    )
    for row in rows:
        _assert_not_authoritative(row.description)


def test_session_commit_with_selected_tasks_keeps_supplied_descriptions(
    authenticated_client, db_session, monkeypatch
):
    project = _create_project(authenticated_client, "Planner AC selected")
    session = _complete_session(authenticated_client, project, monkeypatch)
    supplied = "Client text | AC: stays raw\n\nPlanner acceptance criteria:\n- kept"

    response = authenticated_client.post(
        f"/api/v1/planning/sessions/{session['id']}/commit",
        json={
            "selected_tasks": [
                {"title": "Selected", "description": supplied, "plan_position": 1}
            ]
        },
    )
    assert response.status_code == 200
    db_session.expire_all()
    (task,) = db_session.query(Task).filter(Task.project_id == project["id"]).all()
    assert task.description == supplied


def test_batch_commit_keeps_client_descriptions_verbatim(
    authenticated_client, db_session
):
    project = _create_project(authenticated_client, "Planner AC batch")
    descriptions = [
        "Raw | AC: not decoded on the batch path",
        "Body" + BLOCK + "- already rendered",
    ]
    response = authenticated_client.post(
        f"/api/v1/projects/{project['id']}/batch-tasks",
        json={
            "markdown": "# Batch",
            "tasks": [
                {"title": f"Task {i}", "description": d, "plan_position": i}
                for i, d in enumerate(descriptions, start=1)
            ],
        },
    )
    assert response.status_code == 201
    db_session.expire_all()
    rows = (
        db_session.query(Task)
        .filter(Task.project_id == project["id"])
        .order_by(Task.plan_position)
        .all()
    )
    assert [(t.description, t.plan_position) for t in rows] == [
        (descriptions[0], 1),
        (descriptions[1], 2),
    ]


def test_manual_task_post_is_unaffected(authenticated_client, db_session):
    project = _create_project(authenticated_client, "Planner AC manual")
    description = "Manual | AC: raw text"
    response = authenticated_client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Manual",
            "description": description,
            "plan_position": 3,
        },
    )
    assert response.status_code in (200, 201)
    db_session.expire_all()
    (task,) = db_session.query(Task).filter(Task.project_id == project["id"]).all()
    assert (task.description, task.plan_position) == (description, 3)


def test_parser_rejection_persists_nothing(
    authenticated_client, db_session, monkeypatch
):
    project = _create_project(authenticated_client, "Planner AC rejected")
    session = _complete_session(authenticated_client, project, monkeypatch)
    response = authenticated_client.post(
        f"/api/v1/planning/sessions/{session['id']}/commit",
        json={
            "planner_markdown": TL
            + ROW
            + "A | a | order=1 | AC: x\n"
            + ROW
            + "B | b | order=1 | AC: y\n"
        },
    )
    assert response.status_code == 422
    db_session.expire_all()
    assert db_session.query(Task).filter(Task.project_id == project["id"]).count() == 0
