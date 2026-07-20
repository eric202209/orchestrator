"""Deterministic structural facts for the bounded Engineering Context slice.

The extractor is intentionally syntactic.  It records facts visible in the
captured source bytes (Python definitions, imports, calls, route decorators,
and test symbols); it does not interpret behavior or generate summaries.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


STRUCTURAL_SCHEMA_VERSION = 1
STRUCTURAL_ALGORITHM_VERSION = 1
_SECURITY_TERMS = {
    "access",
    "auth",
    "authenticate",
    "authentication",
    "credential",
    "current_user",
    "permission",
    "security",
    "token",
}
_ROUTE_METHODS = {
    "api_route",
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "trace",
    "websocket",
}
_FACT_KEYS = (
    "files",
    "entry_points",
    "exported_interfaces",
    "routing_relationships",
    "dependency_relationships",
    "call_relationships",
    "authentication_boundary_references",
    "test_ownership",
)


class StructuralInformationError(ValueError):
    """The deterministic structural representation cannot be generated."""


@dataclass(frozen=True)
class StructuralInformation:
    object_id: str
    repository_identity: str
    subsystem_id: str
    subsystem_version: int
    source_fingerprint: str
    scope: tuple[str, ...]
    per_file_hash: Mapping[str, str]
    facts: Mapping[str, Any]
    content_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STRUCTURAL_SCHEMA_VERSION,
            "algorithm_version": STRUCTURAL_ALGORITHM_VERSION,
            "object_id": self.object_id,
            "repository_identity": self.repository_identity,
            "subsystem_id": self.subsystem_id,
            "subsystem_version": self.subsystem_version,
            "source_fingerprint": self.source_fingerprint,
            "scope": list(self.scope),
            "per_file_hash": dict(self.per_file_hash),
            "facts": dict(self.facts),
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StructuralInformation":
        required = {
            "schema_version",
            "algorithm_version",
            "object_id",
            "repository_identity",
            "subsystem_id",
            "subsystem_version",
            "source_fingerprint",
            "scope",
            "per_file_hash",
            "facts",
            "content_hash",
        }
        missing = sorted(required - set(raw))
        if missing:
            raise StructuralInformationError(
                f"malformed_structural_information:missing:{','.join(missing)}"
            )
        if raw["schema_version"] != STRUCTURAL_SCHEMA_VERSION:
            raise StructuralInformationError("malformed_structural_information:schema")
        if raw["algorithm_version"] != STRUCTURAL_ALGORITHM_VERSION:
            raise StructuralInformationError(
                "malformed_structural_information:algorithm"
            )

        scope = _validate_scope(raw["scope"])
        per_file_hash = raw["per_file_hash"]
        if not isinstance(per_file_hash, Mapping) or set(per_file_hash) != set(scope):
            raise StructuralInformationError(
                "malformed_structural_information:scope_hash_mismatch"
            )
        hashes = {str(path): str(value) for path, value in per_file_hash.items()}
        if any(not _is_sha256(value) for value in hashes.values()):
            raise StructuralInformationError("malformed_structural_information:hash")

        facts = raw["facts"]
        if (
            not isinstance(facts, Mapping)
            or set(facts) != set(_FACT_KEYS)
            or any(not isinstance(facts.get(key), list) for key in _FACT_KEYS)
        ):
            raise StructuralInformationError("malformed_structural_information:facts")

        normalized = {
            "schema_version": STRUCTURAL_SCHEMA_VERSION,
            "algorithm_version": STRUCTURAL_ALGORITHM_VERSION,
            "object_id": str(raw["object_id"]),
            "repository_identity": str(raw["repository_identity"]),
            "subsystem_id": str(raw["subsystem_id"]),
            "subsystem_version": raw["subsystem_version"],
            "source_fingerprint": str(raw["source_fingerprint"]),
            "scope": list(scope),
            "per_file_hash": hashes,
            "facts": {key: list(facts[key]) for key in _FACT_KEYS},
        }
        if _content_hash(normalized) != str(raw["content_hash"]):
            raise StructuralInformationError(
                "malformed_structural_information:content_hash"
            )
        version = normalized["subsystem_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise StructuralInformationError("malformed_structural_information:version")
        return cls(
            object_id=normalized["object_id"],
            repository_identity=normalized["repository_identity"],
            subsystem_id=normalized["subsystem_id"],
            subsystem_version=version,
            source_fingerprint=normalized["source_fingerprint"],
            scope=scope,
            per_file_hash=hashes,
            facts=normalized["facts"],
            content_hash=str(raw["content_hash"]),
        )

    def is_fresh(
        self,
        *,
        object_id: str,
        repository_identity: str,
        subsystem_id: str,
        subsystem_version: int,
        source_fingerprint: str,
        scope: Sequence[str],
        per_file_hash: Mapping[str, str],
    ) -> bool:
        return (
            self.object_id == object_id
            and self.repository_identity == repository_identity
            and self.subsystem_id == subsystem_id
            and self.subsystem_version == subsystem_version
            and self.source_fingerprint == source_fingerprint
            and self.scope == tuple(scope)
            and dict(self.per_file_hash) == dict(per_file_hash)
        )


def build_structural_information(
    *,
    object_id: str,
    repository_identity: str,
    subsystem_id: str,
    subsystem_version: int,
    source_fingerprint: str,
    scope: Sequence[str],
    source_bytes: Mapping[str, bytes],
    per_file_hash: Mapping[str, str],
) -> StructuralInformation:
    """Build facts from the exact bytes already captured for raw context."""

    normalized_scope = tuple(sorted(str(path) for path in scope))
    if set(source_bytes) != set(normalized_scope) or set(per_file_hash) != set(
        normalized_scope
    ):
        raise StructuralInformationError("source_scope_mismatch")
    for path in normalized_scope:
        if _sha256(source_bytes[path]) != str(per_file_hash[path]):
            raise StructuralInformationError(f"source_hash_mismatch:{path}")

    visitors: list[_FileFacts] = []
    for relative_path in normalized_scope:
        if not relative_path.endswith(".py"):
            continue
        try:
            text = source_bytes[relative_path].decode("utf-8")
            tree = ast.parse(text, filename=relative_path, type_comments=True)
        except (UnicodeDecodeError, SyntaxError) as exc:
            line = getattr(exc, "lineno", 0) or 0
            column = getattr(exc, "offset", 0) or 0
            raise StructuralInformationError(
                f"python_parse_error:{relative_path}:{line}:{column}"
            ) from exc
        visitor = _FileFacts(relative_path)
        visitor.visit(tree)
        visitors.append(visitor)

    interfaces = [item for visitor in visitors for item in visitor.interfaces]
    imports = [item for visitor in visitors for item in visitor.imports]
    calls = [item for visitor in visitors for item in visitor.calls]
    routes = [item for visitor in visitors for item in visitor.routes]
    tests = [item for visitor in visitors for item in visitor.tests]

    module_to_file = {
        _module_name(path): path for path in normalized_scope if path.endswith(".py")
    }
    dependency_relationships = []
    for item in imports:
        module = item["module"]
        target = module_to_file.get(module)
        if target is None:
            for imported_name in item["names"]:
                target = module_to_file.get(f"{module}.{imported_name['name']}")
                if target is not None:
                    break
        if target is not None:
            dependency_relationships.append(
                {
                    "from_file": item["file"],
                    "to_file": target,
                    "imported_names": item["names"],
                    "line": item["line"],
                }
            )

    security_references = []
    for item in interfaces:
        is_test_symbol = Path(item["file"]).name.startswith("test_") and item[
            "name"
        ].startswith("test_")
        if _security_related(item["name"]) and not is_test_symbol:
            security_references.append(
                {
                    "file": item["file"],
                    "kind": "definition",
                    "owner": "<module>",
                    "reference": item["name"],
                    "line": item["line"],
                }
            )
    for item in imports:
        reference = ".".join(
            [item["module"]] + [name["name"] for name in item["names"]]
        )
        if _security_related(reference):
            security_references.append(
                {
                    "file": item["file"],
                    "kind": "import",
                    "owner": "<module>",
                    "reference": reference,
                    "line": item["line"],
                }
            )
    for item in calls:
        if _security_related(item["callee"]):
            security_references.append(
                {
                    "file": item["file"],
                    "kind": "call",
                    "owner": item["caller"],
                    "reference": item["callee"],
                    "line": item["line"],
                }
            )

    facts = {
        "files": [
            {
                "path": path,
                "language": "python" if path.endswith(".py") else "unknown",
                "bytes": len(source_bytes[path]),
                "sha256": str(per_file_hash[path]),
            }
            for path in normalized_scope
        ],
        "entry_points": sorted((item["entry_point"] for item in routes), key=_sort_key),
        "exported_interfaces": sorted(interfaces, key=_sort_key),
        "routing_relationships": sorted(routes, key=_sort_key),
        "dependency_relationships": sorted(dependency_relationships, key=_sort_key),
        "call_relationships": sorted(calls, key=_sort_key),
        "authentication_boundary_references": sorted(
            security_references, key=_sort_key
        ),
        "test_ownership": sorted(tests, key=_sort_key),
    }
    base = {
        "schema_version": STRUCTURAL_SCHEMA_VERSION,
        "algorithm_version": STRUCTURAL_ALGORITHM_VERSION,
        "object_id": object_id,
        "repository_identity": repository_identity,
        "subsystem_id": subsystem_id,
        "subsystem_version": subsystem_version,
        "source_fingerprint": source_fingerprint,
        "scope": list(normalized_scope),
        "per_file_hash": {path: str(per_file_hash[path]) for path in normalized_scope},
        "facts": facts,
    }
    return StructuralInformation(
        **_constructor_arguments(base), content_hash=_content_hash(base)
    )


class _FileFacts(ast.NodeVisitor):
    def __init__(self, relative_path: str):
        self.relative_path = relative_path
        self.owner_stack: list[str] = []
        self.interfaces: list[dict[str, Any]] = []
        self.imports: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.routes: list[dict[str, Any]] = []
        self.tests: list[dict[str, Any]] = []

    @property
    def owner(self) -> str:
        return ".".join(self.owner_stack) if self.owner_stack else "<module>"

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.append(
            {
                "file": self.relative_path,
                "kind": "import",
                "module": "<import>",
                "names": [
                    {"name": alias.name, "alias": alias.asname} for alias in node.names
                ],
                "line": node.lineno,
            }
        )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * node.level + (node.module or "")
        self.imports.append(
            {
                "file": self.relative_path,
                "kind": "from",
                "module": module,
                "names": [
                    {"name": alias.name, "alias": alias.asname} for alias in node.names
                ],
                "line": node.lineno,
            }
        )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if not self.owner_stack:
            self._record_interface(node, "class")
        self.owner_stack.append(node.name)
        self.generic_visit(node)
        self.owner_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, "async_function")

    def visit_Call(self, node: ast.Call) -> None:
        callee = _dotted_name(node.func)
        if callee:
            self.calls.append(
                {
                    "file": self.relative_path,
                    "caller": self.owner,
                    "callee": callee,
                    "line": node.lineno,
                }
            )
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str):
        if not self.owner_stack:
            self._record_interface(node, kind)
            if Path(self.relative_path).name.startswith(
                "test_"
            ) and node.name.startswith("test_"):
                self.tests.append(
                    {
                        "test_file": self.relative_path,
                        "test_symbol": node.name,
                        "line": node.lineno,
                    }
                )
        for decorator in node.decorator_list:
            route = _route_fact(self.relative_path, node, decorator)
            if route is not None:
                self.routes.append(route)
        self.owner_stack.append(node.name)
        self.generic_visit(node)
        self.owner_stack.pop()

    def _record_interface(self, node: ast.AST, kind: str) -> None:
        name = getattr(node, "name", "")
        self.interfaces.append(
            {
                "file": self.relative_path,
                "name": name,
                "kind": kind,
                "public": not name.startswith("_"),
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
            }
        )


def _route_fact(
    relative_path: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    decorator: ast.expr,
) -> dict[str, Any] | None:
    if not isinstance(decorator, ast.Call):
        return None
    decorator_name = _dotted_name(decorator.func)
    if (
        not decorator_name
        or decorator_name.rsplit(".", 1)[-1].lower() not in _ROUTE_METHODS
    ):
        return None
    method_name = decorator_name.rsplit(".", 1)[-1].lower()
    path = None
    if decorator.args:
        path = _literal_string(decorator.args[0])
    for keyword in decorator.keywords:
        if keyword.arg == "path":
            path = _literal_string(keyword.value)
    entry_point = {
        "file": relative_path,
        "symbol": node.name,
        "kind": (
            "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
        ),
        "line": node.lineno,
    }
    return {
        "file": relative_path,
        "entry_point": entry_point,
        "decorator": decorator_name,
        "method": method_name.upper(),
        "path": path,
        "line": decorator.lineno,
    }


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _literal_string(node: ast.AST) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def _module_name(relative_path: str) -> str:
    path = relative_path[:-3].replace("/", ".")
    return path[:-9] if path.endswith(".__init__") else path


def _security_related(value: str) -> bool:
    tokens = set(re.findall(r"[a-z0-9_]+", value.casefold()))
    tokens.update(part for token in tuple(tokens) for part in token.split("_"))
    return bool(tokens & _SECURITY_TERMS)


def _sort_key(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _constructor_arguments(base: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "object_id": base["object_id"],
        "repository_identity": base["repository_identity"],
        "subsystem_id": base["subsystem_id"],
        "subsystem_version": base["subsystem_version"],
        "source_fingerprint": base["source_fingerprint"],
        "scope": tuple(base["scope"]),
        "per_file_hash": dict(base["per_file_hash"]),
        "facts": dict(base["facts"]),
    }


def _content_hash(base: Mapping[str, Any]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(base, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def _validate_scope(raw_scope: Any) -> tuple[str, ...]:
    if not isinstance(raw_scope, (list, tuple)) or not raw_scope:
        raise StructuralInformationError("malformed_structural_information:scope")
    values = tuple(str(path) for path in raw_scope)
    if values != tuple(sorted(values)) or len(set(values)) != len(values):
        raise StructuralInformationError("malformed_structural_information:scope_order")
    if any(
        not value
        or Path(value).is_absolute()
        or ".." in Path(value).parts
        or value.startswith("./")
        for value in values
    ):
        raise StructuralInformationError("malformed_structural_information:scope_path")
    return values


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
