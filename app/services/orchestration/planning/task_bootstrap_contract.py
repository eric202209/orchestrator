"""Declarative Task-1 bootstrap planning contract."""

from __future__ import annotations

import re
import ast
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from app.services.orchestration.validation.workspace_checks import SOURCE_EXTENSIONS
from app.services.orchestration.planning.planner_contract_registry import (
    PLANNER_CONTRACT_ID,
    PLANNER_CONTRACT_VERSION,
    REGISTERED_PLANNER_SCENARIO_IDS,
    REGISTERED_STRUCTURAL_FACTS,
    SOURCE_EXPECTATIONS,
    TEST_EXPECTATIONS,
    registered_planner_contract,
    truthy_structural_facts,
)


TEST_ROOTS = {"test", "tests", "spec", "specs"}
VERIFICATION_HELPER_SCRIPT_RE = re.compile(r"(?:^|/)verify_task1_step\d+\.py$")
EXPECTED_TEST_REASON_EXPLICIT_CODE_TEST_INTENT = "explicit_code_test_intent"
EXPECTED_TEST_REASON_EXISTING_PROJECT_TESTS_PRESENT = "existing_project_tests_present"
EXPECTED_TEST_REASON_MIXED_TASK_CODE_COMPONENT = "mixed_task_code_component"
EXPECTED_TEST_REASON_UNKNOWN_CONSERVATIVE = "unknown_conservative"
EXPECTED_TEST_REASON_ARTIFACT_ONLY_NO_CODE_TEST_INTENT = (
    "artifact_only_no_code_test_intent"
)
EXPECTED_TEST_REASON_NOT_REQUIRED = "expected_test_not_required"
PLACEHOLDER_RE = re.compile(
    r"\b(?:pass|todo|fixme|stub|placeholder|notimplemented|notimplementederror)\b|"
    r"\bnot[-_\s]*implemented\b",
    re.IGNORECASE,
)


class BootstrapTaskType(StrEnum):
    SOURCE_CODE = "SOURCE_CODE"
    ARTIFACT_ONLY = "ARTIFACT_ONLY"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TaskBootstrapContract:
    bootstrap_task_type: BootstrapTaskType = BootstrapTaskType.UNKNOWN
    classification_evidence: dict[str, Any] = field(default_factory=dict)
    expected_source_files: list[str] = field(default_factory=list)
    expected_test_files: list[str] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)
    required_source_files: list[str] = field(default_factory=list)
    required_test_files: list[str] = field(default_factory=list)
    required_verification: list[str] = field(default_factory=list)
    forbidden_path_drift: list[str] = field(default_factory=list)
    python_package_markers: list[str] = field(default_factory=list)
    python_import_targets: list[str] = field(default_factory=list)
    forbidden_python_src_imports: list[str] = field(default_factory=list)
    missing_python_package_markers: list[str] = field(default_factory=list)
    expected_test_reason: str | None = None
    minimum_implementation_evidence: bool = False
    minimum_artifact_evidence: bool = False
    contract_id: str | None = None
    contract_version: str | None = None
    scenario_id: str | None = None
    source_expectation: str | None = None
    test_expectation: str | None = None
    structural_evidence_used: list[str] = field(default_factory=list)
    selected_planning_path: str | None = None
    rejected_alternatives: list[str] = field(default_factory=list)
    terminal_classification: str | None = None
    limitation_id: str | None = None
    planner_contract_status: str = "legacy_compatibility"

    def to_dict(self) -> dict[str, Any]:
        return {
            "bootstrap_task_type": str(self.bootstrap_task_type),
            "classification_evidence": dict(self.classification_evidence),
            "expected_source_files": list(self.expected_source_files),
            "expected_test_files": list(self.expected_test_files),
            "required_artifacts": list(self.required_artifacts),
            "required_source_files": list(self.required_source_files),
            "required_test_files": list(self.required_test_files),
            "required_verification": list(self.required_verification),
            "forbidden_path_drift": list(self.forbidden_path_drift),
            "python_package_markers": list(self.python_package_markers),
            "python_import_targets": list(self.python_import_targets),
            "forbidden_python_src_imports": list(self.forbidden_python_src_imports),
            "missing_python_package_markers": list(self.missing_python_package_markers),
            "expected_test_reason": self.expected_test_reason,
            "minimum_implementation_evidence": self.minimum_implementation_evidence,
            "minimum_artifact_evidence": self.minimum_artifact_evidence,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "scenario_id": self.scenario_id,
            "source_expectation": self.source_expectation,
            "test_expectation": self.test_expectation,
            "structural_evidence_used": list(self.structural_evidence_used),
            "selected_planning_path": self.selected_planning_path,
            "rejected_alternatives": list(self.rejected_alternatives),
            "terminal_classification": self.terminal_classification,
            "limitation_id": self.limitation_id,
            "planner_contract_status": self.planner_contract_status,
        }


@dataclass(frozen=True)
class TaskBootstrapContractVerdict:
    contract: TaskBootstrapContract
    passed: bool
    violations: list[str] = field(default_factory=list)
    violation_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "violations": list(self.violations),
            "violation_codes": list(self.violation_codes),
            **self.contract.to_dict(),
        }


def _normalize_path(path_text: Any) -> str:
    return str(path_text or "").strip().rstrip("/").lstrip("./")


def _is_verification_helper_script(path_text: str) -> bool:
    """Ops-written scripts from a brittle-inline-Python verification rewrite.

    Excluded from source/test/artifact classification entirely: they exist
    solely to give a nested-quote `python -c "..."` verification command a
    non-brittle shape (see planning_task1_bootstrap.normalize_task1_brittle_
    inline_python_verification) and must not count as project source code the
    contract then demands tests for.
    """

    return bool(VERIFICATION_HELPER_SCRIPT_RE.search(_normalize_path(path_text)))


def _is_test_path(path_text: str) -> bool:
    parts = Path(path_text).parts
    return bool(parts and parts[0].lower() in TEST_ROOTS)


def _is_source_path(path_text: str) -> bool:
    normalized = _normalize_path(path_text)
    if (
        not normalized
        or _is_test_path(normalized)
        or _is_verification_helper_script(normalized)
    ):
        return False
    return Path(normalized).suffix.lower() in SOURCE_EXTENSIONS


def _is_artifact_path(path_text: str) -> bool:
    normalized = _normalize_path(path_text)
    if not normalized or _is_test_path(normalized) or _is_source_path(normalized):
        return False
    path = Path(normalized)
    if not path.suffix:
        return False
    return path.suffix.lower() in {
        ".csv",
        ".json",
        ".md",
        ".pdf",
        ".rst",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }


def _materialized_file_targets(plan: list[dict[str, Any]]) -> set[str]:
    targets: set[str] = set()
    for step in plan:
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "") not in {
                "write_file",
                "append_file",
                "replace_in_file",
            }:
                continue
            path = _normalize_path(operation.get("path"))
            if path:
                targets.add(path)
    return targets


def _materialized_file_contents(plan: list[dict[str, Any]]) -> dict[str, str]:
    contents: dict[str, str] = {}
    for step in plan:
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "") not in {"write_file", "append_file"}:
                continue
            path = _normalize_path(operation.get("path"))
            if not path:
                continue
            existing = contents.get(path, "")
            contents[path] = existing + str(operation.get("content") or "")
    return contents


def _declared_expected_files(plan: list[dict[str, Any]]) -> set[str]:
    paths: set[str] = set()
    for step in plan:
        for path_text in step.get("expected_files") or []:
            path = _normalize_path(path_text)
            if path:
                paths.add(path)
    return paths


def _contract_value(
    planner_contract: Mapping[str, Any] | None,
    *names: str,
) -> Any:
    if not planner_contract:
        return None
    for name in names:
        if name in planner_contract and planner_contract[name] is not None:
            return planner_contract[name]
    return None


def _single_fact_value(
    facts: set[str],
    allowed: set[str] | frozenset[str],
) -> tuple[str | None, bool]:
    matches = sorted(facts & set(allowed))
    if len(matches) == 1:
        return matches[0], True
    return None, not matches


def _registered_planner_contract_resolution(
    planner_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate explicit planner facts without inferring missing values."""

    if not isinstance(planner_contract, Mapping):
        return {
            "status": "missing_registered_contract_facts",
            "contract_id": PLANNER_CONTRACT_ID,
            "contract_version": PLANNER_CONTRACT_VERSION,
            "scenario_id": None,
            "source_expectation": None,
            "test_expectation": None,
            "facts": [],
            "unknown_facts": [],
            "missing_facts": ["CONTRACT_REGISTERED", "SCENARIO_ID_MATCH"],
            "reason": "planner contract facts were not supplied",
        }

    contract_id = str(
        _contract_value(planner_contract, "contract_id", "planner_contract_id") or ""
    ).strip()
    registered = registered_planner_contract(contract_id)
    facts = truthy_structural_facts(
        _contract_value(planner_contract, "structural_evidence", "facts")
    )
    unknown_facts = sorted(facts - REGISTERED_STRUCTURAL_FACTS)
    if registered is None:
        return {
            "status": "unregistered_contract",
            "contract_id": contract_id or None,
            "contract_version": None,
            "scenario_id": str(_contract_value(planner_contract, "scenario_id") or "")
            or None,
            "source_expectation": None,
            "test_expectation": None,
            "facts": sorted(facts),
            "unknown_facts": unknown_facts,
            "missing_facts": [],
            "reason": "planner contract ID is not registered",
        }

    scenario_id = str(_contract_value(planner_contract, "scenario_id") or "").strip()
    if not scenario_id and contract_id in {
        "ST23-S2-1-v1",
        "ST23-S2-2-v1",
        "ST23-S2-3-v1",
        "ST23-S2-4-v1",
        "ST23-S2-5-v1",
        "ST23-S3-1-v1",
        "ST23-S3-2-v1",
        "ST23-S3-3-v1",
    }:
        scenario_id = contract_id.removeprefix("ST23-").removesuffix("-v1")

    source_expectation = str(
        _contract_value(
            planner_contract,
            "source_expectation",
            "source_state",
            "SOURCE_EXPECTATION_DECLARED",
        )
        or ""
    ).strip()
    test_expectation = str(
        _contract_value(
            planner_contract,
            "test_expectation",
            "TEST_EXPECTATION_DECLARED",
        )
        or ""
    ).strip()
    source_fact, source_fact_absent = _single_fact_value(facts, SOURCE_EXPECTATIONS)
    test_fact, test_fact_absent = _single_fact_value(facts, TEST_EXPECTATIONS)
    if not source_expectation and source_fact_absent:
        source_expectation = source_fact or ""
    if not test_expectation and test_fact_absent:
        test_expectation = test_fact or ""

    missing_facts = sorted(
        set(registered.required_facts)
        - facts
        - {
            "SOURCE_EXPECTATION_DECLARED" if source_expectation else "",
            "TEST_EXPECTATION_DECLARED" if test_expectation else "",
        }
        - {"CONTRACT_REGISTERED", "SCENARIO_ID_MATCH"}
    )
    if not contract_id:
        missing_facts.append("CONTRACT_REGISTERED")
    if not scenario_id:
        missing_facts.append("SCENARIO_ID_MATCH")
    elif scenario_id not in REGISTERED_PLANNER_SCENARIO_IDS:
        missing_facts.append("SCENARIO_ID_MATCH")
    if not source_expectation:
        missing_facts.append("SOURCE_EXPECTATION_DECLARED")
    if not test_expectation:
        missing_facts.append("TEST_EXPECTATION_DECLARED")
    if source_expectation and source_expectation not in SOURCE_EXPECTATIONS:
        missing_facts.append("SOURCE_EXPECTATION_VALUE")
    if test_expectation and test_expectation not in TEST_EXPECTATIONS:
        missing_facts.append("TEST_EXPECTATION_VALUE")
    if source_fact and source_expectation and source_fact != source_expectation:
        missing_facts.append("SOURCE_EXPECTATION_CONFLICT")
    if test_fact and test_expectation and test_fact != test_expectation:
        missing_facts.append("TEST_EXPECTATION_CONFLICT")

    version = str(
        _contract_value(planner_contract, "contract_version", "version")
        or registered.version
    ).strip()
    if version != registered.version:
        missing_facts.append("CONTRACT_VERSION_MATCH")

    status = "registered" if not missing_facts and not unknown_facts else "incomplete"
    return {
        "status": status,
        "contract_id": contract_id,
        "contract_version": version,
        "scenario_id": scenario_id or None,
        "source_expectation": source_expectation or None,
        "test_expectation": test_expectation or None,
        "facts": sorted(facts),
        "unknown_facts": unknown_facts,
        "missing_facts": sorted(set(missing_facts)),
        "reason": (
            "registered planner facts accepted"
            if status == "registered"
            else "registered planner facts are incomplete or conflicting"
        ),
    }


def _strict_bootstrap_classification(
    *,
    source_expectation: str,
    source_candidates: list[str],
    artifact_candidates: list[str],
) -> tuple[BootstrapTaskType, str, list[str]]:
    source_required = source_expectation not in {
        "SOURCE_NOT_REQUIRED",
        "SOURCE_NOT_REQUIRED_FOR_OBSERVATION",
    }
    if not source_required:
        return (
            (
                BootstrapTaskType.MIXED
                if source_candidates
                else BootstrapTaskType.ARTIFACT_ONLY
            ),
            "artifact_or_observation_bootstrap",
            ["source_materialization_bootstrap", "existing_source_bootstrap"],
        )
    if not source_candidates:
        return (
            BootstrapTaskType.UNKNOWN,
            "source_materialization_bootstrap",
            ["artifact_or_observation_bootstrap", "existing_source_bootstrap"],
        )
    if artifact_candidates:
        return (
            BootstrapTaskType.MIXED,
            "source_and_artifact_bootstrap",
            ["artifact_or_observation_bootstrap"],
        )
    return (
        BootstrapTaskType.SOURCE_CODE,
        (
            "source_materialization_bootstrap"
            if source_expectation == "SOURCE_MATERIALIZED"
            else "existing_source_bootstrap"
        ),
        ["artifact_or_observation_bootstrap"],
    )


def _strict_test_evidence(
    *,
    test_expectation: str,
    test_candidates: list[str],
    materialized: set[str],
    existing_files: set[str],
) -> tuple[str, list[str], list[str], list[str]]:
    existing_tests = sorted(path for path in existing_files if _is_test_path(path))
    generated_tests = sorted(path for path in materialized if _is_test_path(path))
    known_tests = sorted(set(generated_tests) | set(existing_tests))
    evidence: list[str] = ["TEST_EXPECTATION_DECLARED", "TEST_INTENT_DECISION_RECORDED"]
    rejected: list[str] = []
    if test_expectation == "EXPECTED_TEST_NOT_REQUIRED":
        evidence.append("EXPECTED_TEST_NOT_REQUIRED")
        rejected.extend(["require_test_file", "generate_test_file"])
        return (
            "tests_intentionally_absent" if not known_tests else "ready",
            [],
            evidence,
            rejected,
        )
    if test_expectation == "EXPECTED_TEST_PRESENT":
        if known_tests:
            evidence.append("EXPECTED_TEST_PRESENT")
            rejected.extend(["tests_intentionally_absent", "generate_test_file"])
            return (
                "ready",
                sorted(set(test_candidates) | set(existing_tests)),
                evidence,
                rejected,
            )
        rejected.extend(["tests_intentionally_absent", "accept_missing_test_file"])
        return "missing_required_tests", [], evidence, rejected
    if test_expectation == "EXPECTED_TEST_GENERATED":
        if generated_tests:
            evidence.append("EXPECTED_TEST_GENERATED")
            rejected.extend(["tests_intentionally_absent", "accept_existing_test_file"])
            return (
                "ready",
                sorted(set(test_candidates) | set(generated_tests)),
                evidence,
                rejected,
            )
        rejected.extend(["tests_intentionally_absent", "accept_existing_test_file"])
        return "bootstrap_incomplete", sorted(set(test_candidates)), evidence, rejected
    return (
        "contract_limitation",
        [],
        evidence,
        [
            "accept_unregistered_test_policy",
            "infer_test_policy_from_prompt",
        ],
    )


def _classify_bootstrap_task_type(
    *,
    task_prompt: str,
    all_paths: set[str],
) -> tuple[BootstrapTaskType, dict[str, Any]]:
    prompt_lower = str(task_prompt or "").lower()
    positive_source_intent_text = re.sub(
        r"\b(?:do not|don't|without)\s+"
        r"(?:create|write|add|implement|include|use)\b"
        r"[^.;\n]*(?:source\s+code|code|scripts?|packages?|tests?)",
        " ",
        prompt_lower,
    )
    source_paths = sorted(path for path in all_paths if _is_source_path(path))
    test_paths = sorted(path for path in all_paths if _is_test_path(path))
    artifact_paths = sorted(path for path in all_paths if _is_artifact_path(path))

    source_noun_terms = {
        "cli",
        "code",
        "function",
        "module",
        "package",
        "script",
        "source",
        "tests",
    }
    source_action_terms = {
        "feature",
        "implement",
        "implementation",
    }
    artifact_terms = {
        "checklist",
        "doc",
        "docs",
        "documentation",
        "manifest",
        "markdown",
        "readme",
        "report",
        "summary",
    }

    def has_term(terms: set[str]) -> bool:
        return any(
            re.search(rf"\b{re.escape(term)}\b", positive_source_intent_text)
            for term in terms
        )

    has_source_noun_intent = has_term(source_noun_terms)
    has_source_intent = has_source_noun_intent or has_term(source_action_terms)
    has_artifact_intent = any(
        re.search(rf"\b{re.escape(term)}\b", prompt_lower) for term in artifact_terms
    )
    has_source_surface = bool(source_paths or test_paths)
    has_artifact_surface = bool(artifact_paths)

    if has_source_surface and has_artifact_surface:
        task_type = BootstrapTaskType.MIXED
    elif has_artifact_surface and has_source_noun_intent:
        # An artifact-only plan surface is promoted to MIXED only when the
        # prompt names a concrete source deliverable (code, module, tests, ...).
        # Bare action verbs such as "implement the requested change" also
        # describe pure documentation work and must not force a source-file
        # obligation onto an artifact task. Prompts with real source intent
        # and no artifact vocabulary still fall through to UNKNOWN, which
        # keeps source materialization required.
        task_type = BootstrapTaskType.MIXED
    elif has_source_surface:
        task_type = BootstrapTaskType.SOURCE_CODE
    elif has_artifact_surface and has_artifact_intent and not has_source_surface:
        task_type = BootstrapTaskType.ARTIFACT_ONLY
    else:
        task_type = BootstrapTaskType.UNKNOWN

    return task_type, {
        "source_paths": source_paths[:20],
        "test_paths": test_paths[:20],
        "artifact_paths": artifact_paths[:20],
        "has_source_intent": has_source_intent,
        "has_source_noun_intent": has_source_noun_intent,
        "has_artifact_intent": has_artifact_intent,
        "negated_source_intent_removed": positive_source_intent_text != prompt_lower,
    }


def _verification_commands(plan: list[dict[str, Any]]) -> list[str]:
    commands: list[str] = []
    for step in plan:
        verification = str(step.get("verification") or "").strip()
        if verification:
            commands.append(verification)
    return list(dict.fromkeys(commands))


def _has_explicit_code_test_intent(task_prompt: str) -> bool:
    prompt_lower = str(task_prompt or "").lower()
    positive_test_intent_text = re.sub(
        r"\b(?:do not|don't|without)\s+"
        r"(?:create|write|add|implement|include|use|update|provide)\b"
        r"[^.;\n]*(?:tests?|pytest|unit\s+tests?|test\s+files?)",
        " ",
        prompt_lower,
    )
    explicit_patterns = [
        r"\b(?:with|include|add|write|create|update|provide)\s+"
        r"(?:pytest|unit\s+tests?|tests?|test\s+files?|test\s+coverage)\b",
        r"\bwith\b[^.;\n]{0,80}\btests?\b",
        r"\band\s+(?:pytest|unit\s+tests?|tests?|test\s+files?|test\s+coverage)\b",
        r"\b[a-z_][a-z0-9_-]*\s+tests?\b",
        r"\b(?:pytest|unit\s+tests?|test\s+files?|test\s+coverage)\b",
        r"\btests?\s+(?:for|that|cover|exercise|import)\b",
    ]
    return any(
        re.search(pattern, positive_test_intent_text) for pattern in explicit_patterns
    )


def _has_explicit_new_test_writing_intent(task_prompt: str) -> bool:
    """Strict check for prompts that explicitly ask to WRITE new test files.

    Used only when existing tests are already present. Stricter than
    `_has_explicit_code_test_intent` to avoid false positives from:
      - verification commands ("verify with python3 -m pytest -q")
      - references to the existing test suite ("so the existing tests pass")
      - directory scope instructions ("scoped to the src/ and tests/ files")

    Only matches unambiguous new-test-writing directives such as
    "with unit tests", "add tests", "include test coverage", "tests for X".
    """
    prompt_lower = str(task_prompt or "").lower()
    positive_text = re.sub(
        r"\b(?:do not|don't|without)\s+"
        r"(?:create|write|add|implement|include|use|update|provide)\b"
        r"[^.;\n]*(?:tests?|pytest|unit\s+tests?|test\s+files?)",
        " ",
        prompt_lower,
    )
    strict_patterns = [
        # "add tests", "with unit tests", "include test coverage", etc.
        r"\b(?:with|include|add|write|create|update|provide)\s+"
        r"(?:pytest|unit\s+tests?|tests?|test\s+files?|test\s+coverage)\b",
        # "tests for the feature", "tests that verify", etc.
        r"\btests?\s+(?:for|that|cover|exercise|import)\b",
    ]
    return any(re.search(pattern, positive_text) for pattern in strict_patterns)


def _expected_test_reason(
    *,
    bootstrap_task_type: BootstrapTaskType,
    task_prompt: str,
    all_paths: set[str],
    existing_files: set[str],
    source_candidates: list[str],
) -> str | None:
    existing_tests_present = any(_is_test_path(path) for path in existing_files)
    has_explicit_test_intent = _has_explicit_code_test_intent(task_prompt)

    # When existing tests are present, only override to EXPLICIT_CODE_TEST_INTENT
    # if the prompt unambiguously asks to WRITE new tests (not just run them).
    # Verification commands ("python3 -m pytest -q") and references to the
    # existing suite ("so the existing tests pass") must not trigger enforcement.
    if existing_tests_present and bootstrap_task_type in {
        BootstrapTaskType.SOURCE_CODE,
        BootstrapTaskType.MIXED,
    }:
        if _has_explicit_new_test_writing_intent(task_prompt):
            return EXPECTED_TEST_REASON_EXPLICIT_CODE_TEST_INTENT
        return EXPECTED_TEST_REASON_EXISTING_PROJECT_TESTS_PRESENT

    # Existing tests present but task type is ARTIFACT_ONLY or UNKNOWN.
    if existing_tests_present:
        return EXPECTED_TEST_REASON_EXISTING_PROJECT_TESTS_PRESENT

    if has_explicit_test_intent and bootstrap_task_type in {
        BootstrapTaskType.SOURCE_CODE,
        BootstrapTaskType.MIXED,
    }:
        return EXPECTED_TEST_REASON_EXPLICIT_CODE_TEST_INTENT

    if bootstrap_task_type == BootstrapTaskType.MIXED and source_candidates:
        return EXPECTED_TEST_REASON_MIXED_TASK_CODE_COMPONENT

    if bootstrap_task_type == BootstrapTaskType.UNKNOWN and (
        has_explicit_test_intent or any(_is_source_path(path) for path in all_paths)
    ):
        return EXPECTED_TEST_REASON_UNKNOWN_CONSERVATIVE

    if bootstrap_task_type == BootstrapTaskType.ARTIFACT_ONLY:
        return EXPECTED_TEST_REASON_ARTIFACT_ONLY_NO_CODE_TEST_INTENT

    return None


def _minimum_artifact_evidence(plan: list[dict[str, Any]]) -> bool:
    for step in plan:
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "") not in {"write_file", "append_file"}:
                continue
            path = _normalize_path(operation.get("path"))
            if not _is_artifact_path(path):
                continue
            content = str(operation.get("content") or "").strip()
            if len(content) < 12:
                continue
            if PLACEHOLDER_RE.search(content):
                continue
            return True
    return False


def _minimum_implementation_evidence(plan: list[dict[str, Any]]) -> bool:
    for step in plan:
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "") not in {"write_file", "append_file"}:
                continue
            path = _normalize_path(operation.get("path"))
            if not _is_source_path(path):
                continue
            content = str(operation.get("content") or "").strip()
            if len(content) < 24:
                continue
            if PLACEHOLDER_RE.search(content):
                continue
            return True
    return False


def _python_src_layout_packages(paths: set[str]) -> set[str]:
    packages: set[str] = set()
    for path_text in paths:
        path = Path(_normalize_path(path_text))
        parts = path.parts
        if len(parts) < 3 or parts[0] != "src" or path.suffix.lower() != ".py":
            continue
        package = parts[1]
        if package and package.isidentifier():
            packages.add(package)
    return packages


def _python_import_targets_from_test_content(content: str) -> set[str]:
    try:
        tree = ast.parse(content or "")
    except SyntaxError:
        return set()

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = str(alias.name or "").strip()
                if name:
                    imports.add(name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = str(node.module or "").strip()
            if module:
                imports.add(module)
    return imports


def _python_import_targets(plan: list[dict[str, Any]]) -> list[str]:
    contents = _materialized_file_contents(plan)
    imports: set[str] = set()
    for path, content in contents.items():
        if _is_test_path(path) and Path(path).suffix.lower() == ".py":
            imports.update(_python_import_targets_from_test_content(content))
    return sorted(imports)


def _required_python_package_markers(
    *,
    import_targets: list[str],
    source_paths: set[str],
) -> list[str]:
    packages = _python_src_layout_packages(source_paths)
    required: set[str] = set()
    for import_target in import_targets:
        root = import_target.split(".", 1)[0]
        if root in packages:
            required.add(f"src/{root}/__init__.py")
    return sorted(required)


def _forbidden_python_src_layout_imports(
    *,
    import_targets: list[str],
    source_paths: set[str],
) -> list[str]:
    packages = _python_src_layout_packages(source_paths)
    forbidden: set[str] = set()
    for import_target in import_targets:
        parts = import_target.split(".")
        if len(parts) >= 2 and parts[0] == "src" and parts[1] in packages:
            forbidden.add(import_target)
    return sorted(forbidden)


def build_task1_bootstrap_contract(
    *,
    plan: list[dict[str, Any]],
    task_prompt: str = "",
    forbidden_path_drift: list[str] | None = None,
    existing_files: set[str] | None = None,
    planner_contract: Mapping[str, Any] | None = None,
    require_registered_contract: bool = False,
) -> TaskBootstrapContract:
    materialized = _materialized_file_targets(plan)
    declared = _declared_expected_files(plan)
    all_paths = materialized | declared
    normalized_existing_files = {
        _normalize_path(path) for path in existing_files or set()
    }
    known_paths = all_paths | normalized_existing_files
    source_candidates = sorted(path for path in all_paths if _is_source_path(path))
    test_candidates = sorted(path for path in all_paths if _is_test_path(path))
    artifact_candidates = sorted(path for path in all_paths if _is_artifact_path(path))
    import_targets = _python_import_targets(plan)
    package_markers = _required_python_package_markers(
        import_targets=import_targets,
        source_paths=set(source_candidates),
    )
    forbidden_src_imports = _forbidden_python_src_layout_imports(
        import_targets=import_targets,
        source_paths=set(source_candidates),
    )
    missing_package_markers = sorted(
        marker for marker in package_markers if marker not in known_paths
    )
    required_source_files = sorted(set(source_candidates) | set(package_markers))
    required_test_files = sorted(set(test_candidates))
    required_artifacts = sorted(set(required_source_files) | set(required_test_files))
    strict_mode = require_registered_contract or planner_contract is not None
    if strict_mode:
        resolution = _registered_planner_contract_resolution(planner_contract)
        if resolution["status"] == "registered":
            bootstrap_task_type, selected_path, rejected_alternatives = (
                _strict_bootstrap_classification(
                    source_expectation=resolution["source_expectation"],
                    source_candidates=source_candidates,
                    artifact_candidates=artifact_candidates,
                )
            )
            (
                test_classification,
                strict_required_test_files,
                test_evidence,
                test_rejected,
            ) = _strict_test_evidence(
                test_expectation=resolution["test_expectation"],
                test_candidates=test_candidates,
                materialized=materialized,
                existing_files=normalized_existing_files,
            )
            expected_test_reason = (
                EXPECTED_TEST_REASON_NOT_REQUIRED
                if resolution["test_expectation"] == "EXPECTED_TEST_NOT_REQUIRED"
                else resolution["test_expectation"].lower()
            )
            required_test_files = strict_required_test_files
            if resolution["test_expectation"] == "EXPECTED_TEST_NOT_REQUIRED":
                required_test_files = []
            required_artifacts = sorted(
                set(required_source_files) | set(required_test_files)
            )
            if bootstrap_task_type == BootstrapTaskType.ARTIFACT_ONLY:
                required_source_files = []
                required_test_files = []
                required_artifacts = artifact_candidates
            structural_evidence_used = sorted(
                {
                    "CONTRACT_REGISTERED",
                    "SCENARIO_ID_MATCH",
                    "SOURCE_EXPECTATION_DECLARED",
                    resolution["source_expectation"],
                    *test_evidence,
                }
                & REGISTERED_STRUCTURAL_FACTS
            )
            if resolution["source_expectation"] == "SOURCE_PRESENT" and any(
                _is_source_path(path) for path in normalized_existing_files
            ):
                structural_evidence_used.append("SOURCE_PRESENT")
            if resolution["source_expectation"] == "SOURCE_MATERIALIZED" and any(
                path in materialized for path in source_candidates
            ):
                structural_evidence_used.append("SOURCE_MATERIALIZED")
            terminal_classification = test_classification
            if bootstrap_task_type == BootstrapTaskType.UNKNOWN:
                terminal_classification = "missing_source"
            elif terminal_classification == "ready":
                terminal_classification = "ready"
            selected_path = f"{selected_path}:{resolution['test_expectation'].lower()}"
            rejected_alternatives.extend(test_rejected)
            classification_evidence = {
                "source_paths": source_candidates[:20],
                "test_paths": test_candidates[:20],
                "artifact_paths": artifact_candidates[:20],
                "contract_facts": sorted(resolution["facts"]),
                "contract_status": resolution["status"],
                "source_expectation": resolution["source_expectation"],
                "test_expectation": resolution["test_expectation"],
                "structural_evidence_used": sorted(set(structural_evidence_used)),
            }
            return TaskBootstrapContract(
                bootstrap_task_type=bootstrap_task_type,
                classification_evidence=classification_evidence,
                expected_source_files=source_candidates,
                expected_test_files=test_candidates,
                required_artifacts=required_artifacts,
                required_source_files=required_source_files,
                required_test_files=required_test_files,
                required_verification=_verification_commands(plan),
                forbidden_path_drift=sorted(set(forbidden_path_drift or [])),
                python_package_markers=package_markers,
                python_import_targets=import_targets,
                forbidden_python_src_imports=forbidden_src_imports,
                missing_python_package_markers=missing_package_markers,
                expected_test_reason=expected_test_reason,
                minimum_implementation_evidence=_minimum_implementation_evidence(plan),
                minimum_artifact_evidence=_minimum_artifact_evidence(plan),
                contract_id=resolution["contract_id"],
                contract_version=resolution["contract_version"],
                scenario_id=resolution["scenario_id"],
                source_expectation=resolution["source_expectation"],
                test_expectation=resolution["test_expectation"],
                structural_evidence_used=sorted(set(structural_evidence_used)),
                selected_planning_path=selected_path,
                rejected_alternatives=sorted(set(rejected_alternatives)),
                terminal_classification=terminal_classification,
                limitation_id=(
                    "LIM-31D-03"
                    if terminal_classification
                    in {
                        "missing_required_tests",
                        "bootstrap_incomplete",
                    }
                    else None
                ),
                planner_contract_status="registered",
            )

        # A missing or malformed contract is itself a deterministic terminal
        # limitation. No source/test policy is guessed from prompt wording or
        # from absence in the workspace.
        limitation_id = (
            "LIM-31D-03" if resolution["unknown_facts"] == [] else "LIM-31D-04"
        )
        structural_evidence_used = sorted(
            set(resolution["facts"]) & REGISTERED_STRUCTURAL_FACTS
        )
        if source_candidates and artifact_candidates:
            fallback_task_type = BootstrapTaskType.MIXED
        elif source_candidates:
            fallback_task_type = BootstrapTaskType.SOURCE_CODE
        elif artifact_candidates:
            fallback_task_type = BootstrapTaskType.ARTIFACT_ONLY
        else:
            fallback_task_type = BootstrapTaskType.UNKNOWN
        fallback_required_source_files = required_source_files
        fallback_required_test_files = []
        fallback_required_artifacts = sorted(
            set(fallback_required_source_files) | set(fallback_required_test_files)
        )
        if fallback_task_type == BootstrapTaskType.ARTIFACT_ONLY:
            fallback_required_source_files = []
            fallback_required_artifacts = artifact_candidates
        return TaskBootstrapContract(
            bootstrap_task_type=fallback_task_type,
            classification_evidence={
                "source_paths": source_candidates[:20],
                "test_paths": test_candidates[:20],
                "artifact_paths": artifact_candidates[:20],
                "contract_status": resolution["status"],
                "contract_reason": resolution["reason"],
                "contract_facts": sorted(resolution["facts"]),
                "unknown_facts": list(resolution["unknown_facts"]),
                "missing_facts": list(resolution["missing_facts"]),
                "structural_evidence_used": structural_evidence_used,
            },
            expected_source_files=source_candidates,
            expected_test_files=test_candidates,
            required_artifacts=fallback_required_artifacts,
            required_source_files=fallback_required_source_files,
            required_test_files=fallback_required_test_files,
            required_verification=_verification_commands(plan),
            forbidden_path_drift=sorted(set(forbidden_path_drift or [])),
            python_package_markers=package_markers,
            python_import_targets=import_targets,
            forbidden_python_src_imports=forbidden_src_imports,
            missing_python_package_markers=missing_package_markers,
            expected_test_reason=None,
            minimum_implementation_evidence=_minimum_implementation_evidence(plan),
            minimum_artifact_evidence=_minimum_artifact_evidence(plan),
            contract_id=resolution["contract_id"],
            contract_version=resolution["contract_version"],
            scenario_id=resolution["scenario_id"],
            structural_evidence_used=structural_evidence_used,
            selected_planning_path="hold_for_registered_contract_facts",
            rejected_alternatives=[
                "infer_source_policy_from_prompt",
                "infer_test_policy_from_prompt",
                "treat_missing_file_as_contract_evidence",
            ],
            terminal_classification="terminal_limitation",
            limitation_id=limitation_id,
            planner_contract_status=resolution["status"],
        )

    bootstrap_task_type, classification_evidence = _classify_bootstrap_task_type(
        task_prompt=task_prompt,
        all_paths=all_paths,
    )
    expected_test_reason = _expected_test_reason(
        bootstrap_task_type=bootstrap_task_type,
        task_prompt=task_prompt,
        all_paths=all_paths,
        existing_files=normalized_existing_files,
        source_candidates=source_candidates,
    )
    if bootstrap_task_type == BootstrapTaskType.ARTIFACT_ONLY:
        required_source_files = []
        required_test_files = []
        required_artifacts = sorted(
            path for path in all_paths if _is_artifact_path(path)
        )
    return TaskBootstrapContract(
        bootstrap_task_type=bootstrap_task_type,
        classification_evidence=classification_evidence,
        expected_source_files=source_candidates,
        expected_test_files=test_candidates,
        required_artifacts=required_artifacts,
        required_source_files=required_source_files,
        required_test_files=required_test_files,
        required_verification=_verification_commands(plan),
        forbidden_path_drift=sorted(set(forbidden_path_drift or [])),
        python_package_markers=package_markers,
        python_import_targets=import_targets,
        forbidden_python_src_imports=forbidden_src_imports,
        missing_python_package_markers=missing_package_markers,
        expected_test_reason=expected_test_reason,
        minimum_implementation_evidence=_minimum_implementation_evidence(plan),
        minimum_artifact_evidence=_minimum_artifact_evidence(plan),
    )


def validate_task1_bootstrap_contract(
    *,
    plan: list[dict[str, Any]],
    task_prompt: str = "",
    forbidden_path_drift: list[str] | None = None,
    existing_files: set[str] | None = None,
    planner_contract: Mapping[str, Any] | None = None,
    require_registered_contract: bool = False,
) -> TaskBootstrapContractVerdict:
    contract = build_task1_bootstrap_contract(
        plan=plan,
        task_prompt=task_prompt,
        forbidden_path_drift=forbidden_path_drift,
        existing_files=existing_files,
        planner_contract=planner_contract,
        require_registered_contract=require_registered_contract,
    )
    violations: list[str] = []
    codes: list[str] = []

    source_materialization_required = contract.bootstrap_task_type in {
        BootstrapTaskType.SOURCE_CODE,
        BootstrapTaskType.MIXED,
        BootstrapTaskType.UNKNOWN,
    }

    if source_materialization_required and not contract.expected_source_files:
        violations.append("Task 1 bootstrap must declare or materialize source files")
        codes.append("task1_bootstrap_missing_expected_source_files")

    if (
        planner_contract is not None
        and contract.planner_contract_status
        in {
            "registered",
            "incomplete",
            "unregistered_contract",
            "missing_registered_contract_facts",
        }
        and contract.terminal_classification == "terminal_limitation"
    ):
        violations.append(
            "Task 1 bootstrap requires a registered planner contract with explicit source/test facts"
        )
        codes.append("task1_bootstrap_missing_registered_contract_facts")

    if (
        contract.planner_contract_status == "registered"
        and contract.test_expectation == "EXPECTED_TEST_PRESENT"
        and not contract.expected_test_files
    ):
        violations.append(
            "Task 1 bootstrap requires a declared test artifact, but no test file is present"
        )
        codes.append("task1_bootstrap_missing_expected_test_files")

    if (
        contract.planner_contract_status == "registered"
        and contract.test_expectation == "EXPECTED_TEST_GENERATED"
        and not any(_is_test_path(path) for path in _materialized_file_targets(plan))
    ):
        violations.append(
            "Task 1 bootstrap requires a generated test artifact, but no test file is materialized"
        )
        codes.append("task1_bootstrap_expected_test_not_generated")

    if (
        contract.planner_contract_status == "legacy_compatibility"
        and contract.expected_test_reason
        and contract.expected_test_reason
        != EXPECTED_TEST_REASON_ARTIFACT_ONLY_NO_CODE_TEST_INTENT
        and contract.expected_test_reason
        != EXPECTED_TEST_REASON_EXISTING_PROJECT_TESTS_PRESENT
        and not contract.expected_test_files
    ):
        violations.append(
            "Task 1 bootstrap prompt asks for tests but no test files are declared or materialized"
        )
        codes.append("task1_bootstrap_missing_expected_test_files")

    if not contract.required_verification:
        violations.append("Task 1 bootstrap must include required verification")
        codes.append("task1_bootstrap_missing_required_verification")

    if (
        contract.bootstrap_task_type == BootstrapTaskType.ARTIFACT_ONLY
        and not contract.minimum_artifact_evidence
    ):
        violations.append("Task 1 artifact bootstrap lacks deliverable evidence")
        codes.append("task1_bootstrap_minimum_artifact_evidence_missing")

    if contract.forbidden_path_drift:
        violations.append("Task 1 bootstrap contains forbidden path drift")
        codes.append("task1_bootstrap_forbidden_path_drift")

    if contract.missing_python_package_markers:
        markers = ", ".join(contract.missing_python_package_markers[:4])
        violations.append(
            "Task 1 Python src-layout bootstrap is missing package marker files "
            f"required by test imports: {markers}"
        )
        codes.append("task1_bootstrap_missing_python_package_marker")

    if contract.forbidden_python_src_imports:
        imports = ", ".join(contract.forbidden_python_src_imports[:4])
        violations.append(
            "Task 1 Python src-layout tests must import the package namespace, "
            f"not the src prefix: {imports}"
        )
        codes.append("task1_bootstrap_forbidden_python_src_import")

    if source_materialization_required and not contract.minimum_implementation_evidence:
        violations.append("Task 1 bootstrap lacks minimum implementation evidence")
        codes.append("task1_bootstrap_minimum_implementation_evidence_missing")

    return TaskBootstrapContractVerdict(
        contract=contract,
        passed=not violations,
        violations=violations,
        violation_codes=codes,
    )
