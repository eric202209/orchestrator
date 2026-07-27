"""Declarative Phase 31 Stage 1 scenario and contract registry.

The six Stage 1 task title/description pairs are copied verbatim from the
tracked test/harness fixture
``app/tests/fixtures/phase31c_v4_frozen_scenario_task_specs.json``.  That
fixture preserves the temporary in-process dispatch input used by V4 in
clean CI checkouts; this module is the first promotion of that frozen input
into the canonical runner.

This module owns harness declarations only.  It does not infer planner,
review, publication, or test intent from prompt wording and it does not
change the Phase 30K acceptance classifier.  Observed facts remain owned by
the planner, workspace, lifecycle, review, publication, and evidence seams.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.services.orchestration.acceptance_evidence import (  # noqa: E402
    OutcomeClass,
    ScenarioAcceptanceContract,
)
from app.services.orchestration.outcome_adjudication import (  # noqa: E402
    PublicationExpectation,
    PublicationOutcome,
    RegisteredOutcomeContract,
    ReviewExpectation,
    ReviewOutcome,
    derive_independent_outcomes,
)
from app.services.orchestration.planning.planner_contract_registry import (  # noqa: E402
    PLANNER_CONTRACT_ID,
    PLANNER_CONTRACT_VERSION,
    SOURCE_EXPECTATIONS,
    TEST_EXPECTATIONS,
    registered_planner_contract,
)


STAGE1_SCENARIO_IDS = ("S1-1", "S1-2", "S1-3", "S1-4", "S1-5", "S1-6")
STAGE1_CERTIFICATION_CLASSIFICATION = "STAGE1_CERTIFICATION_MATRIX"
DEBUG_SUBSET_CLASSIFICATION = "NON_CERTIFICATION_DEBUG_SUBSET"
SCENARIO_SPEC_VERSION = "phase31-stage1-scenario/1"

REVIEW_CONTRACT_ID = "ST23-REVIEW-001"
PUBLICATION_CONTRACT_ID = "ST23-PUBLICATION-001"
CONTRACT_VERSION = "v1"

_COMMON_PLANNER_FACTS = (
    "CONTRACT_REGISTERED",
    "SCENARIO_ID_MATCH",
    "SOURCE_EXPECTATION_DECLARED",
    "TEST_EXPECTATION_DECLARED",
)
_COMMON_OUTCOME_FACTS = ("CONTRACT_REGISTERED", "SCENARIO_ID_MATCH")


class ScenarioRegistryError(ValueError):
    """Raised when a scenario registry cannot be certified as complete."""


@dataclass(frozen=True)
class PlannerContractBinding:
    """Explicit Phase 31D-3 planner/bootstrap declarations for one scenario."""

    contract_id: str
    contract_version: str
    scenario_id: str
    source_expectation: str
    test_expectation: str
    structural_evidence: tuple[str, ...]
    source_paths: tuple[str, ...]
    test_paths: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "scenario_id": self.scenario_id,
            "source_expectation": self.source_expectation,
            "test_expectation": self.test_expectation,
            "structural_evidence": list(self.structural_evidence),
            "source_paths": list(self.source_paths),
            "test_paths": list(self.test_paths),
        }


@dataclass(frozen=True)
class ContractBinding:
    """A versioned review or publication contract declaration."""

    contract_id: str
    contract_version: str
    scenario_id: str
    expectation: str
    structural_evidence: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "scenario_id": self.scenario_id,
            "expectation": self.expectation,
            "structural_evidence": list(self.structural_evidence),
        }


@dataclass(frozen=True)
class ScenarioSpecification:
    """All declarations required to set up, dispatch, score, replay, and clean up one lane."""

    specification_version: str
    scenario_id: str
    run_order: int
    task_title: str
    task_description: str
    target_project_requirement: str
    workspace_requirement: str
    source_bootstrap_setup: str
    source_paths: tuple[str, ...]
    test_expectation: str
    test_paths: tuple[str, ...]
    planner_contract: Optional[PlannerContractBinding]
    review_contract: ContractBinding
    publication_contract: ContractBinding
    timeout_seconds: int
    control_parameters: tuple[tuple[str, str], ...]
    expected_acceptance_classes: tuple[str, ...]
    expected_product_outcomes: tuple[str, ...]
    expected_review_outcomes: tuple[str, ...]
    expected_publication_outcomes: tuple[str, ...]
    historical_v4_interpretation: tuple[tuple[str, str], ...]
    cleanup_requirements: tuple[str, ...]
    prior_scenarios: tuple[str, ...]
    acceptance_contract: ScenarioAcceptanceContract

    def to_dict(self) -> dict[str, Any]:
        """Return a stable, JSON-compatible declaration for evidence/replay."""

        def normalize(value: Any) -> Any:
            if hasattr(value, "value") and hasattr(value, "name"):
                return value.value
            if isinstance(value, tuple):
                return [normalize(item) for item in value]
            if isinstance(value, Mapping):
                return {
                    str(key): normalize(item)
                    for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
                }
            if hasattr(value, "__dataclass_fields__"):
                return normalize(asdict(value))
            return value

        return normalize(asdict(self))

    def registered_outcome_contract(self) -> RegisteredOutcomeContract:
        """Build the Phase 31D adjudication envelope from explicit bindings.

        The implementation seam accepts one registered envelope while the
        product contract taxonomy names its review and publication owners
        separately.  The review contract identity is the envelope identity;
        the publication binding remains separately validated and is the source
        of the publication expectation below.
        """

        return RegisteredOutcomeContract(
            contract_id=self.review_contract.contract_id,
            contract_version=self.review_contract.contract_version,
            review_expectation=self.review_contract.expectation,
            publication_expectation=self.publication_contract.expectation,
            required_structural_evidence=tuple(
                sorted(
                    set(self.review_contract.structural_evidence)
                    | set(self.publication_contract.structural_evidence)
                )
            ),
        )

    def adjudicate_authoritative_outcome(
        self,
        *,
        structural_evidence: Mapping[str, Any],
        runtime_record: Mapping[str, Any] | str,
        product_state: Mapping[str, Any],
        review_record: Mapping[str, Any],
        publication_record: Mapping[str, Any],
    ):
        """Adjudicate from authoritative records only; never from task text."""

        validate_scenario_specification(self)
        return derive_independent_outcomes(
            self.registered_outcome_contract(),
            structural_evidence,
            runtime_record,
            product_state,
            review_record,
            publication_record,
        )


@dataclass(frozen=True)
class ScenarioSelection:
    scenario_ids: tuple[str, ...]
    certification_classification: str

    @property
    def is_certification_matrix(self) -> bool:
        return self.certification_classification == STAGE1_CERTIFICATION_CLASSIFICATION


def _planner(
    scenario_id: str,
    *,
    source_expectation: str,
    test_expectation: str,
    source_paths: tuple[str, ...],
    test_paths: tuple[str, ...],
) -> PlannerContractBinding:
    return PlannerContractBinding(
        contract_id=PLANNER_CONTRACT_ID,
        contract_version=PLANNER_CONTRACT_VERSION,
        scenario_id=scenario_id,
        source_expectation=source_expectation,
        test_expectation=test_expectation,
        structural_evidence=_COMMON_PLANNER_FACTS,
        source_paths=source_paths,
        test_paths=test_paths,
    )


def _review(scenario_id: str, expectation: str) -> ContractBinding:
    return ContractBinding(
        contract_id=REVIEW_CONTRACT_ID,
        contract_version=CONTRACT_VERSION,
        scenario_id=scenario_id,
        expectation=expectation,
        structural_evidence=_COMMON_OUTCOME_FACTS + ("REVIEW_DECISION_RECORDED",),
    )


def _publication(scenario_id: str, expectation: str) -> ContractBinding:
    return ContractBinding(
        contract_id=PUBLICATION_CONTRACT_ID,
        contract_version=CONTRACT_VERSION,
        scenario_id=scenario_id,
        expectation=expectation,
        structural_evidence=_COMMON_OUTCOME_FACTS + ("PUBLICATION_FACT_RECORDED",),
    )


def _controls(*, timeout_seconds: int = 1800, **values: str) -> tuple[tuple[str, str], ...]:
    controls = {"timeout_seconds": str(timeout_seconds), **values}
    return tuple(sorted(controls.items()))


def _historical(**values: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(values.items()))


# Authoritative task text from the retained V4 artifact.  Do not edit these
# strings to influence a future planner or evaluator result.
_SCENARIO_SPECIFICATIONS: tuple[ScenarioSpecification, ...] = (
    ScenarioSpecification(
        specification_version=SCENARIO_SPEC_VERSION,
        scenario_id="S1-1",
        run_order=1,
        task_title="Phase 31C S1-1: documentation-only change",
        task_description=(
            "Add a short 'Overview' section to a new file README-PILOT.md at "
            "the project root describing this is a Phase 31C certification "
            "workspace. This is a documentation-only task: do not touch any "
            "other file, do not add code."
        ),
        target_project_requirement="One fresh Phase 31C target project, F10-verified.",
        workspace_requirement="One serially reused workspace root; F12 registration required before dispatch.",
        source_bootstrap_setup="README-PILOT.md is absent before this lane and is the only requested artifact.",
        source_paths=("README-PILOT.md",),
        test_expectation="EXPECTED_TEST_NOT_REQUIRED",
        test_paths=(),
        planner_contract=None,
        review_contract=_review("S1-1", "REVIEW_NOT_REQUIRED"),
        publication_contract=_publication("S1-1", "PUBLICATION_REQUIRED"),
        timeout_seconds=1800,
        control_parameters=_controls(
            serial="true", execution_profile="full_lifecycle", plan_position="1"
        ),
        expected_acceptance_classes=(OutcomeClass.SUCCESS_PUBLISHED.value,),
        expected_product_outcomes=("SUCCESS_PUBLISHED",),
        expected_review_outcomes=("REVIEW_NOT_REQUIRED",),
        expected_publication_outcomes=("PUBLICATION_PUBLISHED",),
        historical_v4_interpretation=_historical(
            acceptance="SUCCESS_PUBLISHED",
            product="SUCCESS_PUBLISHED",
            review="REVIEW_NOT_REQUIRED",
            publication="PUBLICATION_PUBLISHED",
        ),
        cleanup_requirements=(
            "Capture pre/post workspace hashes before any session cleanup.",
            "Retain the scenario record and replay result; do not delete the target before evidence validation.",
        ),
        prior_scenarios=(),
        acceptance_contract=ScenarioAcceptanceContract(
            scenario_kind="documentation_only",
            mutation_expected=True,
            publication_required=True,
            human_review_expected=False,
            evaluator_required=False,
        ),
    ),
    ScenarioSpecification(
        specification_version=SCENARIO_SPEC_VERSION,
        scenario_id="S1-2",
        run_order=2,
        task_title="Phase 31C S1-2: new backend API endpoint",
        task_description=(
            "Add one new GET endpoint at /api/certification/status in the "
            "existing app/main.py module. Return JSON with status='ok' and "
            "certification_stage='31C'. Keep the change limited to the "
            "backend endpoint and its focused test if the repository has a "
            "test layout; do not change frontend files or unrelated code."
        ),
        target_project_requirement="One fresh Phase 31C target project, F10-verified; the existing backend module is the source target.",
        workspace_requirement="Reuse the same serial target workspace after S1-1; bind dispatch to its registered project root.",
        source_bootstrap_setup="Existing app/main.py is the declared source module; the endpoint is materialized in that module. The retained V4 planner evidence recorded tests/test_certification.py when test intent was resolved.",
        source_paths=("app/main.py",),
        test_expectation="EXPECTED_TEST_PRESENT",
        test_paths=("tests/test_certification.py",),
        planner_contract=_planner(
            "S1-2",
            source_expectation="SOURCE_PRESENT",
            test_expectation="EXPECTED_TEST_PRESENT",
            source_paths=("app/main.py",),
            test_paths=("tests/test_certification.py",),
        ),
        review_contract=_review("S1-2", "REVIEW_NOT_REQUIRED"),
        publication_contract=_publication("S1-2", "PUBLICATION_REQUIRED"),
        timeout_seconds=1800,
        control_parameters=_controls(
            serial="true", execution_profile="full_lifecycle", plan_position="1"
        ),
        expected_acceptance_classes=(
            OutcomeClass.SUCCESS_PUBLISHED.value,
            OutcomeClass.SUCCESS_REQUIRES_REVIEW.value,
        ),
        expected_product_outcomes=("SUCCESS_PUBLISHED", "SUCCESS_REQUIRES_REVIEW"),
        expected_review_outcomes=("REVIEW_NOT_REQUIRED", "REVIEW_REQUIRED_PENDING"),
        expected_publication_outcomes=(
            "PUBLICATION_PUBLISHED",
            "PUBLICATION_HELD_FOR_REVIEW",
        ),
        historical_v4_interpretation=_historical(
            acceptance="FAILED_SAFE",
            product="EXPECTED_LIMITATION",
            review="REVIEW_NOT_APPLICABLE",
            publication="PUBLICATION_NOT_PUBLISHED",
        ),
        cleanup_requirements=(
            "Preserve endpoint and focused-test diff evidence until replay and session validation complete.",
            "Do not clean up unrelated files or convert the retained conditional request wording.",
        ),
        prior_scenarios=("S1-1",),
        acceptance_contract=ScenarioAcceptanceContract(
            scenario_kind="new_backend_api_endpoint",
            mutation_expected=True,
            publication_required=True,
            human_review_expected=False,
            evaluator_required=False,
        ),
    ),
    ScenarioSpecification(
        specification_version=SCENARIO_SPEC_VERSION,
        scenario_id="S1-3",
        run_order=3,
        task_title="Phase 31C S1-3: bug fix in existing code",
        task_description=(
            "Fix the documented parity bug in app/calculator.py: is_even "
            "currently returns True for odd integers and False for even "
            "integers. Correct the implementation, preserve the public "
            "function name, and update only the focused test or explanation "
            "needed to demonstrate the fix. Do not redesign the module."
        ),
        target_project_requirement="One fresh Phase 31C target project, F10-verified; app/calculator.py is the existing source target.",
        workspace_requirement="Reuse the same serial target workspace and preserve project-local hashes for comparison.",
        source_bootstrap_setup="Existing app/calculator.py and its package context are the declared source; the retained V4 planning records observed test_calculator.py during repair arbitration.",
        source_paths=("app/calculator.py",),
        test_expectation="EXPECTED_TEST_PRESENT",
        test_paths=("test_calculator.py",),
        planner_contract=_planner(
            "S1-3",
            source_expectation="SOURCE_PRESENT",
            test_expectation="EXPECTED_TEST_PRESENT",
            source_paths=("app/calculator.py",),
            test_paths=("test_calculator.py",),
        ),
        review_contract=_review("S1-3", "REVIEW_NOT_REQUIRED"),
        publication_contract=_publication("S1-3", "PUBLICATION_REQUIRED"),
        timeout_seconds=1800,
        control_parameters=_controls(
            serial="true", execution_profile="full_lifecycle", plan_position="1"
        ),
        expected_acceptance_classes=(
            OutcomeClass.SUCCESS_PUBLISHED.value,
            OutcomeClass.SUCCESS_REQUIRES_REVIEW.value,
        ),
        expected_product_outcomes=("SUCCESS_PUBLISHED", "SUCCESS_REQUIRES_REVIEW"),
        expected_review_outcomes=("REVIEW_NOT_REQUIRED", "REVIEW_REQUIRED_PENDING"),
        expected_publication_outcomes=(
            "PUBLICATION_PUBLISHED",
            "PUBLICATION_HELD_FOR_REVIEW",
        ),
        historical_v4_interpretation=_historical(
            acceptance="FAILED_SAFE",
            product="EXPECTED_LIMITATION",
            review="REVIEW_NOT_APPLICABLE",
            publication="PUBLICATION_NOT_PUBLISHED",
        ),
        cleanup_requirements=(
            "Retain the focused bug-fix/test diff and authoritative terminal facts through replay.",
            "Do not rewrite the request into a different bug or change the public function contract.",
        ),
        prior_scenarios=("S1-1", "S1-2"),
        acceptance_contract=ScenarioAcceptanceContract(
            scenario_kind="bug_fix",
            mutation_expected=True,
            publication_required=True,
            human_review_expected=False,
            evaluator_required=False,
        ),
    ),
    ScenarioSpecification(
        specification_version=SCENARIO_SPEC_VERSION,
        scenario_id="S1-4",
        run_order=4,
        task_title="Phase 31C S1-4: new frontend component",
        task_description=(
            "Add a reusable StatusBadge component under "
            "frontend/src/components/StatusBadge.tsx and render it from the "
            "existing frontend/src/App.tsx with the label 'Ready'. Keep the "
            "change limited to this component and its focused App usage; do "
            "not change backend files or package configuration."
        ),
        target_project_requirement="One fresh Phase 31C target project, F10-verified; frontend source is within the same target.",
        workspace_requirement="Reuse the same serial target workspace and capture the frontend-only diff boundary.",
        source_bootstrap_setup="Existing frontend/src/App.tsx is updated and frontend/src/components/StatusBadge.tsx is materialized; no test artifact is declared by the retained V4 plan.",
        source_paths=("frontend/src/App.tsx", "frontend/src/components/StatusBadge.tsx"),
        test_expectation="EXPECTED_TEST_NOT_REQUIRED",
        test_paths=(),
        planner_contract=None,
        review_contract=_review("S1-4", "REVIEW_NOT_REQUIRED"),
        publication_contract=_publication("S1-4", "PUBLICATION_REQUIRED"),
        timeout_seconds=1800,
        control_parameters=_controls(
            serial="true", execution_profile="full_lifecycle", plan_position="1"
        ),
        expected_acceptance_classes=(
            OutcomeClass.SUCCESS_PUBLISHED.value,
            OutcomeClass.SUCCESS_REQUIRES_REVIEW.value,
        ),
        expected_product_outcomes=("SUCCESS_PUBLISHED", "SUCCESS_REQUIRES_REVIEW"),
        expected_review_outcomes=("REVIEW_NOT_REQUIRED", "REVIEW_REQUIRED_PENDING"),
        expected_publication_outcomes=(
            "PUBLICATION_PUBLISHED",
            "PUBLICATION_HELD_FOR_REVIEW",
        ),
        historical_v4_interpretation=_historical(
            acceptance="SUCCESS_PUBLISHED",
            product="SUCCESS_PUBLISHED",
            review="REVIEW_NOT_REQUIRED",
            publication="PUBLICATION_PUBLISHED",
        ),
        cleanup_requirements=(
            "Retain only the declared component/App diff for evidence; do not alter backend or package files.",
            "Validate replay before removing any disposable target workspace.",
        ),
        prior_scenarios=("S1-1", "S1-2", "S1-3"),
        acceptance_contract=ScenarioAcceptanceContract(
            scenario_kind="new_frontend_component",
            mutation_expected=True,
            publication_required=True,
            human_review_expected=False,
            evaluator_required=False,
        ),
    ),
    ScenarioSpecification(
        specification_version=SCENARIO_SPEC_VERSION,
        scenario_id="S1-5",
        run_order=5,
        task_title="Phase 31C S1-5: multi-file feature change",
        task_description=(
            "Implement a small notification feature across the existing "
            "backend: add app/notifications.py with a pure "
            "format_notification(message) helper, expose a GET "
            "/api/certification/notification endpoint from app/main.py, and "
            "add a focused test under tests/test_notifications.py. Keep the "
            "public behavior simple and the diff limited to those three "
            "feature files."
        ),
        target_project_requirement="One fresh Phase 31C target project, F10-verified; multi-file backend feature remains project-local.",
        workspace_requirement="Reuse the same serial target workspace; review/publication facts must be captured independently.",
        source_bootstrap_setup="app/main.py is extended and app/notifications.py plus tests/test_notifications.py are materialized; the retained V4 lifecycle record confirms all three paths.",
        source_paths=("app/main.py", "app/notifications.py"),
        test_expectation="EXPECTED_TEST_PRESENT",
        test_paths=("tests/test_notifications.py",),
        planner_contract=None,
        review_contract=_review("S1-5", "REVIEW_REQUIRED"),
        publication_contract=_publication("S1-5", "PUBLICATION_REQUIRED"),
        timeout_seconds=1800,
        control_parameters=_controls(
            serial="true", execution_profile="full_lifecycle", plan_position="1"
        ),
        expected_acceptance_classes=(
            OutcomeClass.SUCCESS_PUBLISHED.value,
            OutcomeClass.SUCCESS_REQUIRES_REVIEW.value,
        ),
        expected_product_outcomes=("SUCCESS_PUBLISHED", "SUCCESS_REQUIRES_REVIEW"),
        expected_review_outcomes=("REVIEW_HELD",),
        expected_publication_outcomes=("PUBLICATION_HELD_FOR_REVIEW",),
        historical_v4_interpretation=_historical(
            acceptance="FAILED_SAFE",
            product="SUCCESS_REQUIRES_REVIEW",
            review="REVIEW_HELD",
            publication="PUBLICATION_HELD_FOR_REVIEW",
        ),
        cleanup_requirements=(
            "Preserve the three-file feature boundary and review decision record until adjudication/replay complete.",
            "Do not publish or silently convert a review hold into an autonomous success.",
        ),
        prior_scenarios=("S1-1", "S1-2", "S1-3", "S1-4"),
        acceptance_contract=ScenarioAcceptanceContract(
            scenario_kind="multi_file_feature",
            mutation_expected=True,
            publication_required=True,
            human_review_expected=False,
            evaluator_required=False,
        ),
    ),
    ScenarioSpecification(
        specification_version=SCENARIO_SPEC_VERSION,
        scenario_id="S1-6",
        run_order=6,
        task_title="Phase 31C S1-6: review-required change",
        task_description=(
            "Implement the requested review-held change: add a short 'Review "
            "checklist' section to README-PILOT.md describing that a human "
            "must verify the Stage 1 evidence before publication. This "
            "scenario's acceptance target is a held-for-review outcome. "
            "Prepare the change set and leave it for human review; do not "
            "publish or modify unrelated files."
        ),
        target_project_requirement="One fresh Phase 31C target project, F10-verified, serially reused after S1-1 created README-PILOT.md.",
        workspace_requirement="Same project/workspace as S1-1 through S1-5; review guard and publication fact are authoritative close-out inputs.",
        source_bootstrap_setup="README-PILOT.md must be the prior S1-1 artifact; add only the requested Review checklist section and leave the change set held.",
        source_paths=("README-PILOT.md",),
        test_expectation="EXPECTED_TEST_NOT_REQUIRED",
        test_paths=(),
        planner_contract=None,
        review_contract=_review("S1-6", "REVIEW_REQUIRED"),
        publication_contract=_publication("S1-6", "PUBLICATION_ALLOWED"),
        timeout_seconds=1800,
        control_parameters=_controls(
            serial="true", execution_profile="full_lifecycle", plan_position="1"
        ),
        expected_acceptance_classes=(OutcomeClass.SUCCESS_REQUIRES_REVIEW.value,),
        expected_product_outcomes=("SUCCESS_REQUIRES_REVIEW",),
        expected_review_outcomes=("REVIEW_HELD",),
        expected_publication_outcomes=("PUBLICATION_HELD_FOR_REVIEW",),
        historical_v4_interpretation=_historical(
            acceptance="FAILED_SAFE",
            product="SUCCESS_PUBLISHED",
            review="REVIEW_EXPECTATION_NOT_MET",
            publication="PUBLICATION_PUBLISHED_UNEXPECTEDLY",
        ),
        cleanup_requirements=(
            "Retain the held change set and review/publication records for independent adjudication and replay.",
            "Do not publish the change or mutate unrelated files during cleanup.",
        ),
        prior_scenarios=("S1-1", "S1-2", "S1-3", "S1-4", "S1-5"),
        acceptance_contract=ScenarioAcceptanceContract(
            scenario_kind="held_for_review_flow",
            mutation_expected=True,
            publication_required=True,
            human_review_expected=True,
            evaluator_required=False,
        ),
    ),
)


# Historical Phase 31B/31C callers import this name.  It is now generated
# from the declarative specifications and therefore cannot drift separately.
_SCENARIO_TASKS = {
    specification.scenario_id: {
        "title": specification.task_title,
        "description": specification.task_description,
    }
    for specification in _SCENARIO_SPECIFICATIONS
}


def validate_scenario_specification(specification: ScenarioSpecification) -> None:
    """Fail closed if one specification is incomplete or identity-inconsistent."""

    errors: list[str] = []
    if specification.specification_version != SCENARIO_SPEC_VERSION:
        errors.append("specification_version does not match registry version")
    if specification.scenario_id not in STAGE1_SCENARIO_IDS:
        errors.append("scenario_id is not a canonical Stage 1 ID")
    if specification.run_order < 1:
        errors.append("run_order must be positive")
    for field_name in (
        "task_title",
        "task_description",
        "target_project_requirement",
        "workspace_requirement",
        "source_bootstrap_setup",
        "test_expectation",
    ):
        if not str(getattr(specification, field_name, "")).strip():
            errors.append(f"missing required field: {field_name}")
    if specification.timeout_seconds <= 0:
        errors.append("timeout_seconds must be positive")
    if not specification.control_parameters:
        errors.append("missing required field: control_parameters")
    if not specification.expected_acceptance_classes:
        errors.append("missing required field: expected_acceptance_classes")
    if not specification.cleanup_requirements:
        errors.append("missing required field: cleanup_requirements")
    if specification.test_expectation not in TEST_EXPECTATIONS:
        errors.append(
            f"invalid test_expectation: {specification.test_expectation!r}"
        )

    expected_contract = _REGISTRY.get(specification.scenario_id)
    if expected_contract is None:
        errors.append("acceptance contract has no matching scenario ID")
    elif specification.acceptance_contract != expected_contract:
        errors.append("acceptance contract/scenario identity mismatch")

    planner = specification.planner_contract
    if specification.scenario_id in {"S1-2", "S1-3"} and planner is None:
        errors.append("S1-2/S1-3 require an explicit planner contract")
    if planner is not None:
        if planner.scenario_id != specification.scenario_id:
            errors.append("planner contract/scenario identity mismatch")
        if planner.contract_id != PLANNER_CONTRACT_ID:
            errors.append("planner contract ID is not registered")
        registered = registered_planner_contract(planner.contract_id)
        if registered is None or planner.contract_version != registered.version:
            errors.append("planner contract version does not match registry")
        if planner.source_expectation not in SOURCE_EXPECTATIONS:
            errors.append("planner source expectation is not registered")
        if planner.test_expectation not in TEST_EXPECTATIONS:
            errors.append("planner test expectation is not registered")
        missing_facts = set(_COMMON_PLANNER_FACTS) - set(planner.structural_evidence)
        if missing_facts:
            errors.append(
                "planner structural declarations missing: "
                + ", ".join(sorted(missing_facts))
            )
        if planner.test_expectation != specification.test_expectation:
            errors.append("planner/test expectation mismatch")

    for label, binding, expected_id in (
        ("review", specification.review_contract, REVIEW_CONTRACT_ID),
        ("publication", specification.publication_contract, PUBLICATION_CONTRACT_ID),
    ):
        if binding.scenario_id != specification.scenario_id:
            errors.append(f"{label} contract/scenario identity mismatch")
        if binding.contract_id != expected_id:
            errors.append(f"{label} contract ID is not registered")
        if binding.contract_version != CONTRACT_VERSION:
            errors.append(f"{label} contract version does not match registry")
        if "CONTRACT_REGISTERED" not in binding.structural_evidence:
            errors.append(f"{label} contract is missing CONTRACT_REGISTERED")
        if "SCENARIO_ID_MATCH" not in binding.structural_evidence:
            errors.append(f"{label} contract is missing SCENARIO_ID_MATCH")

    try:
        ReviewExpectation(specification.review_contract.expectation)
    except ValueError:
        errors.append("review expectation is not registered")
    try:
        PublicationExpectation(specification.publication_contract.expectation)
    except ValueError:
        errors.append("publication expectation is not registered")

    known_outcome_values = {
        value.value for value in (*ReviewOutcome, *PublicationOutcome)
    }
    for field_name in (
        "expected_review_outcomes",
        "expected_publication_outcomes",
    ):
        for value in getattr(specification, field_name):
            if value not in known_outcome_values:
                errors.append(f"unknown {field_name} value: {value}")
    if errors:
        raise ScenarioRegistryError(
            f"{specification.scenario_id}: " + "; ".join(errors)
        )


def validate_scenario_registry(
    specifications: Optional[Iterable[ScenarioSpecification]] = None,
    *,
    require_stage1_complete: bool = True,
) -> tuple[str, ...]:
    """Validate uniqueness, completeness, and all contract identities."""

    values = tuple(
        _SCENARIO_SPECIFICATIONS if specifications is None else specifications
    )
    ids = [specification.scenario_id for specification in values]
    duplicates = sorted({scenario_id for scenario_id in ids if ids.count(scenario_id) > 1})
    if duplicates:
        raise ScenarioRegistryError(
            "duplicate scenario IDs: " + ", ".join(duplicates)
        )
    unknown = sorted(set(ids) - set(STAGE1_SCENARIO_IDS))
    if unknown:
        raise ScenarioRegistryError(
            "unknown scenario IDs: " + ", ".join(unknown)
        )
    if require_stage1_complete and set(ids) != set(STAGE1_SCENARIO_IDS):
        missing = sorted(set(STAGE1_SCENARIO_IDS) - set(ids))
        raise ScenarioRegistryError(
            "incomplete Stage 1 registry; missing: " + ", ".join(missing)
        )
    for specification in values:
        validate_scenario_specification(specification)
    return tuple(ids)


def _build_registry() -> dict[str, ScenarioSpecification]:
    validate_scenario_registry()
    return {
        specification.scenario_id: specification
        for specification in _SCENARIO_SPECIFICATIONS
    }


def registered_scenario_ids() -> tuple[str, ...]:
    """Return IDs in canonical matrix order."""

    return STAGE1_SCENARIO_IDS


def scenario_spec(scenario_id: str) -> ScenarioSpecification:
    """Return a complete specification or fail closed for an unknown ID."""

    try:
        return SCENARIO_REGISTRY[scenario_id]
    except KeyError as exc:
        raise KeyError(f"{scenario_id!r} is not a registered Stage 1 scenario") from exc


def validate_requested_scenario_ids(
    scenario_ids: Iterable[str],
) -> ScenarioSelection:
    """Validate a requested matrix before any evidence, DB, or workspace mutation."""

    validate_scenario_registry()
    requested = tuple(str(scenario_id) for scenario_id in scenario_ids)
    if not requested:
        raise ScenarioRegistryError("no scenario IDs requested")
    duplicates = sorted(
        {scenario_id for scenario_id in requested if requested.count(scenario_id) > 1}
    )
    if duplicates:
        raise ScenarioRegistryError(
            "duplicate requested scenario IDs: " + ", ".join(duplicates)
        )
    unknown = sorted(set(requested) - set(STAGE1_SCENARIO_IDS))
    if unknown:
        raise ScenarioRegistryError(
            "unknown requested scenario IDs: " + ", ".join(unknown)
        )
    is_full_matrix = set(requested) == set(STAGE1_SCENARIO_IDS)
    return ScenarioSelection(
        scenario_ids=requested,
        certification_classification=(
            STAGE1_CERTIFICATION_CLASSIFICATION
            if is_full_matrix
            else DEBUG_SUBSET_CLASSIFICATION
        ),
    )


def scenario_contract(scenario_id: str) -> ScenarioAcceptanceContract:
    """Return the retained Phase 30K/31A acceptance contract for one ID."""

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


# The historical acceptance registry remains separate from the complete
# dispatch registry.  Phase 30K replay callers rely on this name/shape.
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


GATING_SCENARIO_IDS = frozenset({"S0-1", "S0-2", "S0-3"})


SCENARIO_REGISTRY = _build_registry()


__all__ = [
    "CONTRACT_VERSION",
    "DEBUG_SUBSET_CLASSIFICATION",
    "GATING_SCENARIO_IDS",
    "PlannerContractBinding",
    "ContractBinding",
    "ScenarioRegistryError",
    "ScenarioSelection",
    "ScenarioSpecification",
    "SCENARIO_REGISTRY",
    "STAGE1_CERTIFICATION_CLASSIFICATION",
    "STAGE1_SCENARIO_IDS",
    "_SCENARIO_TASKS",
    "registered_scenario_ids",
    "scenario_contract",
    "scenario_spec",
    "validate_requested_scenario_ids",
    "validate_scenario_registry",
    "validate_scenario_specification",
]
