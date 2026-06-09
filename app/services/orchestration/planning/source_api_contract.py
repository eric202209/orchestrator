"""Read-only source/API contract capsule for repair grounding."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceModuleContract:
    path: str
    module: str
    public_symbols: list[str]
    framework_family: str | None = None
    source_excerpt: str = ""


@dataclass(frozen=True)
class SourceApiContractCapsule:
    framework_family: str | None
    source_modules: list[str]
    public_symbols: dict[str, list[str]]
    test_imported_symbols: dict[str, list[str]]
    source_excerpt: dict[str, str]
    modules: list[SourceModuleContract] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "framework_family": self.framework_family,
            "source_modules": list(self.source_modules),
            "public_symbols": {
                module: list(symbols) for module, symbols in self.public_symbols.items()
            },
            "test_imported_symbols": {
                module: list(symbols)
                for module, symbols in self.test_imported_symbols.items()
            },
            "source_excerpt": {
                module: excerpt for module, excerpt in self.source_excerpt.items()
            },
            "modules": [asdict(module) for module in self.modules],
        }


def build_source_api_contract_capsule(
    project_dir: Path,
    *,
    max_excerpt_chars: int = 1200,
) -> SourceApiContractCapsule:
    """Build a bounded Python source/API contract without mutating workspace."""

    root = Path(project_dir).resolve()
    modules: list[SourceModuleContract] = []
    for path in _iter_source_python_files(root):
        rel_path = _relative_path(path, root)
        if not rel_path:
            continue
        module_name = _module_name_for_source_path(rel_path)
        if not module_name:
            continue
        text = _read_text(path)
        if text is None:
            continue
        public_symbols = sorted(_top_level_public_symbols(text))
        framework = _detect_framework_family(text)
        modules.append(
            SourceModuleContract(
                path=rel_path,
                module=module_name,
                public_symbols=public_symbols,
                framework_family=framework,
                source_excerpt=_bounded_source_excerpt(text, max_excerpt_chars),
            )
        )

    modules.sort(key=lambda item: item.path)
    framework_family = _dominant_framework(
        module.framework_family for module in modules
    )
    test_imported_symbols = _collect_test_imported_symbols(root)
    return SourceApiContractCapsule(
        framework_family=framework_family,
        source_modules=[module.path for module in modules],
        public_symbols={
            module.module: module.public_symbols
            for module in modules
            if module.public_symbols
        },
        test_imported_symbols=test_imported_symbols,
        source_excerpt={
            module.module: module.source_excerpt
            for module in modules
            if module.source_excerpt
        },
        modules=modules,
    )


def _iter_source_python_files(root: Path) -> list[Path]:
    source_root = root / "src"
    if not source_root.is_dir():
        return []
    return sorted(
        path for path in source_root.rglob("*.py") if not _is_ignored_path(path, root)
    )


def _collect_test_imported_symbols(root: Path) -> dict[str, list[str]]:
    imported: dict[str, set[str]] = {}
    for tests_dir_name in ("tests", "test"):
        tests_dir = root / tests_dir_name
        if not tests_dir.is_dir():
            continue
        for test_path in sorted(tests_dir.rglob("*.py")):
            if _is_ignored_path(test_path, root):
                continue
            text = _read_text(test_path)
            if text is None:
                continue
            _collect_direct_test_imports(text, imported)
    return {
        module: sorted(symbols)
        for module, symbols in sorted(imported.items())
        if symbols
    }


def _collect_direct_test_imports(text: str, imported: dict[str, set[str]]) -> None:
    try:
        tree = ast.parse(text or "")
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        module = str(node.module)
        if module.startswith(".") or not _looks_like_project_module(module):
            continue
        for alias in node.names:
            name = alias.name
            if not name or name == "*" or name.startswith("_"):
                continue
            imported.setdefault(module, set()).add(alias.asname or name)


def _top_level_public_symbols(text: str) -> set[str]:
    try:
        tree = ast.parse(text or "")
    except SyntaxError:
        return set()
    symbols: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name and not node.name.startswith("_"):
                symbols.add(node.name)
    return symbols


def _detect_framework_family(text: str) -> str | None:
    lowered = text.lower()
    if "import argparse" in lowered or "from argparse import" in lowered:
        return "argparse"
    if "import typer" in lowered or "from typer import" in lowered:
        return "typer"
    if "import click" in lowered or "from click import" in lowered:
        return "click"
    if "from fastapi import" in lowered or "fastapi(" in lowered:
        return "fastapi"
    if "from django" in lowered or "import django" in lowered:
        return "django"
    if "from flask import" in lowered or "flask(" in lowered:
        return "flask"
    return None


def _dominant_framework(frameworks: Any) -> str | None:
    counts: dict[str, int] = {}
    for framework in frameworks:
        if not framework:
            continue
        counts[framework] = counts.get(framework, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _bounded_source_excerpt(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[: max_chars - 3].rstrip() + "..."


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


def _relative_path(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def _is_ignored_path(path: Path, root: Path) -> bool:
    rel = _relative_path(path, root)
    if rel is None:
        return True
    ignored = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".openclaw",
        ".agent",
    }
    return bool(set(Path(rel).parts) & ignored)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
