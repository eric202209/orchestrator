"""Scripted, provider-free grounding adapter (PHASE35-PGP1).

Anti-cheating contract
----------------------
This adapter receives exactly four things: the operator task text, the
deterministic orientation, the prior observations, and the remaining budget.
It contains no fixture identifiers, no expected paths, no expected symbols, no
expected routes, and no expected byte ranges. The test suite asserts that
mechanically against this file's own source text.

Its purpose is not to imitate model intelligence. It is to prove the protocol
permits: ask -> observe -> detect insufficiency -> refine once -> cite -> stop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.orchestration.planning.repository_orientation import (
    orientation_task_literals,
)

from . import protocol as P
from .harness import GroundingAdapter
from .structure import fold_token, tokenize

# A task-agnostic English verb -> HTTP method reading. It contains no domain,
# repository, or fixture vocabulary and is never consulted for non-route
# regions.
_VERB_METHOD = {
    "browse": "GET",
    "browsing": "GET",
    "list": "GET",
    "listing": "GET",
    "view": "GET",
    "views": "GET",
    "viewing": "GET",
    "read": "GET",
    "show": "GET",
    "shows": "GET",
    "display": "GET",
    "displays": "GET",
    "return": "GET",
    "returns": "GET",
    "returning": "GET",
    "report": "GET",
    "reported": "GET",
    "reports": "GET",
    "fetch": "GET",
    "export": "GET",
    "create": "POST",
    "creates": "POST",
    "add": "POST",
    "adds": "POST",
    "submit": "POST",
    "submits": "POST",
    "register": "POST",
    "accept": "POST",
    "update": "PUT",
    "updates": "PUT",
    "change": "PUT",
    "changes": "PUT",
    "edit": "PUT",
    "modify": "PUT",
    "set": "PUT",
    "delete": "DELETE",
    "deletes": "DELETE",
    "remove": "DELETE",
    "removes": "DELETE",
    "purge": "DELETE",
}

_WORD_RE = re.compile(r"[a-z0-9]+")

# A cited region must corroborate at least this many distinct task terms in
# its own bounded body. One literal match is never enough -- that is precisely
# how the current selector accepts generic framework boilerplate as a target.
MIN_BODY_CORROBORATION = 3


@dataclass
class GenericGroundingAdapter(GroundingAdapter):
    """A minimal generic policy over (task text, orientation, observations, budget)."""

    min_body_corroboration: int = MIN_BODY_CORROBORATION

    # -- task-derived signals ---------------------------------------------

    def _terms(self, task_text: str) -> tuple[str, ...]:
        return orientation_task_literals(task_text)

    def _methods(self, task_text: str) -> tuple[str, ...]:
        words = set(_WORD_RE.findall(task_text.lower()))
        return tuple(
            sorted({_VERB_METHOD[word] for word in words if word in _VERB_METHOD})
        )

    def _anchor(self, task_text: str, orientation: P.Orientation) -> str | None:
        """The task term the repository's own path vocabulary echoes most.

        This is the ordinary first move, and it is the same class of move the
        current one-turn Discovery makes: pick one literal and look at it.
        """

        best: tuple[int, int, str] | None = None
        for order, term in enumerate(self._terms(task_text)):
            hits = sum(1 for path in orientation.paths if term in path.lower())
            if not hits:
                continue
            candidate = (-hits, order, term)
            if best is None or candidate < best:
                best = candidate
        return best[2] if best else None

    # -- sufficiency judgement --------------------------------------------

    def _is_sufficient(
        self, observation: P.GroundingObservation, terms: tuple[str, ...]
    ) -> bool:
        identity = observation.structural_identity
        if identity is None or identity.kind == "file_window":
            return False
        folded = {fold_token(term.lower()) for term in terms}
        identity_tokens = tokenize(f"{identity.name or ''} {identity.route_path or ''}")
        if not (folded & identity_tokens):
            return False
        body_tokens = tokenize(observation.bounded_content.decode("utf-8", "replace"))
        return len(folded & body_tokens) >= self.min_body_corroboration

    # -- policy -----------------------------------------------------------

    def propose(self, request, observations):
        terms = self._terms(request.task_text)
        relevant = [item for item in observations if self._is_sufficient(item, terms)]
        if relevant:
            return P.GroundingDecision(
                decision=P.DECISION_SUFFICIENT,
                sufficiency=P.SufficiencyClaim(
                    cited_observation_ids=tuple(
                        item.observation_id for item in relevant
                    ),
                    relevant_source_paths=(),
                    structural_locators=(),
                ),
                rationale=(
                    "cited regions carry a task term in their own structural identity "
                    f"and corroborate >= {self.min_body_corroboration} distinct task "
                    "terms in bounded body content"
                ),
            )

        if request.remaining_budget.requests_remaining <= 0:
            return P.GroundingDecision(
                decision=P.DECISION_INSUFFICIENT,
                stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
                rationale="no observed region established the implementation target",
            )
        if not request.orientation.available or not request.orientation.paths:
            return P.GroundingDecision(
                decision=P.DECISION_INSUFFICIENT,
                stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
                rationale="orientation produced no candidate paths",
            )

        if not observations:
            anchor = self._anchor(request.task_text, request.orientation)
            if anchor is None:
                return P.GroundingDecision(
                    decision=P.DECISION_INSUFFICIENT,
                    stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
                    rationale="no task term is echoed by repository path vocabulary",
                )
            # Turn 1: one bounded literal probe, one result. Cheap, and it
            # leaves most of the budget for a refinement that may be needed.
            return P.GroundingAction(
                kind=P.ACTION_SEARCH_TEXT,
                mode=P.SEARCH_MODE_LITERAL,
                query=anchor,
                scope_paths=request.orientation.paths,
                max_results=1,
            )

        # Turn 2: the literal probe did not establish ownership. Escalate from
        # "where does this word appear" to "which named symbol or route is
        # about this task", scoped to orientation plus what turn 1 exposed.
        observed = tuple(dict.fromkeys(item.source_path for item in observations))
        scope = tuple(dict.fromkeys(request.orientation.paths + observed))
        return P.GroundingDecision(
            decision=P.DECISION_NEED_MORE_EVIDENCE,
            next_action=P.GroundingAction(
                kind=P.ACTION_SEARCH_TEXT,
                mode=P.SEARCH_MODE_STRUCTURAL,
                terms=terms,
                http_methods=self._methods(request.task_text),
                scope_paths=scope,
                max_results=request.remaining_budget.regions_remaining,
            ),
            rationale=(
                "the observed evidence does not identify the relevant existing "
                "implementation area"
            ),
        )
