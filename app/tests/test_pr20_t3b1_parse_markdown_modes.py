"""PR20-RESET-03B-T3-B1: planner markdown parser modes and authority boundary.

Every pytest string below is data.  The real acceptance extractor only
classifies text; nothing here runs a command or a subprocess.
"""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app.services.orchestration.validation import candidate_checks
from app.services.orchestration.validation.candidate_checks import (
    task_acceptance_pytest_commands,
)
from app.services.planning import planner_service
from app.services.planning.planner_service import PlannerService
from app.services.planning.planning_session_service import PlanningSessionService

TL = "## Task List\n"


def _result(markdown):
    return PlannerService.parse_markdown_result(markdown)


def _tasks(markdown):
    return [asdict(task) for task in PlannerService.parse_markdown(markdown)]


def _authority(tasks):
    """Union of what the real extractor finds across the parsed descriptions."""

    extractions = [task_acceptance_pytest_commands(t["description"]) for t in tasks]
    return {
        "sections": sum(e.section_found for e in extractions),
        "admitted": sorted({c.text for e in extractions for c in e.admitted}),
        "refused": sorted({r["command"] for e in extractions for r in e.refused}),
    }


NO_AUTHORITY = {"sections": 0, "admitted": [], "refused": []}

# --- M1 TASK_START ----------------------------------------------------------


def test_m1_task_start_exact_fields_match_legacy_output():
    markdown = (
        TL + "- [ ] TASK_START: Fix normalize | Fix the bug | order=1 | P1 | "
        "effort=small | profile=full_lifecycle\n"
    )
    result = _result(markdown)
    assert result.mode == "task_start"
    assert result.rejection_reason is None
    assert _tasks(markdown) == [
        {
            "title": "Fix normalize",
            "description": "Fix the bug | order=1 | P1 | effort=small",
            "execution_profile": "full_lifecycle",
            "workflow_stage": "debug",
            "priority": 90,
            "plan_position": 1,
            "estimated_effort": "medium",
        }
    ]


def test_m1_duplicate_titles_keep_first():
    markdown = (
        TL + "- [ ] TASK_START: Same | first\n- [ ] TASK_START: same | second\n"
        "- [ ] TASK_START: Other | third\n"
    )
    assert _tasks(markdown) == [
        {
            "title": "Same",
            "description": "first",
            "execution_profile": "full_lifecycle",
            "workflow_stage": None,
            "priority": 80,
            "plan_position": 1,
            "estimated_effort": "medium",
        },
        {
            "title": "Other",
            "description": "third",
            "execution_profile": "full_lifecycle",
            "workflow_stage": None,
            "priority": 60,
            "plan_position": 3,
            "estimated_effort": "medium",
        },
    ]


def test_m1_ignores_prose_and_nested_bullets():
    markdown = (
        TL + "- [ ] TASK_START: Real | yes | order=1\n- note: remember the docs\n"
        "  - nested detail\n"
    )
    result = _result(markdown)
    assert result.mode == "task_start"
    assert [t.title for t in result.tasks] == ["Real"]


def test_m1_ignores_fenced_task_start_rows():
    markdown = (
        TL + "- [ ] TASK_START: Real | yes | order=1\n```\n"
        "- [ ] TASK_START: Example | inside fence\n```\n"
    )
    assert [t["title"] for t in _tasks(markdown)] == ["Real"]


def test_m1_stray_trailing_fence_is_not_rejected():
    markdown = TL + "- [ ] TASK_START: Real | yes | order=1\n```\n"
    result = _result(markdown)
    assert result.mode == "task_start"
    assert [t.title for t in result.tasks] == ["Real"]


# --- M2 heading tasks -------------------------------------------------------


def test_m2_single_heading_task_keeps_multiline_body():
    markdown = TL + "### Build API\nCreate the endpoints\nand tests.\n"
    result = _result(markdown)
    assert result.mode == "heading"
    assert [(t.title, t.description) for t in result.tasks] == [
        ("Build API", "Create the endpoints\nand tests.")
    ]


def test_m2_multiple_heading_tasks():
    markdown = TL + "### Build API\nCreate endpoints.\n\n### Build UI\nCreate pages.\n"
    assert [(t["title"], t["plan_position"]) for t in _tasks(markdown)] == [
        ("Build API", 1),
        ("Build UI", 2),
    ]


def test_m2_acceptance_bullets_stay_in_one_task_m09():
    markdown = (
        TL + "### Fix normalize\nFix the bug.\n\n#### Acceptance Criteria\n"
        "- `pytest tests/test_a.py` passes.\n- Do not modify tests/.\n"
    )
    tasks = _tasks(markdown)
    assert [t["title"] for t in tasks] == ["Fix normalize"]
    assert tasks[0]["description"] == (
        "Fix the bug.\n\n#### Acceptance Criteria\n"
        "- `pytest tests/test_a.py` passes.\n- Do not modify tests/."
    )


def test_m2_nested_headings_bullets_and_fences_m11():
    markdown = (
        TL + "### API task\nChange the API.\n```\n### not a task\n- not a task\n```\n"
        "- detail bullet\n  - nested bullet\nAcceptance criteria:\n"
        "- `pytest tests/test_api.py` passes\n  - continuation\n"
        "#### Notes\nsub heading body\n### UI task\nChange UI.\n"
    )
    tasks = _tasks(markdown)
    assert [t["title"] for t in tasks] == ["API task", "UI task"]
    body = tasks[0]["description"]
    for fragment in (
        "```\n### not a task\n- not a task\n```",
        "- detail bullet\n  - nested bullet",
        "  - continuation",
        "#### Notes\nsub heading body",
    ):
        assert fragment in body


def test_m2_shallowest_heading_level_selects_tasks():
    markdown = TL + "#### Deep A\nbody a\n##### Deeper\nx\n#### Deep B\nbody b\n"
    result = _result(markdown)
    assert result.mode == "heading"
    assert [t.title for t in result.tasks] == ["Deep A", "Deep B"]
    assert "##### Deeper\nx" in result.tasks[0].description


def test_m2_acceptance_heading_never_becomes_task():
    markdown = TL + "### Acceptance Criteria\n- `pytest a` passes\n### Real\nbody\n"
    assert [t["title"] for t in _tasks(markdown)] == ["Real"]


def test_m2_empty_body_uses_default_description():
    markdown = TL + "### Build API\n### Build UI\n"
    assert [t["description"] for t in _tasks(markdown)] == [
        "Implement build api",
        "Implement build ui",
    ]


def test_m2_duplicate_headings_keep_first():
    markdown = TL + "### Build API\nfirst\n### build api\nsecond\n"
    assert [(t["title"], t["description"]) for t in _tasks(markdown)] == [
        ("Build API", "first")
    ]


def test_m2_body_with_pipes_backticks_and_pytest_text_is_data():
    markdown = (
        TL + "### Wire CLI\nSupport `a | b` piping.\n"
        "Run `pytest tests/test_cli.py -q` locally | optional.\n"
    )
    tasks = _tasks(markdown)
    assert [t["title"] for t in tasks] == ["Wire CLI"]
    assert tasks[0]["description"] == (
        "Support `a | b` piping.\n"
        "Run `pytest tests/test_cli.py -q` locally | optional."
    )
    assert _authority(tasks) == NO_AUTHORITY


# --- M3 legacy ----------------------------------------------------------------


def test_m3_legacy_bullets_exact_fields():
    markdown = (
        TL + "- Add login | Build the login form\n- Add logout: Clear the session\n"
        "* Star bullet | star form\n"
    )
    assert _result(markdown).mode == "legacy"
    assert _tasks(markdown) == [
        {
            "title": "Add login",
            "description": "Build the login form",
            "execution_profile": "execute_only",
            "workflow_stage": "execute",
            "priority": 60,
            "plan_position": 1,
            "estimated_effort": "medium",
        },
        {
            "title": "Add logout",
            "description": "Clear the session",
            "execution_profile": "execute_only",
            "workflow_stage": None,
            "priority": 60,
            "plan_position": 2,
            "estimated_effort": "medium",
        },
        {
            "title": "Star bullet",
            "description": "star form",
            "execution_profile": "full_lifecycle",
            "workflow_stage": None,
            "priority": 60,
            "plan_position": 3,
            "estimated_effort": "medium",
        },
    ]


def test_m3_legacy_numbered_exact_fields():
    markdown = TL + "1. Create schema | tables\n2. Seed data | fixtures\n3. Wire API\n"
    assert [
        (t["title"], t["description"], t["execution_profile"], t["plan_position"])
        for t in _tasks(markdown)
    ] == [
        ("Create schema", "tables", "execute_only", 1),
        ("Seed data", "fixtures", "debug_only", 2),
        ("Wire API", "Implement wire api", "full_lifecycle", 3),
    ]


# --- ambiguity and recovery -------------------------------------------------


@pytest.mark.parametrize(
    ("markdown", "reason"),
    [
        (
            TL + "- [ ] TASK_START: Real task | Do it | order=1\n"
            "- Legacy row | also a task?\n",
            "mixed_task_syntax",
        ),
        (
            TL + "- Setup env | prepare\n### Build API\nCreate endpoints\n",
            "task_rows_before_first_heading",
        ),
        (
            TL + "### Build API\n```\ncode\n### Build UI\nCreate pages\n",
            "unclosed_code_fence",
        ),
    ],
)
def test_ambiguous_structure_is_rejected_without_partial_tasks(markdown, reason):
    result = _result(markdown)
    assert (result.mode, result.rejection_reason, result.tasks) == (
        "rejected",
        reason,
        [],
    )
    assert PlannerService.parse_markdown(markdown) == []


def test_odd_backticks_in_task_start_row_are_not_rejected_m19():
    markdown = (
        TL + "- [ ] TASK_START: Broken | Do it | AC: `pytest tests/test_a.py passes\n"
    )
    result = _result(markdown)
    assert result.mode == "task_start"
    assert [(t.title, t.description) for t in result.tasks] == [
        ("Broken", "Do it | AC: `pytest tests/test_a.py passes")
    ]


def test_replan_recovery_rows_with_odd_backticks_still_parse():
    service = object.__new__(PlanningSessionService)
    markdown = service._build_replan_recovery_artifacts(
        SimpleNamespace(
            prompt="Replan\n- Broken test: AssertionError: expected `value` but "
            "got `other` and the trace continues with `pytest -q"
        ),
        SimpleNamespace(name="Proj"),
    )["planner_markdown"]
    result = _result(markdown)
    assert result.mode == "task_start"
    assert [t.title for t in result.tasks] == [
        "Diagnose recovered failure",
        "Plan bounded recovery approach",
        "Apply targeted recovery fix",
        "Validate recovery path",
        "Review recovery outcome",
    ]
    assert result.tasks[0].description == (
        "Inspect the failure context for Broken test: AssertionError: expected "
        "`value` but got `other` and the trace continues with `pytest -q | "
        "order=1 | P1 | effort=small"
    )


# --- authority boundary (real extractor) --------------------------------------


def test_neutralizer_mirrors_the_extractor_heading_grammar():
    # If the extractor's operator heading grammar changes, the parser's
    # neutralization must change with it.
    assert (
        planner_service.OPERATOR_ACCEPTANCE_HEADING_RE.pattern
        == candidate_checks._ACCEPTANCE_HEADING.pattern
    )
    assert (
        planner_service.OPERATOR_ACCEPTANCE_HEADING_RE.flags
        == candidate_checks._ACCEPTANCE_HEADING.flags
    )


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            TL + "### Fix normalize\nFix the bug in normalize.\nAcceptance criteria:\n"
            "- `pytest tests/test_a.py` passes.\n- Do not modify tests/.\n",
            id="T1_C02",
        ),
        pytest.param(
            TL + "### API task\nChange the API.\n```\n### not a task\n```\n"
            "Acceptance criteria:\n- `pytest tests/test_api.py` passes\n"
            "### UI task\nChange UI.\n",
            id="T2_M11",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: Leak | Acceptance criteria: "
            "`pytest tests/test_a.py` passes | order=1\n",
            id="T2_M18",
        ),
        pytest.param(
            TL
            + "### Fix normalize\nAcceptance criteria: `pytest tests/test_a.py` passes.\n",
            id="heading_at_description_start",
        ),
        pytest.param(
            TL + "- Fix normalize | Acceptance criteria: `pytest tests/test_a.py`\n",
            id="M3_legacy_pipe_row",
        ),
        pytest.param(
            TL + "### Fix: Acceptance criteria: `pytest tests/test_a.py`\n",
            id="M3_legacy_inline_heading",
        ),
        pytest.param(
            TL + "### Task\nprose\n  \tACCEPTANCE CRITERIA:\n- `pytest tests/a.py`\n"
            "acceptance criteria: `pytest tests/b.py`\n"
            "```\nAcceptance Criteria: `pytest tests/c.py`\n```\n",
            id="repeated_case_whitespace_fenced",
        ),
        pytest.param(
            TL + "### Task\nAcceptance criteria:\n- `pytest tests/a.py; rm -rf x`\n"
            "- `pytest /abs/path.py`\n",
            id="refused_commands",
        ),
        pytest.param(
            TL + "### Task\n Acceptance criteria: `pytest tests/a.py`\n",
            id="unicode_leading_whitespace",
        ),
    ],
)
def test_parsed_descriptions_carry_no_operator_acceptance_authority(markdown):
    tasks = _tasks(markdown)
    assert tasks
    assert _authority(tasks) == NO_AUTHORITY


def test_criterion_text_and_order_are_preserved():
    markdown = (
        TL + "### Fix normalize\nFix the bug in normalize.\nAcceptance criteria:\n"
        "- `pytest tests/test_a.py` passes.\n- Do not modify tests/.\n"
    )
    description = _tasks(markdown)[0]["description"]
    assert description == (
        "Fix the bug in normalize.\nPlanner Acceptance criteria:\n"
        "- `pytest tests/test_a.py` passes.\n- Do not modify tests/."
    )


def test_m18_acceptance_content_cannot_set_execution_metadata():
    markdown = (
        TL + "- [ ] TASK_START: Leak | Acceptance criteria: "
        "`pytest tests/test_a.py` passes | order=1\n"
    )
    assert _tasks(markdown) == [
        {
            "title": "Leak",
            "description": "Planner Acceptance criteria: `pytest tests/test_a.py` "
            "passes | order=1",
            "execution_profile": "full_lifecycle",
            "workflow_stage": None,
            "priority": 60,
            "plan_position": 1,
            "estimated_effort": "medium",
        }
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Acceptance criteria:", "Planner Acceptance criteria:"),
        (
            "  acceptance criteria: `pytest a`",
            "  Planner acceptance criteria: `pytest a`",
        ),
        ("a\nACCEPTANCE CRITERIA:\nb", "a\nPlanner ACCEPTANCE CRITERIA:\nb"),
        ("Planner acceptance criteria: x", "Planner acceptance criteria: x"),
        (
            "Fix it. Acceptance criteria: `pytest a`",
            "Fix it. Acceptance criteria: `pytest a`",
        ),
        ("### Acceptance criteria:", "### Acceptance criteria:"),
        ("**Acceptance criteria:**", "**Acceptance criteria:**"),
        ("- Acceptance criteria:", "- Acceptance criteria:"),
        ("Acceptance criteria (tests):", "Acceptance criteria (tests):"),
        ("Acceptance criterion:", "Acceptance criterion:"),
        ("Acceptance criteria", "Acceptance criteria"),
        ("", ""),
    ],
)
def test_neutralization_grammar_and_idempotence(text, expected):
    once = PlannerService._neutralize_acceptance_headings(text)
    assert once == expected
    assert PlannerService._neutralize_acceptance_headings(once) == once
    # Removing the inserted marker recovers the original text exactly.
    if once != text:
        assert once.replace("Planner ", "", 1) == text
    # Whatever the form, the result never opens an operator section.
    assert not task_acceptance_pytest_commands(once).section_found


@pytest.mark.parametrize(
    "variant",
    [
        "### Acceptance criteria:",
        "**Acceptance criteria:**",
        "- Acceptance criteria:",
        "Acceptance criteria (tests):",
        "Acceptance criterion:",
        "Acceptance criteria",
        "Planner acceptance criteria:",
    ],
)
def test_non_canonical_variants_are_not_operator_sections(variant):
    # Left verbatim by the parser because the extractor does not recognise them.
    text = f"{variant}\n- `pytest tests/test_a.py` passes"
    assert not task_acceptance_pytest_commands(text).section_found


# --- metadata isolation (T3B1-L4) ----------------------------------------------


@pytest.mark.parametrize(
    "acceptance",
    [
        "Acceptance criteria:\n- `pytest tests/test_a.py` passes; verify P0 urgent.\n",
        "#### Acceptance Criteria\n- Review and audit; step 7; phase 3; test qa.\n",
        "- critical blocker: run tests, inspect architecture, refactor workflow.\n",
    ],
)
def test_m2_metadata_is_not_driven_by_acceptance_content(acceptance):
    plain = TL + "### Fix normalize\nFix the bug.\n### Next task\nAdd docs.\n"
    with_acceptance = (
        TL
        + "### Fix normalize\nFix the bug.\n\n"
        + acceptance
        + "### Next task\nAdd docs.\n"
    )
    keys = (
        "title",
        "execution_profile",
        "workflow_stage",
        "priority",
        "plan_position",
        "estimated_effort",
    )
    expected = [{k: t[k] for k in keys} for t in _tasks(plain)]
    assert [{k: t[k] for k in keys} for t in _tasks(with_acceptance)] == expected
    assert expected[0] == {
        "title": "Fix normalize",
        "execution_profile": "debug_only",
        "workflow_stage": "debug",
        "priority": 60,
        "plan_position": 1,
        "estimated_effort": "medium",
    }


def test_m2_metadata_reads_the_heading_paragraph():
    markdown = TL + "### Harden parser\nReview the parser, P0.\n\nmore body\n"
    task = _tasks(markdown)[0]
    assert (task["execution_profile"], task["priority"]) == ("review_only", 100)


# --- R3: M2 metadata integrity (R2-F1) -----------------------------------------

_DEBUG_FIX = {
    "title": "Fix normalize",
    "execution_profile": "debug_only",
    "workflow_stage": "debug",
    "priority": 60,
    "plan_position": 1,
    "estimated_effort": "medium",
}


@pytest.mark.parametrize(
    ("body", "description"),
    [
        pytest.param(
            "Fix the bug.\n\nAcceptance criteria:\n- run with profile=test_only\n",
            "Fix the bug.\n\nPlanner Acceptance criteria:\n- run with profile=test_only",
            id="K1_acceptance_bullet",
        ),
        pytest.param(
            "Fix the bug.\n```\nstage=review\n```\n",
            "Fix the bug.\n```\nstage=review\n```",
            id="K2_fence_in_first_paragraph",
        ),
        pytest.param(
            "- mode=review_only first bullet\n- second bullet -\n",
            "- mode=review_only first bullet\n- second bullet -",
            id="K3_leading_bullet_trailing_hyphen",
        ),
        pytest.param(
            "Fix the bug.\n\nAcceptance criteria:\n- order=9 P0 phase 4 step 2 critical\n",
            "Fix the bug.\n\nPlanner Acceptance criteria:\n"
            "- order=9 P0 phase 4 step 2 critical",
            id="K5_order_priority_phase_step",
        ),
        pytest.param(
            "Fix the bug.\n- outer\n  - nested stage=review\n",
            "Fix the bug.\n- outer\n  - nested stage=review",
            id="K7_nested_list",
        ),
        pytest.param(
            "Fix the bug.\n```\ncode\n```\nprofile=test_only\n",
            "Fix the bug.\n```\ncode\n```\nprofile=test_only",
            id="K10_token_after_fence",
        ),
        pytest.param(
            "Fix the bug.\n\n```\nworkflow_stage=review | profile=test_only -\n```\n",
            "Fix the bug.\n\n```\nworkflow_stage=review | profile=test_only -\n```",
            id="fenced_block_after_blank_line",
        ),
    ],
)
def test_r3_m2_scope_tokens_in_body_are_data_not_metadata(body, description):
    markdown = TL + "### Fix normalize\n" + body + "### Next\nAdd docs.\n"
    tasks = _tasks(markdown)
    assert [t["title"] for t in tasks] == ["Fix normalize", "Next"]
    assert tasks[0] == {**_DEBUG_FIX, "description": description}


def test_r3_m2_criterion_text_with_token_is_preserved_verbatim():
    markdown = (
        TL + "### Task\nAcceptance criteria:\n- `pytest tests/a.py` profile=test_only\n"
    )
    (task,) = _tasks(markdown)
    assert task["description"] == (
        "Planner Acceptance criteria:\n- `pytest tests/a.py` profile=test_only"
    )
    assert (task["execution_profile"], task["workflow_stage"]) == (
        "full_lifecycle",
        None,
    )
    assert _authority([task]) == NO_AUTHORITY


@pytest.mark.parametrize(
    ("heading_and_body", "description", "head_compatible"),
    [
        pytest.param(
            "### Fix normalize\nprofile=test_only\n\nFix the bug.\n",
            "Fix the bug.",
            False,
            id="metadata_only_first_paragraph",
        ),
        pytest.param(
            "### Fix normalize: Fix the bug | profile=test_only\nMore body.\n",
            "Fix the bug\nMore body.",
            True,
            id="inline_heading_description",
        ),
        pytest.param(
            "### Fix normalize profile=test_only\nFix the bug.\n",
            "Fix the bug.",
            True,
            id="heading_title",
        ),
    ],
)
def test_r5_m2_heading_local_metadata_uses_structural_forms(
    heading_and_body, description, head_compatible
):
    markdown = TL + heading_and_body
    (task,) = _tasks(markdown)
    assert task == {
        "title": "Fix normalize",
        "description": description,
        "execution_profile": "test_only",
        "workflow_stage": "validate",
        "priority": 60,
        "plan_position": 1,
        "estimated_effort": "medium",
    }
    if head_compatible:
        (head,) = [
            asdict(t) for t in PlannerService.parse_markdown(TL + heading_and_body)
        ]
        assert {k: v for k, v in head.items() if k != "description"} == {
            k: v for k, v in task.items() if k != "description"
        }


def test_r3_m2_first_paragraph_stops_at_fence_for_inference():
    with_fence = TL + "### Harden parser\nTidy it.\n```\nreview P0 urgent\n```\n"
    plain = TL + "### Harden parser\nTidy it.\n"
    keys = ("execution_profile", "workflow_stage", "priority", "estimated_effort")
    assert [{k: t[k] for k in keys} for t in _tasks(with_fence)] == [
        {k: t[k] for k in keys} for t in _tasks(plain)
    ]


def test_r3_m2_markdown_syntax_and_indentation_are_preserved():
    body = "  - indented first bullet -\n| col | val |\n- item -\ntext ending -  \n"
    (task,) = _tasks(TL + "### Fix normalize\n\n" + body + "\n\n")
    assert task["description"] == (
        "  - indented first bullet -\n| col | val |\n- item -\ntext ending -"
    )


# --- R3: TASK_START boundary safety (R2-F2) -------------------------------------

_ROW = "- [ ] TASK_START: First | a | order=1\n"


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(TL + _ROW + "- [ ] Second: forgot the prefix\n", id="checkbox"),
        pytest.param(TL + _ROW + "- [x] Done already\n", id="checked"),
        pytest.param(TL + _ROW + "2. [ ] Second\n", id="numbered_checkbox"),
        pytest.param(TL + _ROW + "\n- [ ] Second\n", id="after_blank_line"),
        pytest.param(TL + "- [ ] Setup env\n" + _ROW, id="before_first_row"),
        pytest.param(
            TL + _ROW + "  detail\n- [ ] Second\n", id="after_indented_detail"
        ),
        pytest.param(TL + _ROW + "- Add logout | Clear the session\n", id="pipe"),
    ],
)
def test_r3_task_start_with_unprefixed_task_rows_is_rejected(markdown):
    result = _result(markdown)
    assert (result.mode, result.rejection_reason, result.tasks) == (
        "rejected",
        "mixed_task_syntax",
        [],
    )
    assert PlannerService.parse_markdown(markdown) == []


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param("These tasks depend on the API.\n", id="prose_paragraph"),
        pytest.param("- note: remember the docs\n", id="prose_bullet"),
        pytest.param("  - [ ] tests pass\n  - [x] lint clean\n", id="nested_checklist"),
        pytest.param(
            "Acceptance criteria:\n1. tests pass\n2. lint clean\n",
            id="numbered_acceptance_after_label",
        ),
        pytest.param(
            "Acceptance criteria:\n- [ ] tests pass\n", id="checklist_after_label"
        ),
        pytest.param(
            "```\n- [ ] Example task\n2. Example\n- Ex: colon\n```\n",
            id="fenced_examples",
        ),
        pytest.param("```\nx\n```\n- [ ] Next\n", id="checklist_after_fence"),
        # Ambiguous with prose/criteria, kept as before (operator decision).
        pytest.param("2. Wire API\n", id="numbered_row_ambiguous"),
        pytest.param("- Add logout: Clear the session\n", id="colon_row_ambiguous"),
    ],
)
def test_r3_task_start_keeps_non_task_content(tail):
    result = _result(TL + _ROW + tail)
    assert (result.mode, result.rejection_reason) == ("task_start", None)
    assert [(t.title, t.description) for t in result.tasks] == [
        ("First", "a | order=1")
    ]


def test_r3_task_start_acceptance_section_checklist_is_not_a_task_row():
    markdown = "## Plan\n" + _ROW + "\n## Acceptance Criteria\n- [ ] tests pass\n"
    result = _result(markdown)
    assert (result.mode, [t.title for t in result.tasks]) == ("task_start", ["First"])


def test_r3_heading_task_start_followed_by_checklist_is_kept():
    result = _result(TL + "### TASK_START: First\nDo it.\n- [ ] criterion\n")
    assert (result.mode, [(t.title, t.description) for t in result.tasks]) == (
        "task_start",
        [("First", "Do it.")],
    )


def test_r3_multiple_valid_task_start_rows():
    markdown = (
        TL + _ROW + "- [ ] TASK_START: Second | b | order=2\n"
        "- [ ] TASK_START: Third | c | order=3\n"
    )
    result = _result(markdown)
    assert result.mode == "task_start"
    assert [(t.title, t.plan_position) for t in result.tasks] == [
        ("First", 1),
        ("Second", 2),
        ("Third", 3),
    ]


# --- R3: acceptance heading recognition (R2-F3) ---------------------------------


@pytest.mark.parametrize(
    "heading",
    [
        "### Acceptance Criteria",
        "### Acceptance Criteria:",
        "### Acceptance Criterion",
        "### acceptance criteria",
        "### **Acceptance Criteria**",
        "### Acceptance criteria: tests pass",
    ],
)
def test_r3_exact_acceptance_heading_is_not_a_task(heading):
    tasks = _tasks(TL + "### Real\nbody\n" + heading + "\n- a\n")
    assert [(t["title"], t["description"]) for t in tasks] == [
        ("Real", "body\n" + heading + "\n- a")
    ]


@pytest.mark.parametrize(
    "title",
    [
        "Acceptance criteria for login",
        "Acceptance criteria parser",
        "Acceptance criteria migration",
    ],
)
@pytest.mark.parametrize("hashes", ["###", "####", "######"])
def test_r3_acceptance_prefixed_titles_are_tasks(title, hashes):
    markdown = TL + f"{hashes} {title}\nbody\n{hashes} Real\nmore\n"
    result = _result(markdown)
    assert result.mode == "heading"
    assert [(t.title, t.description) for t in result.tasks] == [
        (title, "body"),
        ("Real", "more"),
    ]


def test_r3_nested_acceptance_headings_stay_in_the_task_body():
    markdown = TL + "#### A\nx\n##### Acceptance criteria\n- y\n#### B\nz\n"
    assert [(t["title"], t["description"]) for t in _tasks(markdown)] == [
        ("A", "x\n##### Acceptance criteria\n- y"),
        ("B", "z"),
    ]


def test_r3_acceptance_prefixed_title_gains_no_authority():
    markdown = (
        TL + "### Acceptance criteria for login\nAcceptance criteria:\n"
        "- `pytest tests/test_login.py`\n"
    )
    tasks = _tasks(markdown)
    assert [t["title"] for t in tasks] == ["Acceptance criteria for login"]
    assert tasks[0]["description"] == (
        "Planner Acceptance criteria:\n- `pytest tests/test_login.py`"
    )
    assert _authority(tasks) == NO_AUTHORITY


# --- R3: neutralization integrity -----------------------------------------------

_SEPARATORS = ["\n", "\r\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85"]
_SEPARATORS += [" ", " "]


@pytest.mark.parametrize("sep", _SEPARATORS)
def test_r3_neutralizer_splits_lines_like_the_extractor(sep):
    raw = f"intro{sep}Acceptance criteria:{sep}- `pytest tests/test_a.py`"
    assert task_acceptance_pytest_commands(raw).section_found
    once = PlannerService._neutralize_acceptance_headings(raw)
    assert once == raw.replace("Acceptance", "Planner Acceptance", 1)
    assert PlannerService._neutralize_acceptance_headings(once) == once
    assert not task_acceptance_pytest_commands(once).section_found
    for task in _tasks(TL + "### Task\n" + raw + "\n"):
        assert _authority([task]) == NO_AUTHORITY


@pytest.mark.parametrize(
    "heading",
    [
        "ACCEPTANCE CRITERIA:",
        "acceptance criteria:",
        "AcCePtAnCe CrItErIa:",
        " Acceptance criteria:",
        "　Acceptance criteria:",
        "\t Acceptance criteria:",
    ],
)
def test_r3_case_and_unicode_whitespace_variants_are_neutralized(heading):
    text = f"{heading} `pytest tests/a.py`\n{heading}\n- `pytest tests/b.py`"
    for markdown in (
        TL + "### Task\n" + text + "\n",
        TL + "### Task\nprose\n" + text + "\n",
        TL + "- [ ] TASK_START: Task | " + heading + " `pytest tests/a.py`\n",
        TL + "- Task | " + heading + " `pytest tests/a.py`\n",
    ):
        tasks = _tasks(markdown)
        assert tasks
        assert _authority(tasks) == NO_AUTHORITY
        for task in tasks:
            once = task["description"]
            assert PlannerService._neutralize_acceptance_headings(once) == once


# --- R5: metadata authority, Markdown fences, and mixed task boundaries ---------


@pytest.mark.parametrize(
    ("markdown", "mode", "description"),
    [
        pytest.param(
            TL + "### Normalize parser\nAcceptance criteria: profile=test_only\n",
            "heading",
            "Planner Acceptance criteria: profile=test_only",
            id="m2-acceptance-token",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: Normalize parser | "
            + "Acceptance criteria: profile=test_only\n",
            "task_start",
            "Planner Acceptance criteria: profile=test_only",
            id="m1-acceptance-token",
        ),
        pytest.param(
            TL + "- Normalize parser | Acceptance criteria: profile=test_only\n",
            "legacy",
            "Planner Acceptance criteria: profile=test_only",
            id="m3-acceptance-token",
        ),
    ],
)
def test_r5_acceptance_scope_tokens_are_preserved_as_description_data(
    markdown, mode, description
):
    result = _result(markdown)
    assert result.mode == mode
    assert result.rejection_reason is None
    assert len(result.tasks) == 1
    task = asdict(result.tasks[0])
    assert task == {
        "title": "Normalize parser",
        "description": description,
        "execution_profile": "full_lifecycle",
        "workflow_stage": None,
        "priority": 60,
        "plan_position": 1,
        "estimated_effort": "medium",
    }


@pytest.mark.parametrize(
    ("markdown", "mode", "description"),
    [
        pytest.param(
            TL + "### Normalize parser\nUse literal `profile=test_only` in docs.\n",
            "heading",
            "Use literal `profile=test_only` in docs.",
            id="m2-inline-code",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: Normalize parser | "
            + "Keep literal `profile=test_only`\n",
            "task_start",
            "Keep literal `profile=test_only`",
            id="m1-inline-code",
        ),
        pytest.param(
            TL + "- Normalize parser | Keep literal `profile=test_only`\n",
            "legacy",
            "Keep literal `profile=test_only`",
            id="m3-inline-code",
        ),
    ],
)
def test_r5_inline_code_scope_tokens_do_not_set_metadata_or_disappear(
    markdown, mode, description
):
    result = _result(markdown)
    assert result.mode == mode
    assert result.rejection_reason is None
    (task,) = result.tasks
    assert (task.description, task.execution_profile, task.workflow_stage) == (
        description,
        "full_lifecycle",
        None,
    )


def test_r5_heading_prose_scope_token_stays_description_not_metadata():
    (task,) = PlannerService.parse_markdown(
        TL + "### Normalize parser\nDescribe profile=test_only in the docs.\n"
    )
    assert task.description == "Describe profile=test_only in the docs."
    assert (task.execution_profile, task.workflow_stage) == ("full_lifecycle", None)


def test_r5_fenced_scope_token_is_preserved_but_not_authoritative():
    (task,) = PlannerService.parse_markdown(
        TL + "### Normalize parser\nImplement the parser.\n"
        "````text\nstage=review\n````\n"
    )
    assert task.description == "Implement the parser.\n````text\nstage=review\n````"
    assert (task.execution_profile, task.workflow_stage) == ("execute_only", "execute")


def test_r5_nested_acceptance_metadata_is_description_data():
    (task,) = PlannerService.parse_markdown(
        TL + "### Normalize parser\nImplement the parser.\n"
        "Acceptance criteria:\n- outer\n  - mode=review_only\n"
    )
    assert task.description == (
        "Implement the parser.\nPlanner Acceptance criteria:\n"
        "- outer\n  - mode=review_only"
    )
    assert (task.execution_profile, task.workflow_stage) == ("execute_only", "execute")


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            TL
            + "- [ ] TASK_START: Normalize parser | Do it | "
            + "profile=execute_only | mode=review_only\n",
            id="m1-conflicting-profiles",
        ),
        pytest.param(
            TL + "### Normalize parser\n" + "profile=execute_only | mode=review_only\n",
            id="m2-conflicting-profiles",
        ),
        pytest.param(
            TL
            + "- Normalize parser | Do it | stage=debug | "
            + "workflow_stage=review\n",
            id="m3-conflicting-stages",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: Normalize parser | Do it | "
            + "profile=test_only | stage=review | workflow_stage=debug\n",
            id="conflicting-stage-fields",
        ),
    ],
)
def test_r5_conflicting_explicit_scope_metadata_rejects_without_partial_tasks(
    markdown,
):
    result = _result(markdown)
    assert (result.mode, result.rejection_reason, result.tasks) == (
        "rejected",
        "conflicting_scope_metadata",
        [],
    )


def test_r5_heading_local_metadata_keeps_supported_structural_forms():
    markdown = (
        TL
        + "### Normalize parser profile=test_only stage=validate\nImplement it.\n"
        + "### Inline form: Implement it | profile=test_only | stage=validate\n"
    )
    result = _result(markdown)
    assert result.mode == "heading"
    assert [
        (t.title, t.description, t.execution_profile, t.workflow_stage)
        for t in result.tasks
    ] == [
        ("Normalize parser", "Implement it.", "test_only", "validate"),
        ("Inline form", "Implement it", "test_only", "validate"),
    ]


def test_r5_metadata_only_first_paragraph_remains_a_heading_local_form():
    result = _result(
        TL + "### Normalize parser\nprofile=test_only | stage=validate\n\n"
        "Implement it.\n"
    )
    (task,) = result.tasks
    assert (task.description, task.execution_profile, task.workflow_stage) == (
        "Implement it.",
        "test_only",
        "validate",
    )


def test_r5_explicit_stage_is_independent_of_profile_default_stage():
    (task,) = PlannerService.parse_markdown(
        TL
        + "- [ ] TASK_START: Diagnose | Inspect the failure | "
        + "stage=diagnose | profile=review_only\n"
    )
    assert (task.execution_profile, task.workflow_stage) == (
        "review_only",
        "diagnose",
    )


def test_r5_legacy_pipe_scope_metadata_remains_supported():
    (task,) = PlannerService.parse_markdown(
        TL + "- Normalize parser | Keep the body | profile=test_only | "
        "workflow_stage=validate\n"
    )
    assert (
        task.title,
        task.description,
        task.execution_profile,
        task.workflow_stage,
    ) == (
        "Normalize parser",
        "Keep the body",
        "test_only",
        "validate",
    )


def test_r5_legacy_description_scope_tokens_are_not_metadata():
    result = _result(
        TL
        + "- Normalize parser: Preserve profile=test_only, "
        + "stage=review, scope=debug in the notes\n"
    )
    (task,) = result.tasks
    assert task.description == (
        "Preserve profile=test_only, stage=review, scope=debug in the notes"
    )
    assert (task.execution_profile, task.workflow_stage) == ("full_lifecycle", None)


def test_r5_scope_aliases_accept_equivalent_structured_fields():
    (task,) = PlannerService.parse_markdown(
        TL + "- [ ] TASK_START: Validate | Run checks | scope=validate_only | "
        "mode=test_only\n"
    )
    assert (task.execution_profile, task.workflow_stage) == ("test_only", "validate")


def test_r5_acceptance_fields_do_not_change_priority_effort_or_position():
    markdown = (
        TL
        + "- [ ] TASK_START: Document parser | Acceptance criteria: "
        + "order=9 P0 critical profile=test_only stage=debug\n"
    )
    (task,) = PlannerService.parse_markdown(markdown)
    assert (task.execution_profile, task.workflow_stage) == ("full_lifecycle", None)
    assert (task.priority, task.estimated_effort, task.plan_position) == (
        40,
        "medium",
        1,
    )


def test_r5_recovery_generated_scope_fields_keep_profile_stage_and_order():
    markdown = (
        TL
        + "- [ ] TASK_START: Diagnose recovered failure | Inspect failure | order=1 | "
        + "P1 | effort=small | stage=diagnose | profile=review_only\n"
        + "- [ ] TASK_START: Plan recovery | Plan only | order=2 | P1 | effort=small | "
        + "stage=plan | profile=review_only\n"
        + "- [ ] TASK_START: Apply recovery | Implement fix | order=3 | P1 | "
        + "effort=medium | stage=debug | profile=debug_only\n"
        + "- [ ] TASK_START: Validate recovery | Run focused validation | order=4 | "
        + "P1 | effort=small | stage=validate | profile=test_only\n"
        + "- [ ] TASK_START: Review outcome | Audit evidence | order=5 | P2 | "
        + "effort=small | stage=complete | profile=review_only\n"
    )
    tasks = PlannerService.parse_markdown(markdown)
    assert [
        (t.title, t.execution_profile, t.workflow_stage, t.plan_position) for t in tasks
    ] == [
        ("Diagnose recovered failure", "review_only", "diagnose", 1),
        ("Plan recovery", "review_only", "plan", 2),
        ("Apply recovery", "debug_only", "debug", 3),
        ("Validate recovery", "test_only", "validate", 4),
        ("Review outcome", "review_only", "complete", 5),
    ]


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(TL + "### Acceptance criteria:\n", id="heading-only"),
        pytest.param(
            TL + "### Acceptance criteria:\n- [ ] User can log in\n- [ ] Tests pass\n",
            id="checkbox-criteria",
        ),
        pytest.param(
            TL + "### Acceptance criteria\n1. User can log in\n2. Tests pass\n",
            id="numbered-criteria",
        ),
        pytest.param(
            TL + "### Acceptance criteria:\nThe user can log in and tests pass.\n",
            id="prose-criteria",
        ),
    ],
)
def test_r5_acceptance_only_documents_reject_without_phantom_tasks(markdown):
    result = _result(markdown)
    assert (result.mode, result.rejection_reason, result.tasks) == (
        "rejected",
        "acceptance_only_no_task",
        [],
    )


def test_r5_legacy_task_before_acceptance_section_is_retained_alone():
    result = _result(
        TL + "- Build login | Implement the login flow\n"
        "### Acceptance criteria:\n- [ ] User can log in\n- Tests pass\n"
    )
    assert result.mode == "legacy"
    assert [task.title for task in result.tasks] == ["Build login"]


def test_r5_acceptance_phrase_in_ordinary_legacy_description_is_not_rejected():
    result = _result(TL + "- Document acceptance criteria | Describe the cases\n")
    assert result.mode == "legacy"
    assert [task.title for task in result.tasks] == ["Document acceptance criteria"]


def test_r5_nested_acceptance_heading_remains_inside_real_heading_task():
    result = _result(
        TL + "### Login task\nImplement login.\n#### Acceptance criteria\n"
        "- User can log in\n"
    )
    assert result.mode == "heading"
    assert [(task.title, task.description) for task in result.tasks] == [
        (
            "Login task",
            "Implement login.\n#### Acceptance criteria\n- User can log in",
        )
    ]


def test_r5_qualified_acceptance_heading_remains_a_task():
    result = _result(TL + "### Acceptance criteria for login\nImplement login.\n")
    assert result.mode == "heading"
    assert [task.title for task in result.tasks] == ["Acceptance criteria for login"]


def test_r5_valid_heading_task_with_acceptance_section_remains_compatible():
    result = _result(
        TL + "### Login task\nImplement login.\nAcceptance criteria:\n"
        "- [ ] User can log in\n"
    )
    assert result.mode == "heading"
    assert len(result.tasks) == 1
    assert result.tasks[0].description == (
        "Implement login.\nPlanner Acceptance criteria:\n- [ ] User can log in"
    )


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            TL + "### First task\nBody.\n\u00a0```text\n"
            "### Second task\nBody.\n\u00a0```\n",
            id="nbsp-pseudo-fence",
        ),
        pytest.param(
            TL + "### First task\nBody.\n````text\n```\n"
            "### Example heading\nExample only.\n````\n### Second task\nBody.\n",
            id="four-opener-short-inner-run",
        ),
        pytest.param(
            TL + "### First task\nBody.\n````text\n### Example heading\n````\n"
            "### Second task\nBody.\n",
            id="four-backtick-closer",
        ),
        pytest.param(
            TL + "### First task\nBody.\n````text\n### Example heading\n`````\n"
            "### Second task\nBody.\n",
            id="five-backtick-closer",
        ),
        pytest.param(
            TL + "### First task\nBody.\n````text\n~~~\n"
            "### Example heading\n````\n### Second task\nBody.\n",
            id="wrong-delimiter-character",
        ),
        pytest.param(
            TL + "### First task\nBody.\n```text\n```python\n"
            "### Example heading\n```\n### Second task\nBody.\n",
            id="closer-with-trailing-text",
        ),
        pytest.param(
            TL + "### First task\nBody.\n   ```text\n### Example heading\n"
            "   ```\n### Second task\nBody.\n",
            id="valid-three-space-indentation",
        ),
        pytest.param(
            TL + "### First task\nBody.\n    ```text\n### Second task\nBody.\n",
            id="excessive-four-space-indentation-is-not-fence",
        ),
        pytest.param(
            TL + "### First task\nBody.\n```invalid`\n" "### Second task\nBody.\n",
            id="backtick-in-backtick-fence-info",
        ),
        pytest.param(
            TL + "### First task\nBody.\n```text\n### Example heading\n```\n"
            "### Second task\nBody.\n",
            id="fenced-heading-and-real-following-heading",
        ),
        pytest.param(
            TL + "### First task\nMention ``` and ~~~ inline, not a fence.\n"
            "### Second task\nBody.\n",
            id="markers-inside-prose",
        ),
    ],
)
def test_r5_fence_grammar_keeps_only_real_task_headings_visible(markdown):
    result = _result(markdown)
    assert (result.mode, result.rejection_reason) == ("heading", None)
    assert [task.title for task in result.tasks] == ["First task", "Second task"]
    if "### Example heading" in markdown:
        assert "### Example heading" in result.tasks[0].description


def test_r5_unclosed_fence_with_task_like_text_is_rejected():
    result = _result(TL + "### First task\nBody.\n```text\n### Apparent second task\n")
    assert (result.mode, result.rejection_reason, result.tasks) == (
        "rejected",
        "unclosed_code_fence",
        [],
    )


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            TL + "- [ ] TASK_START: First | Implement first\n"
            "### Second task\nImplement second.\n",
            id="task-start-before-heading",
        ),
        pytest.param(
            TL + "### First task\nImplement first.\n"
            "- [ ] TASK_START: Second | Implement second\n",
            id="heading-before-task-start",
        ),
    ],
)
def test_r5_clear_mixed_task_start_and_heading_syntax_rejects_all(markdown):
    result = _result(markdown)
    assert (result.mode, result.rejection_reason, result.tasks) == (
        "rejected",
        "mixed_task_syntax",
        [],
    )


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param("#### Implementation notes\nKeep details.\n", id="nested-heading"),
        pytest.param("```text\n### Example only\n```\n", id="fenced-heading"),
        pytest.param("### Acceptance criteria\n- [ ] pass\n", id="acceptance-heading"),
    ],
)
def test_r5_nested_fenced_and_acceptance_headings_do_not_trigger_mixed_syntax(tail):
    result = _result(TL + _ROW + tail)
    assert (result.mode, result.rejection_reason) == ("task_start", None)
    assert [task.title for task in result.tasks] == ["First"]


def test_r5_multiple_task_start_rows_remain_valid():
    result = _result(TL + _ROW + "- [ ] TASK_START: Second | b | order=2\n")
    assert result.mode == "task_start"
    assert [(task.title, task.plan_position) for task in result.tasks] == [
        ("First", 1),
        ("Second", 2),
    ]


def test_r6_invalid_structural_stage_rejects_valid_profile_instead_of_falling_back():
    markdown = (
        TL + "- [ ] TASK_START: Validate migration | Run checks | "
        "profile=review_only | stage=revieww\n"
    )
    result = _result(markdown)
    assert (result.mode, result.rejection_reason, result.tasks) == (
        "rejected",
        "invalid_scope_metadata",
        [],
    )


def test_r6_mixed_task_start_row_forms_reject_duplicate_position_reproduction():
    markdown = (
        TL + "\n### TASK_START: First\nFirst body.\n\n"
        "- [ ] TASK_START: Second | Second body | order=2\n\n"
        "### TASK_START: Third\nThird body.\n"
    )
    result = _result(markdown)
    assert (result.mode, result.rejection_reason, result.tasks) == (
        "rejected",
        "mixed_task_syntax",
        [],
    )


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            TL + "- [ ] TASK_START: T | Body | profile=review_only | stage=revieww\n",
            id="valid-profile-invalid-stage",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: T | Body | profile=review_onlyy\n",
            id="invalid-profile",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: T | Body | stage=revieww\n",
            id="invalid-stage",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: T | Body | profile=\n",
            id="empty-profile",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: T | Body | stage=\n",
            id="empty-stage",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: T | Body | profile=test_only;stage=review\n",
            id="unexpected-separator",
        ),
        pytest.param(
            TL + "### Task\nprofile=review_onlyy\n\nBody.\n",
            id="invalid-metadata-only-heading-field",
        ),
        pytest.param(
            TL + "### Task profile=review_onlyy\nBody.\n",
            id="invalid-heading-title-field",
        ),
        pytest.param(
            TL + "- Normalize | Preserve body | workflow_stage=revieww\n",
            id="invalid-legacy-pipe-field",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: First | Body | profile=review_only\n"
            + "- [ ] TASK_START: Second | Body | stage=revieww\n",
            id="invalid-metadata-in-later-task",
        ),
    ],
)
def test_r6_invalid_structural_metadata_rejects_complete_plan(markdown):
    result = _result(markdown)
    assert (result.mode, result.rejection_reason, result.tasks) == (
        "rejected",
        "invalid_scope_metadata",
        [],
    )


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            TL + "- [ ] TASK_START: T | Body | profile=test_only | "
            "mode=debug_only | stage=\n",
            id="same-task",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: First | Body | profile=test_only | mode=debug_only\n"
            + "- [ ] TASK_START: Second | Body | stage=revieww\n",
            id="conflict-before-invalid-later-task",
        ),
    ],
)
def test_r6_invalid_metadata_precedes_valid_conflict_reason_deterministically(markdown):
    result = _result(markdown)
    assert (result.mode, result.rejection_reason, result.tasks) == (
        "rejected",
        "invalid_scope_metadata",
        [],
    )


@pytest.mark.parametrize(
    "markdown,expected_description,expected_profile,expected_stage",
    [
        pytest.param(
            TL + "- [ ] TASK_START: T | The guide says profile=review_onlyy.\n",
            "The guide says profile=review_onlyy.",
            "full_lifecycle",
            None,
            id="ordinary-prose",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: T | Acceptance criteria: profile=test_only\n",
            "Planner Acceptance criteria: profile=test_only",
            "full_lifecycle",
            None,
            id="acceptance-prose",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: T | Example `profile=review_onlyy`\n",
            "Example `profile=review_onlyy`",
            "full_lifecycle",
            None,
            id="inline-code",
        ),
        pytest.param(
            TL + "### T\nImplement.\n\n```text\nstage=revieww\n```\n",
            "Implement.\n\n```text\nstage=revieww\n```",
            "execute_only",
            "execute",
            id="fenced-code",
        ),
        pytest.param(
            TL + "### T\nImplement.\n\n- Notes:\n  - mode=debug_only\n",
            "Implement.\n\n- Notes:\n  - mode=debug_only",
            "execute_only",
            "execute",
            id="nested-bullet",
        ),
        pytest.param(
            TL + "- Normalize: The quoted guide says profile=review_onlyy.\n",
            "The quoted guide says profile=review_onlyy.",
            "full_lifecycle",
            None,
            id="m3-colon-prose",
        ),
    ],
)
def test_r6_metadata_looking_ordinary_content_is_preserved(
    markdown, expected_description, expected_profile, expected_stage
):
    result = _result(markdown)
    assert result.mode in {"task_start", "heading", "legacy"}
    assert result.rejection_reason is None
    assert len(result.tasks) == 1
    task = result.tasks[0]
    assert task.description == expected_description
    assert task.execution_profile == expected_profile
    assert task.workflow_stage == expected_stage


@pytest.mark.parametrize(
    "markdown,expected_profile,expected_stage",
    [
        pytest.param(
            TL + "- [ ] TASK_START: T | Body | profile=plan_only\n",
            "review_only",
            "plan",
            id="profile-alias",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: T | Body | scope=validate_only\n",
            "full_lifecycle",
            "validate",
            id="stage-alias",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: T | Body | profile=test_only | mode=test_only\n",
            "test_only",
            "validate",
            id="repeated-equivalent-profile",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: T | Body | stage=review | workflow_stage=review\n",
            "full_lifecycle",
            "review",
            id="repeated-equivalent-stage",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: T | Body | profile=review_only | stage=diagnose\n",
            "review_only",
            "diagnose",
            id="explicit-stage-overrides-profile-default",
        ),
    ],
)
def test_r6_valid_structural_metadata_remains_compatible(
    markdown, expected_profile, expected_stage
):
    result = _result(markdown)
    assert result.mode == "task_start"
    assert result.rejection_reason is None
    assert len(result.tasks) == 1
    task = result.tasks[0]
    assert (task.execution_profile, task.workflow_stage) == (
        expected_profile,
        expected_stage,
    )


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            TL + "- [ ] TASK_START: A | Body | profile=test_only | mode=debug_only\n",
            id="conflicting-profiles",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: A | Body | stage=review | workflow_stage=debug\n",
            id="conflicting-stages",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: A | Body | stage=review | scope=debug_only\n",
            id="stage-versus-scope",
        ),
    ],
)
def test_r6_conflicting_valid_structural_metadata_still_rejects(markdown):
    result = _result(markdown)
    assert (result.mode, result.rejection_reason, result.tasks) == (
        "rejected",
        "conflicting_scope_metadata",
        [],
    )


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            TL
            + "### TASK_START: First\nFirst body.\n- [ ] TASK_START: Second | Second body\n",
            id="heading-to-list",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: First | First body\n### TASK_START: Second\nSecond body\n",
            id="list-to-heading",
        ),
        pytest.param(
            TL
            + "### TASK_START: First\nFirst body\n- [ ] TASK_START: Second | Second body\n"
            "### TASK_START: Third\nThird body\n",
            id="heading-list-heading",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: First | First body\n### TASK_START: Second\nSecond body\n"
            "- [ ] TASK_START: Third | Third body\n",
            id="list-heading-list",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: First | First body\n"
            "#### TASK_START: Second\nSecond body\n",
            id="list-to-recognized-heading-row",
        ),
    ],
)
def test_r6_mixed_task_start_forms_reject_without_partial_tasks(markdown):
    result = _result(markdown)
    assert (result.mode, result.rejection_reason, result.tasks) == (
        "rejected",
        "mixed_task_syntax",
        [],
    )


def test_r6_task_start_row_form_examples_and_nested_content_do_not_trigger_mixed_rejection():
    markdown = (
        TL + "- [ ] TASK_START: First | Mention TASK_START: in prose.\n"
        "#### Implementation notes\nKeep this nested.\n"
        "```markdown\n### TASK_START: heading example\n"
        "- [ ] TASK_START: list example\n```\n"
    )
    result = _result(markdown)
    assert result.mode == "task_start"
    assert result.rejection_reason is None
    assert [task.title for task in result.tasks] == ["First"]


def test_r6_multiple_heading_task_start_rows_remain_valid_and_ordered():
    result = _result(
        TL + "### TASK_START: First\nFirst body.\n"
        "### TASK_START: Second\nSecond body.\n"
    )
    assert result.mode == "task_start"
    assert [(task.title, task.plan_position) for task in result.tasks] == [
        ("First", 1),
        ("Second", 2),
    ]


def test_r5_clear_mixed_syntax_synthesis_fails_without_committed_tasks(
    authenticated_client, monkeypatch
):
    import json

    project = _r3_project(authenticated_client)
    mixed = (
        "# Project: R3 Boundary\n\n## Task List\n"
        "- [ ] TASK_START: First | Implement first\n"
        "### Second task\nImplement second.\n"
    )
    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt: {
            "status": "completed",
            "output": json.dumps(_r3_payload(mixed)),
        },
    )
    session = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={"project_id": project["id"], "prompt": _R3_PROMPT},
    )
    assert session.status_code == 201
    assert session.json()["status"] == "failed"
    assert session.json()["committed_task_ids"] == []


@pytest.mark.parametrize(
    "planner_markdown",
    [
        pytest.param(
            "# Project: R3 Boundary\n\n## Task List\n"
            "- [ ] TASK_START: Validate | Run checks | profile=review_only | stage=revieww\n",
            id="invalid-structural-metadata",
        ),
        pytest.param(
            "# Project: R3 Boundary\n\n## Task List\n"
            "### TASK_START: First\nFirst body.\n"
            "- [ ] TASK_START: Second | Second body\n",
            id="mixed-task-start-row-forms",
        ),
    ],
)
def test_r6_synthesis_rejection_creates_no_committed_task_ids(
    authenticated_client, monkeypatch, planner_markdown
):
    import json

    project = _r3_project(authenticated_client)
    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt: {
            "status": "completed",
            "output": json.dumps(_r3_payload(planner_markdown)),
        },
    )
    response = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={"project_id": project["id"], "prompt": _R3_PROMPT},
    )
    assert response.status_code == 201
    session = response.json()
    assert session["status"] == "failed"
    assert session["committed_task_ids"] == []
    assert (
        authenticated_client.get(f"/api/v1/projects/{project['id']}/tasks").json() == []
    )


def test_r6_mixed_row_plan_preview_exposes_no_partial_tasks(
    authenticated_client, monkeypatch
):
    import json

    project = _r3_project(authenticated_client)
    valid = "# Project: R3 Boundary\n\n## Task List\n" + _ROW
    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt: {
            "status": "completed",
            "output": json.dumps(_r3_payload(valid)),
        },
    )
    session = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={"project_id": project["id"], "prompt": _R3_PROMPT},
    ).json()
    mixed_rows = (
        "## Task List\n### TASK_START: First\nFirst body.\n"
        "- [ ] TASK_START: Second | Second body | order=2\n"
    )
    original_get_artifact_content = PlanningSessionService._get_artifact_content

    def malformed_preview(self, planning_session, artifact_type):
        if artifact_type == "planner_markdown":
            return mixed_rows
        return original_get_artifact_content(self, planning_session, artifact_type)

    monkeypatch.setattr(
        PlanningSessionService, "_get_artifact_content", malformed_preview
    )
    preview = authenticated_client.get(f"/api/v1/planning/sessions/{session['id']}")
    assert preview.status_code == 200
    assert preview.json()["tasks_preview"] == []


@pytest.mark.parametrize(
    "invalid_markdown",
    [
        pytest.param(
            "## Task List\n- [ ] TASK_START: First | Implement first\n"
            "### Second task\nImplement second.\n",
            id="mixed-task-syntax",
        ),
        pytest.param(
            "## Task List\n### Acceptance criteria:\n"
            "- [ ] User can log in\n- [ ] Tests pass\n",
            id="acceptance-only",
        ),
        pytest.param(
            "## Task List\n- [ ] TASK_START: Validate | Run checks | "
            "profile=review_only | stage=revieww\n",
            id="invalid-structural-metadata",
        ),
        pytest.param(
            "## Task List\n### TASK_START: First\nFirst body.\n"
            "- [ ] TASK_START: Second | Second body\n",
            id="mixed-task-start-row-forms",
        ),
    ],
)
def test_r5_rejected_boundaries_cannot_partially_commit(
    authenticated_client, monkeypatch, invalid_markdown
):
    import json

    project = _r3_project(authenticated_client)
    valid = "# Project: R3 Boundary\n\n## Task List\n" + _ROW
    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt: {
            "status": "completed",
            "output": json.dumps(_r3_payload(valid)),
        },
    )
    session = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={"project_id": project["id"], "prompt": _R3_PROMPT},
    ).json()
    assert session["status"] == "completed"
    commit = authenticated_client.post(
        f"/api/v1/planning/sessions/{session['id']}/commit",
        json={"planner_markdown": invalid_markdown},
    )
    assert commit.status_code == 422
    after = authenticated_client.get(f"/api/v1/planning/sessions/{session['id']}")
    assert after.json()["committed_task_ids"] == []
    assert after.json()["committed_at"] is None
    assert (
        authenticated_client.get(f"/api/v1/projects/{project['id']}/tasks").json() == []
    )


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            TL + "### Fix normalize\nFix the bug.\nAcceptance criteria:\n"
            "- `pytest tests/test_a.py` passes.\n- Do not modify tests/\n",
            id="T1_C02",
        ),
        pytest.param(
            TL + "### API task\nChange the API.\n```\n### not a task\n```\n"
            "Acceptance criteria:\n- `pytest tests/test_api.py` passes\n"
            "### UI task\nChange UI.\n",
            id="T2_M11",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: Leak | Acceptance criteria: "
            "`pytest tests/test_a.py` passes | order=1\n",
            id="T2_M18",
        ),
        pytest.param(
            TL
            + "### Fix normalize\nAcceptance criteria: `pytest tests/test_a.py` passes.\n",
            id="T1_C03",
        ),
        pytest.param(
            TL + "- Fix normalize | Acceptance criteria: `pytest tests/test_a.py`\n",
            id="R1_A01",
        ),
        pytest.param(
            TL + "### Fix: Acceptance criteria: `pytest tests/test_a.py`\n",
            id="R1_A02",
        ),
        pytest.param(
            TL + "### Task\nAcceptance criteria:\n- `pytest tests/a.py && true`\n",
            id="R1_A05",
        ),
        pytest.param(
            TL + "### Task\n\u00a0Acceptance criteria: `pytest tests/a.py`\n",
            id="R1_A07",
        ),
    ],
)
def test_r5_required_authority_corpus_remains_neutralized(markdown):
    tasks = _tasks(markdown)
    assert tasks
    assert _authority(tasks) == NO_AUTHORITY


# --- R7: structural-key completeness and effective Plan positions -----------


@pytest.mark.parametrize(
    "field",
    ["profile", "stage", "mode", "scope", "workflow_stage"],
)
def test_r7_standalone_structural_scope_key_without_equals_rejects_plan(field):
    result = _result(TL + f"- [ ] TASK_START: T | Body | {field}\n")
    assert (result.tasks, result.mode, result.rejection_reason) == (
        [],
        "rejected",
        "invalid_scope_metadata",
    )


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            TL + "- [ ] TASK_START: T | Body | profile=review_only | stage\n",
            id="valid-profile-then-incomplete-stage",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: T | Body | stage | profile=review_only\n",
            id="incomplete-stage-then-valid-profile",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: First | Body | order=1\n"
            + "- [ ] TASK_START: Second | Body | workflow_stage\n",
            id="incomplete-key-in-later-task",
        ),
        pytest.param(
            TL + "### TASK_START: Add endpoint profile\nImplement it.\n",
            id="incomplete-heading-title-key",
        ),
        pytest.param(
            TL + "### TASK_START: Add endpoint: Implement it | stage\n",
            id="incomplete-inline-heading-key",
        ),
        pytest.param(
            TL + "### Add endpoint\nprofile\n\nImplement it.\n",
            id="incomplete-metadata-only-heading-paragraph",
        ),
        pytest.param(
            TL + "- Normalize | Preserve body | workflow_stage\n",
            id="incomplete-m3-terminal-pipe-key",
        ),
    ],
)
def test_r7_incomplete_structural_scope_keys_reject_complete_plan(markdown):
    result = _result(markdown)
    assert (result.tasks, result.mode, result.rejection_reason) == (
        [],
        "rejected",
        "invalid_scope_metadata",
    )


def test_r7_incomplete_metadata_precedes_conflict_and_position_reason():
    markdown = (
        TL
        + "- [ ] TASK_START: First | Body | order=2 | profile=test_only | mode=debug_only\n"
        + "- [ ] TASK_START: Second | Body | order=2 | stage\n"
    )
    result = _result(markdown)
    assert (result.tasks, result.mode, result.rejection_reason) == (
        [],
        "rejected",
        "invalid_scope_metadata",
    )


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            TL + "- [ ] TASK_START: T | The profile field is missing.\n",
            id="ordinary-prose",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: T | Acceptance criteria: Explain why stage is required.\n",
            id="acceptance-criteria",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: T | Example `profile` and `stage`.\n",
            id="inline-code",
        ),
        pytest.param(
            TL
            + "### T\nExplain the profile field.\n\n"
            + '```python\nprofile = "review_only"\n```\n',
            id="fenced-code",
        ),
        pytest.param(
            TL + "### T\nImplement.\n\n- Notes:\n  - mode is discussed here.\n",
            id="nested-bullet",
        ),
        pytest.param(
            TL + "### T\nImplement.\n\n> The quoted example omits stage.\n",
            id="quoted-example",
        ),
        pytest.param(
            TL + "- Normalize: The prose says profile and stage are required.\n",
            id="m3-colon-prose",
        ),
    ],
)
def test_r7_known_metadata_words_in_ordinary_content_remain_data(markdown):
    result = _result(markdown)
    assert result.rejection_reason is None
    assert result.mode in {"task_start", "heading", "legacy"}
    assert len(result.tasks) == 1


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            TL
            + "- [ ] TASK_START: First | First task | order=1 | P1\n"
            + "- [ ] TASK_START: Second | Depends on First | order=1 | P0\n",
            id="duplicate-position-one-different-priority",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: First | First task | order=2 | P1\n"
            + "- [ ] TASK_START: Second | Depends on First | order=2 | P0\n",
            id="duplicate-position-two",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: First | First task | order=1\n"
            + "- [ ] TASK_START: Second | Second task | order=2\n"
            + "- [ ] TASK_START: Third | Third task | order=2\n",
            id="duplicate-in-later-task",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: First | First task | order=2\n"
            + "- [ ] TASK_START: Second | Fallback to row index 2\n",
            id="explicit-fallback-collision",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: First | First task | order=2\n"
            + "- [ ] TASK_START: Second | Zero falls back to row index 2 | order=0\n",
            id="order-zero-fallback-collision",
        ),
    ],
)
def test_r7_duplicate_effective_positions_reject_complete_plan(markdown):
    result = _result(markdown)
    assert (result.tasks, result.mode, result.rejection_reason) == (
        [],
        "rejected",
        "invalid_task_order",
    )


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param(
            TL
            + "### First order=1\nFirst body.\n"
            + "### Second order=1\nSecond body.\n",
            id="heading-mode-duplicate",
        ),
        pytest.param(
            TL
            + "- First | First body | order=1\n"
            + "- Second | Second body | order=1\n",
            id="legacy-mode-duplicate",
        ),
    ],
)
def test_r7_duplicate_effective_positions_rejected_in_heading_and_legacy_modes(
    markdown,
):
    result = _result(markdown)
    assert (result.tasks, result.mode, result.rejection_reason) == (
        [],
        "rejected",
        "invalid_task_order",
    )


@pytest.mark.parametrize(
    "markdown,expected_mode,expected_titles",
    [
        pytest.param(
            TL + "### First order=2\nBody.\n### Second order=1\nBody.\n",
            "heading",
            ["First order=2", "Second order=1"],
            id="valid-heading-mode-explicit-order",
        ),
        pytest.param(
            TL + "- First | Body | order=2\n- Second | Body | order=1\n",
            "legacy",
            ["First", "Second"],
            id="valid-legacy-mode-explicit-order",
        ),
    ],
)
def test_r7_unique_positions_preserve_heading_and_legacy_source_order(
    markdown, expected_mode, expected_titles
):
    result = _result(markdown)
    assert result.mode == expected_mode
    assert result.rejection_reason is None
    assert [task.title for task in result.tasks] == expected_titles
    assert [task.plan_position for task in result.tasks] == [2, 1]


@pytest.mark.parametrize(
    "markdown,expected_positions",
    [
        pytest.param(
            TL
            + "- [ ] TASK_START: First | One | order=1\n"
            + "- [ ] TASK_START: Second | Two | order=2\n",
            [1, 2],
            id="unique-explicit",
        ),
        pytest.param(
            TL + "- [ ] TASK_START: First | One\n" + "- [ ] TASK_START: Second | Two\n",
            [1, 2],
            id="implicit-fallback",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: Second | Two | order=2\n"
            + "- [ ] TASK_START: Third | Three | order=3\n",
            [2, 3],
            id="leading-gap-policy-preserves",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: First | One | order=1\n"
            + "- [ ] TASK_START: Third | Three | order=3\n",
            [1, 3],
            id="interior-gap-preserves",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: Phase 1 discovery | Inspect\n"
            + "- [ ] TASK_START: Phase 1 implementation | Build\n",
            [101, 102],
            id="phase-derived-position",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: Source first | First | order=2\n"
            + "- [ ] TASK_START: Source second | Second | order=1\n",
            [2, 1],
            id="explicit-source-order-divergence",
        ),
        pytest.param(
            TL
            + "- [ ] TASK_START: First | Zero fallback | order=0\n"
            + "- [ ] TASK_START: Second | Row two | order=2\n",
            [0, 2],
            id="order-zero-without-effective-collision",
        ),
    ],
)
def test_r7_unique_effective_position_contract_is_preserved(
    markdown, expected_positions
):
    result = _result(markdown)
    assert result.mode == "task_start"
    assert result.rejection_reason is None
    assert [task.plan_position for task in result.tasks] == expected_positions


def test_r7_actual_planner_generator_positions_remain_valid():
    markdown = PlannerService.generate_markdown(
        "Add a backend API endpoint and database model", "R7 Planner"
    )
    result = _result(markdown)
    assert result.rejection_reason is None
    assert [task.plan_position for task in result.tasks] == list(
        range(1, len(result.tasks) + 1)
    )


def test_r7_actual_five_row_recovery_generator_positions_remain_valid():
    service = PlanningSessionService.__new__(PlanningSessionService)
    artifacts = service._build_replan_recovery_artifacts(
        SimpleNamespace(prompt="Failed task: Add API endpoint. Failure: test failed."),
        SimpleNamespace(name="R7 Recovery"),
    )
    result = _result(artifacts["planner_markdown"])
    assert result.rejection_reason is None
    assert [task.plan_position for task in result.tasks] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    "planner_markdown",
    [
        pytest.param(
            "# Project: R7\n\n## Task List\n"
            "- [ ] TASK_START: Validate | Run checks | profile=review_only | stage\n",
            id="incomplete-structural-metadata",
        ),
        pytest.param(
            "# Project: R7\n\n## Task List\n"
            "- [ ] TASK_START: First | One | order=1\n"
            "- [ ] TASK_START: Second | Two | order=1\n",
            id="duplicate-effective-position",
        ),
    ],
)
def test_r7_synthesis_rejection_creates_no_committed_tasks(
    authenticated_client, monkeypatch, planner_markdown
):
    import json

    project = _r3_project(authenticated_client)
    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt: {
            "status": "completed",
            "output": json.dumps(_r3_payload(planner_markdown)),
        },
    )
    response = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={"project_id": project["id"], "prompt": _R3_PROMPT},
    )
    assert response.status_code == 201
    session = response.json()
    assert session["status"] == "failed"
    assert session["committed_task_ids"] == []
    assert session["finalized_plan_id"] is None
    assert (
        authenticated_client.get(f"/api/v1/projects/{project['id']}/tasks").json() == []
    )


@pytest.mark.parametrize(
    "invalid_markdown",
    [
        pytest.param(
            "## Task List\n- [ ] TASK_START: T | Body | profile=review_only | stage\n",
            id="incomplete-structural-metadata",
        ),
        pytest.param(
            "## Task List\n- [ ] TASK_START: First | One | order=2\n"
            "- [ ] TASK_START: Second | Two | order=0\n",
            id="duplicate-effective-position-through-zero-fallback",
        ),
    ],
)
def test_r7_preview_and_commit_reject_without_partial_tasks(
    authenticated_client, monkeypatch, invalid_markdown
):
    import json

    project = _r3_project(authenticated_client)
    valid = "# Project: R7\n\n## Task List\n" + _ROW
    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt: {
            "status": "completed",
            "output": json.dumps(_r3_payload(valid)),
        },
    )
    session = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={"project_id": project["id"], "prompt": _R3_PROMPT},
    ).json()
    assert session["status"] == "completed"

    original_get_artifact_content = PlanningSessionService._get_artifact_content

    def invalid_preview(self, planning_session, artifact_type):
        if artifact_type == "planner_markdown":
            return invalid_markdown
        return original_get_artifact_content(self, planning_session, artifact_type)

    monkeypatch.setattr(
        PlanningSessionService, "_get_artifact_content", invalid_preview
    )
    preview = authenticated_client.get(f"/api/v1/planning/sessions/{session['id']}")
    assert preview.status_code == 200
    assert preview.json()["tasks_preview"] == []

    commit = authenticated_client.post(
        f"/api/v1/planning/sessions/{session['id']}/commit",
        json={"planner_markdown": invalid_markdown},
    )
    assert commit.status_code == 422
    after = authenticated_client.get(f"/api/v1/planning/sessions/{session['id']}")
    assert after.json()["committed_task_ids"] == []
    assert after.json()["committed_at"] is None
    assert after.json()["finalized_plan_id"] is None
    assert (
        authenticated_client.get(f"/api/v1/projects/{project['id']}/tasks").json() == []
    )


def test_r7_leading_gap_bootstrap_policy_is_preserved_as_unverified():
    from app.services.orchestration.phases.planning_task1_bootstrap import (
        is_first_ordered_task,
    )

    result = _result(
        TL
        + "- [ ] TASK_START: Second | Two | order=2\n"
        + "- [ ] TASK_START: Third | Three | order=3\n"
    )
    assert result.rejection_reason is None
    assert [task.plan_position for task in result.tasks] == [2, 3]
    assert [is_first_ordered_task(task) for task in result.tasks] == [False, False]


# --- R3: rejected plans cannot be committed -------------------------------------


def _r3_project(client):
    response = client.post(
        "/api/v1/projects",
        json={"name": "R3 Boundary", "description": "Existing app with API"},
    )
    assert response.status_code == 201
    return response.json()


_R3_MIXED_MARKDOWN = (
    "# Project: R3 Boundary\n\n## Task List\n" + _ROW + "- [ ] Second: no prefix\n"
)


_R3_PROMPT = (
    "Add JWT authentication to the FastAPI backend and React frontend, including "
    "token refresh, protected routes, and regression tests."
)


def _r3_payload(markdown):
    return {
        "requirements": "# Requirements\n\n- Support resumable planning",
        "design": "# Design\n\n- Use persisted planning sessions",
        "implementation_plan": "# Implementation Plan\n\n1. Add models",
        "planner_markdown": markdown,
    }


def test_r3_mixed_syntax_synthesis_fails_without_tasks(
    authenticated_client, monkeypatch
):
    import json

    project = _r3_project(authenticated_client)
    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt: {
            "status": "completed",
            "output": json.dumps(_r3_payload(_R3_MIXED_MARKDOWN)),
        },
    )
    session = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={"project_id": project["id"], "prompt": _R3_PROMPT},
    )
    assert session.status_code == 201
    assert session.json()["status"] == "failed"
    assert session.json()["committed_task_ids"] == []


def test_r3_mixed_syntax_commit_is_rejected_without_partial_tasks(
    authenticated_client, monkeypatch
):
    import json

    project = _r3_project(authenticated_client)
    valid = "# Project: R3 Boundary\n\n## Task List\n" + _ROW
    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt: {
            "status": "completed",
            "output": json.dumps(_r3_payload(valid)),
        },
    )
    session = authenticated_client.post(
        "/api/v1/planning/sessions",
        json={"project_id": project["id"], "prompt": _R3_PROMPT},
    ).json()
    assert session["status"] == "completed"
    commit = authenticated_client.post(
        f"/api/v1/planning/sessions/{session['id']}/commit",
        json={"planner_markdown": _R3_MIXED_MARKDOWN},
    )
    assert commit.status_code == 422
    after = authenticated_client.get(f"/api/v1/planning/sessions/{session['id']}")
    assert after.json()["committed_task_ids"] == []
    assert after.json()["committed_at"] is None
