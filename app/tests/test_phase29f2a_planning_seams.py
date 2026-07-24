"""Bounded architecture checks for Phase 29F-2A planning seams."""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PLANNING_ROOT = REPO_ROOT / "app" / "services" / "planning"
TARGET_FILES = {
    **{
        ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts): path
        for path in PLANNING_ROOT.glob("*.py")
    },
    "app.services.orchestration.stage_engine": REPO_ROOT
    / "app/services/orchestration/stage_engine.py",
    "app.services.orchestration.events": REPO_ROOT
    / "app/services/orchestration/events/__init__.py",
    "app.services.orchestration.phases.planning_candidate_recovery": REPO_ROOT
    / "app/services/orchestration/phases/planning_candidate_recovery.py",
    "app.services.orchestration.planning.planning_brief_stage": REPO_ROOT
    / "app/services/orchestration/planning/planning_brief_stage.py",
    "app.services.orchestration.planning.structured_task_plan_stage": REPO_ROOT
    / "app/services/orchestration/planning/structured_task_plan_stage.py",
    "app.services.orchestration.planning.stage_sequence": REPO_ROOT
    / "app/services/orchestration/planning/stage_sequence.py",
    "app.tasks.planning_tasks": REPO_ROOT / "app/tasks/planning_tasks.py",
    "app.tasks.planning_dispatch": REPO_ROOT / "app/tasks/planning_dispatch.py",
}

PLANNING_SESSION = "app.services.planning.planning_session_service"
PLANNING_TASK = "app.tasks.planning_tasks"
PLANNING_BRIEF_STAGE = "app.services.planning.planning_brief_stage"
STRUCTURED_TASK_STAGE = "app.services.planning.structured_task_plan_stage"
OPERATOR_REVIEW = "app.services.planning.operator_review"
OPERATOR_REVIEW_PERSISTENCE = "app.services.planning.operator_review_persistence"
PROTOCOL_PERSISTENCE = "app.services.planning.protocol_persistence"

EXPECTED_PLANNING_SCCS = frozenset(
    {
        frozenset({OPERATOR_REVIEW_PERSISTENCE, PROTOCOL_PERSISTENCE}),
    }
)


def _tree(module_name: str) -> ast.Module:
    return ast.parse(TARGET_FILES[module_name].read_text(encoding="utf-8"))


def _imports(tree: ast.Module) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def _module_graph() -> dict[str, set[str]]:
    modules = set(TARGET_FILES)
    return {module: _imports(_tree(module)) & modules for module in sorted(modules)}


def _strongly_connected_components(
    graph: dict[str, set[str]],
) -> frozenset[frozenset[str]]:
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: set[frozenset[str]] = set()

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for successor in graph[node]:
            if successor not in indices:
                visit(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[successor])
        if lowlinks[node] != indices[node]:
            return
        component: set[str] = set()
        while True:
            successor = stack.pop()
            on_stack.remove(successor)
            component.add(successor)
            if successor == node:
                break
        if len(component) > 1:
            components.add(frozenset(component))

    for node in graph:
        if node not in indices:
            visit(node)
    return frozenset(components)


def _class_names(module_name: str) -> set[str]:
    return {
        node.name
        for node in ast.walk(_tree(module_name))
        if isinstance(node, ast.ClassDef)
    }


def test_planning_session_has_no_task_module_import() -> None:
    assert PLANNING_TASK not in _imports(_tree(PLANNING_SESSION))
    assert "app.tasks" not in _imports(_tree(PLANNING_SESSION))


def test_planning_stage_contract_is_engine_independent() -> None:
    assert "app.services.orchestration.stage_engine" not in _imports(
        _tree("app.services.planning.stage_contract")
    )
    for module in (PLANNING_BRIEF_STAGE, STRUCTURED_TASK_STAGE):
        assert "app.services.orchestration.stage_engine" not in _imports(_tree(module))


def test_planning_domain_modules_do_not_import_orchestration_implementations() -> None:
    domain_modules = (
        "app.services.planning.candidate_recovery",
        "app.services.planning.planning_brief_stage_support",
        "app.services.planning.structured_task_plan_stage_support",
        OPERATOR_REVIEW,
        OPERATOR_REVIEW_PERSISTENCE,
        PROTOCOL_PERSISTENCE,
    )
    forbidden_prefixes = (
        "app.services.orchestration.coordinators",
        "app.services.orchestration.phases",
        "app.services.orchestration.recovery",
        "app.services.orchestration.events",
        "app.services.orchestration.state",
        "app.services.orchestration.stage_engine",
    )
    for module in domain_modules:
        assert not any(
            imported.startswith(prefix)
            for imported in _imports(_tree(module))
            for prefix in forbidden_prefixes
        ), module


def test_prepared_seams_have_one_implementation_and_compatibility_exports() -> None:
    assert "StageDefinition" in _class_names("app.services.planning.stage_contract")
    assert "StageDefinition" not in _class_names(
        "app.services.orchestration.stage_engine"
    )
    assert "PlanningTaskDispatcher" in _class_names(
        "app.services.planning.planning_dispatch"
    )
    assert "PlanningTaskDispatcher" not in _class_names("app.tasks.planning_tasks")


def test_planning_stage_scc_is_removed_and_persistence_is_the_only_tolerated_scc() -> (
    None
):
    graph = _module_graph()
    components = _strongly_connected_components(graph)
    assert frozenset({PLANNING_SESSION, PLANNING_TASK}) not in components
    assert components == EXPECTED_PLANNING_SCCS
