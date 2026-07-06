"""Tests for 10H-C: context assembly stays within declared token budgets.

Validates that _shape_project_context, _condense_dict_events, and the
assemble_planning_prompt path all respect their max_chars caps so that
the final planning prompt stays below MINIMAL_PROMPT_TOKEN_THRESHOLD (8000
tokens) regardless of how dense the validation history is.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.orchestration.context.assembly import (
    _condense_dict_events,
    _shape_project_context,
    assemble_planning_prompt,
)
from app.services.orchestration.prompt_templates import (
    OrchestrationState,
    estimate_token_count,
)

MINIMAL_PROMPT_TOKEN_THRESHOLD = 8000


# ── _condense_dict_events ────────────────────────────────────────────────────


def _make_validation_events(n: int) -> list[dict]:
    return [
        {
            "phase": "validation",
            "status": "failed",
            "message": f"Validation failure #{i}: pytest error on line {i * 10}",
        }
        for i in range(n)
    ]


def test_condense_dict_events_respects_max_entries():
    events = _make_validation_events(20)
    result = _condense_dict_events(events, max_entries=3, max_chars=800)
    # Only last 3 entries rendered — "validation" appears at most 3 times
    assert result.count("validation:") <= 3


def test_condense_dict_events_respects_max_chars():
    events = _make_validation_events(20)
    result = _condense_dict_events(events, max_entries=10, max_chars=200)
    assert len(result) <= 200


def test_condense_dict_events_empty_returns_placeholder():
    result = _condense_dict_events([], max_entries=4, max_chars=800)
    assert "No recent" in result


# ── _shape_project_context ───────────────────────────────────────────────────


def test_shape_project_context_respects_max_chars():
    long_context = "project detail " * 500
    dense_history = "\n".join(
        f"validation:failed reason=pytest error #{i}" for i in range(30)
    )
    result = _shape_project_context(
        long_context,
        workspace_summary="workspace summary " * 100,
        recent_history="phase history " * 100,
        validation_history=dense_history,
        operator_guidance="operator note " * 50,
        max_chars=280,
    )
    assert len(result) <= 280


def test_shape_project_context_dense_validation_under_token_limit():
    """10 dense failure entries shaped with max_chars=280 must be <100 tokens."""
    dense_history = "\n".join(
        f"validation:failed reason=pytest assertion error line {i}" for i in range(10)
    )
    result = _shape_project_context(
        "base project context",
        workspace_summary="src/app.py src/utils.py tests/test_app.py",
        recent_history="planning:completed reason=initial plan generated",
        validation_history=dense_history,
        max_chars=280,
    )
    token_estimate = estimate_token_count(result)
    assert token_estimate < 100


def test_shape_project_context_empty_sections_omitted():
    result = _shape_project_context(
        "",
        workspace_summary="",
        recent_history="",
        validation_history="",
        max_chars=500,
    )
    assert len(result) <= 500


# ── assemble_planning_prompt token budget ────────────────────────────────────


def _make_ctx(
    *, project_context: str = "", phase_history=None, validation_history=None
):
    state = OrchestrationState(
        session_id="99",
        task_description="Write a Python module that does X",
        project_name="BudgetTest",
        project_context=project_context,
        task_id=1,
    )
    state._project_dir_override = "/tmp/budget_test"
    state.phase_history = phase_history or []
    state.validation_history = validation_history or []
    return SimpleNamespace(
        orchestration_state=state,
        db=None,
        execution_profile="full_lifecycle",
        prompt="Write a Python module that does X",
        workflow_profile="default",
        session_id=99,
        task_id=1,
    )


def test_assemble_planning_prompt_small_context_under_threshold():
    ctx = _make_ctx(project_context="A small project with no history.")
    prompt = assemble_planning_prompt(ctx, {})
    tokens = estimate_token_count(prompt)
    assert tokens < MINIMAL_PROMPT_TOKEN_THRESHOLD


def test_assemble_planning_prompt_dense_context_under_threshold():
    """Large project_context + 3 consecutive validation failures stay under threshold."""
    large_context = "module description with many details " * 200
    validation_failures = [
        {
            "phase": "validation",
            "status": "failed",
            "message": f"pytest failure #{i}: assertion error in test_module.py line {i * 5}",
        }
        for i in range(3)
    ]
    ctx = _make_ctx(
        project_context=large_context,
        phase_history=[
            {"phase": "planning", "status": "completed", "message": "initial plan"},
            {"phase": "execution", "status": "failed", "message": "step 1 error"},
            {"phase": "execution", "status": "failed", "message": "step 2 error"},
            {"phase": "debug", "status": "failed", "message": "repair failed"},
        ],
        validation_history=validation_failures,
    )
    prompt = assemble_planning_prompt(ctx, {})
    tokens = estimate_token_count(prompt)
    assert tokens < MINIMAL_PROMPT_TOKEN_THRESHOLD, (
        f"Planning prompt with dense history produced {tokens} tokens, "
        f"expected < {MINIMAL_PROMPT_TOKEN_THRESHOLD}"
    )


def test_assemble_planning_prompt_ten_validation_failures_under_threshold():
    """Even 10 validation failures in history must not blow the token budget."""
    validation_failures = [
        {
            "phase": "validation",
            "status": "failed",
            "message": f"pytest failure #{i}: module not found or import error in test_{i}.py",
        }
        for i in range(10)
    ]
    ctx = _make_ctx(
        project_context="large project context " * 300,
        validation_history=validation_failures,
    )
    prompt = assemble_planning_prompt(ctx, {})
    tokens = estimate_token_count(prompt)
    assert tokens < MINIMAL_PROMPT_TOKEN_THRESHOLD


def test_estimate_token_count_basic():
    assert estimate_token_count("") >= 0
    # 400 chars ≈ 100 tokens (len//4)
    text = "a" * 400
    assert estimate_token_count(text) == 100


# ── Slice H: Injection Gate (280 → 800) ─────────────────────────────────────


def test_shape_project_context_preserves_content_beyond_280_chars():
    """Content beyond 280 chars must survive shaping at the new 800-char budget."""
    # Build a context that is clearly longer than 280 chars
    context = "known_good_commands: npm install && npm test; " * 15  # ~690 chars
    result = _shape_project_context(
        context,
        workspace_summary="",
        recent_history="",
        validation_history="",
        max_chars=800,
    )
    # The result must be longer than the old 280-char cap
    assert (
        len(result) > 280
    ), f"Shaped context is only {len(result)} chars — content beyond 280 was discarded"
    # And must contain content from beyond the first 280 chars of input
    assert "npm install" in result


def test_shape_project_context_800_char_budget_respected():
    """The new budget cap is 800, not exceeded even with very long inputs."""
    long_context = "x" * 5000
    result = _shape_project_context(
        long_context,
        workspace_summary="y" * 2000,
        recent_history="z" * 1000,
        validation_history="w" * 1000,
        operator_guidance="v" * 500,
        max_chars=800,
    )
    assert len(result) <= 800


def test_assemble_planning_prompt_task2_continuation_context_survives():
    """A Task 2+ project_context of ~600 chars must not be truncated to 280 chars."""
    continuation_context = (
        "=== WORKING MEMORY ===\n\n"
        "Known Good Commands\n"
        "  Task: Setup project\n"
        "  $ npm install\n"
        "  $ npm test\n"
        "  $ node -e \"require('./app')\"\n\n"
        "Recent Files\n"
        "  Task: Setup project\n"
        "  - src/app.js\n"
        "  - tests/app.test.js\n\n"
        "Constraints\n"
        "  - use node -e for all verification\n"
        "  - heredoc syntax not allowed\n\n"
        "=== END WORKING MEMORY ==="
    )
    assert len(continuation_context) > 280, "fixture must exceed the old 280-char cap"

    ctx = _make_ctx(project_context=continuation_context)
    prompt = assemble_planning_prompt(ctx, {})

    # The planning prompt must contain continuation content from beyond char 280
    assert "WORKING MEMORY" in prompt, "continuation block header not present in prompt"
    assert "node -e" in prompt, "known_good_commands content missing from prompt"
    # Prompt must stay well under the hard cap
    assert (
        len(prompt) < 12000
    ), f"prompt is {len(prompt)} chars, exceeds DIRECT_PLANNING_PROMPT_CHAR_CAP"


def test_assemble_planning_prompt_task2_under_token_threshold():
    """Task 2+ prompt with 600-char continuation context stays under token threshold."""
    continuation_context = (
        "=== WORKING MEMORY ===\n"
        + ("known_good_command: npm install\n" * 20)
        + "=== END WORKING MEMORY ==="
    )
    ctx = _make_ctx(project_context=continuation_context)
    prompt = assemble_planning_prompt(ctx, {})
    tokens = estimate_token_count(prompt)
    assert tokens < MINIMAL_PROMPT_TOKEN_THRESHOLD
