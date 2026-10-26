"""Deterministic observed-mechanism and root-cause attribution (Phase 40D-3).

Five concepts are kept apart:

* observed failure stage -- ``reliability.failure_stage`` (unchanged, v1);
* observed failure mechanisms -- ``reliability.observed_mechanisms``: facts
  read from the record, the case, and the retained acceptance output;
* root-cause attribution -- ``reliability.root_cause.attribution``;
* attribution evidence -- ``reliability.root_cause.evidence_refs``;
* attribution basis and confidence -- ``root_cause.basis`` and ``confidence``.

A mechanism is never promoted to a root cause by inference: an acceptance or
verification failure, or an exhausted DEBUG budget, leaves the root cause
``unknown``. A wrong-file mechanism requires the case's explicit
``target_contract``. No model is consulted.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .schema import ATTEMPT_SCHEMA_V2_VERSION

MODEL_PROPOSAL_REJECTIONS = frozenset(
    {
        "apa_unauthorized_path",
        "gr2_existing_file_rewrite",
        "python_syntax_invalid",
        "python_write_file_drops_definitions",
        "semantic_string_edit_rejected",
        "plan_output_contract",
        "debug_repair_output_contract",
        "unsupported_command",
        "task_scope",
        "missing_verification",
    }
)
_FAILED_TEST = re.compile(r"^(?:FAILED|ERROR) [^\s:]+::([A-Za-z0-9_\[\]-]+)", re.M)


def failing_test_names(acceptance_text: str) -> list[str]:
    return sorted(set(_FAILED_TEST.findall(acceptance_text)))


def _under(path: str, prefixes: list[str]) -> bool:
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
        for prefix in prefixes
    )


def observed_mechanisms(
    record: dict[str, Any],
    case: dict[str, Any],
    *,
    acceptance_text: str | None = None,
    timed_out: bool = False,
) -> list[dict[str, str]]:
    """Facts only; each mechanism names the field or record it was read from."""

    found: list[dict[str, str]] = []

    def add(mechanism: str, evidence: str) -> None:
        item = {"mechanism": mechanism, "evidence": evidence}
        if item not in found:
            found.append(item)

    reliability = record["reliability"]
    planning = record["planning"]
    execution = record["execution"]
    acceptance = record["acceptance"]
    stage = reliability["failure_stage"]
    if not record["attempted"]:
        add(
            f"infrastructure:{record.get('not_attempted_reason') or 'unknown'}",
            "not_attempted_reason",
        )
        return found
    if timed_out or stage == "timeout":
        add("observation_timeout", "reliability.failure_stage")
    terminal = list(reliability["terminal_safety_rejections"] or [])
    prefix = (
        "plan_validation_terminal_rejection"
        if str(stage).startswith("planning")
        else "terminal_safety_rejection"
    )
    for code in terminal:
        add(f"{prefix}:{code}", "reliability.terminal_safety_rejections")
    for code in sorted(set(reliability["safety_rejections"] or []) - set(terminal)):
        add(f"safety_rejection_observed:{code}", "reliability.safety_rejections")
    if (planning.get("repair_count") or 0) > 0 and planning.get("outcome") not in (
        "accepted",
        None,
    ):
        add("planning_repair_did_not_resolve", "planning.outcome")
    for kind in reliability["verification_failure"] or []:
        add(f"verification_failed:{kind}", "reliability.verification_failure")
    if (execution.get("debug_repair_count") or 0) > 0:
        add("debug_repair_attempted", "execution.debug_repair_count")
    for reason in execution.get("debug_repair_rejections") or []:
        add(
            (
                "debug_repair_budget_exhausted"
                if reason == "debug_repair_budget_exhausted"
                else f"debug_repair_rejected:{reason}"
            ),
            "execution.debug_repair_rejections",
        )
    if stage == "no_candidate":
        add("done_without_candidate", "reliability.failure_stage")
    if acceptance.get("tests_modified") is True:
        add("protected_tests_modified", "acceptance.tests_modified")
    if acceptance.get("unauthorized_source_mutation") is True:
        add("unauthorized_source_mutation", "acceptance.unauthorized_source_mutation")
    contract = case.get("target_contract")
    source_paths = acceptance.get("source_paths") or []
    if contract and source_paths:
        outside = [
            path for path in source_paths if not _under(path, contract["source_paths"])
        ]
        if outside:
            add("candidate_outside_declared_target", "case.target_contract")
    if acceptance.get("attempted") and acceptance.get("result") not in (
        "passed",
        None,
    ):
        add("independent_acceptance_failed", "acceptance.result")
        expected = set(case["baseline"].get("expected_failed_tests") or [])
        if acceptance_text is not None and expected:
            still = set(failing_test_names(acceptance_text))
            resolved = expected - still
            if not resolved:
                mechanism = "candidate_resolved_no_baseline_failures"
            elif resolved == expected:
                mechanism = "candidate_resolved_all_baseline_failures"
            else:
                mechanism = "candidate_partially_resolved_baseline_failures"
            add(mechanism, "acceptance.evidence_ref vs case.baseline")
            if still - expected:
                add(
                    "candidate_introduced_new_failures",
                    "acceptance.evidence_ref vs case.baseline",
                )
    return found


def root_cause(
    record: dict[str, Any], mechanisms: list[dict[str, str]]
) -> dict[str, Any]:
    """Proven causes only; anything else is ``unknown`` with ``unproven``."""

    reliability = record["reliability"]
    names = [item["mechanism"] for item in mechanisms]
    refs = sorted(reliability.get("evidence_refs") or [])

    def cause(attribution: str, basis: str, confidence: str) -> dict[str, Any]:
        return {
            "attribution": attribution,
            "basis": basis,
            "confidence": confidence,
            "evidence_refs": [] if attribution == "not_applicable" else refs,
        }

    if not record["attempted"]:
        return cause(
            "infrastructure", "rule:infrastructure_before_execution", "observed"
        )
    stage = reliability["failure_stage"]
    if stage == "none" and reliability.get("independently_accepted") is True:
        return cause("not_applicable", "not_applicable", "not_applicable")
    acceptance = record["acceptance"]
    if (
        stage in ("verification", "acceptance")
        or (acceptance.get("attempted") and acceptance.get("result") == "failed")
        or "debug_repair_budget_exhausted" in names
    ):
        return cause(
            "unknown",
            "unproven:" + (",".join(sorted(set(names))) or stage),
            "unproven",
        )
    registry = (reliability.get("attribution") or {}).get("root_causes") or []
    if reliability.get("execution_authority_violation") is True:
        return cause(
            "application_defect",
            "registry_report:execution_authority",
            "report_grounded",
        )
    if registry:
        kinds = sorted({str(item.get("kind")) for item in registry})
        if len(kinds) == 1 and kinds[0] in ("model_output", "application_defect"):
            return cause(kinds[0], "registry_report", "report_grounded")
        # Several report-grounded causes of different kinds: no single cause.
        return cause(
            "unknown", "registry_report:multiple_kinds:" + ",".join(kinds), "unproven"
        )
    if "candidate_outside_declared_target" in names:
        return cause(
            "model_output",
            "rule:candidate_outside_explicit_target_contract",
            "explicit_contract",
        )
    terminal = set(reliability["terminal_safety_rejections"] or [])
    if terminal and terminal <= MODEL_PROPOSAL_REJECTIONS:
        # The safeguard verdict is deterministic; that it was correct is not
        # independently proven, so the confidence is rule-inferred.
        return cause(
            "model_output",
            "rule:terminal_safety_rejection_of_model_proposal",
            "rule_inferred",
        )
    return cause(
        "unknown",
        "unproven:" + (",".join(sorted(set(names))) or "no_mechanism_observed"),
        "unproven",
    )


def upgrade_to_v2(
    record: dict[str, Any],
    case: dict[str, Any],
    *,
    repo_root: Path,
    timed_out: bool = False,
) -> dict[str, Any]:
    """Return a v2 copy; every v1 field keeps its value and meaning."""

    acceptance_text = None
    ref = record["acceptance"].get("evidence_ref")
    if ref and (repo_root / ref).is_file():
        acceptance_text = (repo_root / ref).read_text(encoding="utf-8")
    mechanisms = observed_mechanisms(
        record, case, acceptance_text=acceptance_text, timed_out=timed_out
    )
    upgraded = {**record, "schema_version": ATTEMPT_SCHEMA_V2_VERSION}
    upgraded["reliability"] = {
        **record["reliability"],
        "observed_mechanisms": mechanisms,
        "root_cause": root_cause(record, mechanisms),
    }
    return upgraded
