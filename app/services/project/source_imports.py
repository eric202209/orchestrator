"""Dependency-free Python import-to-source discovery helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

_PYTHON_IMPORT_LINE_RE = re.compile(
    r"^\s*(?:from\s+(?P<from>[A-Za-z_][A-Za-z0-9_.]*)\s+import\b|"
    r"import\s+(?P<import>[A-Za-z_][A-Za-z0-9_.]*))",
    re.MULTILINE,
)


def source_path_for_module(project_dir: Path, module_name: str) -> Optional[Path]:
    parts = [part for part in module_name.split(".") if part]
    if not parts:
        return None
    candidates = [
        project_dir.joinpath(*parts).with_suffix(".py"),
        project_dir.joinpath(*parts, "__init__.py"),
        project_dir.joinpath("src", *parts).with_suffix(".py"),
        project_dir.joinpath("src", *parts, "__init__.py"),
    ]
    root = project_dir.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root):
                continue
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None


def imported_source_excerpts_from_tests(
    project_dir: Path,
    *,
    truncate,
    max_chars: int,
) -> dict[str, str]:
    """Return compact source excerpts imported by Python test files."""

    excerpts: dict[str, str] = {}
    root = project_dir.resolve()
    ignored_parts = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".openclaw",
    }
    for test_path in sorted(project_dir.rglob("*.py")):
        try:
            rel = test_path.resolve().relative_to(root)
        except ValueError:
            continue
        rel_text = str(rel).replace("\\", "/")
        if set(rel.parts) & ignored_parts:
            continue
        if not (rel.name.startswith("test_") or rel.name.endswith("_test.py")):
            continue
        try:
            test_text = test_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            test_text = test_path.read_text(encoding="utf-8", errors="ignore")
        for match in _PYTHON_IMPORT_LINE_RE.finditer(test_text):
            module_name = (match.group("from") or match.group("import") or "").strip()
            if not module_name:
                continue
            source_path = source_path_for_module(project_dir, module_name)
            if source_path is None:
                continue
            try:
                source_rel = str(source_path.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            if source_rel in excerpts:
                continue
            try:
                source_text = source_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                source_text = source_path.read_text(encoding="utf-8", errors="ignore")
            excerpts[source_rel] = truncate(
                f"# imported by {rel_text}\n{source_text}",
                max_chars,
            )
    return excerpts
