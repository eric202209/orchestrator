"""Deterministic benchmark metrics with explicit denominators.

Each rate reports its numerator, denominator, the denominator's definition,
and how many records were excluded because the value is unknown. Unknown
values are never counted as failures. Results are grouped by phase, source
identity, runtime configuration, and model; the cross-phase overview is
descriptive only.
"""

from __future__ import annotations

from collections import Counter
from statistics import median
from typing import Any, Callable, Iterable

Record = dict[str, Any]
Predicate = Callable[[Record], "bool | None"]

PLANNING_STAGES = frozenset(
    {"planning_validation", "planning_repair_output", "discovery"}
)


def rate(
    records: Iterable[Record],
    numerator: Predicate,
    denominator: Predicate,
    definition: str,
) -> dict[str, Any]:
    """Rate over records where ``denominator`` is True and ``numerator`` is known."""

    num = den = unknown = 0
    for record in records:
        if denominator(record) is not True:
            continue
        value = numerator(record)
        if value is None:
            unknown += 1
            continue
        den += 1
        num += 1 if value else 0
    return {
        "numerator": num,
        "denominator": den,
        "denominator_definition": definition,
        "excluded_unknown": unknown,
        "value": round(num / den, 4) if den else "N/A",
    }


def count(
    records: Iterable[Record], predicate: Predicate, definition: str
) -> dict[str, Any]:
    matched = known = unknown = 0
    for record in records:
        value = predicate(record)
        if value is None:
            unknown += 1
            continue
        known += 1
        matched += 1 if value else 0
    return {
        "count": matched,
        "known_records": known,
        "excluded_unknown": unknown,
        "definition": definition,
    }


def _attempted(record: Record) -> bool:
    return record["attempted"] is True


def _candidate(record: Record) -> bool | None:
    if not _attempted(record):
        return False
    return record["execution"]["source_mutation_captured"]


def _attribution_label(record: Record) -> str:
    kinds = record["reliability"]["attribution"]["kinds"]
    return "+".join(kinds) if kinds else "none"


def compute_metrics(records: list[Record]) -> dict[str, Any]:
    attempted = [record for record in records if _attempted(record)]

    def planning_reached(record: Record) -> bool | None:
        return record["planning"]["reached"]

    def execution_reached(record: Record) -> bool | None:
        return record["execution"]["reached"]

    def has(key: str) -> Predicate:
        return lambda record: key in record["reliability"]["safety_rejections"]

    def has_terminal(key: str) -> Predicate:
        return lambda record: key in record["reliability"]["terminal_safety_rejections"]

    durations = [
        record["reliability"]["duration_seconds"]
        for record in attempted
        if record["reliability"]["duration_seconds"] is not None
    ]
    retries = [record["reliability"]["retry_count"] for record in attempted]
    recoveries = [record["reliability"]["recovery_count"] for record in attempted]
    completeness = [
        record["reliability"]["evidence_completeness"] for record in attempted
    ]

    attempts_per_case: dict[str, dict[str, int]] = {}
    for record in records:
        entry = attempts_per_case.setdefault(
            record["identity"]["case_id"],
            {"attempted": 0, "not_attempted": 0, "independently_accepted": 0},
        )
        entry["attempted" if _attempted(record) else "not_attempted"] += 1
        if record["reliability"]["independently_accepted"] is True:
            entry["independently_accepted"] += 1

    return {
        "slots": {
            "total": len(records),
            "attempted": len(attempted),
            "not_attempted": len(records) - len(attempted),
        },
        "planning": {
            "planning_reached_rate": rate(
                attempted, planning_reached, _attempted, "attempted slots"
            ),
            "valid_plan_rate": rate(
                attempted,
                lambda r: (
                    None
                    if r["planning"]["outcome"] is None
                    else r["planning"]["outcome"] == "accepted"
                ),
                lambda r: planning_reached(r) is True,
                "attempts that reached Planning",
            ),
            "planning_repair_success_rate": rate(
                attempted,
                lambda r: (
                    None
                    if r["planning"]["outcome"] is None
                    else r["planning"]["outcome"] == "accepted"
                ),
                lambda r: (r["planning"]["repair_count"] or 0) > 0,
                "attempts with at least one Planning repair",
            ),
            "planning_terminal_failure_rate": rate(
                attempted,
                lambda r: r["reliability"]["failure_stage"] in PLANNING_STAGES,
                lambda r: planning_reached(r) is True,
                "attempts that reached Planning",
            ),
        },
        "execution": {
            "execution_reached_rate": rate(
                attempted, execution_reached, _attempted, "attempted slots"
            ),
            "source_mutation_rate": rate(
                attempted,
                lambda r: r["execution"]["source_mutation_captured"],
                lambda r: execution_reached(r) is True,
                "attempts that reached execution (mutation = non-empty captured ChangeSet)",
            ),
            "verification_pass_rate": rate(
                attempted,
                lambda r: (
                    None
                    if r["execution"]["verification_result"] is None
                    else r["execution"]["verification_result"] == "passed"
                ),
                lambda r: execution_reached(r) is True,
                "attempts that reached execution",
            ),
            "debug_repair_admission_rate": rate(
                attempted,
                lambda r: (r["execution"]["debug_repair_count"] or 0) > 0,
                lambda r: (r["execution"]["debug_feedback_count"] or 0) > 0,
                "attempts with captured DEBUG feedback",
            ),
            "debug_repair_success_rate": rate(
                attempted,
                lambda r: (
                    r["execution"]["task_status"] == "DONE"
                    if r["execution"]["task_status"]
                    else None
                ),
                lambda r: (r["execution"]["debug_repair_count"] or 0) > 0,
                "attempts with at least one DEBUG repair",
            ),
        },
        "acceptance": {
            "independent_acceptance_attempt_rate": rate(
                attempted,
                lambda r: r["acceptance"]["attempted"],
                lambda r: _candidate(r) is True,
                "attempts with a non-empty captured ChangeSet (candidates)",
            ),
            "independent_acceptance_pass_rate": rate(
                attempted,
                lambda r: r["reliability"]["independently_accepted"],
                lambda r: r["acceptance"]["attempted"] is True,
                "candidates with an independent acceptance record",
            ),
            "held_candidate_pass_rate": rate(
                attempted,
                lambda r: r["reliability"]["independently_accepted"],
                lambda r: _candidate(r) is True
                and r["change_set"]["promoted"] is False,
                "held (unpromoted) candidates",
            ),
            "end_to_end_accepted_rate": rate(
                attempted,
                lambda r: r["reliability"]["independently_accepted"],
                _attempted,
                "attempted slots (unknown acceptance excluded)",
            ),
            "unauthorized_mutation_count": count(
                attempted,
                lambda r: (
                    None
                    if r["acceptance"]["attempted"] is not True
                    else bool(
                        r["acceptance"]["unauthorized_source_mutation"]
                        or r["acceptance"]["tests_modified"]
                    )
                ),
                "acceptance records showing test-file or out-of-scope source changes",
            ),
        },
        "safety": {
            "unsupported_command_rejections": count(
                attempted,
                has("unsupported_command"),
                "attempts with >=1 unsupported-command rejection",
            ),
            "apa_rejections": count(
                attempted,
                has("apa_unauthorized_path"),
                "attempts with >=1 APA path rejection",
            ),
            "gr2_rejections": count(
                attempted,
                has("gr2_existing_file_rewrite"),
                "attempts with >=1 GR2 provenance rejection",
            ),
            "terminal_safety_rejections": {
                code: count(
                    attempted, has_terminal(code), f"attempts terminated by {code}"
                )
                for code in sorted(
                    {
                        code
                        for r in attempted
                        for code in r["reliability"]["terminal_safety_rejections"]
                    }
                )
            },
            "unexpected_execution_authority_violations": count(
                attempted,
                lambda r: r["reliability"]["execution_authority_violation"],
                "attempts with a report-grounded execution-authority application defect",
            ),
        },
        "reliability": {
            "attempts_per_case": dict(sorted(attempts_per_case.items())),
            "median_attempt_duration_seconds": (
                round(median(durations), 3) if durations else "N/A"
            ),
            "duration_known_attempts": len(durations),
            "retry_count": {
                "total": sum(value for value in retries if value is not None),
                "unknown": sum(1 for value in retries if value is None),
            },
            "recovery_count": {
                "total": sum(value for value in recoveries if value is not None),
                "unknown": sum(1 for value in recoveries if value is None),
            },
            "failure_distribution_by_stage": dict(
                sorted(
                    Counter(
                        r["reliability"]["failure_stage"] for r in attempted
                    ).items()
                )
            ),
            "failure_distribution_by_attribution": dict(
                sorted(
                    Counter(
                        _attribution_label(r)
                        for r in attempted
                        if r["reliability"]["failure_stage"] != "none"
                    ).items()
                )
            ),
            "evidence_completeness_rate": {
                "fully_complete_attempts": sum(
                    1 for item in completeness if item and not item["missing"]
                ),
                "denominator": len(completeness),
                "mean_artifact_ratio": (
                    round(
                        sum(item["ratio"] for item in completeness) / len(completeness),
                        4,
                    )
                    if completeness
                    else "N/A"
                ),
            },
        },
    }


def group_key(record: Record) -> tuple[str, str, str, str]:
    identity = record["identity"]
    source = identity["source_identity"]
    return (
        identity["phase"],
        f"{source['entering_head']}+{source['worktree_state']}",
        str(identity["runtime_identity"].get("configuration_fingerprint")),
        str(identity["provider_identity"].get("model")),
    )


def grouped_metrics(records: list[Record]) -> dict[str, Any]:
    order = {
        phase: index
        for index, phase in enumerate(
            dict.fromkeys(record["identity"]["phase"] for record in records)
        )
    }
    phase_groups: dict[str, list[Record]] = {}
    for record in records:
        phase_groups.setdefault(record["identity"]["phase"], []).append(record)

    groups = []
    for phase in sorted(phase_groups, key=lambda name: order[name]):
        members = phase_groups[phase]
        identities: dict[tuple[str, str, str, str], list[Record]] = {}
        for record in members:
            if record["attempted"]:
                identities.setdefault(group_key(record), []).append(record)
        groups.append(
            {
                "phase": phase,
                "identity_groups": [
                    {
                        "source_identity": key[1],
                        "configuration_fingerprint": key[2],
                        "model": key[3],
                        "attempt_ids": [
                            record["identity"]["attempt_id"]
                            for record in identity_members
                        ],
                    }
                    for key, identity_members in sorted(identities.items())
                ],
                "comparable_within_group": len(identities) <= 1,
                "metrics": compute_metrics(members),
            }
        )
    return {
        "groups": groups,
        "cross_phase_overview": {
            "descriptive_only": True,
            "statistically_comparable": False,
            "note": "Phases ran under different source, validator, prompt, and runtime versions; this overview is not a reliability estimate.",
            "metrics": compute_metrics(records),
        },
    }
