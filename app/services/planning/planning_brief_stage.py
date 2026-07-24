"""Temporary compatibility surface; canonical implementations live in planning support and orchestration planning modules."""

from app.services.planning.planning_brief_stage_support import (
    DEFAULT_SOURCE_CHAR_LIMIT,
    DEFAULT_TOTAL_SOURCE_CHAR_LIMIT,
    DEFAULT_PROVIDER_FIRST_OUTPUT_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    PLANNING_BRIEF_CANDIDATE_FIELDS,
    PLANNING_BRIEF_CANDIDATE_RECORD_TYPES,
    PlanningBriefApplicationError,
    PlanningBriefCandidate,
    PlanningBriefProviderInput,
    PlanningBriefProviderOutputError,
    PlanningBriefProviderRuntimeError,
    PlanningBriefStageError,
    PlanningBriefTransportError,
    PlanningBriefValidationError,
    build_planning_brief_request,
    build_planning_brief_provider_input,
    canonicalize_planning_brief_candidate,
    parse_planning_brief_candidate,
)
from app.services.orchestration.planning.planning_brief_stage import PlanningBriefStage
from app.services.orchestration.planning.stage_sequence import (
    build_protocol_v2_stage_definitions,
)

__all__ = [
    "DEFAULT_SOURCE_CHAR_LIMIT",
    "DEFAULT_TOTAL_SOURCE_CHAR_LIMIT",
    "DEFAULT_PROVIDER_FIRST_OUTPUT_TIMEOUT_SECONDS",
    "DEFAULT_PROVIDER_TIMEOUT_SECONDS",
    "PLANNING_BRIEF_CANDIDATE_FIELDS",
    "PLANNING_BRIEF_CANDIDATE_RECORD_TYPES",
    "PlanningBriefApplicationError",
    "PlanningBriefCandidate",
    "PlanningBriefProviderInput",
    "PlanningBriefProviderOutputError",
    "PlanningBriefProviderRuntimeError",
    "PlanningBriefStage",
    "PlanningBriefStageError",
    "PlanningBriefTransportError",
    "PlanningBriefValidationError",
    "build_planning_brief_request",
    "build_planning_brief_provider_input",
    "build_protocol_v2_stage_definitions",
    "canonicalize_planning_brief_candidate",
    "parse_planning_brief_candidate",
]
