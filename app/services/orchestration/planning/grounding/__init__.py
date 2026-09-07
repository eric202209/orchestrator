"""Isolated production grounding contracts and deterministic executors.

Slice 1 intentionally exposes this package only to focused tests and future
coordinator work.  Existing live Planning imports no grounding modules.
"""

from .contracts import (
    EnclosingSymbolLocator,
    GroundingAction,
    GroundingActionKind,
    GroundingBudgetAccounting,
    GroundingBudgetDelta,
    GroundingBudgetLimits,
    GroundingBudgetSnapshot,
    GroundingExecutionError,
    GroundingObservation,
    GroundingOutcome,
    GroundingRequest,
    GroundingRequestRejection,
    GroundingSearchHit,
    InspectFileAction,
    MountedRouteLocator,
    ObservationProvenance,
    RequestProvenance,
    ResolveStructureAction,
    SearchTextAction,
    StructuralIdentity,
    StructuralRelation,
    SymbolDefinitionLocator,
    parse_grounding_request,
)
from .executor import GroundingExecutor

__all__ = [
    "EnclosingSymbolLocator",
    "GroundingAction",
    "GroundingActionKind",
    "GroundingBudgetAccounting",
    "GroundingBudgetDelta",
    "GroundingBudgetLimits",
    "GroundingBudgetSnapshot",
    "GroundingExecutionError",
    "GroundingExecutor",
    "GroundingObservation",
    "GroundingOutcome",
    "GroundingRequest",
    "GroundingRequestRejection",
    "GroundingSearchHit",
    "InspectFileAction",
    "MountedRouteLocator",
    "ObservationProvenance",
    "RequestProvenance",
    "ResolveStructureAction",
    "SearchTextAction",
    "StructuralIdentity",
    "StructuralRelation",
    "SymbolDefinitionLocator",
    "parse_grounding_request",
]
