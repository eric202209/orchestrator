"""Small public API guard for bounded debug source repairs."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEBUG_REPAIR_PUBLIC_API_REMOVED_REASON = "debug_repair_public_api_removed"


@dataclass(frozen=True)
class DebugRepairPublicApiRemoval:
    path: str
    module: str
    removed_symbols: list[str]


def detect_debug_repair_public_api_removal(
    *,
    project_dir: Path,
    ops: Any,
) -> list[DebugRepairPublicApiRemoval]:
    """Return required public test imports removed by Python source ops."""

    candidate_contents = _candidate_python_source_contents(project_dir, ops)
    if not candidate_contents:
        return []

    required_by_module = _required_test_symbols_by_module(project_dir)
    removals: list[DebugRepairPublicApiRemoval] = []
    for rel_path, candidate_content in candidate_contents.items():
        module = _module_name_for_source_path(rel_path)
        if not module:
            continue
        required_symbols = {
            symbol
            for symbol in required_by_module.get(module, set())
            if symbol and not symbol.startswith("_")
        }
        if not required_symbols:
            continue
        current_path = project_dir / rel_path
        try:
            current_content = current_path.read_text(encoding="utf-8")
        except OSError:
            current_content = ""
        before_symbols = _public_symbols_from_python(current_content)
        after_symbols = _public_symbols_from_python(candidate_content)
        removed_symbols = sorted(
            symbol
            for symbol in required_symbols
            if symbol in before_symbols and symbol not in after_symbols
        )
        if removed_symbols:
            removals.append(
                DebugRepairPublicApiRemoval(
                    path=rel_path,
                    module=module,
                    removed_symbols=removed_symbols,
                )
            )
    return removals


def public_api_removal_event_details(
    removals: list[DebugRepairPublicApiRemoval],
) -> dict[str, Any]:
    return {
        "removed_public_api": [
            {
                "path": removal.path,
                "module": removal.module,
                "removed_symbols": list(removal.removed_symbols),
            }
            for removal in removals
        ],
        "removed_public_api_paths": [removal.path for removal in removals],
        "removed_public_api_symbols": sorted(
            {symbol for removal in removals for symbol in removal.removed_symbols}
        ),
    }


def _candidate_python_source_contents(
    project_dir: Path,
    ops: Any,
) -> dict[str, str]:
    if not isinstance(ops, list):
        return {}
    contents: dict[str, str] = {}
    for op in ops:
        if not isinstance(op, dict):
            continue
        op_name = str(op.get("op") or "").strip()
        rel_path = str(op.get("path") or "").strip().replace("\\", "/").lstrip("./")
        if not rel_path.startswith("src/") or not rel_path.endswith(".py"):
            continue
        current_content = contents.get(rel_path)
        if current_content is None:
            try:
                current_content = (project_dir / rel_path).read_text(encoding="utf-8")
            except OSError:
                current_content = ""
        if op_name == "write_file":
            contents[rel_path] = str(op.get("content") or "")
            continue
        if op_name == "replace_in_file":
            old = str(op.get("old") or "")
            new = str(op.get("new") or "")
            contents[rel_path] = (
                current_content.replace(old, new, 1) if old else current_content
            )
            continue
        if op_name == "append_file":
            contents[rel_path] = current_content + str(op.get("content") or "")
    return contents


def _required_test_symbols_by_module(project_dir: Path) -> dict[str, set[str]]:
    required: dict[str, set[str]] = {}
    for tests_dir_name in ("tests", "test"):
        tests_dir = project_dir / tests_dir_name
        if not tests_dir.is_dir():
            continue
        for test_path in tests_dir.rglob("*.py"):
            _collect_test_import_requirements(test_path, required)
    return required


def _collect_test_import_requirements(
    test_path: Path,
    required: dict[str, set[str]],
) -> None:
    try:
        tree = ast.parse(test_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return

    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if not _looks_like_project_module(node.module):
                continue
            for alias in node.names:
                name = alias.name
                if name == "*" or name.startswith("_"):
                    continue
                required.setdefault(node.module, set()).add(name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not _looks_like_project_module(alias.name):
                    continue
                local_name = alias.asname or alias.name.split(".")[-1]
                aliases[local_name] = alias.name

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        root = node.value
        if isinstance(root, ast.Name) and root.id in aliases:
            attr = node.attr
            if attr and not attr.startswith("_"):
                required.setdefault(aliases[root.id], set()).add(attr)


def _public_symbols_from_python(content: str) -> set[str]:
    try:
        tree = ast.parse(content or "")
    except SyntaxError:
        return set()
    symbols: set[str] = set()
    for node in tree.body:
        name = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_name = alias.asname or alias.name
                if imported_name and imported_name != "*":
                    symbols.add(imported_name)
            continue
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_name = alias.asname or alias.name.split(".")[0]
                if imported_name:
                    symbols.add(imported_name)
            continue
        if name and not name.startswith("_"):
            symbols.add(name)
    return {symbol for symbol in symbols if not symbol.startswith("_")}


def _module_name_for_source_path(path: str) -> str | None:
    normalized = path.replace("\\", "/").lstrip("./")
    if not normalized.startswith("src/") or not normalized.endswith(".py"):
        return None
    module_path = normalized[len("src/") : -len(".py")]
    if module_path.endswith("/__init__"):
        module_path = module_path[: -len("/__init__")]
    return module_path.replace("/", ".") if module_path else None


def _looks_like_project_module(module: str) -> bool:
    return bool(module and not module.startswith((".", "_")) and "." in module)
