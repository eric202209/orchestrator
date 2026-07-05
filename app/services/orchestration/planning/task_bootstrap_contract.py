"""Declarative Task-1 bootstrap planning contract."""

from __future__ import annotations

import re
import ast
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.services.orchestration.validation.workspace_checks import SOURCE_EXTENSIONS


TEST_ROOTS = {"test", "tests", "spec", "specs"}
VERIFICATION_HELPER_SCRIPT_RE = re.compile(r"(?:^|/)verify_task1_step\d+\.py$")
EXPECTED_TEST_REASON_EXPLICIT_CODE_TEST_INTENT = "explicit_code_test_intent"
EXPECTED_TEST_REASON_EXISTING_PROJECT_TESTS_PRESENT = "existing_project_tests_present"
EXPECTED_TEST_REASON_MIXED_TASK_CODE_COMPONENT = "mixed_task_code_component"
EXPECTED_TEST_REASON_UNKNOWN_CONSERVATIVE = "unknown_conservative"
EXPECTED_TEST_REASON_ARTIFACT_ONLY_NO_CODE_TEST_INTENT = (
    "artifact_only_no_code_test_intent"
)
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

    source_terms = {
        "cli",
        "code",
        "function",
        "feature",
        "implement",
        "implementation",
        "module",
        "package",
        "script",
        "source",
        "tests",
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

    has_source_intent = has_term(source_terms)
    has_artifact_intent = any(
        re.search(rf"\b{re.escape(term)}\b", prompt_lower) for term in artifact_terms
    )
    has_source_surface = bool(source_paths or test_paths)
    has_artifact_surface = bool(artifact_paths)

    if has_source_surface and has_artifact_surface:
        task_type = BootstrapTaskType.MIXED
    elif has_artifact_surface and has_source_intent:
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
) -> TaskBootstrapContract:
    materialized = _materialized_file_targets(plan)
    declared = _declared_expected_files(plan)
    all_paths = materialized | declared
    normalized_existing_files = {
        _normalize_path(path) for path in existing_files or set()
    }
    known_paths = all_paths | normalized_existing_files
    bootstrap_task_type, classification_evidence = _classify_bootstrap_task_type(
        task_prompt=task_prompt,
        all_paths=all_paths,
    )
    source_candidates = sorted(path for path in all_paths if _is_source_path(path))
    test_candidates = sorted(path for path in all_paths if _is_test_path(path))
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
) -> TaskBootstrapContractVerdict:
    contract = build_task1_bootstrap_contract(
        plan=plan,
        task_prompt=task_prompt,
        forbidden_path_drift=forbidden_path_drift,
        existing_files=existing_files,
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
        contract.expected_test_reason
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
