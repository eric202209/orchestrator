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
    "render_planning_brief": (
        "app.services.planning.planning_brief",
        "render_planning_brief",
    ),
    "project_compatibility": (
        "app.services.planning.planning_brief",
        "project_compatibility",
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
