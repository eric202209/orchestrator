"""Bounded Phase 29F-3A execution seam architecture checks."""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
EXECUTION_ROOT = REPO_ROOT / "app" / "services" / "execution"
EXECUTION_MODULES = tuple(
    sorted(
        ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)
        for path in EXECUTION_ROOT.glob("*.py")
        if path.name != "__init__.py"
    )
)
RUNTIME_SERVICE = "app.services.execution.execution_task_runtime_execution_service"
RUNTIME_INTEGRITY = "app.services.execution.runtime_integrity"
CANDIDATE_CONTENT = "app.services.execution.candidate_content"
TRANSITION_SERVICE = "app.services.execution.execution_task_transition_service"
WORKER = "app.tasks.worker"
PLANNING_SESSION = "app.services.planning.planning_session_service"
PLANNING_TASK = "app.tasks.planning_tasks"
BRIEF_STAGE = "app.services.planning.planning_brief_stage"
STRUCTURED_STAGE = "app.services.planning.structured_task_plan_stage"

DEFERRED_EXECUTION_SCCS = frozenset()
FORBIDDEN_ORCHESTRATION_PREFIXES = (
    "app.services.orchestration.coordinators",
    "app.services.orchestration.phases",
    "app.services.orchestration.recovery",
    "app.services.orchestration.state",
    "app.services.orchestration.stage_engine",
)
ALLOWED_REVIEW_CONTRACT_PREFIXES = (
    "app.services.planning.operator_review",
    "app.services.planning.validation_contract",
    "app.services.planning.structured_task_plan",
)


def _path(module_name: str) -> Path:
    return REPO_ROOT / (module_name.replace(".", "/") + ".py")


def _tree(module_name: str) -> ast.Module:
    return ast.parse(_path(module_name).read_text(encoding="utf-8"))


def _imports(module_name: str) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(_tree(module_name)):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def _module_graph() -> dict[str, set[str]]:
    modules = set(EXECUTION_MODULES)
    return {module: _imports(module) & modules for module in sorted(modules)}


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


def test_execution_services_do_not_import_concrete_task_modules() -> None:
    for module in EXECUTION_MODULES:
        assert not any(
            imported == "app.tasks" or imported.startswith("app.tasks.")
            for imported in _imports(module)
        ), module


def test_execution_domain_has_no_concrete_orchestration_dependency() -> None:
    for module in EXECUTION_MODULES:
        assert not any(
            imported.startswith(prefix)
            for imported in _imports(module)
            for prefix in FORBIDDEN_ORCHESTRATION_PREFIXES
        ), module


def test_runtime_integrity_seam_removes_candidate_runtime_cycle() -> None:
    assert RUNTIME_SERVICE not in _imports(CANDIDATE_CONTENT)
    assert CANDIDATE_CONTENT not in _imports(RUNTIME_INTEGRITY)
    assert RUNTIME_SERVICE not in _imports(RUNTIME_INTEGRITY)
    assert "verify_attempt_outcome_integrity" in _imports(RUNTIME_INTEGRITY) or (
        "verify_attempt_outcome_integrity" in _path(RUNTIME_INTEGRITY).read_text()
    )
    graph = _module_graph()
    assert _strongly_connected_components(graph) == DEFERRED_EXECUTION_SCCS


def test_runtime_service_keeps_one_compatibility_entry_point() -> None:
    runtime_tree = _tree(RUNTIME_SERVICE)
    integrity_tree = _tree(RUNTIME_INTEGRITY)
    assert (
        sum(
            isinstance(node, ast.FunctionDef)
            and node.name == "verify_attempt_outcome_integrity"
            for node in ast.walk(runtime_tree)
        )
        == 1
    )
    assert (
        sum(
            isinstance(node, ast.FunctionDef)
            and node.name == "verify_attempt_outcome_integrity"
            for node in ast.walk(integrity_tree)
        )
        == 1
    )
    assert (
        sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ExecutionTaskTransition"
            for node in ast.walk(_tree(TRANSITION_SERVICE))
        )
        == 1
    )
    assert (
        sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ExecutionTaskTransition"
            for module in EXECUTION_MODULES
            for node in ast.walk(_tree(module))
        )
        == 1
    )


def test_task_adapter_owns_celery_registration() -> None:
    worker_tree = _tree(WORKER)
    receive_functions = [
        node
        for node in worker_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "receive_execution_task_dispatch"
    ]
    assert len(receive_functions) == 1
    assert any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "task"
        for decorator in receive_functions[0].decorator_list
    )
    assert "app.services.execution.execution_task_dispatch_service" not in _imports(
        WORKER
    )


def test_review_contract_imports_are_execution_safe() -> None:
    contract_modules = (
        "app.services.execution.changeset",
        "app.services.execution.candidate_content",
        "app.services.execution.candidate_evidence",
        "app.services.execution.controlled_apply",
        "app.services.execution.execution_eligibility_service",
        "app.services.execution.execution_task_dispatch_service",
        "app.services.execution.execution_task_recovery_service",
        "app.services.execution.execution_task_runtime_execution_service",
        "app.services.execution.execution_task_transition_service",
        "app.services.execution.validation_run",
        "app.services.execution.workspace_authority",
    )
    for module in contract_modules:
        imports = _imports(module)
        assert not any(
            imported.startswith("app.services.orchestration") for imported in imports
        ), module
        assert all(
            not imported.startswith("app.services.planning")
            or imported.startswith(ALLOWED_REVIEW_CONTRACT_PREFIXES)
            or imported
            in {
                "app.services.planning.protocol_persistence",
                "app.services.planning.structured_task_plan",
            }
            for imported in imports
        ), module


def test_existing_planning_cycles_and_compatibility_shims_remain_stable() -> None:
    assert PLANNING_TASK not in _imports(PLANNING_SESSION)
    assert "app.services.planning.planning_brief_stage_support" in _imports(BRIEF_STAGE)
    assert "app.services.planning.structured_task_plan_stage_support" in _imports(
        STRUCTURED_STAGE
    )
    assert "app.services.orchestration.planning.planning_brief_stage" in _imports(
        BRIEF_STAGE
    )
    assert "app.services.orchestration.planning.structured_task_plan_stage" in _imports(
        STRUCTURED_STAGE
    )
    for module in (BRIEF_STAGE, STRUCTURED_STAGE):
        assert not [
            node
            for node in _tree(module).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
