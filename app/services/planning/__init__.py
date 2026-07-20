"""Planning services package.

Concrete services are loaded lazily to prevent orchestration startup cycles
through agent runtime imports.
"""

from __future__ import annotations

from typing import Any


_EXPORTS = {
    "CandidatePlanningOutcome": (
        "app.services.planning.candidate_planning_outcome",
        "CandidatePlanningOutcome",
    ),
    "PlanCandidate": ("app.services.planning.plan_candidate", "PlanCandidate"),
    "PlanCommitService": (
        "app.services.planning.plan_commit_service",
        "PlanCommitService",
    ),
    "PlannerService": ("app.services.planning.planner_service", "PlannerService"),
    "PlanningSessionService": (
        "app.services.planning.planning_session_service",
        "PlanningSessionService",
    ),
    "PlanningProtocolPersistenceService": (
        "app.services.planning.protocol_persistence",
        "PlanningProtocolPersistenceService",
    ),
    "InputManifest": (
        "app.services.planning.input_manifest",
        "InputManifest",
    ),
    "InputManifestBuilder": (
        "app.services.planning.input_manifest",
        "InputManifestBuilder",
    ),
    "validate_input_manifest": (
        "app.services.planning.input_manifest",
        "validate_input_manifest",
    ),
    "PlanningBrief": (
        "app.services.planning.planning_brief",
        "PlanningBrief",
    ),
    "PlanningBriefAcceptance": (
        "app.services.planning.planning_brief",
        "PlanningBriefAcceptance",
    ),
    "validate_planning_brief": (
        "app.services.planning.planning_brief",
        "validate_planning_brief",
    ),
    "PlanningBriefStage": (
        "app.services.planning.planning_brief_stage",
        "PlanningBriefStage",
    ),
    "PlanningBriefProvider": (
        "app.services.planning.planning_brief_stage",
        "PlanningBriefProvider",
    ),
    "build_protocol_v2_stage_definitions": (
        "app.services.planning.planning_brief_stage",
        "build_protocol_v2_stage_definitions",
    ),
    "render_planning_brief": (
        "app.services.planning.planning_brief",
        "render_planning_brief",
    ),
    "project_compatibility": (
        "app.services.planning.planning_brief",
        "project_compatibility",
    ),
    "StructuredTaskPlan": (
        "app.services.planning.structured_task_plan",
        "StructuredTaskPlan",
    ),
    "TaskPlan": ("app.services.planning.structured_task_plan", "TaskPlan"),
    "Task": ("app.services.planning.structured_task_plan", "Task"),
    "Dependency": ("app.services.planning.structured_task_plan", "Dependency"),
    "ExecutionGroup": (
        "app.services.planning.structured_task_plan",
        "ExecutionGroup",
    ),
    "WorkItem": ("app.services.planning.structured_task_plan", "WorkItem"),
    "Traceability": (
        "app.services.planning.structured_task_plan",
        "Traceability",
    ),
    "validate_structured_task_plan": (
        "app.services.planning.structured_task_plan",
        "validate_structured_task_plan",
    ),
    "render_structured_task_plan": (
        "app.services.planning.structured_task_plan",
        "render_structured_task_plan",
    ),
    "project_structured_task_plan": (
        "app.services.planning.structured_task_plan",
        "project_structured_task_plan",
    ),
    "diff_structured_task_plans": (
        "app.services.planning.structured_task_plan",
        "diff_structured_task_plans",
    ),
    "ProtocolPersistenceService": (
        "app.services.planning.protocol_persistence",
        "ProtocolPersistenceService",
    ),
    "select_candidate": (
        "app.services.planning.candidate_selection_policy",
        "select_candidate",
    ),
    "selection_key": (
        "app.services.planning.candidate_selection_policy",
        "selection_key",
    ),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(
            f"module 'app.services.planning' has no attribute {name!r}"
        )
    module_name, attr_name = _EXPORTS[name]
    from importlib import import_module

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
