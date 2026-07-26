#!/usr/bin/env python3
"""Phase 31B certification validation pipeline.

Validates that a certification session's evidence directory contains
everything the evidence program requires before the session can be
declared complete. Fails deterministically with an explicit, itemized
diagnostic list -- never silently accepts a partial evidence set.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ValidationFailure(list):
    """A list of diagnostic strings; truthy iff non-empty (i.e. failed)."""


def validate_session_evidence(
    evidence_dir: Path,
    *,
    session_number: int,
    scenario_ids: list[str],
    launch_preconditions_required: bool = True,
    replay_required: bool = True,
) -> dict[str, Any]:
    """Validate one certification session's evidence directory.

    Returns `{"valid": bool, "failures": [str, ...], "checked": {...}}`.
    Every check is explicit; an empty `failures` list is the only way
    `valid` is True.
    """
    failures: list[str] = []
    checked: dict[str, Any] = {}

    preamble_path = evidence_dir / f"session-{session_number}-preamble.json"
    if not preamble_path.exists():
        failures.append(f"missing required evidence file: {preamble_path.name}")
        preamble: dict[str, Any] = {}
    else:
        preamble = json.loads(preamble_path.read_text(encoding="utf-8"))
    checked["preamble_present"] = preamble_path.exists()

    if launch_preconditions_required:
        f10 = preamble.get("f10_result") if preamble else None
        f11 = preamble.get("f11_result") if preamble else None
        f12 = preamble.get("f12_result") if preamble else None
        if f10 is None:
            failures.append("preamble record missing f10_result")
        elif not f10.get("passed"):
            failures.append("F10 launch precondition did not pass at session start")
        if f11 is None:
            failures.append("preamble record missing f11_result")
        elif not f11.get("passed"):
            failures.append("F11 launch precondition did not pass at session start")
        if f12 is None:
            failures.append("preamble record missing f12_result")
        elif not f12.get("passed"):
            failures.append("F12 launch precondition did not pass at session start")
    checked["launch_preconditions_checked"] = launch_preconditions_required

    closing_path = evidence_dir / f"session-{session_number}-closing.json"
    if not closing_path.exists():
        failures.append(f"missing required evidence file: {closing_path.name}")
    checked["closing_present"] = closing_path.exists()

    notes_path = evidence_dir / f"session-{session_number}-operator-notes.md"
    if not notes_path.exists():
        failures.append(f"missing required evidence file: {notes_path.name}")
    checked["operator_notes_present"] = notes_path.exists()

    scenario_checks: dict[str, dict[str, Any]] = {}
    for scenario_id in scenario_ids:
        record_path = evidence_dir / f"{scenario_id.lower()}-r1.json"
        entry: dict[str, Any] = {"record_present": record_path.exists()}
        if not record_path.exists():
            failures.append(f"missing scenario evidence record for {scenario_id}")
        else:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            for required_key in (
                "declared_contract",
                "captured_facts",
                "outcome_class",
                "result",
            ):
                if required_key not in record or record[required_key] is None:
                    failures.append(
                        f"{scenario_id} evidence record missing required field: "
                        f"{required_key}"
                    )
            entry["outcome_class"] = record.get("outcome_class")

        if replay_required:
            replay_path = evidence_dir / f"{scenario_id.lower()}-r1-replay.json"
            entry["replay_present"] = replay_path.exists()
            if not replay_path.exists():
                failures.append(f"missing replay result for {scenario_id}")
            else:
                replay = json.loads(replay_path.read_text(encoding="utf-8"))
                entry["replay_match"] = replay.get("match")
                if replay.get("match") is not True:
                    failures.append(
                        f"{scenario_id} replay did not match the captured record"
                    )
        scenario_checks[scenario_id] = entry

    checked["scenarios"] = scenario_checks

    return {"valid": not failures, "failures": failures, "checked": checked}
