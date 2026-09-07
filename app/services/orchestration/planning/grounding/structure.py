"""Deterministic Python structure mechanics for grounding observations."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import re

from .contracts import (
    GroundingOutcome,
    MAX_STRUCTURAL_REGION_BYTES,
    MountedRouteLocator,
    SourceDocument,
    StructuralIdentity,
    StructuralRelation,
)


HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_ROUTE_METHOD_RE = re.compile(r"^[A-Z]+$")


class PythonStructureError(ValueError):
    """The requested Python source cannot be deterministically parsed."""


@dataclass(frozen=True, slots=True)
class SymbolRegion:
    name: str
    kind: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    route: "RouteDecorator | None" = None


@dataclass(frozen=True, slots=True)
class RouteDecorator:
    method: str
    local_path: str
    handler_name: str
    region: SymbolRegion
    local_router_prefix: str | None = ""


@dataclass(frozen=True, slots=True)
class MountFact:
    source_path: str
    prefix: str | None
    child_module: str
    child_identity: str


@dataclass(frozen=True, slots=True)
class StructuralResolution:
    outcome: GroundingOutcome
    identity: StructuralIdentity | None = None
    documents: tuple[SourceDocument, ...] = ()
    candidates: tuple[str, ...] = ()


def _line_offsets(raw: bytes) -> list[int]:
    offsets = [0]
    for index, byte in enumerate(raw):
        if byte == 10:
            offsets.append(index + 1)
    return offsets


def _line_start(offsets: list[int], line: int) -> int:
    if line <= 0 or line > len(offsets):
        return 0
    return offsets[line - 1]


def _line_end(raw: bytes, offsets: list[int], line: int) -> int:
    if line < len(offsets):
        return offsets[line]
    return len(raw)


def _parse(path: str, raw: bytes) -> tuple[ast.Module, list[int]]:
    try:
        tree = ast.parse(raw.decode("utf-8", errors="replace"), filename=path)
    except (SyntaxError, ValueError, TypeError) as exc:
        raise PythonStructureError(f"unable to parse Python source: {path}") from exc
    return tree, _line_offsets(raw)


def _node_region(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    raw: bytes,
    offsets: list[int],
) -> SymbolRegion:
    decorator_lines = [decorator.lineno for decorator in node.decorator_list]
    start_line = min([node.lineno, *decorator_lines])
    end_line = node.end_lineno or node.lineno
    return SymbolRegion(
        name=node.name,
        kind="class" if isinstance(node, ast.ClassDef) else "function",
        start_line=start_line,
        end_line=end_line,
        start_byte=_line_start(offsets, start_line),
        end_byte=_line_end(raw, offsets, end_line),
    )


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_keyword(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return _literal_string(keyword.value)
    return None


def _local_router_prefixes(tree: ast.Module) -> dict[str, str | None]:
    prefixes: dict[str, str | None] = {}
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
        prefix = _literal_keyword(value, "prefix")
        if (
            any(keyword.arg == "prefix" for keyword in value.keywords)
            and prefix is None
        ):
            prefix = None
        else:
            prefix = prefix or ""
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix
    return prefixes


def _route_from_node(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    region: SymbolRegion,
    router_prefixes: dict[str, str | None],
) -> RouteDecorator | None:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(
            decorator.func, ast.Attribute
        ):
            continue
        method = decorator.func.attr.upper()
        if method not in HTTP_METHODS or not _ROUTE_METHOD_RE.fullmatch(method):
            continue
        if not decorator.args:
            continue
        local_path = _literal_string(decorator.args[0])
        if not local_path or not local_path.startswith("/") or "//" in local_path:
            continue
        try:
            router_name = ast.unparse(decorator.func.value)
        except (AttributeError, ValueError):
            router_name = ""
        return RouteDecorator(
            method,
            local_path,
            node.name,
            region,
            router_prefixes.get(router_name, ""),
        )
    return None


def symbol_regions(path: str, raw: bytes) -> tuple[SymbolRegion, ...]:
    """Return exact function/class AST regions, including nested definitions."""

    tree, offsets = _parse(path, raw)
    router_prefixes = _local_router_prefixes(tree)
    regions: list[SymbolRegion] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        base = _node_region(node, raw, offsets)
        route = (
            _route_from_node(node, base, router_prefixes)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            else None
        )
        regions.append(
            SymbolRegion(
                name=base.name,
                kind=base.kind,
                start_line=base.start_line,
                end_line=base.end_line,
                start_byte=base.start_byte,
                end_byte=base.end_byte,
                route=route,
            )
        )
    return tuple(
        sorted(regions, key=lambda item: (item.start_byte, item.end_byte, item.name))
    )


def _route_regions(path: str, raw: bytes) -> tuple[RouteDecorator, ...]:
    return tuple(
        region.route for region in symbol_regions(path, raw) if region.route is not None
    )


def _identity_for_region(
    relation: str,
    document: SourceDocument,
    region: SymbolRegion,
    *,
    route: RouteDecorator | None = None,
    effective_route_path: str | None = None,
    mount_chain: tuple[str, ...] = (),
) -> StructuralIdentity:
    return StructuralIdentity(
        relation=StructuralRelation(relation),
        source_path=document.path,
        symbol_name=region.name,
        handler_name=route.handler_name if route is not None else None,
        http_method=route.method if route is not None else None,
        decorator_path=route.local_path if route is not None else None,
        local_router_prefix=route.local_router_prefix if route is not None else None,
        effective_route_path=effective_route_path,
        mount_chain=mount_chain,
        start_line=region.start_line,
        end_line=region.end_line,
        start_byte=region.start_byte,
        end_byte=region.end_byte,
    )


def resolve_symbol_definition(
    document: SourceDocument, name: str
) -> StructuralResolution:
    regions = tuple(
        region
        for region in symbol_regions(document.path, document.raw)
        if region.name == name
    )
    if not regions:
        return StructuralResolution(GroundingOutcome.NOT_FOUND, documents=(document,))
    if len(regions) > 1:
        return StructuralResolution(
            GroundingOutcome.AMBIGUOUS,
            documents=(document,),
            candidates=tuple(
                f"{document.path}:{region.start_line}" for region in regions
            ),
        )
    return StructuralResolution(
        GroundingOutcome.FOUND,
        identity=_identity_for_region("symbol_definition", document, regions[0]),
        documents=(document,),
    )


def resolve_enclosing_symbol(
    document: SourceDocument, line: int
) -> StructuralResolution:
    regions = tuple(
        region
        for region in symbol_regions(document.path, document.raw)
        if region.start_line <= line <= region.end_line
    )
    if not regions:
        return StructuralResolution(GroundingOutcome.NOT_FOUND, documents=(document,))
    smallest_span = min(region.end_byte - region.start_byte for region in regions)
    smallest = tuple(
        region
        for region in regions
        if region.end_byte - region.start_byte == smallest_span
    )
    if len(smallest) > 1:
        return StructuralResolution(
            GroundingOutcome.AMBIGUOUS,
            documents=(document,),
            candidates=tuple(
                f"{document.path}:{region.start_line}" for region in smallest
            ),
        )
    return StructuralResolution(
        GroundingOutcome.FOUND,
        identity=_identity_for_region("enclosing_symbol", document, smallest[0]),
        documents=(document,),
    )


def _module_name(path: str) -> str:
    module = path[:-3] if path.endswith(".py") else path
    if module.endswith("/__init__"):
        module = module[: -len("/__init__")]
    return module.replace("/", ".")


def _route_candidate_paths(path: str, tracked_paths: Iterable[str]) -> tuple[str, ...]:
    tracked = set(tracked_paths)
    parts = path.split("/")[:-1]
    candidates: list[str] = []
    for index in range(len(parts), -1, -1):
        prefix = "/".join(parts[:index])
        for filename in ("router.py", "routes.py"):
            candidate = f"{prefix}/{filename}" if prefix else filename
            if candidate in tracked and candidate != path:
                candidates.append(candidate)
    return tuple(dict.fromkeys(candidates))


def _resolve_import_module(node: ast.ImportFrom, source_module: str) -> str:
    if node.level == 0:
        return node.module or ""
    package_parts = source_module.split(".")[:-1]
    if node.level > len(package_parts) + 1:
        return ""
    base = package_parts[: len(package_parts) - node.level + 1]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(part for part in base if part)


def _imported_identities(
    tree: ast.Module, target_module: str, source_module: str
) -> dict[str, str]:
    identities: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.ImportFrom)
            and _resolve_import_module(node, source_module) == target_module
        ):
            for alias in node.names:
                if alias.name == "router":
                    local = alias.asname or alias.name
                    identities[local] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == target_module:
                    identities[alias.asname or alias.name.rsplit(".", 1)[-1]] = "router"
    return identities


def _call_identity(node: ast.AST, imported: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return imported.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id in imported and node.attr == "router":
            return imported[node.value.id]
    return None


def _mount_facts(path: str, raw: bytes, target_module: str) -> tuple[MountFact, ...]:
    tree, _ = _parse(path, raw)
    imported = _imported_identities(tree, target_module, _module_name(path))
    router_prefixes = _local_router_prefixes(tree)
    if not imported:
        return ()
    facts: list[MountFact] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "include_router" or not node.args:
            continue
        child_identity = _call_identity(node.args[0], imported)
        if child_identity is None:
            continue
        include_prefix: str | None = "/"
        prefix_is_static = True
        for keyword in node.keywords:
            if keyword.arg == "prefix":
                include_prefix = _literal_string(keyword.value)
                prefix_is_static = include_prefix is not None
        if (
            prefix_is_static
            and include_prefix
            and (not include_prefix.startswith("/") or "//" in include_prefix)
        ):
            include_prefix = None
        try:
            receiver_name = ast.unparse(node.func.value)
        except (AttributeError, ValueError):
            receiver_name = ""
        receiver_prefix = router_prefixes.get(receiver_name, "")
        if receiver_prefix is None or include_prefix is None:
            prefix = None
        else:
            prefix = _join_route_paths(receiver_prefix, include_prefix)
        facts.append(
            MountFact(
                source_path=path,
                prefix=prefix,
                child_module=target_module,
                child_identity=child_identity,
            )
        )
    return tuple(facts)


def _join_route_paths(prefix: str | None, local_path: str) -> str:
    if prefix in {"", "/"}:
        return local_path or "/"
    if local_path == "/":
        return prefix.rstrip("/") or "/"
    result = f"/{prefix.strip('/')}/{local_path.strip('/')}"
    return f"{result}/" if local_path.endswith("/") else result


def _mount_chains(
    target_module: str,
    target_path: str,
    tracked_paths: Iterable[str],
    read_source: Callable[[str], SourceDocument],
    visited: tuple[str, ...] = (),
) -> tuple[tuple[MountFact, ...], tuple[SourceDocument, ...]]:
    if target_module in visited:
        return (), ()
    candidates: list[tuple[MountFact, SourceDocument]] = []
    for candidate_path in _route_candidate_paths(target_path, tracked_paths):
        document = read_source(candidate_path)
        for fact in _mount_facts(candidate_path, document.raw, target_module):
            candidates.append((fact, document))
    if not candidates:
        return ((),), ()

    chains: list[tuple[MountFact, ...]] = []
    documents: dict[str, SourceDocument] = {}
    next_visited = (*visited, target_module)
    for fact, document in candidates:
        documents[document.path] = document
        outer_chains, outer_documents = _mount_chains(
            _module_name(fact.source_path),
            fact.source_path,
            tracked_paths,
            read_source,
            next_visited,
        )
        documents.update({item.path: item for item in outer_documents})
        if not outer_chains:
            chains.append((fact,))
        else:
            chains.extend((*outer, fact) for outer in outer_chains)
    unique_chains: list[tuple[MountFact, ...]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for chain in chains:
        key = tuple((item.source_path, item.prefix) for item in chain)
        if key not in seen:
            seen.add(key)
            unique_chains.append(chain)
    return tuple(unique_chains), tuple(documents.values())


def resolve_mounted_route(
    document: SourceDocument,
    locator: MountedRouteLocator,
    tracked_paths: Iterable[str],
    read_source: Callable[[str], SourceDocument],
) -> StructuralResolution:
    routes = tuple(
        route
        for route in _route_regions(document.path, document.raw)
        if route.method == locator.method and route.local_path == locator.decorator_path
    )
    if not routes:
        return StructuralResolution(GroundingOutcome.NOT_FOUND, documents=(document,))
    if len(routes) > 1:
        return StructuralResolution(
            GroundingOutcome.AMBIGUOUS,
            documents=(document,),
            candidates=tuple(
                f"{document.path}:{route.handler_name}" for route in routes
            ),
        )

    route = routes[0]
    chains, mount_documents = _mount_chains(
        _module_name(document.path), document.path, tracked_paths, read_source
    )
    if len(chains) > 1:
        documents = (
            document,
            *tuple(item for item in mount_documents if item.path != document.path),
        )
        return StructuralResolution(
            GroundingOutcome.AMBIGUOUS,
            documents=documents,
            candidates=tuple(
                "+".join(f"{item.source_path}:{item.prefix}" for item in chain)
                for chain in chains
            ),
        )
    chain = chains[0] if chains else ()
    if any(item.prefix is None for item in chain):
        documents = (
            document,
            *tuple(item for item in mount_documents if item.path != document.path),
        )
        return StructuralResolution(
            GroundingOutcome.NOT_FOUND,
            documents=documents,
            candidates=tuple(
                f"{item.source_path}:dynamic_prefix"
                for item in chain
                if item.prefix is None
            ),
        )
    if route.local_router_prefix is None:
        documents = (
            document,
            *tuple(item for item in mount_documents if item.path != document.path),
        )
        return StructuralResolution(
            GroundingOutcome.NOT_FOUND,
            documents=documents,
            candidates=(f"{document.path}:dynamic_local_router_prefix",),
        )
    prefixes = [item.prefix for item in chain]
    effective_path = _join_route_paths(route.local_router_prefix, route.local_path)
    for prefix in reversed(prefixes):
        effective_path = _join_route_paths(prefix, effective_path)
    mount_chain = tuple(f"{item.source_path}:{item.prefix}" for item in chain)
    identity = _identity_for_region(
        "mounted_route",
        document,
        route.region,
        route=route,
        effective_route_path=effective_path,
        mount_chain=mount_chain,
    )
    documents = (
        document,
        *tuple(item for item in mount_documents if item.path != document.path),
    )
    return StructuralResolution(
        GroundingOutcome.FOUND, identity=identity, documents=documents
    )


def bounded_region(
    document: SourceDocument, identity: StructuralIdentity
) -> tuple[bytes, bool]:
    content = document.raw[identity.start_byte : identity.end_byte]
    truncated = len(content) > MAX_STRUCTURAL_REGION_BYTES
    return content[:MAX_STRUCTURAL_REGION_BYTES], truncated


def extract_structural_facts(path: str, raw: bytes) -> dict[str, object]:
    """Expose only generic AST facts directly present in one requested file."""

    tree, offsets = _parse(path, raw)
    router_prefixes = _local_router_prefixes(tree)
    imports: list[str] = []
    symbols: list[dict[str, object]] = []
    routes: list[dict[str, object]] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(ast.unparse(node))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            region = _node_region(node, raw, offsets)
            symbols.append(
                {
                    "name": region.name,
                    "kind": region.kind,
                    "start_line": region.start_line,
                    "end_line": region.end_line,
                }
            )
            route = (
                _route_from_node(node, region, router_prefixes)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                else None
            )
            if route is not None:
                routes.append(
                    {
                        "method": route.method,
                        "path": route.local_path,
                        "router_prefix": route.local_router_prefix,
                        "handler": route.handler_name,
                        "line": region.start_line,
                    }
                )
    return {
        "parse_status": "ok",
        "imports": tuple(imports),
        "top_level_symbols": tuple(symbols),
        "route_decorators": tuple(routes),
    }
