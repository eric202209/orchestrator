"""PGP2 trial harness: PGP1 execution without PGP1's semantic ranking.

PGP1 disclosed that its `search_text mode=structural` survey ranked candidate
symbols and routes by task-term corroboration, and that this ranking performed
much of the semantic discrimination. PGP2 exists to test whether a real model
can do that work itself, so the ranking must be unavailable.

`TrialHarness` therefore refuses `mode=structural` outright and widens literal
search to report occurrences in deterministic file/byte order rather than
relevance order. Everything else -- path scope, budgets, provenance, structural
resolution of a *model-named* symbol or route -- is inherited unchanged.
"""

from __future__ import annotations

from . import protocol as P
from .harness import GroundingHarness
from .structure import file_window_identity, owning_region

REJECT_STRUCTURAL_RANKING_DISABLED = "structural_ranking_disabled_for_trial"


class TrialHarness(GroundingHarness):
    """Read-only execution only. The harness never chooses what is relevant."""

    def validate(self, action: P.GroundingAction, budget: P.Budget) -> str | None:
        reason = super().validate(action, budget)
        if reason is not None:
            return reason
        if (
            action.kind == P.ACTION_SEARCH_TEXT
            and action.mode == P.SEARCH_MODE_STRUCTURAL
        ):
            return REJECT_STRUCTURAL_RANKING_DISABLED
        return None

    def _structural_candidates(self, action):  # pragma: no cover - unreachable
        raise AssertionError(
            "the task-term ranking survey is disabled for the PGP2 trial"
        )

    def _literal_candidates(self, action: P.GroundingAction):
        """Every occurrence, in scope-path order then byte order.

        No relevance ordering and no scoring: the model sees where its own
        literal actually appears, and decides for itself which occurrence
        matters.
        """

        needle = (action.query or "").encode("utf-8")
        found: list[tuple[str, int, int, P.StructuralIdentity | None, dict]] = []
        seen: set[tuple[str, int, int]] = set()
        limit = max(1, action.max_results)
        for path in action.scope_paths:
            raw = self._source(path)
            if raw is None:
                continue
            regions = self._regions(path)
            offset = raw.find(needle)
            while offset >= 0 and len(found) < limit:
                owner = owning_region(regions, offset)
                if owner is not None:
                    start, end = owner.start_byte, owner.end_byte
                    identity = owner.identity()
                else:
                    start = max(0, offset - P.MAX_REGION_BYTES // 2)
                    end = min(len(raw), start + P.MAX_REGION_BYTES)
                    start = raw.rfind(b"\n", 0, start) + 1
                    identity = file_window_identity(raw, start, end)
                key = (path, start, end)
                if key not in seen:
                    seen.add(key)
                    found.append(
                        (
                            path,
                            start,
                            end,
                            identity,
                            {
                                "match_byte": offset,
                                "match_count": raw.count(needle),
                                "selection_strategy": "literal_occurrence_in_file_order",
                            },
                        )
                    )
                offset = raw.find(needle, offset + 1)
            if len(found) >= limit:
                break
        return found
