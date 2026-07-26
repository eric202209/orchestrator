#!/usr/bin/env python3
"""Phase 31B scenario registry.

Loads the `ScenarioAcceptanceContract` for a matrix scenario ID. Scenario
*definitions* (objective, evidence, expected class) remain owned by
`docs/roadmap/workflow/phase31/phase31-certification-scenario-matrix.md`;
this module only translates the Stage 0/1 rows that are already fully
specified there into the typed contract
`app/services/orchestration/acceptance_evidence.py` needs to classify a
dispatch. It does not redefine or restate the matrix.

Only Stage 0 (gating, not scored) and Stage 1 (Phase 31C scope, S1-1 also
the Phase 31B pilot) are registered. Stage 2-4 scenarios need failure-path
and multi-project contract shapes (rollback expectations, duplicate-request
handling, endurance-window aggregation) that Phase 31A's matrix describes
at the objective level but that were never turned into per-scenario
`ScenarioAcceptanceContract` field values -- registering them here now
would mean inventing those field values rather than reusing a decision
already made. `scenario_contract(...)` raises `NotImplementedError` for any
unregistered ID; this is a named Phase 31B limitation (see the phase
report), not a defect, and is Phase 31D's job to close for Stage 2/3.
"""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.services.orchestration.acceptance_evidence import (  # noqa: E402
    ScenarioAcceptanceContract,
)

# Matrix source: docs/roadmap/workflow/phase31/phase31-certification-scenario-matrix.md
_REGISTRY: dict[str, ScenarioAcceptanceContract] = {
    "S1-1": ScenarioAcceptanceContract(
        scenario_kind="documentation_only",
        mutation_expected=True,
        publication_required=True,
        human_review_expected=False,
        evaluator_required=False,
    ),
    "S1-2": ScenarioAcceptanceContract(
        scenario_kind="new_backend_api_endpoint",
        mutation_expected=True,
        publication_required=True,
        human_review_expected=False,
        evaluator_required=False,
    ),
    "S1-3": ScenarioAcceptanceContract(
        scenario_kind="bug_fix",
        mutation_expected=True,
        publication_required=True,
        human_review_expected=False,
        evaluator_required=False,
    ),
    "S1-4": ScenarioAcceptanceContract(
        scenario_kind="new_frontend_component",
        mutation_expected=True,
        publication_required=True,
        human_review_expected=False,
        evaluator_required=False,
    ),
    "S1-5": ScenarioAcceptanceContract(
        scenario_kind="multi_file_feature",
        mutation_expected=True,
        publication_required=True,
        human_review_expected=False,
        evaluator_required=False,
    ),
    "S1-6": ScenarioAcceptanceContract(
        scenario_kind="held_for_review_flow",
        mutation_expected=True,
        publication_required=True,
        human_review_expected=True,
        evaluator_required=False,
    ),
}

# Stage 0 rows are gating preamble checks, not acceptance-classified
# dispatches -- they have no ScenarioAcceptanceContract. Listed here only
# so callers can distinguish "gating, no contract by design" from
# "not yet registered".
GATING_SCENARIO_IDS = frozenset({"S0-1", "S0-2", "S0-3"})


def scenario_contract(scenario_id: str) -> ScenarioAcceptanceContract:
    """Return the registered contract for *scenario_id*.

    Raises `NotImplementedError` for Stage 0 (gating -- has no contract by
    design) and any Stage 2-4 ID (contract not yet defined, see module
    docstring). Raises `KeyError` for an ID that appears nowhere in the
    matrix.
    """
    if scenario_id in GATING_SCENARIO_IDS:
        raise NotImplementedError(
            f"{scenario_id} is a Stage 0 gating check, not an acceptance-"
            "classified scenario -- it has no ScenarioAcceptanceContract "
            "by design."
        )
    if scenario_id in _REGISTRY:
        return _REGISTRY[scenario_id]
    if scenario_id.startswith(("S2-", "S3-", "S4-")):
        raise NotImplementedError(
            f"{scenario_id} has no registered ScenarioAcceptanceContract yet "
            "-- Stage 2-4 contract field values were not defined in Phase "
            "31A and are Phase 31D/31E scope to specify, not to invent here."
        )
    raise KeyError(f"{scenario_id!r} is not a scenario ID in the Phase 31 matrix")
