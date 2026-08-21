from types import SimpleNamespace

from app.services.orchestration.phases.planning_support import (
    MINIMAL_PROMPT_TOKEN_THRESHOLD,
    select_minimal_prompt_first_strategy,
)


def _context(
    task: str = "Update the current implementation", project_context: str = ""
):
    return SimpleNamespace(
        prompt=task,
        orchestration_state=SimpleNamespace(project_context=project_context),
        logger=SimpleNamespace(info=lambda *args, **kwargs: None),
    )


def _select(ctx, *, existing: bool = False, tokens: int = 100, compress=None):
    return select_minimal_prompt_first_strategy(
        ctx=ctx,
        workspace_review={"has_existing_files": existing},
        planning_prompt_tokens=tokens,
        compress_project_context=compress or (lambda state: "compressed"),
    )


def test_empty_or_new_workspace_does_not_force_minimal():
    assert _select(_context(), existing=False) == (False, None)


def test_small_existing_workspace_keeps_full_selection_when_no_other_trigger():
    assert _select(_context(), existing=True) == (False, None)


def test_explicit_existing_file_task_keeps_full_selection():
    ctx = _context("Change the timestamp comparison in app/tasks/maintenance.py")
    assert _select(ctx, existing=True) == (False, None)


def test_cross_module_architecture_task_keeps_full_selection():
    ctx = _context("Coordinate API, service, and database behavior for one change")
    assert _select(ctx, existing=True) == (False, None)


def test_new_file_task_keeps_full_selection_without_another_trigger():
    ctx = _context("Create the authorized adapter module and its focused test")
    assert _select(ctx, existing=True) == (False, None)


def test_minimal_marker_retains_task_heuristic():
    ctx = _context("Review the current project structure before changing it")
    assert _select(ctx, existing=True) == (True, "planner_heuristic")


def test_dense_prompt_retains_token_guard_and_compression():
    ctx = _context(project_context="x" * 100)
    compressed = []

    def compress_project_context(state):
        compressed.append(state.project_context)
        return "compressed"

    assert _select(
        ctx,
        existing=True,
        tokens=MINIMAL_PROMPT_TOKEN_THRESHOLD + 1,
        compress=compress_project_context,
    ) == (True, "dense_planning_context")
    assert compressed == ["x" * 100]
    assert ctx.orchestration_state.project_context == "compressed"
