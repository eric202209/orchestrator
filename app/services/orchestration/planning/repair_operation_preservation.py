"""Reason-scoped source-operation preservation for generic Plan Repair."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Any


COMMAND_CAPABILITY_RULE_ID = "step_command_capability_unsupported"
COMMAND_CAPABILITY_REASON_PREFIX = "unsupported_step_command:"

# A command-only Repair may restructure command and verification work and may
# reindex steps. Every other field on a step containing source operations is
# retained as operation context and compared with the operation payload.
_REPAIRABLE_STEP_CONTEXT_FIELDS = frozenset(
    {
        "step_number",
        "step_index",
        "index",
        "commands",
        "verification",
        "ops",
    }
)
_INDEPENDENT_FINDING_DETAIL_KEYS = frozenset(
    {
        "semantic_violation_codes",
        "source_operation_findings",
        "structured_operation_findings",
        "operation_findings",
        "grounding_findings",
        "path_findings",
        "test_integrity_findings",
    }
)
_KNOWN_COMMAND_REASON_PATTERNS = (
    re.compile(
        r"unsupported_step_command: step \d+ mixes inspection and verification "
        r"commands; use an inspection-only step and a separate step with exactly "
        r"one verification command\."
    ),
    re.compile(
        r"unsupported_step_command: step \d+ has \d+ verification commands; use "
        r"exactly one per step \(one pytest call may list several node IDs\)\."
    ),
    re.compile(
        r"unsupported_step_command: step \d+ commands fit no single admitted "
        r"channel \(inspection only, one verification, or safe shell only\); split "
        r"them\."
    ),
    re.compile(
        r"unsupported_step_command: rejected .+; structured runtime has no command "
        r"tools\. Plan ops for edits; keep goal, scope, checks\."
    ),
)


@dataclass(frozen=True)
class CommandOnlyOperationPreservationBaseline:
    """Immutable snapshot of the canonical operations before generic Repair."""

    rule_ids: tuple[str, ...]
    entries: tuple[dict[str, Any], ...]
    projection_error: str | None = None


def is_command_capability_only_rejection(verdict: Any) -> bool:
    """Return true only when structured evidence positively proves eligibility.

    Rule IDs alone are insufficient: Validator reasons can contain findings
    that are not represented in the normalized ID list. Every reported reason
    and every available structured finding must therefore be recognized.
    """

    if verdict is None or getattr(verdict, "accepted", None) is not False:
        return False
    status = getattr(verdict, "status", None)
    status = getattr(status, "value", status)
    if status != "repair_required":
        return False

    reasons = getattr(verdict, "reasons", None)
    if not isinstance(reasons, (list, tuple)) or not reasons:
        return False
    if any(
        not isinstance(reason, str) or not _is_known_command_capability_reason(reason)
        for reason in reasons
    ):
        return False

    details = getattr(verdict, "details", None)
    if not isinstance(details, Mapping):
        return False
    detail_rule_ids = _string_list(details.get("validator_rule_ids"))
    if detail_rule_ids != [COMMAND_CAPABILITY_RULE_ID]:
        return False

    # Some Validator result wrappers also expose normalized IDs at top level.
    # If present, they must agree with details rather than contradict it.
    top_level_rule_ids = getattr(verdict, "validator_rule_ids", None)
    if top_level_rule_ids is not None:
        if _string_list(top_level_rule_ids) != detail_rule_ids:
            return False

    unsupported_steps = details.get("steps_with_unsupported_commands")
    if not isinstance(unsupported_steps, Mapping) or not unsupported_steps:
        return False
    if any(
        not isinstance(step_commands, (list, tuple))
        or not step_commands
        or any(
            not isinstance(command, str) or not command.strip()
            for command in step_commands
        )
        for step_commands in unsupported_steps.values()
    ):
        return False

    for key in _INDEPENDENT_FINDING_DETAIL_KEYS:
        if details.get(key):
            return False

    findings = getattr(verdict, "findings", None)
    if findings is not None:
        if not isinstance(findings, (list, tuple)):
            return False
        if any(
            _finding_rule_id(finding) != COMMAND_CAPABILITY_RULE_ID
            for finding in findings
        ):
            return False

    return True


def capture_command_only_operation_baseline(
    original_plan: Any, original_verdict: Any
) -> CommandOnlyOperationPreservationBaseline | None:
    """Capture the original source operations only for an eligible rejection."""

    if not is_command_capability_only_rejection(original_verdict):
        return None

    details = original_verdict.details
    rule_ids = tuple(_string_list(details.get("validator_rule_ids")) or [])
    try:
        entries = tuple(project_source_operations(original_plan))
    except (TypeError, ValueError):
        # Eligibility is already established. An unprojectable baseline must
        # fail closed when a repaired candidate reaches the acceptance gate.
        return CommandOnlyOperationPreservationBaseline(
            rule_ids=rule_ids,
            entries=(),
            projection_error="original_operation_projection_unavailable",
        )
    return CommandOnlyOperationPreservationBaseline(rule_ids=rule_ids, entries=entries)


def project_source_operations(plan: Any) -> list[dict[str, Any]]:
    """Flatten canonical `ops` in Plan order, retaining operation step context.

    Command-only steps do not appear in the projection. Step indices, commands
    and verification placement are omitted because command-capability Repair
    may restructure those fields. Every field in each operation object and
    every other field on an operation-bearing step is retained. The flattened
    order and operation-bearing step grouping preserve source-operation
    ordering, multiplicity and context while allowing command-only step edits.
    """

    if not isinstance(plan, (list, tuple)):
        raise TypeError("canonical plan must be a list")

    projection: list[dict[str, Any]] = []
    operation_step_ordinal = 0
    for step_position, step in enumerate(plan):
        if not isinstance(step, Mapping):
            raise TypeError("canonical Plan step must be a mapping")
        operations = step.get("ops") or []
        if not isinstance(operations, (list, tuple)):
            raise TypeError("canonical step ops must be a list")
        if not operations:
            continue

        step_context = {
            key: deepcopy(value)
            for key, value in step.items()
            if key not in _REPAIRABLE_STEP_CONTEXT_FIELDS
        }
        _canonical_json(step_context)
        operation_step_ordinal += 1
        for operation_position, operation in enumerate(operations):
            if not isinstance(operation, Mapping):
                raise TypeError("canonical operation must be a mapping")
            operation_value = deepcopy(dict(operation))
            _canonical_json(operation_value)
            projection.append(
                {
                    "flat_position": len(projection),
                    "step_position": step_position,
                    "operation_position": operation_position,
                    "operation_step_ordinal": operation_step_ordinal,
                    "operation": operation_value,
                    "step_context": deepcopy(step_context),
                }
            )
    return projection


def compare_command_only_operation_projection(
    baseline: CommandOnlyOperationPreservationBaseline, repaired_plan: Any
) -> dict[str, Any]:
    """Compare complete canonical operations and return safe structured diffs."""

    if baseline.projection_error:
        return _unverifiable_result(
            baseline,
            reason=baseline.projection_error,
            repaired_count=None,
        )
    try:
        candidate_entries = project_source_operations(repaired_plan)
    except (TypeError, ValueError):
        return _unverifiable_result(
            baseline,
            reason="repaired_operation_projection_unavailable",
            repaired_count=None,
        )

    original_entries = list(baseline.entries)
    original_keys = [_entry_key(entry) for entry in original_entries]
    candidate_keys = [_entry_key(entry) for entry in candidate_entries]

    candidate_positions: dict[str, deque[int]] = defaultdict(deque)
    for index, key in enumerate(candidate_keys):
        candidate_positions[key].append(index)

    exact_matches: list[tuple[int, int]] = []
    unmatched_original: list[int] = []
    matched_candidate_indexes: set[int] = set()
    for original_index, key in enumerate(original_keys):
        candidates = candidate_positions.get(key)
        if candidates:
            candidate_index = candidates.popleft()
            exact_matches.append((original_index, candidate_index))
            matched_candidate_indexes.add(candidate_index)
        else:
            unmatched_original.append(original_index)
    unmatched_candidate = [
        index
        for index in range(len(candidate_entries))
        if index not in matched_candidate_indexes
    ]

    reorder_pairs: list[tuple[int, int]] = []
    for left in range(len(exact_matches)):
        for right in range(left + 1, len(exact_matches)):
            if exact_matches[left][1] > exact_matches[right][1]:
                reorder_pairs.extend((exact_matches[left], exact_matches[right]))
    reorder_pairs = list(dict.fromkeys(reorder_pairs))

    modified: list[dict[str, Any]] = []
    # A same-path pairing identifies type or payload changes without exposing
    # source text in the diagnostic.
    for original_index in list(unmatched_original):
        original_path = _operation_path(original_entries[original_index])
        if original_path is None:
            continue
        match_index = next(
            (
                candidate_index
                for candidate_index in unmatched_candidate
                if _operation_path(candidate_entries[candidate_index]) == original_path
            ),
            None,
        )
        if match_index is not None:
            modified.append(
                _modified_descriptor(
                    original_entries[original_index],
                    candidate_entries[match_index],
                )
            )
            unmatched_original.remove(original_index)
            unmatched_candidate.remove(match_index)

    # Same-position pairing diagnoses individual type/path/anchor/content
    # mutations. It is only used when the source-operation counts are equal,
    # so a simple addition or deletion remains classified as such.
    if len(original_entries) == len(candidate_entries):
        for original_index in list(unmatched_original):
            if original_index in unmatched_candidate:
                modified.append(
                    _modified_descriptor(
                        original_entries[original_index],
                        candidate_entries[original_index],
                    )
                )
                unmatched_original.remove(original_index)
                unmatched_candidate.remove(original_index)

    added = [candidate_entries[index] for index in unmatched_candidate]
    removed = [original_entries[index] for index in unmatched_original]
    reordered = [
        {
            "from": _describe_entry(original_entries[original_index], original_index),
            "to": _describe_entry(candidate_entries[candidate_index], candidate_index),
        }
        for original_index, candidate_index in reorder_pairs
    ]

    changes = {
        "added": [_describe_entry(entry, entry["flat_position"]) for entry in added],
        "removed": [
            _describe_entry(entry, entry["flat_position"]) for entry in removed
        ],
        "reordered": reordered,
        "modified": modified,
    }
    change_counts = {key: len(value) for key, value in changes.items()}
    return {
        "preserved": not any(change_counts.values()),
        "original_rejection_class": COMMAND_CAPABILITY_RULE_ID,
        "original_rule_ids": list(baseline.rule_ids),
        "original_operation_count": len(original_entries),
        "repaired_operation_count": len(candidate_entries),
        "change_counts": change_counts,
        "changes": {key: value[:20] for key, value in changes.items()},
        "diagnostic_truncated": any(len(value) > 20 for value in changes.values()),
    }


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in value
    ):
        return None
    return list(value)


def _is_known_command_capability_reason(reason: str) -> bool:
    normalized = reason.strip()
    return any(
        pattern.fullmatch(normalized) for pattern in _KNOWN_COMMAND_REASON_PATTERNS
    )


def _finding_rule_id(finding: Any) -> str | None:
    if isinstance(finding, Mapping):
        value = finding.get("rule_id") or finding.get("code") or finding.get("id")
    else:
        value = (
            getattr(finding, "rule_id", None)
            or getattr(finding, "code", None)
            or getattr(finding, "id", None)
        )
    return value if isinstance(value, str) and value else None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _entry_key(entry: Mapping[str, Any]) -> str:
    return _canonical_json(
        {
            "operation": entry["operation"],
            "step_context": entry["step_context"],
            "operation_step_ordinal": entry["operation_step_ordinal"],
        }
    )


def _operation_path(entry: Mapping[str, Any]) -> str | None:
    path = entry["operation"].get("path")
    return path if isinstance(path, str) else None


def _modified_descriptor(
    original_entry: Mapping[str, Any], candidate_entry: Mapping[str, Any]
) -> dict[str, Any]:
    old_operation = original_entry["operation"]
    new_operation = candidate_entry["operation"]
    return {
        "from": _describe_entry(original_entry, original_entry["flat_position"]),
        "to": _describe_entry(candidate_entry, candidate_entry["flat_position"]),
        "changed_fields": sorted(
            set(_changed_fields(old_operation, new_operation, "operation"))
            | set(
                _changed_fields(
                    original_entry["step_context"],
                    candidate_entry["step_context"],
                    "step_context",
                )
            )
            | (
                {"operation_step_ordinal"}
                if original_entry.get("operation_step_ordinal")
                != candidate_entry.get("operation_step_ordinal")
                else set()
            )
        ),
    }


def _changed_fields(before: Any, after: Any, prefix: str) -> list[str]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changed: list[str] = []
        for key in sorted(set(before) | set(after), key=str):
            field = f"{prefix}.{key}"
            if key not in before or key not in after:
                changed.append(field)
            else:
                changed.extend(_changed_fields(before[key], after[key], field))
        return changed
    if isinstance(before, list) and isinstance(after, list):
        return [] if _canonical_json(before) == _canonical_json(after) else [prefix]
    return [] if type(before) is type(after) and before == after else [prefix]


def _describe_entry(entry: Mapping[str, Any], fallback_index: int) -> dict[str, Any]:
    operation = entry["operation"]
    raw_path = operation.get("path")
    safe_path = None
    if isinstance(raw_path, str):
        safe_path = (
            raw_path if not raw_path.startswith(("/", "\\")) else "<absolute-path>"
        )
        safe_path = safe_path.replace("\n", " ").replace("\r", " ")[:180]
    return {
        "flat_position": int(entry.get("flat_position", fallback_index)) + 1,
        "step_position": int(entry.get("step_position", 0)) + 1,
        "operation_position": int(entry.get("operation_position", 0)) + 1,
        "operation_step_ordinal": int(entry.get("operation_step_ordinal", 0)),
        "op": operation.get("op") if isinstance(operation.get("op"), str) else None,
        "path": safe_path,
    }


def _unverifiable_result(
    baseline: CommandOnlyOperationPreservationBaseline,
    *,
    reason: str,
    repaired_count: int | None,
) -> dict[str, Any]:
    return {
        "preserved": False,
        "original_rejection_class": COMMAND_CAPABILITY_RULE_ID,
        "original_rule_ids": list(baseline.rule_ids),
        "original_operation_count": len(baseline.entries),
        "repaired_operation_count": repaired_count,
        "change_counts": {"added": 0, "removed": 0, "reordered": 0, "modified": 0},
        "changes": {"added": [], "removed": [], "reordered": [], "modified": []},
        "projection_error": reason,
        "diagnostic_truncated": False,
    }
