"""Independent product, review, publication, and certification outcomes.

The Phase 30K acceptance classifier remains the historical compatibility
boundary.  This module is the Phase 31D semantic boundary: it consumes a
registered outcome contract, registered structural evidence, and authoritative
runtime/product/review/publication records.  It never reads prompts or logs
and never infers publication from runtime completion.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class RuntimeOutcome(str, Enum):
    RUNTIME_NOT_STARTED = "RUNTIME_NOT_STARTED"
    RUNTIME_IN_PROGRESS = "RUNTIME_IN_PROGRESS"
    RUNTIME_COMPLETED = "RUNTIME_COMPLETED"
    RUNTIME_FAILED = "RUNTIME_FAILED"
    RUNTIME_CANCELLED = "RUNTIME_CANCELLED"
    RUNTIME_TIMED_OUT = "RUNTIME_TIMED_OUT"


class ProductOutcome(str, Enum):
    SUCCESS_PUBLISHED = "SUCCESS_PUBLISHED"
    SUCCESS_REQUIRES_REVIEW = "SUCCESS_REQUIRES_REVIEW"
    SUCCESS_PARTIAL = "SUCCESS_PARTIAL"
    SAFE_FAILURE = "SAFE_FAILURE"
    EXPECTED_LIMITATION = "EXPECTED_LIMITATION"


class CertificationOutcome(str, Enum):
    CERTIFICATION_ACCEPTED = "CERTIFICATION_ACCEPTED"
    CERTIFICATION_ACCEPTED_WITH_LIMITATION = "CERTIFICATION_ACCEPTED_WITH_LIMITATION"
    CERTIFICATION_EXPECTATION_MISMATCH = "CERTIFICATION_EXPECTATION_MISMATCH"
    CERTIFICATION_INVALID_EVIDENCE = "CERTIFICATION_INVALID_EVIDENCE"
    CERTIFICATION_SUSPENDED = "CERTIFICATION_SUSPENDED"


class ReviewExpectation(str, Enum):
    REVIEW_NOT_APPLICABLE = "REVIEW_NOT_APPLICABLE"
    REVIEW_NOT_REQUIRED = "REVIEW_NOT_REQUIRED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REVIEW_POLICY_DERIVED = "REVIEW_POLICY_DERIVED"


class ReviewOutcome(str, Enum):
    REVIEW_NOT_APPLICABLE = "REVIEW_NOT_APPLICABLE"
    REVIEW_NOT_REQUIRED = "REVIEW_NOT_REQUIRED"
    REVIEW_REQUIRED_PENDING = "REVIEW_REQUIRED_PENDING"
    REVIEW_HELD = "REVIEW_HELD"
    REVIEW_APPROVED = "REVIEW_APPROVED"
    REVIEW_REJECTED = "REVIEW_REJECTED"
    REVIEW_EXPECTATION_NOT_MET = "REVIEW_EXPECTATION_NOT_MET"


class PublicationExpectation(str, Enum):
    PUBLICATION_FORBIDDEN = "PUBLICATION_FORBIDDEN"
    PUBLICATION_ALLOWED = "PUBLICATION_ALLOWED"
    PUBLICATION_REQUIRED = "PUBLICATION_REQUIRED"


class PublicationOutcome(str, Enum):
    PUBLICATION_NOT_APPLICABLE = "PUBLICATION_NOT_APPLICABLE"
    PUBLICATION_NOT_PUBLISHED = "PUBLICATION_NOT_PUBLISHED"
    PUBLICATION_HELD_FOR_REVIEW = "PUBLICATION_HELD_FOR_REVIEW"
    PUBLICATION_PUBLISHED = "PUBLICATION_PUBLISHED"
    PUBLICATION_FAILED = "PUBLICATION_FAILED"
    PUBLICATION_PUBLISHED_UNEXPECTEDLY = "PUBLICATION_PUBLISHED_UNEXPECTEDLY"


class ExpectationComparison(str, Enum):
    PUBLICATION_EXPECTATION_MATCHED = "PUBLICATION_EXPECTATION_MATCHED"
    CERTIFICATION_EXPECTATION_MISMATCH = "CERTIFICATION_EXPECTATION_MISMATCH"


# The product contract vocabulary uses this name in the structural evidence
# registration.  Keep the more explicit alias available to callers too.
PublicationExpectationComparison = ExpectationComparison


class OutcomeEvidenceError(ValueError):
    """Raised when an authoritative contract or fact record is incomplete."""


@dataclass(frozen=True)
class RegisteredStructuralEvidence:
    """Evidence facts owned by the registered structural-evidence registry."""

    contract_id: str
    contract_version: str
    facts: Mapping[str, Any]


@dataclass(frozen=True)
class RegisteredOutcomeContract:
    """The registered review/publication envelope for one scenario lane."""

    contract_id: str
    contract_version: str
    review_expectation: ReviewExpectation | str
    publication_expectation: PublicationExpectation | str
    required_structural_evidence: tuple[str, ...] = ()
    permitted_review_outcomes: tuple[ReviewOutcome | str, ...] = ()
    permitted_publication_outcomes: tuple[PublicationOutcome | str, ...] = ()
    limitation_id: Optional[str] = None

    def normalized_review_expectation(self) -> ReviewExpectation:
        return _coerce_enum(
            ReviewExpectation, self.review_expectation, "review_expectation"
        )

    def normalized_publication_expectation(self) -> PublicationExpectation:
        return _coerce_enum(
            PublicationExpectation,
            self.publication_expectation,
            "publication_expectation",
        )


# Names used by different Phase 31D documents are intentionally compatible.
ScenarioOutcomeContract = RegisteredOutcomeContract
ReviewPublicationContract = RegisteredOutcomeContract


@dataclass(frozen=True)
class OutcomeAdjudicationResult:
    """All independent semantic layers for one recorded scenario."""

    contract_id: str
    contract_version: str
    runtime_outcome: RuntimeOutcome
    product_outcome: ProductOutcome
    review_outcome: ReviewOutcome
    publication_outcome: PublicationOutcome
    publication_expectation_comparison: ExpectationComparison
    certification_outcome: CertificationOutcome
    structural_evidence_used: tuple[str, ...]

    @property
    def comparison(self) -> ExpectationComparison:
        """Short alias for report consumers."""

        return self.publication_expectation_comparison

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "runtime_outcome": self.runtime_outcome.value,
            "product_outcome": self.product_outcome.value,
            "review_outcome": self.review_outcome.value,
            "publication_outcome": self.publication_outcome.value,
            "publication_expectation_comparison": (
                self.publication_expectation_comparison.value
            ),
            "certification_outcome": self.certification_outcome.value,
            "structural_evidence_used": list(self.structural_evidence_used),
        }


def _coerce_enum(enum_type: type[Enum], value: Any, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value))
    except ValueError as exc:
        raise OutcomeEvidenceError(f"invalid {field_name}: {value!r}") from exc


def _mapping(value: Mapping[str, Any] | Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OutcomeEvidenceError(f"{field_name} must be a mapping")
    return value


def _structural_facts(
    contract: RegisteredOutcomeContract,
    evidence: RegisteredStructuralEvidence | Mapping[str, Any],
) -> tuple[Mapping[str, Any], tuple[str, ...]]:
    if isinstance(evidence, RegisteredStructuralEvidence):
        evidence_id = evidence.contract_id
        evidence_version = evidence.contract_version
        facts = evidence.facts
    else:
        payload = _mapping(evidence, "structural_evidence")
        evidence_id = payload.get("contract_id")
        evidence_version = payload.get("contract_version")
        facts = payload.get("facts")

    if evidence_id != contract.contract_id:
        raise OutcomeEvidenceError(
            "structural evidence contract_id does not match registered contract"
        )
    if evidence_version != contract.contract_version:
        raise OutcomeEvidenceError(
            "structural evidence contract_version does not match registered contract"
        )
    fact_map = _mapping(facts, "structural_evidence.facts")

    required = list(contract.required_structural_evidence)
    review_expectation = contract.normalized_review_expectation()
    publication_expectation = contract.normalized_publication_expectation()
    if review_expectation != ReviewExpectation.REVIEW_NOT_APPLICABLE:
        required.append("REVIEW_DECISION_RECORDED")
    if publication_expectation is not None:
        required.append("PUBLICATION_FACT_RECORDED")

    missing = sorted(
        {
            fact_name
            for fact_name in required
            if fact_name not in fact_map or fact_map[fact_name] is False
        }
    )
    if missing:
        raise OutcomeEvidenceError(
            "missing registered structural evidence: " + ", ".join(missing)
        )
    return fact_map, tuple(sorted(set(required)))


def _runtime_outcome(
    record: RuntimeOutcome | str | Mapping[str, Any]
) -> RuntimeOutcome:
    if isinstance(record, (RuntimeOutcome, str)):
        return _coerce_enum(RuntimeOutcome, record, "runtime_outcome")
    payload = _mapping(record, "runtime_record")
    if "outcome" not in payload:
        raise OutcomeEvidenceError(
            "runtime_record.outcome is required; completion is not inferred"
        )
    return _coerce_enum(RuntimeOutcome, payload["outcome"], "runtime_outcome")


def _review_record_state(record: Mapping[str, Any]) -> Optional[ReviewOutcome]:
    payload = _mapping(record, "review_record")
    outcome_value = payload.get("outcome", payload.get("review_outcome"))
    if outcome_value is not None:
        raw = str(outcome_value)
        aliases = {
            "hold_for_review": ReviewOutcome.REVIEW_HELD,
            "pending": ReviewOutcome.REVIEW_REQUIRED_PENDING,
            "approved": ReviewOutcome.REVIEW_APPROVED,
            "rejected": ReviewOutcome.REVIEW_REJECTED,
            "auto_promote": None,
            "review_not_required": None,
            "review_not_applicable": ReviewOutcome.REVIEW_NOT_APPLICABLE,
        }
        if raw in aliases:
            return aliases[raw]
        try:
            return _coerce_enum(ReviewOutcome, raw, "review_record.outcome")
        except OutcomeEvidenceError:
            raise
    if "held_for_review" in payload:
        held = payload["held_for_review"]
        if not isinstance(held, bool):
            raise OutcomeEvidenceError("review_record.held_for_review must be boolean")
        return ReviewOutcome.REVIEW_HELD if held else None
    raise OutcomeEvidenceError(
        "review_record must contain an explicit outcome or held_for_review field"
    )


def derive_review_outcome(
    contract: RegisteredOutcomeContract,
    review_record: Mapping[str, Any],
) -> ReviewOutcome:
    """Derive review state from the registered expectation and review record."""

    expectation = contract.normalized_review_expectation()
    observed = _review_record_state(review_record)
    if observed is not None:
        return observed
    if expectation == ReviewExpectation.REVIEW_NOT_APPLICABLE:
        return ReviewOutcome.REVIEW_NOT_APPLICABLE
    if expectation == ReviewExpectation.REVIEW_NOT_REQUIRED:
        return ReviewOutcome.REVIEW_NOT_REQUIRED
    return ReviewOutcome.REVIEW_EXPECTATION_NOT_MET


def _publication_record_state(
    record: Mapping[str, Any],
) -> PublicationOutcome:
    payload = _mapping(record, "publication_record")
    outcome_value = payload.get("outcome", payload.get("publication_outcome"))
    if outcome_value is not None:
        raw = str(outcome_value)
        aliases = {
            "not_applicable": PublicationOutcome.PUBLICATION_NOT_APPLICABLE,
            "not_published": PublicationOutcome.PUBLICATION_NOT_PUBLISHED,
            "held_for_review": PublicationOutcome.PUBLICATION_HELD_FOR_REVIEW,
            "published": PublicationOutcome.PUBLICATION_PUBLISHED,
            "failed": PublicationOutcome.PUBLICATION_FAILED,
            "published_unexpectedly": (
                PublicationOutcome.PUBLICATION_PUBLISHED_UNEXPECTEDLY
            ),
        }
        if raw in aliases:
            return aliases[raw]
        return _coerce_enum(PublicationOutcome, raw, "publication_record.outcome")

    if "status" in payload:
        raw_status = str(payload["status"])
        aliases = {
            "not_applicable": PublicationOutcome.PUBLICATION_NOT_APPLICABLE,
            "not_published": PublicationOutcome.PUBLICATION_NOT_PUBLISHED,
            "held_for_review": PublicationOutcome.PUBLICATION_HELD_FOR_REVIEW,
            "published": PublicationOutcome.PUBLICATION_PUBLISHED,
            "failed": PublicationOutcome.PUBLICATION_FAILED,
            "published_unexpectedly": (
                PublicationOutcome.PUBLICATION_PUBLISHED_UNEXPECTEDLY
            ),
        }
        if raw_status in aliases:
            return aliases[raw_status]
        return _coerce_enum(PublicationOutcome, raw_status, "publication_record.status")
    if "published" in payload:
        published = payload["published"]
        if not isinstance(published, bool):
            raise OutcomeEvidenceError("publication_record.published must be boolean")
        if published:
            return PublicationOutcome.PUBLICATION_PUBLISHED
        if payload.get("held_for_review") is True:
            return PublicationOutcome.PUBLICATION_HELD_FOR_REVIEW
        if payload.get("failed") is True:
            return PublicationOutcome.PUBLICATION_FAILED
        return PublicationOutcome.PUBLICATION_NOT_PUBLISHED
    raise OutcomeEvidenceError(
        "publication_record must contain an explicit outcome, status, or published field"
    )


def derive_publication_outcome(
    contract: RegisteredOutcomeContract,
    review_outcome: ReviewOutcome,
    publication_record: Mapping[str, Any],
) -> PublicationOutcome:
    """Derive publication state from the publication fact and review guard."""

    expectation = contract.normalized_publication_expectation()
    if expectation is None:  # pragma: no cover - enum validation makes this impossible
        return PublicationOutcome.PUBLICATION_NOT_APPLICABLE
    observed = _publication_record_state(publication_record)
    if observed == PublicationOutcome.PUBLICATION_PUBLISHED and (
        review_outcome
        in {
            ReviewOutcome.REVIEW_HELD,
            ReviewOutcome.REVIEW_REQUIRED_PENDING,
            ReviewOutcome.REVIEW_EXPECTATION_NOT_MET,
        }
        or expectation == PublicationExpectation.PUBLICATION_FORBIDDEN
    ):
        return PublicationOutcome.PUBLICATION_PUBLISHED_UNEXPECTEDLY
    return observed


def _product_outcome(
    product_state: Mapping[str, Any],
    review_outcome: ReviewOutcome,
    publication_outcome: PublicationOutcome,
    contract: RegisteredOutcomeContract,
) -> ProductOutcome:
    payload = _mapping(product_state, "product_state")
    if "outcome" in payload:
        return _coerce_enum(ProductOutcome, payload["outcome"], "product_state.outcome")
    if "useful_work_completed" not in payload:
        raise OutcomeEvidenceError(
            "product_state.useful_work_completed is required; product success is not inferred"
        )
    useful_work_completed = payload["useful_work_completed"]
    if not isinstance(useful_work_completed, bool):
        raise OutcomeEvidenceError(
            "product_state.useful_work_completed must be boolean"
        )
    if not useful_work_completed:
        if contract.limitation_id or payload.get("limitation_id"):
            return ProductOutcome.EXPECTED_LIMITATION
        return ProductOutcome.SAFE_FAILURE
    if (
        review_outcome
        in {
            ReviewOutcome.REVIEW_HELD,
            ReviewOutcome.REVIEW_REQUIRED_PENDING,
        }
        or publication_outcome == PublicationOutcome.PUBLICATION_HELD_FOR_REVIEW
    ):
        return ProductOutcome.SUCCESS_REQUIRES_REVIEW
    if publication_outcome in {
        PublicationOutcome.PUBLICATION_PUBLISHED,
        PublicationOutcome.PUBLICATION_PUBLISHED_UNEXPECTEDLY,
    }:
        return ProductOutcome.SUCCESS_PUBLISHED
    return ProductOutcome.SUCCESS_PARTIAL


def _review_expectation_matches(
    expectation: ReviewExpectation,
    outcome: ReviewOutcome,
    permitted: tuple[ReviewOutcome | str, ...],
) -> bool:
    if outcome in {
        _coerce_enum(ReviewOutcome, value, "permitted_review_outcome")
        for value in permitted
    }:
        return True
    if expectation == ReviewExpectation.REVIEW_NOT_APPLICABLE:
        return outcome == ReviewOutcome.REVIEW_NOT_APPLICABLE
    if expectation == ReviewExpectation.REVIEW_NOT_REQUIRED:
        return outcome == ReviewOutcome.REVIEW_NOT_REQUIRED
    return outcome in {
        ReviewOutcome.REVIEW_REQUIRED_PENDING,
        ReviewOutcome.REVIEW_HELD,
        ReviewOutcome.REVIEW_APPROVED,
        ReviewOutcome.REVIEW_REJECTED,
    }


def _publication_expectation_matches(
    expectation: PublicationExpectation,
    outcome: PublicationOutcome,
    permitted: tuple[PublicationOutcome | str, ...],
) -> bool:
    if outcome in {
        _coerce_enum(PublicationOutcome, value, "permitted_publication_outcome")
        for value in permitted
    }:
        return True
    if outcome == PublicationOutcome.PUBLICATION_PUBLISHED_UNEXPECTEDLY:
        return False
    if expectation == PublicationExpectation.PUBLICATION_FORBIDDEN:
        return outcome in {
            PublicationOutcome.PUBLICATION_NOT_APPLICABLE,
            PublicationOutcome.PUBLICATION_NOT_PUBLISHED,
            PublicationOutcome.PUBLICATION_HELD_FOR_REVIEW,
        }
    if expectation == PublicationExpectation.PUBLICATION_ALLOWED:
        return outcome in {
            PublicationOutcome.PUBLICATION_NOT_APPLICABLE,
            PublicationOutcome.PUBLICATION_NOT_PUBLISHED,
            PublicationOutcome.PUBLICATION_HELD_FOR_REVIEW,
            PublicationOutcome.PUBLICATION_PUBLISHED,
        }
    return outcome == PublicationOutcome.PUBLICATION_PUBLISHED


def derive_review_publication_outcomes(
    contract: RegisteredOutcomeContract,
    structural_evidence: RegisteredStructuralEvidence | Mapping[str, Any],
    runtime_record: RuntimeOutcome | str | Mapping[str, Any],
    product_state: Mapping[str, Any],
    review_record: Mapping[str, Any],
    publication_record: Mapping[str, Any],
) -> OutcomeAdjudicationResult:
    """Derive independent outcomes from registered and authoritative records."""

    _, evidence_used = _structural_facts(contract, structural_evidence)
    runtime = _runtime_outcome(runtime_record)
    review = derive_review_outcome(contract, review_record)
    publication = (
        PublicationOutcome.PUBLICATION_NOT_APPLICABLE
        if contract.normalized_publication_expectation() is None
        else derive_publication_outcome(contract, review, publication_record)
    )
    product = _product_outcome(product_state, review, publication, contract)
    review_matches = _review_expectation_matches(
        contract.normalized_review_expectation(),
        review,
        contract.permitted_review_outcomes,
    )
    publication_matches = _publication_expectation_matches(
        contract.normalized_publication_expectation(),
        publication,
        contract.permitted_publication_outcomes,
    )
    comparison = (
        ExpectationComparison.PUBLICATION_EXPECTATION_MATCHED
        if review_matches and publication_matches
        else ExpectationComparison.CERTIFICATION_EXPECTATION_MISMATCH
    )
    if comparison == ExpectationComparison.CERTIFICATION_EXPECTATION_MISMATCH:
        certification = CertificationOutcome.CERTIFICATION_EXPECTATION_MISMATCH
    elif contract.limitation_id:
        certification = CertificationOutcome.CERTIFICATION_ACCEPTED_WITH_LIMITATION
    else:
        certification = CertificationOutcome.CERTIFICATION_ACCEPTED
    return OutcomeAdjudicationResult(
        contract_id=contract.contract_id,
        contract_version=contract.contract_version,
        runtime_outcome=runtime,
        product_outcome=product,
        review_outcome=review,
        publication_outcome=publication,
        publication_expectation_comparison=comparison,
        certification_outcome=certification,
        structural_evidence_used=evidence_used,
    )


# A concise name for callers that already use the five-layer model.
derive_independent_outcomes = derive_review_publication_outcomes
