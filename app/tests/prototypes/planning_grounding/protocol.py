"""Prototype-only grounding protocol types (PHASE35-PGP1).

These structures exist to evaluate one architectural question: can a bounded,
read-only, model-directed pre-Plan grounding loop acquire the relevant
implementation region for an ordinary product-language Task without granting
any mutation authority?

Nothing here is production code and nothing here is imported by production
code. No structure in this module carries, implies, or can be converted into a
mutation grant: there is no Plan, no APA, no accepted path, and no version
fence, by design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

# --- Hard architectural bounds (harness-enforced, not adapter-enforced) -----

MAX_GROUNDING_REQUESTS = 2
MAX_TOTAL_SOURCE_EVIDENCE_BYTES = 12 * 1024
MAX_DISTINCT_FILES = 4
MAX_PRIMARY_REGIONS = 4
# One primary region may never consume more than its equal share of the total
# evidence budget. This is what stops a 113 KiB function from becoming "the"
# region simply because a task literal appears somewhere inside it.
MAX_REGION_BYTES = MAX_TOTAL_SOURCE_EVIDENCE_BYTES // MAX_PRIMARY_REGIONS

# --- Provenance -------------------------------------------------------------

PROVENANCE_OPERATOR_TASK = "operator_task"
PROVENANCE_DETERMINISTIC_ORIENTATION = "deterministic_orientation"
PROVENANCE_MODEL_REQUEST = "model_request"
PROVENANCE_HARNESS_OBSERVATION = "harness_observation"
# Structural facts the harness derived itself (AST symbol/route ownership).
# They are neither operator authority nor model output.
PROVENANCE_HARNESS_STRUCTURAL_RESOLUTION = "harness_structural_resolution"

ALL_PROVENANCE = (
    PROVENANCE_OPERATOR_TASK,
    PROVENANCE_DETERMINISTIC_ORIENTATION,
    PROVENANCE_MODEL_REQUEST,
    PROVENANCE_HARNESS_OBSERVATION,
    PROVENANCE_HARNESS_STRUCTURAL_RESOLUTION,
)

# --- Action kinds -----------------------------------------------------------

ACTION_SEARCH_TEXT = "search_text"
ACTION_INSPECT_SYMBOL = "inspect_symbol"
ACTION_INSPECT_ROUTE = "inspect_route"
SUPPORTED_ACTIONS = (ACTION_SEARCH_TEXT, ACTION_INSPECT_SYMBOL, ACTION_INSPECT_ROUTE)

# `search_text` has exactly two modes. LITERAL reproduces the current
# production selector shape (one literal, first occurrence). STRUCTURAL asks
# the harness to resolve named symbol/route regions that corroborate a set of
# task terms. `read_region` from the G1R sketch is deliberately absent: no
# fixture needs a raw byte-offset read, and offering one would let a caller
# bypass structural ownership.
SEARCH_MODE_LITERAL = "literal"
SEARCH_MODE_STRUCTURAL = "structural"
SEARCH_MODES = (SEARCH_MODE_LITERAL, SEARCH_MODE_STRUCTURAL)

# --- Decisions --------------------------------------------------------------

DECISION_REQUEST_MORE = "REQUEST_MORE"
DECISION_SUFFICIENT = "SUFFICIENT"
DECISION_INSUFFICIENT = "INSUFFICIENT"

STOP_INSUFFICIENT_GROUNDING = "insufficient_grounding"

# --- Rejection reasons ------------------------------------------------------

REJECT_UNSUPPORTED_ACTION = "unsupported_action_kind"
REJECT_UNSUPPORTED_MODE = "unsupported_search_mode"
REJECT_MUTATION_FIELD = "mutation_field_present"
REJECT_PATH_OUTSIDE_SCOPE = "path_outside_tracked_product_scope"
REJECT_TURN_BUDGET = "grounding_turn_budget_exhausted"
REJECT_EMPTY_QUERY = "empty_query"


@dataclass(frozen=True)
class Budget:
    """Remaining allowance, recomputed by the harness before every request."""

    requests_remaining: int
    evidence_bytes_remaining: int
    files_remaining: int
    regions_remaining: int

    def as_details(self) -> dict[str, int]:
        return {
            "requests_remaining": self.requests_remaining,
            "evidence_bytes_remaining": self.evidence_bytes_remaining,
            "files_remaining": self.files_remaining,
            "regions_remaining": self.regions_remaining,
        }


@dataclass(frozen=True)
class Orientation:
    """Deterministic repository orientation handed to the adapter.

    It is advisory. `is_authoritative_locator` is a constant NO: an oriented
    path is a candidate to look at, never an answer and never a mutation
    target.
    """

    available: bool
    paths: tuple[str, ...]
    literals: tuple[str, ...]
    entries_total: int
    truncated: bool
    unavailable_reason: str | None
    provenance: str = PROVENANCE_DETERMINISTIC_ORIENTATION
    is_authoritative_locator: bool = False


@dataclass(frozen=True)
class StructuralIdentity:
    """Harness-derived ownership of a bounded region."""

    kind: str  # "function" | "class" | "route" | "file_window"
    name: str | None
    http_method: str | None
    route_path: str | None
    start_line: int
    end_line: int
    region_start_byte: int
    region_end_byte: int
    provenance: str = PROVENANCE_HARNESS_STRUCTURAL_RESOLUTION

    @property
    def locator(self) -> str:
        if self.kind == "route":
            return f"route:{self.http_method} {self.route_path} -> {self.name}"
        if self.name:
            return f"{self.kind}:{self.name}"
        return f"{self.kind}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class GroundingAction:
    """A read-only action request. It can express nothing else."""

    kind: str
    scope_paths: tuple[str, ...] = ()
    query: str | None = None
    mode: str | None = None
    terms: tuple[str, ...] = ()
    http_methods: tuple[str, ...] = ()
    symbol_name: str | None = None
    route_method: str | None = None
    route_path: str | None = None
    max_results: int = 1
    provenance: str = PROVENANCE_MODEL_REQUEST

    def replay_key(self) -> tuple:
        return (
            self.kind,
            self.scope_paths,
            self.query,
            self.mode,
            self.terms,
            self.http_methods,
            self.symbol_name,
            self.route_method,
            self.route_path,
            self.max_results,
        )


@dataclass(frozen=True)
class GroundingRequest:
    """What the adapter is given. `task_text` is operator text and nothing else."""

    task_text: str
    task_text_provenance: str
    orientation: Orientation
    prior_observation_ids: tuple[str, ...]
    remaining_budget: Budget
    turn: int


@dataclass(frozen=True)
class GroundingObservation:
    observation_id: str
    action: GroundingAction
    source_path: str
    source_version: str
    bounded_content: bytes
    structural_identity: StructuralIdentity | None
    provenance: str
    byte_count: int
    truncated: bool = False
    notes: Mapping[str, object] = field(default_factory=dict)

    def replay_key(self) -> tuple:
        identity = None
        if self.structural_identity is not None:
            identity = (
                self.structural_identity.kind,
                self.structural_identity.name,
                self.structural_identity.http_method,
                self.structural_identity.route_path,
                self.structural_identity.region_start_byte,
                self.structural_identity.region_end_byte,
            )
        return (
            self.observation_id,
            self.source_path,
            self.source_version,
            self.byte_count,
            identity,
            self.bounded_content,
        )


@dataclass(frozen=True)
class SufficiencyClaim:
    """The whole terminal contract. Deliberately not APA-shaped.

    There is no operation, no target span for replacement, no accepted-path
    set, and no version fence: this object cannot be turned into a grant.
    """

    cited_observation_ids: tuple[str, ...]
    relevant_source_paths: tuple[str, ...]
    structural_locators: tuple[str, ...]
    source_versions: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GroundingDecision:
    decision: str
    sufficiency: SufficiencyClaim | None = None
    stop_reason: str | None = None
    rationale: str = ""


@dataclass(frozen=True)
class GroundingOutcome:
    """Everything one bounded grounding lifecycle produced."""

    decision: GroundingDecision
    requests: tuple[GroundingAction, ...]
    observations: tuple[GroundingObservation, ...]
    rejections: tuple[tuple[GroundingAction, str], ...]
    orientation: Orientation
    task_text: str
    budget_trace: tuple[Budget, ...]
    final_budget: Budget

    # Grounding never reaches any of these. They are recorded as constants so
    # the test suite can assert the authority boundary directly.
    plan_created: bool = False
    apa_created: bool = False
    mutation_authority_granted: bool = False
    controlled_apply_reached: bool = False
