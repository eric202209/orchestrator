"""Registered planner contract vocabulary for Phase 31D.

The Phase 31D-2 documents are the authority for these identifiers and facts.
This module is deliberately small: it validates the shape of facts supplied
to planning and does not infer a contract from request wording or workspace
absence.
"""

from __future__ import annotations

import json
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


def planner_grounding_evidence(
    planner_contract: Mapping[str, Any] | None,
    *,
    runtime_context: Mapping[str, Any] | None = None,
    planner_prompt: str | None = None,
) -> dict[str, Any]:
    """Return inspectable evidence for one planner grounding boundary.

    The payload is observational.  It never fills missing contract values from
    task prose or workspace inspection; an absent contract remains visibly
    absent so legacy, non-certification planning keeps its existing path.
    """

    contract = dict(planner_contract) if isinstance(planner_contract, Mapping) else {}
    registered = contract.get("registered_scenario_contract")
    registered_contract = dict(registered) if isinstance(registered, Mapping) else None
    propagated = contract.get("propagated_planner_contract")
    propagated_contract = (
        dict(propagated)
        if isinstance(propagated, Mapping)
        else (dict(contract) if contract else None)
    )
    review_contract = contract.get("review_contract")
    publication_contract = contract.get("publication_contract")
    review = dict(review_contract) if isinstance(review_contract, Mapping) else {}
    publication = (
        dict(publication_contract) if isinstance(publication_contract, Mapping) else {}
    )
    return {
        "authoritative_contract_available": bool(contract),
        "registered_scenario_contract": registered_contract,
        "propagated_planner_contract": propagated_contract,
        "source_expectations": {
            "source": contract.get("source_expectation"),
            "scenario_source": (
                registered_contract.get("source_expectation")
                if registered_contract
                else None
            ),
        },
        "test_expectations": {
            "test": contract.get("test_expectation"),
            "scenario_test": (
                registered_contract.get("test_expectation")
                if registered_contract
                else None
            ),
        },
        "review_expectations": {
            "review": review.get("expectation"),
            "contract": review,
        },
        "publication_expectations": {
            "publication": publication.get("expectation"),
            "contract": publication,
        },
        "required_source_inventory": list(
            contract.get("required_source_inventory")
            or contract.get("source_paths")
            or []
        ),
        "required_test_inventory": list(
            contract.get("required_test_inventory") or contract.get("test_paths") or []
        ),
        "runtime_planner_context": dict(runtime_context or {}),
        "planner_prompt": (
            planner_prompt if contract and planner_prompt is not None else None
        ),
        "authority": (
            "registered_certification_contract"
            if contract
            else "legacy_runtime_inference"
        ),
    }


def render_planner_contract_context(
    planner_contract: Mapping[str, Any] | None,
) -> str:
    """Render authoritative planner facts as a prompt block when supplied."""

    if not isinstance(planner_contract, Mapping) or not planner_contract:
        return ""
    payload = json.dumps(
        dict(planner_contract), ensure_ascii=True, sort_keys=True, indent=2
    )
    return (
        "## AUTHORITATIVE REGISTERED PLANNER CONTRACT\n"
        "These facts come from the registered certification scenario contract. "
        "Use them unchanged for planning and repair; do not infer, replace, or "
        "override them from task wording or repository inspection.\n\n"
        f"{payload}"
    )


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
    "planner_grounding_evidence",
    "render_planner_contract_context",
    "truthy_structural_facts",
]
