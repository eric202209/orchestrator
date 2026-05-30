"""Planning source materialization helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

SOURCE_MATERIALIZATION_EXTENSIONS = ".py .js .jsx .ts .tsx .css .html .md".split()
IMPLEMENTATION_SOURCE_EXTENSIONS = ".py .js .jsx .ts .tsx .css .html".split()
SOURCE_MATERIALIZATION_REPAIR_MARKERS = (
    "missing_source_materialization",
    "does not materialize any source changes",
    "no source materialization",
    "plan does not materialize source changes",
    "contextual python control-flow fragments",
    "unsafe_python_append",
    "framework_mismatch",
    "decorators whose root name is undefined",
    "undefined decorator root",
    "undefined_python_test_names",
    "obvious undefined names",
    "placeholder_only_implementation",
    "placeholder or stub implementations",
)


def plan_source_materialization_paths(plan: Any) -> set[str]:
    """Return concrete source-like file write targets from a plan."""

    if not isinstance(plan, list):
        return set()

    paths: set[str] = set()
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
            }:
                continue
            path_text = (
                str(operation.get("path") or "").strip().rstrip("/").lstrip("./")
            )
            if not path_text:
                continue
            path = Path(path_text)
            if path.suffix.lower() not in SOURCE_MATERIALIZATION_EXTENSIONS:
                continue
            paths.add(path.as_posix())
    return paths


def repair_removed_source_materialization(
    previous_plan: Any, repaired_plan: Any
) -> list[str]:
    previous_source_paths = plan_source_materialization_paths(previous_plan)
    if not previous_source_paths:
        return []
    repaired_source_paths = plan_source_materialization_paths(repaired_plan)
    if repaired_source_paths:
        return []
    return sorted(previous_source_paths)


def top_level_package_roots(project_dir: Path) -> set[str]:
    roots: set[str] = set()
    try:
        for child in project_dir.iterdir():
            if (
                child.is_dir()
                and child.name not in {"tests", "test", "__pycache__"}
                and (child / "__init__.py").exists()
            ):
                roots.add(child.name)
    except OSError:
        return roots
    return roots


def is_concrete_source_materialization_path(path_text: str, project_dir: Path) -> bool:
    normalized = str(path_text or "").strip().rstrip("/").lstrip("./")
    if not normalized:
        return False
    path = Path(normalized)
    parts = path.parts
    if not parts or parts[0] in {"tests", "test"}:
        return False
    if path.suffix.lower() not in IMPLEMENTATION_SOURCE_EXTENSIONS:
        return False
    if parts[0] == "src" and len(parts) > 1:
        return True
    return parts[0] in top_level_package_roots(project_dir)


def plan_has_concrete_source_materialization(plan: Any, project_dir: Path) -> bool:
    if not isinstance(plan, list):
        return False
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
            }:
                continue
            if is_concrete_source_materialization_path(
                str(operation.get("path") or ""),
                project_dir,
            ):
                return True
    return False


def repair_context_requires_source_materialization(
    *,
    execution_profile: str | None,
    reason: str = "",
    rejection_reasons: list[str] | None = None,
) -> bool:
    if str(execution_profile or "") not in {"implementation", "full_lifecycle"}:
        return False
    text = "\n".join(
        [str(reason or "")] + [str(item or "") for item in (rejection_reasons or [])]
    ).lower()
    return any(marker in text for marker in SOURCE_MATERIALIZATION_REPAIR_MARKERS)
