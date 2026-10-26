"""Bounded direct first-party import facts for grounding admission (Phase 40C-2).

Grounding can name a source file (``dataflow/aggregate.py``) while the defect
lives in a first-party module it imports (``dataflow/normalize.py``).  This
module resolves only the *direct* first-party Python imports of already
selected source files, so admission can ask for them instead of claiming that
the task-mentioned files alone are sufficient.

Static and conservative, mirroring ``orientation_import_facts``:

* ``ast`` ``Import``/``ImportFrom`` nodes only; no code is executed or imported.
* An import is first-party only when its top-level name is a package (a
  directory with ``__init__.py``) or module at the workspace root or under
  ``src/``.  Standard-library and third-party imports are therefore ignored.
* Relative imports resolve against the importing file and never above the
  workspace root.
* Candidates are declared through the Accepted Path Authority rules and
  observed segment by segment; a symlink anywhere disqualifies the path.
* One hop only: imports of imported modules are never followed, so cycles and
  repository-wide traversal cannot occur.

Nothing here reads source into Planning or grants authority; the caller feeds
resolved paths through the existing bounded materializer and its budgets.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.services.orchestration.planning.orientation_import_facts import (
    _read_source,
    is_import_fact_source,
    is_test_path,
)

# Direct dependencies admitted per task.  Exceeding it means relevance cannot
# be bounded, so admission keeps the normal discovery pathway.
MAX_DIRECT_IMPORT_FILES = 4

_SOURCE_ROOTS = ("", "src/")


@dataclass(frozen=True)
class DirectImportExpansion:
    """Direct first-party imports of the anchor files, one hop only."""

    resolved: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    exceeded: bool = False


def _regular_file(root: Path, relative: str) -> bool:
    # Deferred: the validation package imports the planning package.
    from app.services.orchestration.validation.path_authority import (
        EntryType,
        PathAuthorityError,
        declare,
        observe,
    )

    try:
        canonical = declare(relative)
        observation = observe(root, canonical, include_content=False)
    except (PathAuthorityError, OSError):
        return False
    return (
        observation.exists
        and not observation.symlink_segment
        and observation.entry_type is EntryType.REGULAR_FILE
    )


def first_party_roots(root: Path) -> frozenset[str]:
    """Top-level importable names owned by the workspace."""

    names: set[str] = set()
    for base in _SOURCE_ROOTS:
        directory = root / base if base else root
        try:
            children = list(directory.iterdir())
        except OSError:
            continue
        for child in children:
            name = child.name
            if name.startswith(".") or child.is_symlink():
                continue
            if child.is_dir() and name.isidentifier():
                if _regular_file(root, f"{base}{name}/__init__.py"):
                    names.add(name)
            elif child.suffix == ".py" and child.stem.isidentifier():
                if child.stem != "__init__" and _regular_file(root, f"{base}{name}"):
                    names.add(child.stem)
    names.discard("tests")
    names.discard("test")
    return frozenset(names)


def _module_file(root: Path, base: str, dotted: str) -> str | None:
    parts = dotted.split(".") if dotted else []
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    stem = "/".join(parts)
    for candidate in (f"{base}{stem}.py", f"{base}{stem}/__init__.py"):
        if _regular_file(root, candidate):
            return candidate
    return None


def _absolute_targets(
    root: Path, module: str, names: Iterable[str], roots: frozenset[str]
) -> tuple[list[str], bool]:
    """Resolve one absolute import; ``(paths, first_party)``."""

    if module.split(".", 1)[0] not in roots:
        return [], False
    for base in _SOURCE_ROOTS:
        package = _module_file(root, base, module)
        if package is None:
            continue
        paths = [package]
        if package.endswith("/__init__.py"):
            # ``from pkg import name`` may name a submodule.
            for name in names:
                submodule = _module_file(root, base, f"{module}.{name}")
                if submodule is not None:
                    paths.append(submodule)
        return paths, True
    return [], True


def _relative_targets(
    root: Path, importer: str, level: int, module: str | None, names: Iterable[str]
) -> list[str] | None:
    """Resolve one relative import; ``None`` when it cannot be resolved."""

    package_parts = importer.split("/")[:-1]
    if level - 1 > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - (level - 1)]
    prefix = "/".join(base_parts) + "/" if base_parts else ""
    if module:
        target = _module_file(root, prefix, module)
        if target is None:
            return None
        paths = [target]
        if target.endswith("/__init__.py"):
            for name in names:
                submodule = _module_file(root, prefix, f"{module}.{name}")
                if submodule is not None:
                    paths.append(submodule)
        return paths
    paths = []
    for name in names:
        submodule = _module_file(root, prefix, name)
        if submodule is not None:
            paths.append(submodule)
    if not paths and _regular_file(root, f"{prefix}__init__.py"):
        paths.append(f"{prefix}__init__.py")
    return paths or None


def direct_first_party_imports(
    project_dir: Path,
    anchor_paths: Iterable[str],
    *,
    maximum_files: int = MAX_DIRECT_IMPORT_FILES,
) -> DirectImportExpansion:
    """Resolve the direct first-party imports of product Python anchor files."""

    root = Path(project_dir).resolve()
    anchors = [
        path
        for path in dict.fromkeys(str(item or "") for item in anchor_paths)
        if path.endswith(".py") and is_import_fact_source(path)
    ]
    if not anchors:
        return DirectImportExpansion()
    roots = first_party_roots(root)
    anchor_set = set(anchors)
    resolved: list[str] = []
    unresolved: list[str] = []

    def add(path: str) -> None:
        if path in anchor_set or path in resolved or is_test_path(path):
            return
        resolved.append(path)

    for anchor in anchors:
        text = _read_source(root, anchor)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    paths, first_party = _absolute_targets(root, alias.name, (), roots)
                    if first_party and not paths:
                        unresolved.append(alias.name)
                    for path in paths:
                        add(path)
            elif isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names if alias.name != "*"]
                if node.level:
                    paths = _relative_targets(
                        root, anchor, node.level, node.module, names
                    )
                    if paths is None:
                        unresolved.append("." * node.level + (node.module or ""))
                        continue
                else:
                    paths, first_party = _absolute_targets(
                        root, node.module or "", names, roots
                    )
                    if first_party and not paths:
                        unresolved.append(node.module or "")
                for path in paths:
                    add(path)
    return DirectImportExpansion(
        resolved=tuple(resolved[:maximum_files]),
        unresolved=tuple(dict.fromkeys(unresolved)),
        exceeded=len(resolved) > maximum_files,
    )
