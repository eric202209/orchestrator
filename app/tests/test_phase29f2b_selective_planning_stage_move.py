"""Focused Phase 29F-2B architecture and compatibility checks."""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PLANNING_BRIEF_SUPPORT = "app.services.planning.planning_brief_stage_support"
STRUCTURED_SUPPORT = "app.services.planning.structured_task_plan_stage_support"
BRIEF_ADAPTER = "app.services.orchestration.planning.planning_brief_stage"
STRUCTURED_ADAPTER = "app.services.orchestration.planning.structured_task_plan_stage"
STAGE_SEQUENCE = "app.services.orchestration.planning.stage_sequence"


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


def test_canonical_adapters_are_one_way_and_support_stays_planning_owned():
    brief_imports = _imports(BRIEF_ADAPTER)
    structured_imports = _imports(STRUCTURED_ADAPTER)
    assert STRUCTURED_ADAPTER not in brief_imports
    assert BRIEF_ADAPTER not in structured_imports
    assert PLANNING_BRIEF_SUPPORT in brief_imports
    assert STRUCTURED_SUPPORT in structured_imports
    for module_name in (PLANNING_BRIEF_SUPPORT, STRUCTURED_SUPPORT):
        assert not any(
            imported.startswith("app.services.orchestration")
            for imported in _imports(module_name)
        ), module_name
    assert {BRIEF_ADAPTER, STRUCTURED_ADAPTER} <= _imports(STAGE_SEQUENCE)


def test_compatibility_modules_have_no_implementation_bodies():
    for module_name in (
        "app.services.planning.planning_brief_stage",
        "app.services.planning.structured_task_plan_stage",
    ):
        assert not [
            node
            for node in _tree(module_name).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ], module_name


def test_each_stage_class_has_one_canonical_implementation():
    brief_class_modules = (BRIEF_ADAPTER, "app.services.planning.planning_brief_stage")
    task_class_modules = (
        STRUCTURED_ADAPTER,
        "app.services.planning.structured_task_plan_stage",
    )
    assert (
        sum(
            node.name == "PlanningBriefStage"
            for module_name in brief_class_modules
            for node in ast.walk(_tree(module_name))
            if isinstance(node, ast.ClassDef)
        )
        == 1
    )
    assert (
        sum(
            node.name == "StructuredTaskPlanStage"
            for module_name in task_class_modules
            for node in ast.walk(_tree(module_name))
            if isinstance(node, ast.ClassDef)
        )
        == 1
    )


def test_old_and_new_paths_preserve_stage_and_helper_identity():
    from app.services.orchestration.planning import planning_brief_stage as new_brief
    from app.services.orchestration.planning import (
        structured_task_plan_stage as new_task,
    )
    from app.services.orchestration.planning.stage_sequence import (
        build_protocol_v2_stage_configuration,
        build_protocol_v2_stage_definitions,
    )
    from app.services.planning import planning_brief_stage as old_brief
    from app.services.planning import structured_task_plan_stage as old_task
    from app.services.planning import planning_brief_stage_support as brief_support
    from app.services.planning import (
        structured_task_plan_stage_support as task_support,
    )

    assert old_brief.PlanningBriefStage is new_brief.PlanningBriefStage
    assert old_task.StructuredTaskPlanStage is new_task.StructuredTaskPlanStage
    assert (
        old_brief.build_protocol_v2_stage_definitions
        is build_protocol_v2_stage_definitions
    )
    assert (
        old_task.build_protocol_v2_stage_definitions
        is build_protocol_v2_stage_definitions
    )
    assert (
        old_task.build_protocol_v2_stage_configuration
        is build_protocol_v2_stage_configuration
    )
    for name in (
        "PlanningBriefProviderInput",
        "build_planning_brief_request",
        "parse_planning_brief_candidate",
        "canonicalize_planning_brief_candidate",
        "PlanningBriefProviderOutputError",
    ):
        assert getattr(old_brief, name) is getattr(brief_support, name), name
    for name in (
        "StructuredTaskPlanProviderInput",
        "build_structured_task_plan_request",
        "parse_structured_task_plan_candidate",
        "canonicalize_structured_task_plan_candidate",
        "StructuredTaskPlanProviderOutputError",
    ):
        assert getattr(old_task, name) is getattr(task_support, name), name


def test_planning_stage_sequence_preserves_order_prerequisites_and_fingerprint():
    from app.services.orchestration.planning.stage_sequence import (
        PlanningStageSequence,
        build_protocol_v2_stage_configuration,
        build_protocol_v2_stage_definitions,
    )

    provider = object()
    definitions = build_protocol_v2_stage_definitions(None, planning_provider=provider)
    assert [definition.identifier for definition in definitions] == [
        "planning_brief",
        "structured_task_plan",
    ]
    assert definitions[0].prerequisites == ()
    assert definitions[1].prerequisites == ("planning_brief",)
    assert [
        item.identifier for item in PlanningStageSequence(provider).definitions()
    ] == [
        "planning_brief",
        "structured_task_plan",
    ]
    assert build_protocol_v2_stage_configuration(definitions)["stages"] == [
        {
            "identifier": "planning_brief",
            "version": 1,
            "prerequisites": [],
        },
        {
            "identifier": "structured_task_plan",
            "version": 1,
            "prerequisites": ["planning_brief"],
        },
    ]


def test_stage_contract_identity_remains_shared_with_lifecycle_engine():
    from app.services.orchestration.stage_engine import (
        StageDefinition as EngineDefinition,
    )
    from app.services.planning.stage_contract import StageDefinition

    assert StageDefinition is EngineDefinition


def test_planning_session_and_task_cycle_remains_absent():
    assert "app.tasks.planning_tasks" not in _imports(
        "app.services.planning.planning_session_service"
    )
