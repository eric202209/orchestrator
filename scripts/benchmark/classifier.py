"""Deterministic failure classification for benchmark attempts.

Observed outcome (stage, safety rejections, verification failure) is derived
only from raw evidence text and event sequences. Attribution is taken from
report-grounded registry entries when present; otherwise a single narrow rule
applies, and everything else stays ``unknown``. No model is consulted.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# Expected safety rejections (taxonomy D). Order is irrelevant; output is sorted.
SAFETY_REJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "unsupported_command",
        r"unsupported_step_command|cannot be executed under this execution topology"
        r"|shell command this execution topology cannot run|non-runnable pseudo-commands",
    ),
    ("apa_unauthorized_path", r"unauthorized_source_path"),
    (
        "gr2_existing_file_rewrite",
        r"existing_file_rewrite_requires_complete_planning_source",
    ),
    ("python_syntax_invalid", r"python_source_syntax_invalid"),
    (
        "python_write_file_drops_definitions",
        r"python_write_file_drops_referenced_definitions",
    ),
    ("task_scope", r"task scope violation|outside the explicit scope"),
    ("semantic_string_edit_rejected", r"semantic_string_edit_rejected"),
    (
        "plan_output_contract",
        r"repair returned prose|repair_output_contract_violation|expected bare JSON",
    ),
    (
        "debug_repair_output_contract",
        r"Invalid bounded debug repair output: missing_command|\bmissing_command\b",
    ),
    ("missing_verification", r"missing verification commands"),
)

# Verification failure kinds (taxonomy E).
VERIFICATION_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "collection_error",
        r"ERROR collecting|error during collection|errors? during collection",
    ),
    ("name_error", r"\bNameError\b"),
    ("import_failure", r"\b(?:ImportError|ModuleNotFoundError)\b"),
    (
        "test_failure",
        r"\bAssertionError\b|^E\s+assert |\b\d+ failed\b|^FAILED |={3,} FAILURES ={3,}",
    ),
)

APPLICATION_AUTHORITY_CODES = frozenset({"execution_authority_mismatch"})


def _match_codes(
    patterns: Iterable[tuple[str, str]], texts: Iterable[str]
) -> list[str]:
    joined = "\n".join(text for text in texts if text)
    return sorted(
        {
            code
            for code, pattern in patterns
            if re.search(pattern, joined, flags=re.IGNORECASE | re.MULTILINE)
        }
    )


def safety_rejections(texts: Iterable[str]) -> list[str]:
    return _match_codes(SAFETY_REJECTION_PATTERNS, texts)


def verification_failures(texts: Iterable[str]) -> list[str]:
    return _match_codes(VERIFICATION_PATTERNS, texts)


def failure_stage(facts: dict[str, Any]) -> str:
    """Return the observed terminal failure stage, or ``none`` on success."""

    if not facts.get("attempted"):
        return "not_attempted"
    events = facts.get("events")
    if events is None:
        return "unknown"
    phases_started = {
        event["phase"] for event in events if event["type"] == "phase_started"
    }
    terminal_errors = [
        event.get("error") for event in events if event["type"] == "task_failed"
    ]
    if facts.get("task_status") == "DONE" and any(
        event["type"] == "task_completed" for event in events
    ):
        if facts.get("acceptance_result") == "failed":
            return "acceptance"
        return "none"
    if any(
        str(error or "").startswith("read_only_discovery") for error in terminal_errors
    ):
        return "discovery"
    if "executing" not in phases_started:
        if any(
            event["type"] == "phase_finished"
            and event.get("phase") == "planning"
            and event.get("status") == "repair_output_contract_violation"
            for event in events
        ):
            return "planning_repair_output"
        if "planning" in phases_started:
            return "planning_validation"
        return "unknown"
    if "debugging" in phases_started:
        return "debug_repair"
    if any(
        event["type"] == "phase_finished"
        and event.get("status") == "verification_failed"
        for event in events
    ):
        return "verification"
    return "execution_step"


def attribution(
    facts: dict[str, Any], root_causes: list[dict[str, Any]]
) -> dict[str, Any]:
    """Return attribution, keeping report-grounded causes separate from rules."""

    if not facts.get("attempted"):
        return {"basis": "not_applicable", "kinds": [], "root_causes": []}
    if facts.get("stage") == "none":
        return {"basis": "not_applicable", "kinds": [], "root_causes": []}
    if root_causes:
        return {
            "basis": "registry_report",
            "kinds": sorted({cause["kind"] for cause in root_causes}),
            "root_causes": sorted(
                root_causes, key=lambda cause: (cause["kind"], cause["code"])
            ),
        }
    # Narrow rule: a planning-stage safety rejection of model-proposed content
    # with no report attribution is inferred (not confirmed) model output.
    if facts.get("stage") in {
        "planning_validation",
        "planning_repair_output",
    } and facts.get("safety_rejections"):
        return {
            "basis": "rule:planning_safety_rejection_of_model_proposal",
            "kinds": ["model_output"],
            "root_causes": [],
        }
    return {"basis": "none", "kinds": ["unknown"], "root_causes": []}


def execution_authority_violation(
    root_causes: list[dict[str, Any]], attempted: bool
) -> bool | None:
    """Only a report-grounded authority defect counts; safety rejections never do."""

    if not attempted:
        return None
    return any(
        cause.get("code") in APPLICATION_AUTHORITY_CODES for cause in root_causes
    )


def independently_accepted(
    *,
    attempted: bool,
    task_status: str | None,
    change_set_paths: list[str] | None,
    acceptance: dict[str, Any],
) -> bool | None:
    """True only with passing independent acceptance tied to the ChangeSet.

    A DONE task without an acceptance record is ``None`` (unknown), never True.
    """

    if not attempted:
        return None
    if not acceptance.get("attempted"):
        if task_status == "DONE" and change_set_paths:
            return None
        return False
    if acceptance.get("result") != "passed":
        return False
    if not change_set_paths:
        return False
    if acceptance.get("tests_modified") is not False:
        return False
    if acceptance.get("unauthorized_source_mutation") is not False:
        return False
    if sorted(acceptance.get("source_paths") or []) != sorted(change_set_paths):
        return False
    return True
