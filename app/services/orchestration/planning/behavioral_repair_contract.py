"""Deterministic behavioral-repair completeness contract for Planning.

Phase36 observed the same Planning defect twice (A1B, A2D): a task asked for an
existing product behavior to be corrected, Grounding materialized the relevant
implementation source, and Planning still emitted a structurally valid Plan
whose only executable mutation wrote a test.  In A2D the generated test asserted
that the defective behavior was correct and should be preserved.

This module expresses the missing invariant, and nothing else:

    If a task requires changing existing product behavior, and Planning has
    grounded existing implementation source for that behavior, then a Plan that
    mutates only test/verification artifacts does not satisfy the task.

Two deliberate limits keep it narrow:

* ``description`` text and ``expected_files`` are never evidence of a behavior
  change -- only executable mutating operations are (Phase36-PC1 R2/R3).
* The invariant is imposed only on *positive* evidence from both sides (repair
  intent *and* grounded existing implementation).  Ambiguity keeps the previous
  behavior rather than inventing a rejection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping, Sequence

from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_EXISTING,
)
from app.services.orchestration.planning.task_bootstrap_contract import (
    _is_test_path,
    _is_verification_helper_script,
)

BEHAVIORAL_REPAIR_MISSING_IMPLEMENTATION_CHANGE = (
    "behavioral_repair_missing_implementation_change"
)

# Operations whose effect is a content mutation of a project path.  ``mkdir`` is
# absent on purpose: creating a directory changes no behavior, so it can neither
# satisfy nor trigger this contract.
BEHAVIORAL_MUTATION_OPS = frozenset(
    {"write_file", "append_file", "replace_in_file", "create_file", "delete_file"}
)

# Positive evidence that the task asks for *existing* behavior to be corrected.
# Deliberately conservative: terms that occur just as often in "add coverage for
# behavior that is already right" tasks (``ensure``, ``regression``, a bare
# ``correct``) are excluded, because a false rejection of a legitimate test-only
# task is as damaging as the defect this contract closes.
_BEHAVIORAL_REPAIR_INTENT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("fix", r"\bfix(?:es|ed|ing)?\b"),
    ("repair", r"\brepair(?:s|ed|ing)?\b"),
    (
        "correct_existing",
        r"\bcorrect(?:s|ed|ing)?\s+(?:the|this|that|its|an?|existing|current)\b",
    ),
    ("incorrect", r"\bincorrect(?:ly)?\b"),
    ("wrong", r"\bwrong(?:ly)?\b"),
    ("bug", r"\bbugs?\b|\bbuggy\b"),
    ("broken", r"\bbroken\b"),
    ("defect", r"\bdefect(?:s|ive)?\b"),
    ("misbehavior", r"\bmisbehav\w*\b"),
    ("should_not", r"\bshould\s+not\b"),
    ("must_not", r"\bmust\s+not\b"),
    ("no_longer", r"\bno\s+longer\b"),
    ("prevent", r"\bprevent(?:s|ed|ing)?\b"),
    ("change_behavior", r"\bchange\s+(?:the\s+)?behaviou?rs?\b"),
    (
        "update_rule",
        r"\bupdate\s+(?:the\s+)?(?:policy|policies|behaviou?r|logic|rule|rules"
        r"|configuration|config|default|defaults)\b",
    ),
    ("stop_doing", r"\bstop\s+\w+ing\b"),
)

# Explicit scope limits that say the task is *not* asking for a behavior change.
# When one is present the contract stands down (fail open, Phase36-PC1 S34).
_NON_BEHAVIORAL_SCOPE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("without_changing", r"\bwithout\s+(?:changing|modifying|editing|touching)\b"),
    ("do_not_change", r"\bdo\s+not\s+(?:change|modify|edit|touch)\b"),
    ("tests_only", r"\btests?[-\s]only\b"),
    ("docs_only", r"\bdocs?[-\s]only\b|\bdocumentation[-\s]only\b"),
    ("already_correct", r"\balready\s+(?:correct|right|behaves)\b"),
    (
        "no_behavior_change",
        r"\bno\s+(?:source|implementation|behaviou?r(?:al)?)\s+change\b",
    ),
)


@dataclass(frozen=True)
class BehavioralRepairContractVerdict:
    """Why the behavioral-completeness invariant did or did not bind."""

    behavior_change_required: bool = False
    behavior_change_satisfied: bool = False
    failure_code: str | None = None
    intent_signals: list[str] = field(default_factory=list)
    suppression_signals: list[str] = field(default_factory=list)
    grounded_implementation_paths: list[str] = field(default_factory=list)
    mutating_paths: list[str] = field(default_factory=list)
    behavior_capable_paths: list[str] = field(default_factory=list)
    test_only_mutation_paths: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.failure_code is None

    @property
    def violation_codes(self) -> list[str]:
        return [self.failure_code] if self.failure_code else []

    def to_dict(self) -> dict[str, Any]:
        return {
            "behavior_change_required": self.behavior_change_required,
            "behavior_change_satisfied": self.behavior_change_satisfied,
            "failure_code": self.failure_code,
            "intent_signals": list(self.intent_signals),
            "suppression_signals": list(self.suppression_signals),
            "grounded_implementation_paths": list(self.grounded_implementation_paths),
            "mutating_paths": list(self.mutating_paths),
            "behavior_capable_paths": list(self.behavior_capable_paths),
            "test_only_mutation_paths": list(self.test_only_mutation_paths),
            # Phase36-PC1 R2/R3: recorded so the evidence is auditable, and so a
            # later reader cannot mistake prose or declarations for authority.
            "description_counts_as_behavior_change": False,
            "expected_files_counts_as_behavior_change": False,
        }


def _normalize_path(path_text: Any) -> str:
    return str(path_text or "").strip().replace("\\", "/").rstrip("/").lstrip("./")


def _matched_signals(text: str, patterns: Sequence[tuple[str, str]]) -> list[str]:
    haystack = str(text or "")
    return [
        name
        for name, pattern in patterns
        if re.search(pattern, haystack, flags=re.IGNORECASE)
    ]


def plan_behavioral_mutation_paths(
    plan: Sequence[Mapping[str, Any]] | None,
    *,
    additional_mutation_paths: Sequence[str] = (),
) -> list[str]:
    """Return the project paths a Plan actually mutates when executed.

    Only executable mutation is considered: typed ``ops`` here, plus whatever
    shell-write targets the caller already resolved (the validator owns the
    bounded shell-write vocabulary).  ``description`` and ``expected_files`` are
    ignored by design.
    """

    paths: set[str] = set()
    for step in plan or []:
        if not isinstance(step, Mapping):
            continue
        for operation in step.get("ops") or []:
            if not isinstance(operation, Mapping):
                continue
            if str(operation.get("op") or "").strip() not in BEHAVIORAL_MUTATION_OPS:
                continue
            path = _normalize_path(operation.get("path"))
            if path:
                paths.add(path)
    for target in additional_mutation_paths or ():
        path = _normalize_path(target)
        if path:
            paths.add(path)
    return sorted(paths)


def _is_behavior_capable_path(path: str) -> bool:
    """A mutation of this path could plausibly change product behavior.

    Source, configuration, data, and migration paths all qualify: Phase36-PC1
    explicitly forbids a "must edit implementation source" rule.  Only test
    artifacts and the verification helper scripts Planning writes for its own
    commands are excluded, because neither can change what the product does.
    """

    return (
        bool(path)
        and not _is_test_path(path)
        and not _is_verification_helper_script(path)
    )


def grounded_implementation_paths(source_materialization: Any) -> list[str]:
    """Existing non-test source Planning was actually shown."""

    paths: set[str] = set()
    for item in getattr(source_materialization, "files", ()) or ():
        if getattr(item, "status", None) != SOURCE_STATUS_EXISTING:
            continue
        path = _normalize_path(getattr(item, "relative_path", ""))
        if _is_behavior_capable_path(path):
            paths.add(path)
    return sorted(paths)


def evaluate_behavioral_repair_contract(
    *,
    plan: Sequence[Mapping[str, Any]] | None,
    task_text: str,
    source_materialization: Any = None,
    additional_mutation_paths: Sequence[str] = (),
) -> BehavioralRepairContractVerdict:
    """Fail closed against test-only Plans for grounded behavioral repairs."""

    suppression_signals = _matched_signals(task_text, _NON_BEHAVIORAL_SCOPE_PATTERNS)
    intent_signals = _matched_signals(task_text, _BEHAVIORAL_REPAIR_INTENT_PATTERNS)
    implementation_paths = grounded_implementation_paths(source_materialization)
    mutating_paths = plan_behavioral_mutation_paths(
        plan, additional_mutation_paths=additional_mutation_paths
    )
    behavior_capable = [
        path for path in mutating_paths if _is_behavior_capable_path(path)
    ]
    test_only = [path for path in mutating_paths if not _is_behavior_capable_path(path)]

    behavior_change_required = bool(
        intent_signals and not suppression_signals and implementation_paths
    )
    behavior_change_satisfied = bool(behavior_capable)

    failure_code: str | None = None
    if (
        behavior_change_required
        # A Plan that mutates nothing is a read-only/inspection Plan and is
        # governed by the workflow-stage rules, not by this contract.
        and mutating_paths
        and not behavior_change_satisfied
    ):
        failure_code = BEHAVIORAL_REPAIR_MISSING_IMPLEMENTATION_CHANGE

    return BehavioralRepairContractVerdict(
        behavior_change_required=behavior_change_required,
        behavior_change_satisfied=behavior_change_satisfied,
        failure_code=failure_code,
        intent_signals=intent_signals,
        suppression_signals=suppression_signals,
        grounded_implementation_paths=implementation_paths,
        mutating_paths=mutating_paths,
        behavior_capable_paths=behavior_capable,
        test_only_mutation_paths=test_only,
    )


__all__ = [
    "BEHAVIORAL_REPAIR_MISSING_IMPLEMENTATION_CHANGE",
    "BEHAVIORAL_MUTATION_OPS",
    "BehavioralRepairContractVerdict",
    "evaluate_behavioral_repair_contract",
    "grounded_implementation_paths",
    "plan_behavioral_mutation_paths",
]
