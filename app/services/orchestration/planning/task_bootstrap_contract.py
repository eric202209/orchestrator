"""Declarative Task-1 bootstrap planning contract."""

from __future__ import annotations

import re
import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.orchestration.validation.workspace_checks import SOURCE_EXTENSIONS


TEST_ROOTS = {"test", "tests", "spec", "specs"}
PLACEHOLDER_RE = re.compile(
    r"\b(?:pass|todo|fixme|stub|placeholder|notimplemented|notimplementederror)\b|"
    r"\bnot[-_\s]*implemented\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TaskBootstrapContract:
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
    minimum_implementation_evidence: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
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
            "minimum_implementation_evidence": self.minimum_implementation_evidence,
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


def _is_test_path(path_text: str) -> bool:
    parts = Path(path_text).parts
    return bool(parts and parts[0].lower() in TEST_ROOTS)


def _is_source_path(path_text: str) -> bool:
    normalized = _normalize_path(path_text)
    if not normalized or _is_test_path(normalized):
        return False
    return Path(normalized).suffix.lower() in SOURCE_EXTENSIONS


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


def _verification_commands(plan: list[dict[str, Any]]) -> list[str]:
    commands: list[str] = []
    for step in plan:
        verification = str(step.get("verification") or "").strip()
        if verification:
            commands.append(verification)
    return list(dict.fromkeys(commands))


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
    forbidden_path_drift: list[str] | None = None,
    existing_files: set[str] | None = None,
) -> TaskBootstrapContract:
    materialized = _materialized_file_targets(plan)
    declared = _declared_expected_files(plan)
    all_paths = materialized | declared
    known_paths = all_paths | {
        _normalize_path(path) for path in existing_files or set()
    }
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
    return TaskBootstrapContract(
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
        minimum_implementation_evidence=_minimum_implementation_evidence(plan),
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
        forbidden_path_drift=forbidden_path_drift,
        existing_files=existing_files,
    )
    violations: list[str] = []
    codes: list[str] = []

    if not contract.expected_source_files:
        violations.append("Task 1 bootstrap must declare or materialize source files")
        codes.append("task1_bootstrap_missing_expected_source_files")

    prompt_lower = str(task_prompt or "").lower()
    if "test" in prompt_lower and not contract.expected_test_files:
        violations.append(
            "Task 1 bootstrap prompt asks for tests but no test files are declared or materialized"
        )
        codes.append("task1_bootstrap_missing_expected_test_files")

    if not contract.required_verification:
        violations.append("Task 1 bootstrap must include required verification")
        codes.append("task1_bootstrap_missing_required_verification")

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

    if not contract.minimum_implementation_evidence:
        violations.append("Task 1 bootstrap lacks minimum implementation evidence")
        codes.append("task1_bootstrap_minimum_implementation_evidence_missing")

    return TaskBootstrapContractVerdict(
        contract=contract,
        passed=not violations,
        violations=violations,
        violation_codes=codes,
    )
