"""Read-only planning repair arbitration diagnostics."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from app.services.orchestration.planning.source_api_contract import (
    SourceApiContractCapsule,
)
from app.services.orchestration.planning.source_materialization import (
    plan_source_materialization_paths,
)

_FRAMEWORK_MARKERS: dict[str, tuple[str, ...]] = {
    "argparse": ("import argparse", "from argparse import"),
    "click": ("import click", "from click import", "@click."),
    "typer": ("import typer", "from typer import", "typer.Typer", "@app.command"),
    "fastapi": ("from fastapi import", "FastAPI(", "@app.", "@router."),
    "django": ("from django", "import django"),
    "flask": ("from flask import", "Flask("),
}


def classify_planning_repair_candidate(
    *,
    previous_plan: Any,
    repaired_plan: Any,
    project_dir: Path,
    source_api_capsule: SourceApiContractCapsule | None = None,
    immediate_repair_issues: dict[str, list[int]] | None = None,
    validation_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare a repaired planning candidate to the prior rejected candidate.

    The result is diagnostics only. It intentionally does not decide whether a
    repaired plan should be accepted or rejected.
    """

    details = dict(validation_details or {})
    issues = dict(immediate_repair_issues or {})
    previous_paths = plan_source_materialization_paths(previous_plan)
    repaired_paths = plan_source_materialization_paths(repaired_plan)
    previous_verifiers = _verification_commands(previous_plan)
    repaired_verifiers = _verification_commands(repaired_plan)
    repaired_missing_verification_steps = _missing_verification_steps(repaired_plan)
    previous_invalid_python = _invalid_python_write_paths(previous_plan)
    repaired_invalid_python = _invalid_python_write_paths(repaired_plan)

    labels: list[str] = []
    materialization_status = _materialization_status(previous_paths, repaired_paths)
    verification_status = _verification_status(
        previous_verifiers,
        repaired_verifiers,
        issues=issues,
        details=details,
        missing_verification_steps=repaired_missing_verification_steps,
    )
    syntax_status = _syntax_status(previous_invalid_python, repaired_invalid_python)
    framework_status = _framework_status(
        repaired_plan,
        source_api_capsule=source_api_capsule,
        details=details,
    )
    source_api_analysis = _source_api_contract_analysis(
        repaired_plan, source_api_capsule
    )
    source_api_status = _source_api_status(
        source_api_analysis,
        source_api_capsule=source_api_capsule,
        details=details,
    )
    risk_status = _risk_status(repaired_plan, issues=issues, details=details)

    if materialization_status in {"removed", "moved"}:
        labels.append("removed_materialization")
    if verification_status in {"removed", "invalid"}:
        labels.append("removed_verification")
    if issues.get("stale_replace_ops_steps"):
        labels.append("stale_replace")
    if framework_status == "regressed":
        labels.append("framework_drift")
    if risk_status["test_write_risk"]:
        labels.append("test_rewrite")
    if risk_status["workspace_write_risk"]:
        labels.append("workspace_rewrite")
    if risk_status["package_root_drift"]:
        labels.append("package_root_drift")
    if source_api_status == "regressed":
        labels.append("source_api_regression")
    if not isinstance(repaired_plan, list) or syntax_status in {
        "regressed",
        "still_invalid",
    }:
        labels.append("invalid_output")

    if not labels and (
        materialization_status in {"added", "preserved"}
        or verification_status in {"added", "preserved"}
        or syntax_status == "improved"
    ):
        outcome = "improved_or_preserved"
    elif labels:
        outcome = "regressed"
    else:
        outcome = "neutral"

    return {
        "arbitration_version": "phase11x.read_only.v1",
        "outcome": outcome,
        "regression_labels": list(dict.fromkeys(labels)),
        "source_materialization": {
            "status": materialization_status,
            "previous_paths": sorted(previous_paths)[:20],
            "repaired_paths": sorted(repaired_paths)[:20],
        },
        "verification_contract": {
            "status": verification_status,
            "previous_count": len(previous_verifiers),
            "repaired_count": len(repaired_verifiers),
            "missing_verification_steps": _normalized_ints(
                details.get("missing_verification_steps") or []
            )
            or repaired_missing_verification_steps,
            "weak_verification_steps": _normalized_ints(
                details.get("weak_verification_steps")
                or issues.get("weak_verification_steps")
                or []
            ),
        },
        "python_syntax": {
            "status": syntax_status,
            "previous_invalid_python_writes": previous_invalid_python[:20],
            "repaired_invalid_python_writes": repaired_invalid_python[:20],
        },
        "framework_contract": {
            "status": framework_status,
            "capsule_framework": (
                source_api_capsule.framework_family if source_api_capsule else None
            ),
            "undefined_decorator_files": list(
                details.get("undefined_python_decorator_materializations") or []
            )[:20],
        },
        "source_api_contract": {
            "status": source_api_status,
            "missing_required_symbols": source_api_analysis["missing_required_symbols"][
                :20
            ],
            "source_api_regression_suppressed_due_to_syntax": source_api_analysis[
                "suppressed_due_to_syntax"
            ],
            "physical_src_import_materializations": list(
                details.get("physical_src_import_materializations") or []
            )[:20],
        },
        "write_risk": risk_status,
        "immediate_repair_issues": {
            key: value[:20]
            for key, value in sorted(issues.items())
            if isinstance(value, list) and value
        },
    }


def _materialization_status(previous_paths: set[str], repaired_paths: set[str]) -> str:
    if previous_paths and not repaired_paths:
        return "removed"
    if not previous_paths and repaired_paths:
        return "added"
    if previous_paths and repaired_paths:
        if previous_paths & repaired_paths:
            return "preserved"
        return "moved"
    return "absent"


def _verification_status(
    previous_verifiers: list[str],
    repaired_verifiers: list[str],
    *,
    issues: dict[str, list[int]],
    details: dict[str, Any],
    missing_verification_steps: list[int],
) -> str:
    if previous_verifiers and not repaired_verifiers:
        return "removed"
    if (
        missing_verification_steps
        or details.get("missing_verification_steps")
        or issues.get("weak_verification_steps")
    ):
        return "invalid"
    if not previous_verifiers and repaired_verifiers:
        return "added"
    if previous_verifiers and repaired_verifiers:
        return "preserved"
    return "absent"


def _syntax_status(previous_invalid: list[str], repaired_invalid: list[str]) -> str:
    if previous_invalid and not repaired_invalid:
        return "improved"
    if not previous_invalid and repaired_invalid:
        return "regressed"
    if previous_invalid and repaired_invalid:
        return "still_invalid"
    return "preserved"


def _framework_status(
    plan: Any,
    *,
    source_api_capsule: SourceApiContractCapsule | None,
    details: dict[str, Any],
) -> str:
    if details.get("undefined_python_decorator_materializations"):
        return "regressed"
    expected = source_api_capsule.framework_family if source_api_capsule else None
    if not expected:
        return "unknown"
    observed = _frameworks_in_plan(plan)
    if not observed:
        return "preserved"
    if any(framework != expected for framework in observed):
        return "regressed"
    return "preserved"


def _source_api_status(
    analysis: dict[str, Any],
    *,
    source_api_capsule: SourceApiContractCapsule | None,
    details: dict[str, Any],
) -> str:
    if details.get("physical_src_import_materializations"):
        return "regressed"
    if analysis["missing_required_symbols"]:
        return "regressed"
    if analysis["suppressed_due_to_syntax"]:
        return "unknown"
    if source_api_capsule and source_api_capsule.test_imported_symbols:
        return "preserved"
    return "unknown"


def _risk_status(
    plan: Any,
    *,
    issues: dict[str, list[int]],
    details: dict[str, Any],
) -> dict[str, Any]:
    paths = _write_paths(plan)
    test_paths = [path for path in paths if _is_test_path(path)]
    workspace_paths = [
        path
        for path in paths
        if _looks_like_workspace_rewrite(path)
        or _is_nested_project_root_path(path)
        or path in set(details.get("nested_workspace_paths") or [])
    ]
    package_root_paths = [
        path for path in paths if _has_physical_src_package_drift(path)
    ]
    return {
        "test_write_risk": bool(
            test_paths
            or issues.get("test_assertion_loss_ops_steps")
            or issues.get("test_deletion_ops_steps")
            or details.get("undefined_python_test_name_materializations")
        ),
        "test_write_paths": test_paths[:20],
        "workspace_write_risk": bool(
            workspace_paths
            or details.get("nested_workspace_steps")
            or details.get("nested_project_root_steps")
        ),
        "workspace_write_paths": workspace_paths[:20],
        "package_root_drift": bool(package_root_paths),
        "package_root_drift_paths": package_root_paths[:20],
    }


def _verification_commands(plan: Any) -> list[str]:
    commands: list[str] = []
    if not isinstance(plan, list):
        return commands
    for step in plan:
        if not isinstance(step, dict):
            continue
        verifier = str(step.get("verification") or "").strip()
        if verifier:
            commands.append(verifier)
    return commands


def _missing_verification_steps(plan: Any) -> list[int]:
    missing: list[int] = []
    if not isinstance(plan, list):
        return missing
    for index, step in enumerate(plan, start=1):
        if not isinstance(step, dict):
            continue
        verifier = str(step.get("verification") or "").strip()
        if verifier:
            continue
        has_material_work = bool(step.get("expected_files")) or any(
            isinstance(operation, dict)
            and str(operation.get("op") or "")
            in {"write_file", "append_file", "replace_in_file", "delete_file"}
            for operation in (step.get("ops") or [])
        )
        if has_material_work:
            try:
                missing.append(int(step.get("step_number") or index))
            except (TypeError, ValueError):
                missing.append(index)
    return sorted(set(missing))


def _write_paths(plan: Any) -> list[str]:
    paths: list[str] = []
    if not isinstance(plan, list):
        return paths
    for step in plan:
        if not isinstance(step, dict):
            continue
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "") not in {
                "write_file",
                "append_file",
                "replace_in_file",
                "delete_file",
            }:
                continue
            path = str(operation.get("path") or "").strip().lstrip("./")
            if path:
                paths.append(path)
    return list(dict.fromkeys(paths))


def _invalid_python_write_paths(plan: Any) -> list[str]:
    invalid: list[str] = []
    if not isinstance(plan, list):
        return invalid
    for step in plan:
        if not isinstance(step, dict):
            continue
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "") not in {"write_file", "append_file"}:
                continue
            path = str(operation.get("path") or "").strip()
            content = operation.get("content")
            if not path.endswith(".py") or not isinstance(content, str):
                continue
            try:
                ast.parse(content)
            except SyntaxError:
                invalid.append(path.lstrip("./"))
    return list(dict.fromkeys(invalid))


def _frameworks_in_plan(plan: Any) -> set[str]:
    frameworks: set[str] = set()
    if not isinstance(plan, list):
        return frameworks
    for step in plan:
        if not isinstance(step, dict):
            continue
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            content = operation.get("content") or operation.get("new") or ""
            if not isinstance(content, str):
                continue
            lowered = content.lower()
            for framework, markers in _FRAMEWORK_MARKERS.items():
                if any(marker.lower() in lowered for marker in markers):
                    frameworks.add(framework)
    return frameworks


def _source_api_contract_analysis(
    plan: Any, source_api_capsule: SourceApiContractCapsule | None
) -> dict[str, Any]:
    if not source_api_capsule:
        return {
            "missing_required_symbols": [],
            "suppressed_due_to_syntax": False,
        }
    required = source_api_capsule.test_imported_symbols or {}
    if not required:
        return {
            "missing_required_symbols": [],
            "suppressed_due_to_syntax": False,
        }

    module_by_path = {
        module.path: module.module for module in source_api_capsule.modules or []
    }
    missing: list[str] = []
    suppressed_due_to_syntax = False
    for step in plan if isinstance(plan, list) else []:
        if not isinstance(step, dict):
            continue
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "") != "write_file":
                continue
            path = str(operation.get("path") or "").strip().lstrip("./")
            module_name = module_by_path.get(path) or _module_name_from_src_path(path)
            if not module_name or module_name not in required:
                continue
            content = operation.get("content")
            if not isinstance(content, str):
                continue
            public = _top_level_public_symbols(content)
            if public is None:
                suppressed_due_to_syntax = True
                continue
            for symbol in required[module_name]:
                if symbol not in public:
                    missing.append(f"{module_name}.{symbol}")
    return {
        "missing_required_symbols": list(dict.fromkeys(missing)),
        "suppressed_due_to_syntax": suppressed_due_to_syntax,
    }


def _module_name_from_src_path(path: str) -> str | None:
    normalized = str(path or "").replace("\\", "/").lstrip("./")
    if not normalized.startswith("src/") or not normalized.endswith(".py"):
        return None
    module = normalized[len("src/") : -len(".py")]
    if module.endswith("/__init__"):
        module = module[: -len("/__init__")]
    return module.replace("/", ".") if module else None


def _top_level_public_symbols(content: str) -> set[str] | None:
    try:
        tree = ast.parse(content or "")
    except SyntaxError:
        return None
    symbols: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name and not node.name.startswith("_"):
                symbols.add(node.name)
    return symbols


def _is_test_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").lstrip("./")
    name = Path(normalized).name
    return normalized.startswith(("tests/", "test/")) or (
        normalized.endswith(".py")
        and (name.startswith("test_") or name.endswith("_test.py"))
    )


def _looks_like_workspace_rewrite(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").lstrip("./")
    parts = Path(normalized).parts
    return bool(parts) and parts[0] in {
        ".agent",
        "checkpoints",
        "logs",
        "run",
        "qdrant",
        "venv",
        ".venv",
        "node_modules",
    }


def _is_nested_project_root_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").lstrip("./")
    parts = Path(normalized).parts
    if len(parts) < 2:
        return False
    project_markers = {
        "package.json",
        "pyproject.toml",
        "setup.py",
        "vite.config.ts",
        "vite.config.js",
    }
    return parts[-1] in project_markers and parts[0] not in {
        ".",
        "src",
        "tests",
        "test",
        "frontend",
        "backend",
        "app",
    }


def _has_physical_src_package_drift(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").lstrip("./")
    return bool(re.search(r"(^|/)src/src/", normalized))


def _normalized_ints(values: Any) -> list[int]:
    normalized: list[int] = []
    for value in values or []:
        try:
            normalized.append(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(set(normalized))
