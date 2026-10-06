"""Static one-hop reverse-import facts for repository orientation (OI-A).

Given anchor paths that lexical orientation already shows, find the Git-tracked,
non-test product source files that *statically* import each anchor.  This is a
repository-topology fact only: nothing here ranks, infers relevance, reads
source into Planning evidence, or grants any authority.

Resolution is deliberately conservative and never executes or imports code:

* Python: ``ast`` ``Import``/``ImportFrom`` nodes only.  ``from P import n``
  resolves to ``P/n.py`` (or ``P/n/__init__.py``) when that module is tracked,
  otherwise to ``P`` itself (a symbol import).  ``import a.b.c`` resolves only
  to ``a/b/c`` exactly.  ``importlib``/``__import__`` and other expressions are
  ignored.
* TypeScript/JavaScript: string-literal specifiers of static ``import``/
  ``export ... from`` and ``import("...")`` only.  Relative specifiers resolve
  against the importer; other specifiers resolve only through the nearest
  tracked ``tsconfig.json`` ``compilerOptions.paths`` entry.  Anything else,
  including bare package names and non-literal dynamic imports, is ignored.

Unreadable, oversized, symlinked, non-UTF-8, or unparseable files contribute no
facts; they never raise.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
from pathlib import Path
from typing import Iterable

PYTHON_SUFFIXES = (".py",)
SCRIPT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx")
MAX_IMPORT_SOURCE_BYTES = 1024 * 1024

_TEST_SEGMENTS = frozenset({"test", "tests", "__tests__", "__mocks__"})
_GENERATED_SEGMENTS = frozenset({"build", "dist", "generated", "__generated__"})
_SCRIPT_RESOLUTION_SUFFIXES = ("", *SCRIPT_SUFFIXES) + tuple(
    f"/index{suffix}" for suffix in SCRIPT_SUFFIXES
)

_SCRIPT_IMPORT_RES = (
    re.compile(
        r"""\b(?:import|export)\s+(?:type\s+)?[\w$*{}\s,]+?\s+from\s*(['"])([^'"\n]+)\1"""
    ),
    re.compile(r"""\bimport\s*(['"])([^'"\n]+)\1"""),
    re.compile(r"""\bimport\s*\(\s*(['"])([^'"\n]+)\1\s*\)"""),
)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        any(part in _TEST_SEGMENTS for part in parts[:-1])
        or name.startswith("test_")
        or name == "conftest.py"
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def is_import_fact_source(path: str) -> bool:
    """Return whether ``path`` may anchor or provide a reverse-import fact."""

    if not path.endswith(PYTHON_SUFFIXES + SCRIPT_SUFFIXES):
        return False
    if any(part in _GENERATED_SEGMENTS for part in path.split("/")[:-1]):
        return False
    return not is_test_path(path)


def _read_source(root: Path, relative: str) -> str | None:
    path = root / relative
    try:
        # A symlink anywhere in the path changes the resolved location.
        if path.resolve() != root / relative or not path.is_file():
            return None
        with path.open("rb") as handle:
            data = handle.read(MAX_IMPORT_SOURCE_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_IMPORT_SOURCE_BYTES:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _strip_script_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments outside string literals."""

    out: list[str] = []
    index, length, quote = 0, len(text), None
    while index < length:
        char = text[index]
        if quote is not None:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"`":
            quote = char
            out.append(char)
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            index = length if end < 0 else end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _python_resolver(tracked: frozenset[str]):
    def resolve(dotted: str) -> str | None:
        parts = dotted.split(".") if dotted else []
        if not parts or not all(part.isidentifier() for part in parts):
            return None
        base = "/".join(parts)
        for candidate in (f"{base}.py", f"{base}/__init__.py"):
            if candidate in tracked:
                return candidate
        return None

    return resolve


def _python_imports(relative: str, text: str, resolve) -> set[str]:
    try:
        tree = ast.parse(text, filename=relative)
    except (SyntaxError, ValueError):
        return set()
    parts = relative[: -len(".py")].split("/")
    package_parts = parts[:-1]  # for __init__.py this is the package itself
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = resolve(alias.name)
                if target:
                    targets.add(target)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                drop = node.level - 1
                if drop > len(package_parts):
                    continue
                base_parts = package_parts[: len(package_parts) - drop]
                if node.module:
                    base_parts = [*base_parts, *node.module.split(".")]
                base = ".".join(base_parts)
            else:
                base = node.module or ""
            for alias in node.names:
                target = None
                if alias.name != "*":
                    target = resolve(f"{base}.{alias.name}" if base else alias.name)
                target = target or resolve(base)
                if target:
                    targets.add(target)
    targets.discard(relative)
    return targets


def _tsconfig_aliases(
    root: Path, config_path: str
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """Return ``(prefix, suffix, target_patterns)`` from one tsconfig, or ()."""

    text = _read_source(root, config_path)
    if text is None:
        return ()
    try:
        data = json.loads(_TRAILING_COMMA_RE.sub(r"\1", _strip_script_comments(text)))
        options = data.get("compilerOptions") or {}
        paths = options.get("paths") or {}
        base_url = options.get("baseUrl") or "."
        if not isinstance(paths, dict) or not isinstance(base_url, str):
            return ()
    except (ValueError, AttributeError):
        return ()
    base_dir = posixpath.normpath(
        posixpath.join(posixpath.dirname(config_path), base_url)
    )
    aliases = []
    for pattern, targets in paths.items():
        if not isinstance(pattern, str) or pattern.count("*") > 1:
            continue
        if not isinstance(targets, list) or not all(
            isinstance(item, str) and item.count("*") <= pattern.count("*")
            for item in targets
        ):
            continue
        prefix, _, suffix = pattern.partition("*")
        resolved = tuple(
            posixpath.normpath(posixpath.join(base_dir, item)) for item in targets
        )
        aliases.append((prefix, suffix if "*" in pattern else None, resolved))
    # Most specific (longest) prefix first, as TypeScript resolves them.
    aliases.sort(key=lambda alias: (-len(alias[0]), alias[0]))
    return tuple(aliases)


def _script_resolver(root: Path, tracked: frozenset[str]):
    alias_cache: dict[str, tuple] = {}

    def aliases_for(importer: str):
        directory = posixpath.dirname(importer)
        while True:
            candidate = (
                posixpath.join(directory, "tsconfig.json")
                if directory
                else "tsconfig.json"
            )
            if candidate in tracked:
                if candidate not in alias_cache:
                    alias_cache[candidate] = _tsconfig_aliases(root, candidate)
                return alias_cache[candidate]
            if not directory:
                return ()
            directory = posixpath.dirname(directory)

    def resolve(importer: str, spec: str) -> str | None:
        if spec.startswith(("./", "../")):
            bases = [
                posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
            ]
        else:
            bases = []
            for prefix, suffix, targets in aliases_for(importer):
                if suffix is None:
                    if spec != prefix:
                        continue
                    bases = list(targets)
                elif (
                    spec.startswith(prefix)
                    and spec.endswith(suffix)
                    and len(spec) >= len(prefix) + len(suffix)
                ):
                    middle = spec[len(prefix) : len(spec) - len(suffix)]
                    bases = [
                        posixpath.normpath(item.replace("*", middle))
                        for item in targets
                    ]
                else:
                    continue
                break
        for base in bases:
            if base == ".." or base.startswith(("../", "/")):
                continue
            for suffix in _SCRIPT_RESOLUTION_SUFFIXES:
                candidate = base + suffix
                if candidate in tracked and candidate.endswith(SCRIPT_SUFFIXES):
                    return candidate
        return None

    return resolve


def _script_imports(relative: str, text: str, resolve) -> set[str]:
    stripped = _strip_script_comments(text)
    targets = set()
    for pattern in _SCRIPT_IMPORT_RES:
        for match in pattern.finditer(stripped):
            target = resolve(relative, match.group(2))
            if target:
                targets.add(target)
    targets.discard(relative)
    return targets


def _anchor_token(anchor: str) -> str:
    """The literal every static import of ``anchor`` must contain."""

    parts = anchor.split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    if stem in ("__init__", "index") and len(parts) > 1:
        return parts[-2]
    return stem


def reverse_import_neighbors(
    project_dir: Path, tracked_paths: Iterable[str], anchors: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """Map each anchor to the sorted non-test tracked files that import it.

    Exactly one hop: only the anchors' direct importers are reported, and an
    importer is never itself expanded.
    """

    root = Path(project_dir).resolve()
    tracked = frozenset(tracked_paths)
    wanted = [
        a for a in dict.fromkeys(anchors) if a in tracked and is_import_fact_source(a)
    ]
    result: dict[str, list[str]] = {anchor: [] for anchor in wanted}
    if not wanted:
        return {}
    python_anchors = {a for a in wanted if a.endswith(PYTHON_SUFFIXES)}
    script_anchors = {a for a in wanted if a.endswith(SCRIPT_SUFFIXES)}
    python_tokens = {_anchor_token(a) for a in python_anchors}
    script_tokens = {_anchor_token(a) for a in script_anchors}
    resolve_python = _python_resolver(tracked)
    resolve_script = _script_resolver(root, tracked)
    for importer in sorted(tracked):
        if not is_import_fact_source(importer):
            continue
        is_python = importer.endswith(PYTHON_SUFFIXES)
        tokens, anchor_set = (
            (python_tokens, python_anchors)
            if is_python
            else (script_tokens, script_anchors)
        )
        if not anchor_set:
            continue
        text = _read_source(root, importer)
        if text is None or not any(token in text for token in tokens):
            continue
        targets = (
            _python_imports(importer, text, resolve_python)
            if is_python
            else _script_imports(importer, text, resolve_script)
        )
        for target in sorted(targets & anchor_set):
            result[target].append(importer)
    return {anchor: tuple(importers) for anchor, importers in result.items()}
