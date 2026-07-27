"""Registered planner contract vocabulary for Phase 31D.

The Phase 31D-2 documents are the authority for these identifiers and facts.
This module is deliberately small: it validates the shape of facts supplied
to planning and does not infer a contract from request wording or workspace
absence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


PLANNER_CONTRACT_ID = "ST23-PLANNER-001"
PLANNER_CONTRACT_VERSION = "v1"

REGISTERED_SCENARIO_CONTRACT_IDS = frozenset(
    {
        "ST23-S2-1-v1",
        "ST23-S2-2-v1",
        "ST23-S2-3-v1",
        "ST23-S2-4-v1",
        "ST23-S2-5-v1",
        "ST23-S3-1-v1",
        "ST23-S3-2-v1",
        "ST23-S3-3-v1",
    }
)

# S1-2/S1-3 are retained here because Phase 31D-3 resolves their historical
# Family B planning limitation through the same registered cross-scenario
# planner contract. Their acceptance/publication contracts remain owned by
# the existing Stage 1 registry.
REGISTERED_PLANNER_SCENARIO_IDS = frozenset(
    {"S1-2", "S1-3"}
    | {
        contract_id.rsplit("-v", 1)[0].replace("ST23-", "")
        for contract_id in REGISTERED_SCENARIO_CONTRACT_IDS
    }
)

REGISTERED_STRUCTURAL_FACTS = frozenset(
    {
        "CONTRACT_REGISTERED",
        "SCENARIO_ID_MATCH",
        "SOURCE_EXPECTATION_DECLARED",
        "SOURCE_NOT_REQUIRED",
        "SOURCE_NOT_REQUIRED_FOR_OBSERVATION",
        "SOURCE_PRESENT",
        "SOURCE_MATERIALIZED",
        "TEST_EXPECTATION_DECLARED",
        "TEST_INTENT_DECISION_RECORDED",
        "EXPECTED_TEST_PRESENT",
        "EXPECTED_TEST_GENERATED",
        "EXPECTED_TEST_NOT_REQUIRED",
    }
)

SOURCE_EXPECTATIONS = frozenset(
    {
        "SOURCE_NOT_REQUIRED",
        "SOURCE_NOT_REQUIRED_FOR_OBSERVATION",
        "SOURCE_PRESENT",
        "SOURCE_MATERIALIZED",
        "SOURCE_UNCHANGED",
        "SOURCE_ROLLED_BACK",
        "SOURCE_ISOLATED",
    }
)

TEST_EXPECTATIONS = frozenset(
    {
        "EXPECTED_TEST_PRESENT",
        "EXPECTED_TEST_GENERATED",
        "EXPECTED_TEST_NOT_REQUIRED",
    }
)


@dataclass(frozen=True)
class RegisteredPlannerContract:
    contract_id: str
    version: str
    required_facts: frozenset[str]


REGISTERED_PLANNER_CONTRACTS = {
    PLANNER_CONTRACT_ID: RegisteredPlannerContract(
        contract_id=PLANNER_CONTRACT_ID,
        version=PLANNER_CONTRACT_VERSION,
        required_facts=frozenset(
            {
                "CONTRACT_REGISTERED",
                "SCENARIO_ID_MATCH",
                "SOURCE_EXPECTATION_DECLARED",
                "TEST_EXPECTATION_DECLARED",
            }
        ),
    ),
    **{
        contract_id: RegisteredPlannerContract(
            contract_id=contract_id,
            version="v1",
            required_facts=frozenset(
                {
                    "CONTRACT_REGISTERED",
                    "SCENARIO_ID_MATCH",
                    "SOURCE_EXPECTATION_DECLARED",
                    "TEST_EXPECTATION_DECLARED",
                }
            ),
        )
        for contract_id in REGISTERED_SCENARIO_CONTRACT_IDS
    },
}


def registered_planner_contract(contract_id: Any) -> RegisteredPlannerContract | None:
    """Return a registered contract, or ``None`` for an unregistered ID."""

    return REGISTERED_PLANNER_CONTRACTS.get(str(contract_id or "").strip())


def truthy_structural_facts(value: Any) -> set[str]:
    """Normalize a fact list/map without assigning meaning to unknown facts."""

    if isinstance(value, Mapping):
        return {
            str(name).strip()
            for name, enabled in value.items()
            if enabled and str(name).strip()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(name).strip() for name in value if str(name).strip()}
    return set()


__all__ = [
    "PLANNER_CONTRACT_ID",
    "PLANNER_CONTRACT_VERSION",
    "REGISTERED_PLANNER_CONTRACTS",
    "REGISTERED_PLANNER_SCENARIO_IDS",
    "REGISTERED_SCENARIO_CONTRACT_IDS",
    "REGISTERED_STRUCTURAL_FACTS",
    "SOURCE_EXPECTATIONS",
    "TEST_EXPECTATIONS",
    "RegisteredPlannerContract",
    "registered_planner_contract",
    "truthy_structural_facts",
]
