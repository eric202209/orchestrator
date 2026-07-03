"""Deterministic Slot Merge candidate operator."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class SlotMergeInput:
    parent_a_plan: list[dict[str, Any]]
    parent_b_plan: list[dict[str, Any]]
    parent_a_reasons: tuple[str, ...] = ()
    parent_b_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class SlotMergeResult:
    merged_plan: list[dict[str, Any]]
    parent_candidate_ids: tuple[str, str]
    merged_candidate_id: str = "candidate-slot-merge-1"
    operator: str = "slot_merge"


class SlotMergeOperator:
    """Merge one candidate from two failed lineages using deterministic slots."""

    parent_a_candidate_id = "candidate-original"
    parent_b_candidate_id = "candidate-repair"
    merged_candidate_id = "candidate-slot-merge-1"

    def merge(self, merge_input: SlotMergeInput) -> SlotMergeResult:
        parent_a = list(merge_input.parent_a_plan or [])
        parent_b = list(merge_input.parent_b_plan or [])
        by_step_a = _steps_by_number(parent_a)
        by_step_b = _steps_by_number(parent_b)
        ordered_steps = _ordered_step_numbers(parent_a, parent_b)
        a_failed_steps = _step_numbers_from_reasons(merge_input.parent_a_reasons)
        b_failed_steps = _step_numbers_from_reasons(merge_input.parent_b_reasons)

        merged: list[dict[str, Any]] = []
        for step_number in ordered_steps:
            in_a = step_number in by_step_a
            in_b = step_number in by_step_b
            if in_a and in_b:
                use_b = (
                    step_number in a_failed_steps and step_number not in b_failed_steps
                )
                merged.append(
                    copy.deepcopy(
                        by_step_b[step_number] if use_b else by_step_a[step_number]
                    )
                )
            elif in_a:
                merged.append(copy.deepcopy(by_step_a[step_number]))
            else:
                merged.append(copy.deepcopy(by_step_b[step_number]))

        return SlotMergeResult(
            merged_plan=merged,
            parent_candidate_ids=(
                self.parent_a_candidate_id,
                self.parent_b_candidate_id,
            ),
            merged_candidate_id=self.merged_candidate_id,
        )


def _steps_by_number(plan: list[dict[str, Any]]) -> Mapping[int, dict[str, Any]]:
    steps: dict[int, dict[str, Any]] = {}
    for index, step in enumerate(plan, start=1):
        if not isinstance(step, dict):
            continue
        try:
            step_number = int(step.get("step_number") or index)
        except (TypeError, ValueError):
            step_number = index
        steps.setdefault(step_number, step)
    return steps


def _ordered_step_numbers(
    parent_a: list[dict[str, Any]], parent_b: list[dict[str, Any]]
) -> tuple[int, ...]:
    ordered: list[int] = []
    for plan in (parent_a, parent_b):
        for step_number in _steps_by_number(plan):
            if step_number not in ordered:
                ordered.append(step_number)
    return tuple(ordered)


def _step_numbers_from_reasons(reasons: tuple[str, ...]) -> set[int]:
    step_numbers: set[int] = set()
    for reason in reasons:
        for raw in re.findall(r"\bstep\s+(\d+)\b", str(reason), flags=re.IGNORECASE):
            try:
                step_numbers.add(int(raw))
            except ValueError:
                continue
    return step_numbers
