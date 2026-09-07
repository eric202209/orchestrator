"""Prototype grounding harness (PHASE35-PGP1).

The harness owns everything that must not be delegated to a model: scope,
action schema, execution, structural resolution, budget accounting,
provenance, and fail-closed termination. The adapter owns only *which*
read-only question to ask and *whether* the returned evidence is enough.

The harness executes reads. The adapter never touches the filesystem.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Callable, Sequence

from app.services.orchestration.planning.repository_orientation import (
    derive_repository_orientation,
    orientation_task_literals,
    tracked_product_paths,
)

from . import protocol as P
from .structure import (
    SymbolRegion,
    file_window_identity,
    fold_token,
    owning_region,
    mounted_router_prefixes,
    read_tracked_source,
    symbol_regions,
    tokenize,
)

# The prototype's own package is excluded from grounding scope. It is
# scaffolding for the evaluation, not product source, and letting the harness
# ground on it would make fixture results depend on whether the prototype
# happens to be committed.
PROTOTYPE_SCOPE_EXCLUSION_PREFIX = "app/tests/prototypes/"

# Fields whose presence would make an action anything other than read-only.
_MUTATION_FIELD_NAMES = (
    "write",
    "replace",
    "content",
    "patch",
    "operation",
    "command",
    "shell",
)


class GroundingAdapter:
    """Provider-free decision policy interface."""

    def propose(
        self,
        request: P.GroundingRequest,
        observations: Sequence[P.GroundingObservation],
    ) -> P.GroundingAction | P.GroundingDecision:  # pragma: no cover - interface
        raise NotImplementedError


def source_version(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]


def derive_orientation(project_dir: Path, task_text: str) -> P.Orientation:
    """Deterministic orientation, reusing the existing production function."""

    derived = derive_repository_orientation(project_dir, task_text)
    paths = tuple(
        path
        for path in derived.paths
        if not path.startswith(PROTOTYPE_SCOPE_EXCLUSION_PREFIX)
    )
    return P.Orientation(
        available=derived.available and bool(paths),
        paths=paths,
        literals=orientation_task_literals(task_text),
        entries_total=derived.entries_total,
        truncated=derived.truncated,
        unavailable_reason=derived.unavailable_reason,
    )


class GroundingHarness:
    def __init__(self, project_dir: Path) -> None:
        self.project_dir = Path(project_dir)
        tracked = tracked_product_paths(self.project_dir)
        self._tracked: frozenset[str] = frozenset(
            path
            for path in (tracked or ())
            if not path.startswith(PROTOTYPE_SCOPE_EXCLUSION_PREFIX)
        )
        self._source_cache: dict[str, bytes] = {}
        self._region_cache: dict[str, tuple[SymbolRegion, ...]] = {}
        self._route_prefix_cache: dict[str, tuple[str, ...]] = {}

    # -- scope ------------------------------------------------------------

    def in_scope(self, relative_path: str) -> bool:
        return relative_path in self._tracked

    def _source(self, relative_path: str) -> bytes | None:
        if relative_path in self._source_cache:
            return self._source_cache[relative_path]
        raw = read_tracked_source(self.project_dir, relative_path)
        if raw is None:
            return None
        self._source_cache[relative_path] = raw
        return raw

    def _regions(self, relative_path: str) -> tuple[SymbolRegion, ...]:
        if relative_path not in self._region_cache:
            raw = self._source(relative_path) or b""
            if relative_path not in self._route_prefix_cache:
                self._route_prefix_cache[relative_path] = mounted_router_prefixes(
                    self.project_dir,
                    relative_path,
                    tuple(sorted(self._tracked)),
                )
            self._region_cache[relative_path] = symbol_regions(
                relative_path,
                raw,
                self._route_prefix_cache[relative_path],
            )
        return self._region_cache[relative_path]

    # -- validation -------------------------------------------------------

    def validate(self, action: P.GroundingAction, budget: P.Budget) -> str | None:
        """Return a rejection reason, or None when the action is admissible."""

        if budget.requests_remaining <= 0:
            return P.REJECT_TURN_BUDGET
        if action.kind not in P.SUPPORTED_ACTIONS:
            return P.REJECT_UNSUPPORTED_ACTION
        for name in _MUTATION_FIELD_NAMES:
            if hasattr(action, name):
                return P.REJECT_MUTATION_FIELD
        if action.kind == P.ACTION_SEARCH_TEXT:
            if action.mode not in P.SEARCH_MODES:
                return P.REJECT_UNSUPPORTED_MODE
            if (
                action.mode == P.SEARCH_MODE_LITERAL
                and not (action.query or "").strip()
            ):
                return P.REJECT_EMPTY_QUERY
            if action.mode == P.SEARCH_MODE_STRUCTURAL and not action.terms:
                return P.REJECT_EMPTY_QUERY
        if (
            action.kind == P.ACTION_INSPECT_SYMBOL
            and not (action.symbol_name or "").strip()
        ):
            return P.REJECT_EMPTY_QUERY
        if (
            action.kind == P.ACTION_INSPECT_ROUTE
            and not (action.route_path or "").strip()
        ):
            return P.REJECT_EMPTY_QUERY
        if not action.scope_paths:
            return P.REJECT_PATH_OUTSIDE_SCOPE
        for candidate in action.scope_paths:
            if not self.in_scope(candidate):
                return P.REJECT_PATH_OUTSIDE_SCOPE
        return None

    # -- execution --------------------------------------------------------

    def execute(
        self,
        action: P.GroundingAction,
        budget: P.Budget,
        used_paths: set[str],
        turn: int,
        next_index: int,
    ) -> list[P.GroundingObservation]:
        if action.kind == P.ACTION_SEARCH_TEXT and action.mode == P.SEARCH_MODE_LITERAL:
            candidates = self._literal_candidates(action)
        elif action.kind == P.ACTION_SEARCH_TEXT:
            candidates = self._structural_candidates(action)
        elif action.kind == P.ACTION_INSPECT_SYMBOL:
            candidates = self._symbol_candidates(action)
        else:
            candidates = self._route_candidates(action)
        return self._materialize(
            candidates, action, budget, used_paths, turn, next_index
        )

    def _literal_candidates(
        self, action: P.GroundingAction
    ) -> list[tuple[str, int, int, P.StructuralIdentity | None, dict[str, object]]]:
        """First byte occurrence per file, in caller-supplied scope order.

        This deliberately reproduces the current production selector shape:
        one literal, `bytes.find`, first occurrence wins.
        """

        found: list[
            tuple[str, int, int, P.StructuralIdentity | None, dict[str, object]]
        ] = []
        needle = (action.query or "").encode("utf-8")
        for path in action.scope_paths:
            raw = self._source(path)
            if raw is None:
                continue
            offset = raw.find(needle)
            if offset < 0:
                continue
            owner = owning_region(self._regions(path), offset)
            if owner is not None:
                start, end = owner.start_byte, owner.end_byte
                identity = owner.identity()
            else:
                start = max(0, offset - P.MAX_REGION_BYTES // 2)
                end = min(len(raw), start + P.MAX_REGION_BYTES)
                start = raw.rfind(b"\n", 0, start) + 1
                identity = file_window_identity(raw, start, end)
            found.append(
                (
                    path,
                    start,
                    end,
                    identity,
                    {
                        "match_byte": offset,
                        "match_count": raw.count(needle),
                        "selection_strategy": "first_literal_occurrence",
                    },
                )
            )
            if len(found) >= max(1, action.max_results):
                break
        return found

    def _structural_candidates(
        self, action: P.GroundingAction
    ) -> list[tuple[str, int, int, P.StructuralIdentity | None, dict[str, object]]]:
        """Rank named symbol regions by task-term corroboration.

        Admission floor: the region's own *identity* (symbol name or route
        path) must carry at least one task term. A region that merely contains
        a task literal somewhere in its body is never a candidate -- that is
        exactly the failure mode the current selector exhibits.
        """

        terms = {fold_token(term.lower()) for term in action.terms}
        methods = {method.upper() for method in action.http_methods}
        scored: list[
            tuple[tuple, str, int, int, P.StructuralIdentity, dict[str, object]]
        ] = []
        for path in action.scope_paths:
            raw = self._source(path)
            if raw is None:
                continue
            for region in self._regions(path):
                if region.route_path and methods and region.http_method not in methods:
                    continue
                bounded_end = min(
                    region.end_byte, region.start_byte + P.MAX_REGION_BYTES
                )
                body = raw[region.start_byte : bounded_end]
                identity_tokens = tokenize(f"{region.name} {region.route_path or ''}")
                identity_terms = sorted(terms & identity_tokens)
                if not identity_terms:
                    continue
                body_terms = sorted(terms & tokenize(body.decode("utf-8", "replace")))
                route_coverage = 0.0
                if region.route_path:
                    segments = [s for s in region.route_path.split("/") if s]
                    if segments:
                        covered = sum(
                            1
                            for segment in segments
                            if not segment.startswith("{")
                            and (tokenize(segment) & terms)
                        )
                        route_coverage = covered / len(segments)
                score = 3 * len(identity_terms) + len(body_terms) + 3 * route_coverage
                scored.append(
                    (
                        (-score, path, region.start_byte),
                        path,
                        region.start_byte,
                        region.end_byte,
                        region.identity(),
                        {
                            "identity_terms": tuple(identity_terms),
                            "body_terms": tuple(body_terms),
                            "route_segment_coverage": round(route_coverage, 3),
                            "corroboration_score": round(score, 3),
                            "selection_strategy": "structural_term_corroboration",
                        },
                    )
                )
        scored.sort(key=lambda item: item[0])
        limit = max(1, action.max_results)
        return [(p, s, e, i, n) for _, p, s, e, i, n in scored[:limit]]

    def _symbol_candidates(
        self, action: P.GroundingAction
    ) -> list[tuple[str, int, int, P.StructuralIdentity | None, dict[str, object]]]:
        out = []
        for path in action.scope_paths:
            for region in self._regions(path):
                if region.name == action.symbol_name:
                    out.append(
                        (
                            path,
                            region.start_byte,
                            region.end_byte,
                            region.identity(),
                            {"selection_strategy": "symbol_definition_resolution"},
                        )
                    )
        return out[: max(1, action.max_results)]

    def _route_candidates(
        self, action: P.GroundingAction
    ) -> list[tuple[str, int, int, P.StructuralIdentity | None, dict[str, object]]]:
        out = []
        method = (action.route_method or "").upper() or None
        for path in action.scope_paths:
            for region in self._regions(path):
                if region.route_path != action.route_path:
                    continue
                if method and region.http_method != method:
                    continue
                out.append(
                    (
                        path,
                        region.start_byte,
                        region.end_byte,
                        region.identity(),
                        {"selection_strategy": "route_handler_resolution"},
                    )
                )
        return out[: max(1, action.max_results)]

    def _materialize(
        self,
        candidates,
        action: P.GroundingAction,
        budget: P.Budget,
        used_paths: set[str],
        turn: int,
        next_index: int,
    ) -> list[P.GroundingObservation]:
        observations: list[P.GroundingObservation] = []
        if not candidates:
            # A valid read-only lookup that ran out of matches is still a
            # bounded observation. It does not consume source bytes or a
            # structural-region slot, but the surrounding request already
            # consumes one grounding turn in run_grounding().
            if budget.evidence_bytes_remaining <= 0 or budget.regions_remaining <= 0:
                return observations
            notes: dict[str, object] = {
                "outcome": P.OBSERVATION_NOT_FOUND,
                "requested_scope": tuple(action.scope_paths),
            }
            if action.kind == P.ACTION_INSPECT_ROUTE:
                notes.update(
                    {
                        "requested_method": (action.route_method or "").upper(),
                        "requested_path": action.route_path or "",
                        "route_declaration_count": sum(
                            1
                            for path in action.scope_paths
                            for region in self._regions(path)
                            if region.route_path is not None
                        ),
                    }
                )
            elif action.kind == P.ACTION_INSPECT_SYMBOL:
                notes["requested_symbol"] = action.symbol_name or ""
            else:
                notes["requested_query"] = action.query or ""
            observations.append(
                P.GroundingObservation(
                    observation_id=f"obs-{turn}-{next_index}",
                    action=action,
                    source_path=action.scope_paths[0],
                    source_version="not_materialized",
                    bounded_content=b"",
                    structural_identity=None,
                    provenance=P.PROVENANCE_HARNESS_OBSERVATION,
                    byte_count=0,
                    outcome=P.OBSERVATION_NOT_FOUND,
                    notes=notes,
                )
            )
            return observations
        bytes_remaining = budget.evidence_bytes_remaining
        regions_remaining = budget.regions_remaining
        files_remaining = budget.files_remaining
        index = next_index
        for path, start, end, identity, notes in candidates:
            if regions_remaining <= 0 or bytes_remaining <= 0:
                break
            if path not in used_paths and files_remaining <= 0:
                continue
            raw = self._source(path)
            if raw is None:
                continue
            allowance = min(P.MAX_REGION_BYTES, bytes_remaining, end - start)
            if allowance <= 0:
                continue
            content = raw[start : start + allowance]
            truncated = (start + allowance) < end
            if path not in used_paths:
                used_paths.add(path)
                files_remaining -= 1
            bytes_remaining -= len(content)
            regions_remaining -= 1
            observations.append(
                P.GroundingObservation(
                    observation_id=f"obs-{turn}-{index}",
                    action=action,
                    source_path=path,
                    source_version=source_version(raw),
                    bounded_content=content,
                    structural_identity=identity,
                    provenance=P.PROVENANCE_HARNESS_OBSERVATION,
                    byte_count=len(content),
                    truncated=truncated,
                    notes=dict(notes),
                )
            )
            index += 1
        return observations


def run_grounding(
    project_dir: Path,
    task_text: str,
    adapter: GroundingAdapter,
    *,
    harness: GroundingHarness | None = None,
) -> P.GroundingOutcome:
    """One bounded pre-Plan grounding lifecycle. Creates no Plan and no APA."""

    harness = harness or GroundingHarness(project_dir)
    orientation = derive_orientation(Path(project_dir), task_text)

    observations: list[P.GroundingObservation] = []
    requests: list[P.GroundingAction] = []
    rejections: list[tuple[P.GroundingAction, str]] = []
    assessments: list[P.GroundingDecision] = []
    budget_trace: list[P.Budget] = []
    used_paths: set[str] = set()

    budget = P.Budget(
        requests_remaining=P.MAX_GROUNDING_REQUESTS,
        evidence_bytes_remaining=P.MAX_TOTAL_SOURCE_EVIDENCE_BYTES,
        files_remaining=P.MAX_DISTINCT_FILES,
        regions_remaining=P.MAX_PRIMARY_REGIONS,
    )
    budget_trace.append(budget)

    decision: P.GroundingDecision | None = None
    turn = 1
    while True:
        explicit_next_action = False
        request = P.GroundingRequest(
            task_text=task_text,
            task_text_provenance=P.PROVENANCE_OPERATOR_TASK,
            orientation=orientation,
            prior_observation_ids=tuple(item.observation_id for item in observations),
            remaining_budget=budget,
            turn=turn,
        )
        proposal = adapter.propose(request, tuple(observations))
        if isinstance(proposal, P.GroundingDecision):
            assessment = _validate_decision(proposal, observations)
            if observations:
                assessments.append(assessment)
            if assessment.decision == P.DECISION_NEED_MORE_EVIDENCE:
                proposal = assessment.next_action
                explicit_next_action = True
                if proposal is None:
                    decision = P.GroundingDecision(
                        decision=P.DECISION_INSUFFICIENT,
                        stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
                        rationale="need_more_evidence_without_next_action",
                    )
                    break
            else:
                decision = assessment
                break

        if (
            observations
            and isinstance(proposal, P.GroundingAction)
            and not explicit_next_action
        ):
            decision = P.GroundingDecision(
                decision=P.DECISION_INSUFFICIENT,
                stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
                rationale="missing_explicit_evidence_assessment",
            )
            break

        reason = harness.validate(proposal, budget)
        if reason is not None:
            rejections.append((proposal, reason))
            decision = P.GroundingDecision(
                decision=P.DECISION_INSUFFICIENT,
                stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
                rationale=reason,
            )
            break

        requests.append(proposal)
        produced = harness.execute(
            proposal, budget, used_paths, turn, next_index=len(observations) + 1
        )
        observations.extend(produced)
        spent = sum(item.byte_count for item in produced)
        source_regions = sum(item.outcome == P.OBSERVATION_FOUND for item in produced)
        budget = P.Budget(
            requests_remaining=budget.requests_remaining - 1,
            evidence_bytes_remaining=budget.evidence_bytes_remaining - spent,
            files_remaining=P.MAX_DISTINCT_FILES - len(used_paths),
            regions_remaining=budget.regions_remaining - source_regions,
        )
        budget_trace.append(budget)
        turn += 1

    assert decision is not None
    decision = _validate_decision(decision, observations)
    return P.GroundingOutcome(
        decision=decision,
        requests=tuple(requests),
        observations=tuple(observations),
        rejections=tuple(rejections),
        orientation=orientation,
        task_text=task_text,
        budget_trace=tuple(budget_trace),
        final_budget=budget,
        assessments=tuple(assessments),
    )


def _validate_decision(
    decision: P.GroundingDecision, observations: Sequence[P.GroundingObservation]
) -> P.GroundingDecision:
    """Sufficiency without a live observation citation is not sufficiency."""

    if decision.decision == P.DECISION_NEED_MORE_EVIDENCE:
        if decision.next_action is None:
            return P.GroundingDecision(
                decision=P.DECISION_INSUFFICIENT,
                stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
                rationale="need_more_evidence_without_next_action",
            )
        return decision
    if decision.decision != P.DECISION_SUFFICIENT:
        return decision
    known = {item.observation_id: item for item in observations}
    claim = decision.sufficiency
    if claim is None or not claim.cited_observation_ids:
        return P.GroundingDecision(
            decision=P.DECISION_INSUFFICIENT,
            stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
            rationale="sufficiency_without_observation_citation",
        )
    if any(item not in known for item in claim.cited_observation_ids):
        return P.GroundingDecision(
            decision=P.DECISION_INSUFFICIENT,
            stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
            rationale="sufficiency_cited_unknown_observation",
        )
    cited = [known[item] for item in claim.cited_observation_ids]
    if any(
        item.structural_identity is None or item.outcome != P.OBSERVATION_FOUND
        for item in cited
    ):
        return P.GroundingDecision(
            decision=P.DECISION_INSUFFICIENT,
            stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
            rationale="sufficiency_without_positive_structural_observation",
        )
    return replace(
        decision,
        sufficiency=P.SufficiencyClaim(
            cited_observation_ids=claim.cited_observation_ids,
            relevant_source_paths=tuple(
                dict.fromkeys(item.source_path for item in cited)
            ),
            structural_locators=tuple(
                item.structural_identity.locator for item in cited  # type: ignore[union-attr]
            ),
            source_versions={item.source_path: item.source_version for item in cited},
        ),
    )


def replay(
    project_dir: Path,
    captured_requests: Sequence[P.GroundingAction],
    *,
    harness: GroundingHarness | None = None,
) -> tuple[P.GroundingObservation, ...]:
    """Re-execute captured requests with no adapter and no provider."""

    harness = harness or GroundingHarness(project_dir)
    observations: list[P.GroundingObservation] = []
    used_paths: set[str] = set()
    budget = P.Budget(
        requests_remaining=P.MAX_GROUNDING_REQUESTS,
        evidence_bytes_remaining=P.MAX_TOTAL_SOURCE_EVIDENCE_BYTES,
        files_remaining=P.MAX_DISTINCT_FILES,
        regions_remaining=P.MAX_PRIMARY_REGIONS,
    )
    for turn, action in enumerate(captured_requests, start=1):
        if harness.validate(action, budget) is not None:
            break
        produced = harness.execute(
            action, budget, used_paths, turn, next_index=len(observations) + 1
        )
        observations.extend(produced)
        spent = sum(item.byte_count for item in produced)
        source_regions = sum(item.outcome == P.OBSERVATION_FOUND for item in produced)
        budget = P.Budget(
            requests_remaining=budget.requests_remaining - 1,
            evidence_bytes_remaining=budget.evidence_bytes_remaining - spent,
            files_remaining=P.MAX_DISTINCT_FILES - len(used_paths),
            regions_remaining=budget.regions_remaining - source_regions,
        )
    return tuple(observations)
