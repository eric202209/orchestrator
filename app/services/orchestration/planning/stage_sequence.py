"""Bounded owner for the Protocol v2 planning-stage sequence."""

from collections.abc import Sequence
from typing import Any

from app.services.planning.providers import PlanningProvider, create_planning_provider
from app.services.planning.stage_contract import StageDefinition
from app.services.planning.structured_task_plan import DEFAULT_TASK_PLAN_POLICY
from app.services.planning.structured_task_plan_stage_support import (
    DEFAULT_TASK_PLAN_CANDIDATE_BYTES,
    DEFAULT_TASK_PLAN_PROVIDER_INPUT_BYTES,
    DEFAULT_TASK_PLAN_SOURCE_CHAR_LIMIT,
    DEFAULT_TASK_PLAN_TOTAL_SOURCE_CHAR_LIMIT,
)
from app.services.orchestration.planning.planning_brief_stage import (
    PlanningBriefStage,
)
from app.services.orchestration.planning.structured_task_plan_stage import (
    StructuredTaskPlanStage,
)


class PlanningStageSequence:
    """Construct exactly the ordered Protocol v2 planning stages."""

    def __init__(self, provider: PlanningProvider):
        self._provider = provider

    def definitions(self) -> tuple[StageDefinition, ...]:
        return (
            PlanningBriefStage(self._provider),
            StructuredTaskPlanStage(self._provider),
        )


def build_protocol_v2_stage_configuration(
    definitions: Sequence[StageDefinition] | None = None,
) -> dict[str, Any]:
    """Build the deterministic default stage configuration/fingerprint input."""

    definitions = tuple(definitions or ())
    return {
        "stages": [
            {
                "identifier": definition.identifier,
                "version": definition.version,
                "prerequisites": list(definition.prerequisites),
            }
            for definition in definitions
        ],
        "structured_task_plan": {
            **dict(DEFAULT_TASK_PLAN_POLICY),
            "auto_accept": True,
            "max_source_chars": DEFAULT_TASK_PLAN_SOURCE_CHAR_LIMIT,
            "max_total_source_chars": DEFAULT_TASK_PLAN_TOTAL_SOURCE_CHAR_LIMIT,
            "max_provider_input_bytes": DEFAULT_TASK_PLAN_PROVIDER_INPUT_BYTES,
            "max_candidate_bytes": DEFAULT_TASK_PLAN_CANDIDATE_BYTES,
        },
    }


def build_protocol_v2_stage_definitions(
    db: Any,
    *,
    planning_provider: PlanningProvider | None = None,
) -> tuple[StageDefinition, ...]:
    """Return the default v2 graph while preserving explicit custom providers."""

    provider = planning_provider or create_planning_provider(db)
    return PlanningStageSequence(provider).definitions()


__all__ = [
    "PlanningStageSequence",
    "build_protocol_v2_stage_configuration",
    "build_protocol_v2_stage_definitions",
]
