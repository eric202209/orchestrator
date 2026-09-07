"""Minimal prototype structural resolver (PHASE35-PGP1).

Scope discipline: this is *not* a semantic code index. It is `ast` plus a
decorator regex, both already available in the repository, used to answer two
questions the current literal selector cannot answer:

    which named symbol owns this byte?
    which bounded region does this route/symbol name resolve to?

There is no embedding, no vector store, no persisted index, and nothing here
survives the request.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from .protocol import StructuralIdentity

_ROUTE_DECORATOR_RE = re.compile(
    r"(?P<router>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\."
    r"(?P<method>get|post|put|delete|patch)\(\s*[\"'](?P<path>[^\"']+)[\"']"
)
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")

MIN_TOKEN_CHARS = 4


def fold_token(word: str) -> str:
    """Fold a trivial English plural. No stemmer, no synonym table.

    This exists so `projects` in a Task matches `projects` in a route and
    `counts` matches `count`, while keeping `hand` distinct from `handle` and
    `cycle` distinct from `lifecycle` -- the substring collisions that make
    the current literal selector accept unrelated regions.
    """

    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokenize(text: str) -> set[str]:
    """Identifier-aware word tokens, folded. Whole tokens only, never substrings."""

    normalized = _CAMEL_BOUNDARY_RE.sub(r"\1 \2", text).lower()
    return {
        fold_token(word)
        for word in _TOKEN_SPLIT_RE.split(normalized)
        if len(word) >= MIN_TOKEN_CHARS
    }


@dataclass(frozen=True)
class SymbolRegion:
    """One named symbol region of one file, with byte and line spans."""

    path: str
    kind: str
    name: str
    http_method: str | None
    route_path: str | None
    decorator_path: str | None
    mounted_path: str | None
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int

    def identity(self) -> StructuralIdentity:
        return StructuralIdentity(
            kind="route" if self.route_path else self.kind,
            name=self.name,
            http_method=self.http_method,
            route_path=self.route_path,
            decorator_path=self.decorator_path,
            mounted_path=self.mounted_path,
            start_line=self.start_line,
            end_line=self.end_line,
            region_start_byte=self.start_byte,
            region_end_byte=self.end_byte,
        )


def _line_offsets(raw: bytes) -> tuple[list[bytes], list[int]]:
    lines = raw.split(b"\n")
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line) + 1
    return lines, offsets


def _literal_keyword(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg != name:
            continue
        try:
            value = ast.literal_eval(keyword.value)
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, str) else None
    return None


def _local_router_prefixes(tree: ast.AST) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        try:
            constructor = ast.unparse(value.func)
        except (AttributeError, ValueError):
            continue
        if constructor not in {"APIRouter", "fastapi.APIRouter"}:
            continue
        prefix = _literal_keyword(value, "prefix") or ""
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix
    return prefixes


def _join_route_paths(*parts: str | None) -> str:
    segments: list[str] = []
    for part in parts:
        if not part:
            continue
        segments.extend(segment for segment in part.split("/") if segment)
    return "/" + "/".join(segments) if segments else "/"


def _module_name(path: str) -> str:
    module = path[:-3].replace("/", ".")
    return module[:-9] if module.endswith(".__init__") else module


def mounted_router_prefixes(
    project_dir: Path, target_path: str, tracked_paths: tuple[str, ...]
) -> tuple[str, ...]:
    """Find deterministic include_router prefixes for one tracked module.

    This is deliberately a narrow AST read for the prototype: it resolves
    ``from <target module> import router as alias`` followed by
    ``include_router(alias, prefix=...)``. It does not execute imports or
    attempt to model FastAPI's complete router graph.
    """

    target_module = _module_name(target_path)
    aliases: set[str] = set()
    prefixes: set[str] = set()
    target_parts = Path(target_path).parts
    ancestor_dirs = {
        Path(*target_parts[:index]) for index in range(1, len(target_parts))
    }
    candidate_paths = (
        relative_path
        for relative_path in tracked_paths
        if relative_path != target_path
        and Path(relative_path).parent in ancestor_dirs
        and Path(relative_path).name in {"router.py", "routes.py"}
    )
    for relative_path in sorted(candidate_paths):
        candidate = (project_dir / relative_path).resolve()
        try:
            raw = candidate.read_bytes()
            tree = ast.parse(raw.decode("utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            continue
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.module != target_module:
                continue
            for imported in node.names:
                if imported.name == "router":
                    aliases.add(imported.asname or imported.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            try:
                function = ast.unparse(node.func)
                first_arg = ast.unparse(node.args[0]) if node.args else ""
            except (AttributeError, ValueError, IndexError):
                continue
            if not function.endswith(".include_router") or first_arg not in aliases:
                continue
            prefix = _literal_keyword(node, "prefix")
            if prefix:
                prefixes.add(prefix)
    return tuple(sorted(prefixes))


def symbol_regions(
    path: str, raw: bytes, mounted_prefixes: tuple[str, ...] = ()
) -> tuple[SymbolRegion, ...]:
    """Enumerate function/class regions of one Python file.

    A decorated function's region starts at its first decorator, so a route
    resolves to decorator + handler body rather than to the body alone.
    """

    if not path.endswith(".py"):
        return ()
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return ()

    lines, offsets = _line_offsets(raw)
    router_prefixes = _local_router_prefixes(tree)
    regions: list[SymbolRegion] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        decorator_lines = [item.lineno for item in node.decorator_list]
        start_line = min(decorator_lines + [node.lineno])
        end_line = node.end_lineno or node.lineno
        if end_line > len(lines):
            continue
        start_byte = offsets[start_line - 1]
        end_byte = offsets[end_line - 1] + len(lines[end_line - 1])

        http_method: str | None = None
        route_path: str | None = None
        decorator_path: str | None = None
        mounted_path: str | None = None
        for decorator in node.decorator_list:
            try:
                rendered = ast.unparse(decorator)
            except (AttributeError, ValueError):
                continue
            match = _ROUTE_DECORATOR_RE.search(rendered)
            if match:
                http_method = match.group("method").upper()
                decorator_path = match.group("path")
                local_prefix = router_prefixes.get(match.group("router"), "")
                local_path = _join_route_paths(local_prefix, decorator_path)
                route_path = _join_route_paths(*mounted_prefixes, local_path)
                mounted_path = route_path

        regions.append(
            SymbolRegion(
                path=path,
                kind="class" if isinstance(node, ast.ClassDef) else "function",
                name=node.name,
                http_method=http_method,
                route_path=route_path,
                decorator_path=decorator_path,
                mounted_path=mounted_path,
                start_line=start_line,
                end_line=end_line,
                start_byte=start_byte,
                end_byte=end_byte,
            )
        )
    return tuple(regions)


def owning_region(
    regions: tuple[SymbolRegion, ...], byte_offset: int
) -> SymbolRegion | None:
    """Smallest symbol region containing `byte_offset`, if any."""

    owners = [
        region
        for region in regions
        if region.start_byte <= byte_offset < region.end_byte
    ]
    if not owners:
        return None
    return min(owners, key=lambda region: (region.end_byte - region.start_byte))


def file_window_identity(
    raw: bytes, start_byte: int, end_byte: int
) -> StructuralIdentity:
    """Identity for a region no symbol owns (module level, or a non-Python file)."""

    head = raw[:start_byte]
    start_line = head.count(b"\n") + 1
    end_line = start_line + raw[start_byte:end_byte].count(b"\n")
    return StructuralIdentity(
        kind="file_window",
        name=None,
        http_method=None,
        route_path=None,
        decorator_path=None,
        mounted_path=None,
        start_line=start_line,
        end_line=end_line,
        region_start_byte=start_byte,
        region_end_byte=end_byte,
    )


def read_tracked_source(project_dir: Path, relative_path: str) -> bytes | None:
    candidate = (project_dir / relative_path).resolve()
    try:
        candidate.relative_to(project_dir.resolve())
    except ValueError:
        return None
    if not candidate.is_file() or candidate.is_symlink():
        return None
    try:
        return candidate.read_bytes()
    except OSError:
        return None
