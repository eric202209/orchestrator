"""Finite AST-only source gate. No application imports or production authority.

The caller selects and pins an external policy. Validation of its body is not
issuer authentication. Run this tool from outside the candidate source tree.
Ordinary imported bodies are hashed, never recursively analysed or executed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA = "orchestrator.worker-root-policy.v2"
POLICY_ID = "phase37-worker-safety-roots.1"
CANONICALIZATION = "orchestrator.canonical-json.v1"
RESOLVER_VERSION = "finite-ast.2"
LAZY_LOADER_GRAMMAR = "getattr-export-map-importlib-attribute-cache.1"
LAZY_CLASSIFICATION = "DORMANT_UNLOADED_LAZY_SITE"
COMPATIBILITY_DIGEST = (
    "sha256:"
    + hashlib.sha256(
        (
            "finite-ast.2:explicit-roots:direct-edges:initializer-fixed-point:"
            "fail-closed:lazy-dormant-v1:export-map-immutable:package-alias-v1"
        ).encode()
    ).hexdigest()
)
# Independently reviewed contract constants, NOT imported from candidate code.
ROOT_GROUPS = {
    "registration": (
        "app.celery_app",
        "app.tasks.worker",
        "app.tasks.maintenance",
        "app.tasks.github_tasks",
        "app.tasks.planning_tasks",
    ),
    "helpers": (
        "app.tasks.worker_support.worker_helpers",
        "app.tasks.worker_support.checkpoint",
        "app.tasks.worker_support.common",
        "app.tasks.worker_support.context",
        "app.tasks.worker_support.dispatch",
        "app.tasks.worker_support.execution_state",
        "app.tasks.worker_support.workspace",
        "app.services.orchestration",
    ),
    "s1": (
        "app.services.session.session_lifecycle_service",
        "app.services.session.orphan_ownership",
        "app.services.session.recovery_coordinator",
        "app.services.agents.backend_concurrency",
        "app.services.agents.backend_slot_reconciliation",
        "app.services.observability.maintenance_observability",
        "app.services.orchestration.execution.binding_reconciliation",
        "app.services.orchestration.lifecycle.continuation_recovery",
        "app.services.orchestration.lifecycle.worker_bootstrap",
        "app.services.orchestration.lifecycle.worker_capacity",
    ),
    "d37": (
        "app.services.agents.agent_runtime",
        "app.services.agents.openclaw_service",
        "app.services.agents.runtime_adapters.openclaw_adapter",
        "app.services.agents.runtime_configuration",
        "app.services.agents.runtime_invocation",
        "app.services.agents.subprocess_lifecycle",
        "app.services.orchestration.execution.executor_workspace_binding",
        "app.services.execution.execution_task_runtime_ownership_service",
    ),
}
LIMITS = {
    "explicit_roots": 256,
    "derived_identities": 4096,
    "initializer_parses": 1024,
    "dotted_components": 64,
    "source_bytes": 4 * 1024 * 1024,
    "total_parsed_bytes": 64 * 1024 * 1024,
}
NAMESPACE_LOCATIONS = {
    "app": ["app"],
    "app.services.agents.runtime_adapters": ["app/services/agents/runtime_adapters"],
}
REGISTRATIONS = tuple(sorted(ROOT_GROUPS["registration"][1:]))
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class PolicyError(ValueError):
    """Invalid policy or caller selection; not a production approval verdict."""


class SourceError(ValueError):
    def __init__(self, reason: str, *, uncertain: bool = False):
        super().__init__(reason)
        self.uncertain = uncertain


def canonical(value: Any) -> bytes:
    """Restricted, deterministic canonical JSON; never silently normalize data."""

    def check(item: Any) -> None:
        if item is None or type(item) in (str, int, bool):
            return
        if isinstance(item, list):
            for child in item:
                check(child)
            return
        if isinstance(item, dict) and all(type(k) is str for k in item):
            for child in item.values():
                check(child)
            return
        raise PolicyError("NON_CANONICAL_TYPE")

    check(value)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def body_digest(body: dict) -> str:
    return digest(SCHEMA.encode() + b"\0" + canonical(body))


def lazy_export_map_entries(
    mapping: dict[str, tuple[str, str]]
) -> list[dict[str, str]]:
    """Return the only canonical representation of a reviewed export map."""

    return [
        {"name": name, "module": value[0], "attribute": value[1]}
        for name, value in sorted(mapping.items())
    ]


def lazy_export_map_digest(entries: list[dict[str, str]]) -> str:
    return digest(b"orchestrator.lazy-export-map.v1\0" + canonical(entries))


def _keys(value: Any, keys: set[str], reason: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise PolicyError(reason)


def _pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"), object_pairs_hook=_pairs)


def _module(name: Any) -> str:
    if (
        type(name) is not str
        or not name
        or not all(p.isidentifier() for p in name.split("."))
    ):
        raise PolicyError("INVALID_MODULE_IDENTITY")
    return name


def _path(value: Any) -> str:
    if (
        type(value) is not str
        or not value
        or "\\" in value
        or "\0" in value
        or unicodedata.normalize("NFC", value) != value
        or any(p in ("", ".", "..") for p in value.split("/"))
        or value.startswith("/")
    ):
        raise PolicyError("INVALID_RELATIVE_PATH")
    return value


def root_path(module: str) -> str:
    suffix = "/__init__.py" if module == "app.services.orchestration" else ".py"
    return module.replace(".", "/") + suffix


@dataclass(frozen=True)
class Policy:
    """Immutable validated bytes, with authorization deliberately separate."""

    body_json: bytes
    digest: str
    selected_path: str
    authorization_status: str = "TEST_ONLY_UNAUTHENTICATED"

    @property
    def body(self) -> dict:
        return json.loads(self.body_json)


def load_policy(path: Path, *, candidate_root: Path, expected_digest: str) -> Policy:
    root = candidate_root.resolve(strict=True)
    selected = path.resolve(strict=True)
    if path.absolute().is_relative_to(root) or selected.is_relative_to(root):
        raise PolicyError("CANDIDATE_POLICY_NOT_AUTHORITATIVE")
    if Path(__file__).resolve().is_relative_to(root):
        raise PolicyError("VERIFIER_INSIDE_CANDIDATE")
    if not DIGEST_RE.fullmatch(expected_digest):
        raise PolicyError("CALLER_POLICY_PIN_REQUIRED")
    if selected.stat().st_size > 1024 * 1024:
        raise PolicyError("POLICY_TOO_LARGE")
    envelope = read_json(selected)
    _keys(envelope, {"body", "digest", "issuer_envelope"}, "POLICY_ENVELOPE_FIELDS")
    body = envelope["body"]
    _keys(
        body,
        {
            "schema",
            "policy_id",
            "revision",
            "issuer_id",
            "review_record_id",
            "source_namespace",
            "roots",
            "namespaces",
            "lazy_imports",
            "limits",
            "canonicalization_id",
            "resolver_version",
            "verifier_compatibility_digest",
        },
        "POLICY_BODY_FIELDS",
    )
    if (
        body["schema"] != SCHEMA
        or body["policy_id"] != POLICY_ID
        or type(body["revision"]) is not int
        or body["revision"] != 1
        or body["source_namespace"] != "app"
        or body["canonicalization_id"] != CANONICALIZATION
        or body["resolver_version"] != RESOLVER_VERSION
        or body["verifier_compatibility_digest"] != COMPATIBILITY_DIGEST
    ):
        raise PolicyError("POLICY_VERSION_COMPATIBILITY")
    for field in ("issuer_id", "review_record_id"):
        if type(body[field]) is not str or not body[field].strip():
            raise PolicyError("POLICY_REVIEW_METADATA")
    _keys(body["roots"], set(ROOT_GROUPS), "ROOT_GROUP_FIELDS")
    seen = set()
    for group, names in ROOT_GROUPS.items():
        records = body["roots"][group]
        if not isinstance(records, list):
            raise PolicyError("ROOT_GROUP_TYPE")
        wanted = sorted((name, root_path(name)) for name in names)
        actual = []
        for record in records:
            _keys(record, {"module", "path"}, "ROOT_RECORD_FIELDS")
            name = _module(record["module"])
            actual.append((name, _path(record["path"])))
            if name in seen:
                raise PolicyError("DUPLICATE_ROOT")
            seen.add(name)
        if sorted(actual) != wanted or actual != sorted(actual):
            raise PolicyError("ROOT_CONTRACT_MISMATCH")
    _keys(body["limits"], set(LIMITS), "LIMIT_FIELDS")
    for key, maximum in LIMITS.items():
        value = body["limits"][key]
        if type(value) is not int or not 0 < value <= maximum:
            raise PolicyError("UNSAFE_LIMIT")
    # Lower externally selected limits are allowed and fail inconclusive on use.
    namespaces = body["namespaces"]
    if not isinstance(namespaces, list):
        raise PolicyError("NAMESPACE_TYPE")
    ns = {}
    for record in namespaces:
        _keys(record, {"module", "locations"}, "NAMESPACE_FIELDS")
        name = _module(record["module"])
        locations = record["locations"]
        if name in ns or not isinstance(locations, list) or len(locations) != 1:
            raise PolicyError("NAMESPACE_AMBIGUOUS")
        ns[name] = [_path(locations[0])]
    if ns != NAMESPACE_LOCATIONS or list(ns) != sorted(ns):
        raise PolicyError("NAMESPACE_CONTRACT_MISMATCH")
    lazy = body["lazy_imports"]
    if not isinstance(lazy, list):
        raise PolicyError("LAZY_TYPE")
    sites = set()
    for item in lazy:
        _keys(
            item,
            {
                "module",
                "source_digest",
                "site_digest",
                "export_map_digest",
                "export_map",
                "review_record_id",
                "export_names",
                "loader_grammar",
                "verifier_compatibility_digest",
                "classification",
            },
            "LAZY_FIELDS",
        )
        name = _module(item["module"])
        if (
            name != "app.services.planning"
            or item["loader_grammar"] != LAZY_LOADER_GRAMMAR
            or item["verifier_compatibility_digest"] != COMPATIBILITY_DIGEST
            or item["classification"] != LAZY_CLASSIFICATION
        ):
            raise PolicyError("UNSUPPORTED_LAZY_DECLARATION")
        for key in ("source_digest", "site_digest"):
            if not isinstance(item[key], str) or not DIGEST_RE.fullmatch(item[key]):
                raise PolicyError("LAZY_IDENTITY_REQUIRED")
        exports = item["export_names"]
        if (
            not isinstance(exports, list)
            or not exports
            or not all(type(x) is str and x.isidentifier() for x in exports)
            or exports != sorted(set(exports))
            or not isinstance(item["review_record_id"], str)
            or not item["review_record_id"].strip()
        ):
            raise PolicyError("LAZY_REVIEW_REQUIRED")
        export_map = item["export_map"]
        if (
            not isinstance(export_map, list)
            or not all(
                isinstance(value, dict) and "name" in value for value in export_map
            )
            or export_map != sorted(export_map, key=lambda value: value["name"])
        ):
            raise PolicyError("LAZY_EXPORT_MAP_REQUIRED")
        names = []
        for entry in export_map:
            _keys(entry, {"name", "module", "attribute"}, "LAZY_EXPORT_FIELDS")
            entry_name = entry["name"]
            if (
                type(entry_name) is not str
                or not entry_name.isidentifier()
                or entry_name in names
                or _module(entry["module"]) != entry["module"]
                or type(entry["attribute"]) is not str
                or not entry["attribute"].isidentifier()
            ):
                raise PolicyError("LAZY_EXPORT_MAP_REQUIRED")
            names.append(entry_name)
        if names != exports or not DIGEST_RE.fullmatch(item["export_map_digest"]):
            raise PolicyError("LAZY_EXPORT_MAP_IDENTITY")
        if lazy_export_map_digest(export_map) != item["export_map_digest"]:
            raise PolicyError("LAZY_EXPORT_MAP_IDENTITY")
        if name in sites:
            raise PolicyError("DUPLICATE_LAZY_SITE")
        sites.add(name)
    # Envelope retained only as unauthenticated metadata. Never infer authorization.
    _keys(envelope["issuer_envelope"], {"kind"}, "ISSUER_ENVELOPE_FIELDS")
    if envelope["issuer_envelope"]["kind"] != "isolated-test-fixture":
        raise PolicyError("PRODUCTION_ISSUANCE_NOT_IMPLEMENTED")
    actual_digest = body_digest(body)
    if actual_digest != envelope["digest"] or actual_digest != expected_digest:
        raise PolicyError("POLICY_DIGEST_MISMATCH")
    return Policy(canonical(body), actual_digest, str(selected))


@dataclass(frozen=True, order=True)
class Edge:
    source: str
    target: str
    kind: str
    line: int
    column: int


@dataclass(frozen=True)
class Derivation:
    root_status: str
    policy_digest: str
    source_root: str
    verifier_digest: str
    records: tuple[dict, ...]
    required_modules: tuple[str, ...]
    edges: tuple[Edge, ...]
    parsed_modules: tuple[str, ...]
    reasons: tuple[str, ...]
    diagnostics: tuple[str, ...]
    dormant_lazy_sites: tuple[dict, ...]
    parsed_bytes: int

    @property
    def required(self) -> tuple[str, ...]:
        return self.required_modules


@dataclass(frozen=True)
class Verification:
    root_status: str
    module_status: str
    reasons: tuple[str, ...]
    extra_records: tuple[str, ...]
    derivation: Derivation

    @property
    def finite_gate_passed(self) -> bool:
        return (
            self.root_status == "ROOT_VALID"
            and self.module_status == "MODULE_IDENTITY_VALID"
        )


class _Deriver:
    def __init__(self, root: Path, policy: Policy):
        self.root = root.resolve(strict=True)
        self.policy = policy
        self.body = policy.body
        self.limits = self.body["limits"]
        self.explicit = {
            item["module"]: item["path"]
            for group in self.body["roots"].values()
            for item in group
        }
        self.required: set[str] = set()
        self.records: dict[str, dict] = {}
        self.parsed: set[str] = set()
        self.edges: set[Edge] = set()
        self.invalid: set[str] = set()
        self.uncertain: set[str] = set()
        self.diagnostics: set[str] = set()
        self.parsed_bytes = 0
        self.initializers = 0
        self.namespace_policy = {
            n["module"]: n["locations"] for n in self.body["namespaces"]
        }
        self.lazy_access: set[str] = set()
        self.lazy_modules: set[str] = set()
        self.dormant_lazy_sites: dict[str, dict] = {}
        self.lazy_declarations = {
            item["module"]: item for item in self.body["lazy_imports"]
        }
        self.export_binding_id: int | None = None

    def contain(self, relative: str) -> Path:
        try:
            _path(relative)
            path = self.root / relative
            # Reject all source symlinks, including in-root aliases. No candidate
            # file may redirect this scanner to another file/credential/resource.
            current = self.root
            for part in relative.split("/"):
                current = current / part
                if current.is_symlink():
                    raise SourceError("SOURCE_SYMLINK:" + relative)
            if not path.resolve(strict=False).is_relative_to(self.root):
                raise SourceError("SOURCE_ROOT_ESCAPE:" + relative)
            return path
        except (OSError, RuntimeError, PolicyError) as exc:
            raise SourceError("SOURCE_PATH_INVALID:" + relative) from exc

    def resolve(self, name: str, *, optional: bool = False) -> tuple[Path, str] | None:
        try:
            _module(name)
        except PolicyError as exc:
            raise SourceError(
                "INVALID_IMPORT_TARGET:" + str(name), uncertain=True
            ) from exc
        if len(name.split(".")) > self.limits["dotted_components"]:
            raise SourceError("LIMIT_EXCEEDED:dotted_components", uncertain=True)
        stem = name.replace(".", "/")
        module = self.contain(stem + ".py")
        initializer = self.contain(stem + "/__init__.py")
        if module.exists() and initializer.exists():
            raise SourceError("MODULE_PACKAGE_COLLISION:" + name)
        if name in self.namespace_policy and (module.exists() or initializer.exists()):
            raise SourceError("NAMESPACE_INITIALIZER_DRIFT:" + name)
        for path, kind in ((initializer, "initializer"), (module, "module")):
            if path.exists():
                if not stat.S_ISREG(path.stat().st_mode):
                    raise SourceError("NONREGULAR_SOURCE:" + name)
                return path, kind
        directory = self.contain(stem)
        if name in self.namespace_policy and directory.is_dir():
            return directory, "namespace"
        if optional and not directory.exists():
            return None
        raise SourceError("MISSING_SOURCE_OR_UNAPPROVED_NAMESPACE:" + name)

    def require(
        self,
        name: str,
        source: str = "policy",
        node: ast.AST | None = None,
        kind: str = "root",
    ) -> None:
        if not (name == "app" or name.startswith("app.")):
            self.diagnostics.add("EXTERNAL_IMPORT:" + name)
            return
        _module(name)
        parts = name.split(".")
        if len(parts) > self.limits["dotted_components"]:
            raise SourceError("LIMIT_EXCEEDED:dotted_components", uncertain=True)
        additions = {".".join(parts[:i]) for i in range(1, len(parts) + 1)}
        if len(self.required | additions) > self.limits["derived_identities"]:
            raise SourceError("LIMIT_EXCEEDED:derived_identities", uncertain=True)
        self.required.update(additions)
        self.edges.add(
            Edge(
                source,
                name,
                kind,
                getattr(node, "lineno", 0),
                getattr(node, "col_offset", 0),
            )
        )
        for parent in sorted(additions - {name}):
            self.edges.add(Edge(name, parent, "parent", 0, 0))

    def read(self, path: Path) -> bytes:
        # Bounded descriptor read rejects devices/FIFOs and unexpected file races.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise SourceError(
                    "NONREGULAR_SOURCE:" + str(path.relative_to(self.root))
                )
            if before.st_size > self.limits["source_bytes"]:
                raise SourceError("LIMIT_EXCEEDED:source_bytes", uncertain=True)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(self.limits["source_bytes"] + 1)
            after = os.fstat(fd)
            if len(data) > self.limits["source_bytes"]:
                raise SourceError("LIMIT_EXCEEDED:source_bytes", uncertain=True)
            if (
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise SourceError("SOURCE_CHANGED_DURING_READ", uncertain=True)
            return data
        finally:
            os.close(fd)

    def relative(self, source: str, path: Path, value: str, level: int) -> str:
        package = source if path.name == "__init__.py" else source.rpartition(".")[0]
        parts = package.split(".")
        if level > len(parts):
            raise SourceError("RELATIVE_IMPORT_BEYOND_ROOT:" + source, uncertain=True)
        prefix = ".".join(parts[: len(parts) - level + 1])
        return prefix + ("." + value if value else "")

    def record_error(self, exc: SourceError) -> None:
        (self.uncertain if exc.uncertain else self.invalid).add(str(exc))

    def parse(self, name: str, path: Path, data: bytes) -> None:
        if self.parsed_bytes + len(data) > self.limits["total_parsed_bytes"]:
            raise SourceError("LIMIT_EXCEEDED:total_parsed_bytes", uncertain=True)
        if path.name == "__init__.py":
            if self.initializers >= self.limits["initializer_parses"]:
                raise SourceError("LIMIT_EXCEEDED:initializer_parses", uncertain=True)
            self.initializers += 1
        self.parsed_bytes += len(data)
        try:
            tree = ast.parse(data.decode("utf-8"), filename=str(path))
        except (SyntaxError, UnicodeError, ValueError, RecursionError) as exc:
            raise SourceError(
                "SOURCE_PARSE_UNAVAILABLE:" + name, uncertain=True
            ) from exc
        self.parsed.add(name)
        nodes = list(ast.walk(tree))
        aliases: dict[str, str] = {
            "__import__": "__import__",
            "import_module": "unknown",
        }
        package_aliases: dict[str, str] = {}
        bindings: dict[str, int] = {}
        allowed_package_stores: set[int] = set()
        for node in nodes:
            if isinstance(node, ast.Import):
                for item in node.names:
                    bound = item.asname or item.name.split(".")[0]
                    bindings[bound] = bindings.get(bound, 0) + 1
                    aliases[bound] = (
                        item.name if item.asname else item.name.split(".")[0]
                    )
                    if item.name.startswith("app"):
                        package_aliases[bound] = item.name if item.asname else bound
            elif isinstance(node, ast.ImportFrom):
                base = (
                    self.relative(name, path, node.module or "", node.level)
                    if node.level
                    else node.module or ""
                )
                for item in node.names:
                    bound = item.asname or item.name
                    bindings[bound] = bindings.get(bound, 0) + 1
                    aliases[bound] = base + "." + item.name
                    if base.startswith("app") and item.name != "*":
                        try:
                            resolved = self.resolve(
                                base + "." + item.name, optional=True
                            )
                        except SourceError:
                            resolved = None
                        if resolved is not None and resolved[1] in (
                            "initializer",
                            "namespace",
                        ):
                            package_aliases[bound] = base + "." + item.name
            elif isinstance(node, ast.Name) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                bindings[node.id] = bindings.get(node.id, 0) + 1
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                bindings[node.name] = bindings.get(node.name, 0) + 1
            elif isinstance(node, ast.arg):
                bindings[node.arg] = bindings.get(node.arg, 0) + 1
        for node in nodes:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if not (
                isinstance(value, ast.Call)
                and self.call_name(value.func, aliases) == "importlib.import_module"
                and len(value.args) == 1
                and isinstance(value.args[0], ast.Constant)
                and type(value.args[0].value) is str
            ):
                continue
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if not isinstance(target, ast.Name):
                continue
            try:
                resolved = self.resolve(value.args[0].value, optional=True)
            except SourceError:
                resolved = None
            if resolved is not None and resolved[1] in ("initializer", "namespace"):
                package_aliases[target.id] = value.args[0].value
                allowed_package_stores.add(id(target))
        for node in nodes:
            if isinstance(node, ast.arg) and node.arg in package_aliases:
                self.uncertain.add("PACKAGE_ALIAS_REBIND:" + name + ":" + node.arg)
        if name == "app.celery_app":
            declarations = []
            for call in nodes:
                if isinstance(call, ast.Call) and self.call_name(
                    call.func, aliases
                ) in ("celery.Celery", "Celery"):
                    value = next(
                        (k.value for k in call.keywords if k.arg == "include"), None
                    )
                    if not isinstance(value, (ast.List, ast.Tuple)) or not all(
                        isinstance(x, ast.Constant) and type(x.value) is str
                        for x in value.elts
                    ):
                        self.uncertain.add("REGISTRATION_DECLARATION_UNRESOLVED")
                    else:
                        declarations.append(tuple(sorted(x.value for x in value.elts)))
                        for element in value.elts:
                            self.require(element.value, name, element, "celery-include")
                elif (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "autodiscover_tasks"
                ):
                    value = call.args[0] if call.args else None
                    if not isinstance(value, (ast.List, ast.Tuple)) or not all(
                        isinstance(x, ast.Constant) and type(x.value) is str
                        for x in value.elts
                    ):
                        self.uncertain.add("AUTODISCOVERY_UNRESOLVED")
                    elif [x.value for x in value.elts] != ["app.tasks"]:
                        self.invalid.add("AUTODISCOVERY_CONTRACT_MISMATCH")
            if declarations != [REGISTRATIONS]:
                self.invalid.add("REGISTRATION_CONTRACT_MISMATCH")
        lazy_functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
        ]
        nested_lazy_functions = [
            node
            for node in nodes
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "__getattr__"
            and node not in lazy_functions
        ]
        parents = {
            id(child): parent
            for parent in nodes
            for child in ast.iter_child_nodes(parent)
        }
        if lazy_functions or nested_lazy_functions:
            self.lazy_modules.add(name)
        if nested_lazy_functions:
            self.uncertain.add("LAZY_FUNCTION_NOT_MODULE_LEVEL:" + name)
        reviewed_sites: set[str] = set()
        lazy_calls: set[int] = set()
        self.export_binding_id = None
        if len(lazy_functions) > 1:
            self.uncertain.add("LAZY_FUNCTION_COUNT:" + name)
        elif lazy_functions:
            function = lazy_functions[0]
            site = digest(ast.dump(function, include_attributes=True).encode())
            declaration = self.lazy_declarations.get(name)
            reviewed = self.reviewed_lazy_site(tree, function, declaration, data)
            if reviewed and not reviewed.get("identity_mismatch"):
                reviewed_sites.add(site)
                self.dormant_lazy_sites[name] = {
                    "module": name,
                    "source_digest": digest(data),
                    "site_digest": site,
                    "export_map_digest": reviewed["export_map_digest"],
                    "classification": LAZY_CLASSIFICATION,
                }
                self.diagnostics.add(LAZY_CLASSIFICATION + ":" + name)
                lazy_calls = {
                    id(node)
                    for node in ast.walk(function)
                    if isinstance(node, ast.Call)
                    and self.call_name(node.func, {"import_module": "import_module"})
                    == "import_module"
                }
            elif declaration is None:
                self.uncertain.add(
                    "LAZY_REVIEW_REQUIRED:" + name + ":" + str(function.lineno)
                )
            elif reviewed and reviewed.get("identity_mismatch"):
                self.uncertain.add("LAZY_POLICY_IDENTITY_MISMATCH:" + name)
            else:
                self.uncertain.add("LAZY_MECHANISM_UNSUPPORTED:" + name)
        elif name in self.lazy_declarations:
            self.uncertain.add("LAZY_DECLARATION_WITHOUT_SITE:" + name)
        for node in sorted(
            nodes,
            key=lambda n: (
                getattr(n, "lineno", 0),
                getattr(n, "col_offset", 0),
                type(n).__name__,
            ),
        ):
            try:
                if isinstance(node, (ast.Name, ast.Attribute)):
                    resolved_name = self.call_name(node, aliases)
                    parent = parents.get(id(node))
                    if resolved_name in ("importlib.import_module", "__import__"):
                        if not (isinstance(parent, ast.Call) and parent.func is node):
                            raise SourceError(
                                "DYNAMIC_ALIAS_ESCAPE:" + name + ":" + str(node.lineno),
                                uncertain=True,
                            )
                    if resolved_name in (
                        "sys.path",
                        "sys.meta_path",
                        "sys.path_hooks",
                    ) and isinstance(node.ctx, ast.Store):
                        raise SourceError(
                            "IMPORT_PATH_MUTATION:" + name, uncertain=True
                        )
                if isinstance(node, ast.Import):
                    for item in node.names:
                        self.require(item.name, name, node, "import")
                elif isinstance(node, ast.ImportFrom):
                    if any(item.name == "*" for item in node.names):
                        raise SourceError("WILDCARD_IMPORT:" + name, uncertain=True)
                    base = (
                        self.relative(name, path, node.module or "", node.level)
                        if node.level
                        else node.module or ""
                    )
                    self.require(base, name, node, "from")
                    if base == "app" or base.startswith("app."):
                        for item in node.names:
                            child = base + "." + item.name
                            if self.resolve(child, optional=True) is not None:
                                self.require(child, name, node, "from-submodule")
                            elif (
                                base in self.lazy_declarations
                                and item.name
                                in self.lazy_declarations[base]["export_names"]
                            ):
                                self.lazy_access.add(base)
                                self.diagnostics.add("LAZY_SYMBOL_IMPORT:" + child)
                            elif base in self.lazy_declarations:
                                self.uncertain.add("UNRESOLVED_FROM_IMPORT:" + child)
                            else:
                                self.diagnostics.add("SYMBOL_IMPORT:" + child)
                elif isinstance(node, ast.Call):
                    function = self.call_name(node.func, aliases)
                    if function in ("importlib.import_module", "__import__"):
                        bound = self.left_name(node.func)
                        if bindings.get(bound, 0) > (
                            0 if function == "__import__" else 1
                        ):
                            raise SourceError(
                                "AMBIGUOUS_DYNAMIC_ALIAS:" + name, uncertain=True
                            )
                        self.dynamic(name, path, node, lazy_calls, lazy_functions)
                    elif function in ("globals", "locals", "vars"):
                        if not self.is_exact_lazy_cache(node, parents):
                            raise SourceError(
                                "REFLECTION_UNSUPPORTED:"
                                + name
                                + ":"
                                + str(node.lineno),
                                uncertain=True,
                            )
                    elif function in ("getattr", "hasattr") and node.args:
                        package = self.lazy_package_name(node.args[0], package_aliases)
                        if package:
                            self.lazy_access.add(package)
                        elif function == "getattr" and self.call_name(
                            node.args[0], aliases
                        ).startswith("importlib"):
                            raise SourceError("COMPUTED_LOADER:" + name, uncertain=True)
                    elif (
                        function in ("eval", "exec")
                        or function.rsplit(".", 1)[-1]
                        in (
                            "import_module",
                            "__import__",
                            "exec_module",
                            "load_module",
                            "find_spec",
                            "spec_from_file_location",
                            "spec_from_loader",
                            "module_from_spec",
                            "SourceFileLoader",
                        )
                        or isinstance(node.func, ast.Name)
                        and node.func.id == "import_module"
                    ):
                        raise SourceError(
                            "UNKNOWN_LOADER:" + name + ":" + str(node.lineno),
                            uncertain=True,
                        )
                    elif isinstance(node.func, ast.Attribute) and node.func.attr in (
                        "append",
                        "extend",
                        "insert",
                    ):
                        if self.call_name(node.func.value, aliases) in (
                            "sys.path",
                            "sys.meta_path",
                            "sys.path_hooks",
                        ):
                            raise SourceError(
                                "IMPORT_PATH_MUTATION:" + name, uncertain=True
                            )
                if isinstance(node, ast.Attribute):
                    package = self.lazy_package_name(node.value, package_aliases)
                    full = self.call_name(node, package_aliases)
                    if full in self.lazy_declarations:
                        parent = parents.get(id(node))
                        if not (
                            isinstance(node.ctx, ast.Store)
                            or isinstance(parent, ast.Attribute)
                            and parent.value is node
                        ):
                            raise SourceError(
                                "PACKAGE_HANDLE_ESCAPE:"
                                + name
                                + ":"
                                + str(node.lineno),
                                uncertain=True,
                            )
                    if node.attr == "__dict__":
                        raise SourceError(
                            "REFLECTION_UNSUPPORTED:" + name + ":" + str(node.lineno),
                            uncertain=True,
                        )
                    if package:
                        parent = parents.get(id(node))
                        if node.attr == "__getattr__":
                            if isinstance(parent, ast.Call) and parent.func is node:
                                self.lazy_access.add(package)
                        else:
                            declaration = self.lazy_declarations.get(package)
                            suffix = full[len(package) + 1 :]
                            if declaration and suffix in declaration["export_names"]:
                                self.lazy_access.add(package)
                            elif suffix and not self.is_concrete_child(package, suffix):
                                raise SourceError(
                                    "UNRESOLVED_PACKAGE_ATTRIBUTE:"
                                    + name
                                    + ":"
                                    + str(node.lineno),
                                    uncertain=True,
                                )
                    if node.attr in (
                        "__path__",
                        "__loader__",
                        "__spec__",
                    ) and isinstance(node.ctx, ast.Store):
                        raise SourceError(
                            "IMPORT_PATH_MUTATION:" + name, uncertain=True
                        )
                if isinstance(node, ast.Name) and node.id in package_aliases:
                    parent = parents.get(id(node))
                    if not (isinstance(parent, ast.Attribute) and parent.value is node):
                        if isinstance(node.ctx, ast.Load):
                            raise SourceError(
                                "PACKAGE_HANDLE_ESCAPE:"
                                + name
                                + ":"
                                + str(node.lineno),
                                uncertain=True,
                            )
                if (
                    isinstance(node, ast.Name)
                    and self.call_name(node, package_aliases) in self.lazy_declarations
                ):
                    parent = parents.get(id(node))
                    if isinstance(node.ctx, ast.Load) and not (
                        isinstance(parent, ast.Attribute) and parent.value is node
                    ):
                        raise SourceError(
                            "PACKAGE_HANDLE_ESCAPE:" + name + ":" + str(node.lineno),
                            uncertain=True,
                        )
                if isinstance(node, ast.Name) and node.id == "_EXPORTS":
                    if not self.allowed_export_map_use(node, parents):
                        raise SourceError(
                            "EXPORT_MAP_ESCAPE_OR_MUTATION:"
                            + name
                            + ":"
                            + str(node.lineno),
                            uncertain=True,
                        )
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, (ast.Store, ast.Del))
                    and node.id in package_aliases
                    and id(node) not in allowed_package_stores
                ):
                    raise SourceError(
                        "PACKAGE_ALIAS_REBIND:" + name + ":" + str(node.lineno),
                        uncertain=True,
                    )
            except (SourceError, PolicyError) as exc:
                self.record_error(
                    exc
                    if isinstance(exc, SourceError)
                    else SourceError(str(exc), uncertain=True)
                )

    def lazy_package_name(
        self, node: ast.AST, package_aliases: dict[str, str]
    ) -> str | None:
        full = self.call_name(node, package_aliases)
        if full in self.lazy_declarations:
            return full
        return None

    def is_concrete_child(self, package: str, suffix: str) -> bool:
        try:
            return self.resolve(package + "." + suffix, optional=True) is not None
        except SourceError:
            return False

    @staticmethod
    def is_exact_lazy_cache(node: ast.Call, parents: dict[int, ast.AST]) -> bool:
        parent = parents.get(id(node))
        if not (
            isinstance(parent, ast.Subscript)
            and parent.value is node
            and isinstance(node.func, ast.Name)
            and node.func.id == "globals"
            and not node.args
            and not node.keywords
            and isinstance(parent.slice, ast.Name)
            and parent.slice.id == "name"
        ):
            return False
        assignment = parents.get(id(parent))
        return (
            isinstance(assignment, ast.Assign)
            and len(assignment.targets) == 1
            and assignment.targets[0] is parent
            and isinstance(assignment.value, ast.Name)
            and assignment.value.id == "value"
        )

    def allowed_export_map_use(
        self, node: ast.Name, parents: dict[int, ast.AST]
    ) -> bool:
        if id(node) == self.export_binding_id:
            return isinstance(node.ctx, ast.Store)
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            return False
        parent = parents.get(id(node))
        if (
            isinstance(parent, ast.Subscript)
            and parent.value is node
            and isinstance(parent.slice, ast.Name)
            and parent.slice.id == "name"
        ):
            return True
        if isinstance(parent, ast.Compare):
            return (
                isinstance(parent.left, ast.Name)
                and parent.left.id == "name"
                and len(parent.ops) == 1
                and isinstance(parent.ops[0], ast.NotIn)
            )
        if isinstance(parent, ast.Call) and node in parent.args:
            grandparent = parents.get(id(parent))
            return (
                isinstance(parent.func, ast.Name)
                and parent.func.id == "sorted"
                and not parent.args[1:]
                and not parent.keywords
                and isinstance(grandparent, ast.Assign)
                and len(grandparent.targets) == 1
                and isinstance(grandparent.targets[0], ast.Name)
                and grandparent.targets[0].id == "__all__"
            )
        return False

    def reviewed_lazy_site(
        self,
        tree: ast.Module,
        function: ast.FunctionDef,
        declaration: dict | None,
        data: bytes,
    ) -> dict | None:
        assignments = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_EXPORTS"
        ]
        if len(assignments) != 1:
            return None
        self.export_binding_id = id(assignments[0].targets[0])
        try:
            mapping = ast.literal_eval(assignments[0].value)
        except (ValueError, TypeError, SyntaxError, RecursionError):
            return None
        if not isinstance(mapping, dict) or not all(
            type(key) is str
            and key.isidentifier()
            and isinstance(value, tuple)
            and len(value) == 2
            and type(value[0]) is str
            and type(value[1]) is str
            for key, value in mapping.items()
        ):
            return None
        try:
            for module, attribute in mapping.values():
                _module(module)
                if not attribute.isidentifier():
                    return None
        except PolicyError:
            return None
        entries = lazy_export_map_entries(mapping)
        if declaration is None:
            return None
        if (
            declaration["source_digest"] != digest(data)
            or declaration["site_digest"]
            != digest(ast.dump(function, include_attributes=True).encode())
            or declaration["export_names"] != [entry["name"] for entry in entries]
            or declaration["export_map"] != entries
            or declaration["export_map_digest"] != lazy_export_map_digest(entries)
        ):
            return {"identity_mismatch": True}
        if not self.lazy_shape(tree, function):
            return None
        return {"export_map_digest": declaration["export_map_digest"]}

    @staticmethod
    def lazy_shape(tree: ast.Module, function: ast.FunctionDef) -> bool:
        """Recognize only the reviewed finite lazy-loader grammar."""

        args = function.args
        if (
            function.decorator_list
            or len(args.posonlyargs) + len(args.args) != 1
            or args.args[0].arg != "name"
            or args.vararg
            or args.kwarg
            or args.kwonlyargs
            or args.defaults
            or args.kw_defaults
        ):
            return False
        statements = list(function.body)
        if statements and _Deriver.lazy_guard(statements[0]):
            statements = statements[1:]
        if len(statements) not in (4, 5):
            return False
        map_binding, import_stmt, value_binding = statements[:3]
        if not (
            isinstance(map_binding, ast.Assign)
            and len(map_binding.targets) == 1
            and isinstance(map_binding.targets[0], ast.Tuple)
            and [getattr(item, "id", None) for item in map_binding.targets[0].elts]
            == ["module_name", "attr_name"]
            and isinstance(map_binding.value, ast.Subscript)
            and isinstance(map_binding.value.value, ast.Name)
            and map_binding.value.value.id == "_EXPORTS"
            and isinstance(map_binding.value.slice, ast.Name)
            and map_binding.value.slice.id == "name"
        ):
            return False
        if not (
            isinstance(import_stmt, ast.ImportFrom)
            and import_stmt.level == 0
            and import_stmt.module == "importlib"
            and len(import_stmt.names) == 1
            and import_stmt.names[0].name == "import_module"
            and import_stmt.names[0].asname is None
        ):
            return False
        if not (
            isinstance(value_binding, ast.Assign)
            and len(value_binding.targets) == 1
            and isinstance(value_binding.targets[0], ast.Name)
            and value_binding.targets[0].id == "value"
            and isinstance(value_binding.value, ast.Call)
            and isinstance(value_binding.value.func, ast.Name)
            and value_binding.value.func.id == "getattr"
            and len(value_binding.value.args) == 2
            and isinstance(value_binding.value.args[0], ast.Call)
            and isinstance(value_binding.value.args[0].func, ast.Name)
            and value_binding.value.args[0].func.id == "import_module"
            and len(value_binding.value.args[0].args) == 1
            and isinstance(value_binding.value.args[0].args[0], ast.Name)
            and value_binding.value.args[0].args[0].id == "module_name"
            and isinstance(value_binding.value.args[1], ast.Name)
            and value_binding.value.args[1].id == "attr_name"
            and not value_binding.value.keywords
        ):
            return False
        index = 3
        if len(statements) == 5:
            if not _Deriver.is_exact_lazy_cache_statement(statements[index]):
                return False
            index += 1
        return (
            index == len(statements) - 1
            and isinstance(statements[index], ast.Return)
            and isinstance(statements[index].value, ast.Name)
            and statements[index].value.id == "value"
        )

    @staticmethod
    def lazy_guard(statement: ast.stmt) -> bool:
        if not (
            isinstance(statement, ast.If)
            and not statement.orelse
            and len(statement.body) == 1
            and isinstance(statement.test, ast.Compare)
            and len(statement.test.ops) == 1
            and isinstance(statement.test.ops[0], ast.NotIn)
            and isinstance(statement.test.left, ast.Name)
            and statement.test.left.id == "name"
            and len(statement.test.comparators) == 1
            and isinstance(statement.test.comparators[0], ast.Name)
            and statement.test.comparators[0].id == "_EXPORTS"
        ):
            return False
        raise_stmt = statement.body[0]
        return (
            isinstance(raise_stmt, ast.Raise)
            and isinstance(raise_stmt.exc, ast.Call)
            and isinstance(raise_stmt.exc.func, ast.Name)
            and raise_stmt.exc.func.id == "AttributeError"
        )

    @staticmethod
    def is_exact_lazy_cache_statement(statement: ast.stmt) -> bool:
        return (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Subscript)
            and isinstance(statement.targets[0].value, ast.Call)
            and isinstance(statement.targets[0].value.func, ast.Name)
            and statement.targets[0].value.func.id == "globals"
            and not statement.targets[0].value.args
            and not statement.targets[0].value.keywords
            and isinstance(statement.targets[0].slice, ast.Name)
            and statement.targets[0].slice.id == "name"
            and isinstance(statement.value, ast.Name)
            and statement.value.id == "value"
        )

    @staticmethod
    def left_name(node: ast.AST) -> str:
        while isinstance(node, ast.Attribute):
            node = node.value
        return node.id if isinstance(node, ast.Name) else ""

    @staticmethod
    def call_name(node: ast.AST, aliases: dict[str, str]) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return _Deriver.call_name(node.value, aliases) + "." + node.attr
        return "<computed>"

    def dynamic(
        self,
        name: str,
        path: Path,
        node: ast.Call,
        lazy_calls: set[int],
        functions: list,
    ) -> None:
        if id(node) in lazy_calls:
            # The exact reviewed loader is a dormant diagnostic, never loaded
            # evidence. Its target comes from the independently reviewed map.
            self.diagnostics.add(LAZY_CLASSIFICATION + ":" + name)
            return
        argument = (
            node.args[0]
            if node.args
            else next((k.value for k in node.keywords if k.arg == "name"), None)
        )
        if not isinstance(argument, ast.Constant) or type(argument.value) is not str:
            raise SourceError(
                "UNRESOLVED_DYNAMIC_IMPORT:" + name + ":" + str(node.lineno),
                uncertain=True,
            )
        target = argument.value
        if target.startswith("."):
            package = (
                node.args[1]
                if len(node.args) > 1
                else next((k.value for k in node.keywords if k.arg == "package"), None)
            )
            if not isinstance(package, ast.Constant) or type(package.value) is not str:
                raise SourceError("DYNAMIC_PACKAGE_UNRESOLVED:" + name, uncertain=True)
            _module(package.value)
            level = len(target) - len(target.lstrip("."))
            parts = package.value.split(".")
            if level > len(parts):
                raise SourceError(
                    "DYNAMIC_RELATIVE_BEYOND_ROOT:" + name, uncertain=True
                )
            target = ".".join(parts[: len(parts) - level + 1]) + (
                "." + target.lstrip(".") if target.lstrip(".") else ""
            )
        # __import__ relative level/fromlist semantics not guessed.
        if self.call_name(node.func, {"__import__": "__import__"}) == "__import__":
            if len(node.args) > 1 or node.keywords:
                raise SourceError(
                    "UNSUPPORTED_IMPORT_ARGUMENTS:" + name, uncertain=True
                )
        # Literal package and concrete-child imports are import edges, not
        # attribute access. Package-handle use is analyzed separately.
        self.require(target, name, node, "literal-dynamic")

    def run(self) -> Derivation:
        if len(self.explicit) > self.limits["explicit_roots"]:
            self.uncertain.add("LIMIT_EXCEEDED:explicit_roots")
        else:
            for name in sorted(self.explicit):
                try:
                    self.require(name)
                except SourceError as exc:
                    self.record_error(exc)
        processed = set()
        while self.required - processed:
            name = min(self.required - processed)
            processed.add(name)
            try:
                resolved = self.resolve(name)
                if resolved is None:
                    raise SourceError("MISSING_SOURCE:" + name)
                path, kind = resolved
                relative = str(path.relative_to(self.root))
                if name in self.explicit and relative != self.explicit[name]:
                    raise SourceError("ROOT_PATH_MISMATCH:" + name)
                if kind == "namespace":
                    entries = []
                    for child in path.iterdir():
                        self.contain(str(child.relative_to(self.root)))
                        info = child.lstat()
                        entries.append([child.name, stat.S_IFMT(info.st_mode)])
                        if len(entries) > self.limits["derived_identities"]:
                            raise SourceError(
                                "LIMIT_EXCEEDED:namespace_entries", uncertain=True
                            )
                    self.records[name] = {
                        "module": name,
                        "path": relative,
                        "kind": kind,
                        "locations": [relative],
                        "hash": digest(canonical(sorted(entries))),
                    }
                else:
                    data = self.read(path)
                    self.records[name] = {
                        "module": name,
                        "path": relative,
                        "kind": kind,
                        "hash": digest(data),
                    }
                    if name in self.explicit or kind == "initializer":
                        self.parse(name, path, data)
            except SourceError as exc:
                self.record_error(exc)
            except (OSError, ValueError, RuntimeError, RecursionError) as exc:
                self.uncertain.add(
                    "SOURCE_UNAVAILABLE:" + name + ":" + type(exc).__name__
                )
        for name, record in self.records.items():
            if record["kind"] == "module" and any(
                child.startswith(name + ".") for child in self.required
            ):
                self.invalid.add("NONPACKAGE_PARENT:" + name)
        for name in sorted(self.lazy_modules & self.lazy_access):
            self.uncertain.add("WORKER_FACING_LAZY_ACCESS:" + name)
        status = (
            "ROOT_INVALID"
            if self.invalid
            else "ROOT_INCONCLUSIVE" if self.uncertain else "ROOT_VALID"
        )
        # Limits always mean incomplete proof even if a separate hard defect exists.
        if any(r.startswith("LIMIT_EXCEEDED") for r in self.uncertain):
            status = "ROOT_INCONCLUSIVE"
        return Derivation(
            status,
            self.policy.digest,
            str(self.root),
            digest(Path(__file__).read_bytes()),
            tuple(self.records[n] for n in sorted(self.records)),
            tuple(sorted(self.required)),
            tuple(sorted(self.edges)),
            tuple(sorted(self.parsed)),
            tuple(sorted(self.invalid | self.uncertain)),
            tuple(sorted(self.diagnostics)),
            tuple(self.dormant_lazy_sites[n] for n in sorted(self.dormant_lazy_sites)),
            self.parsed_bytes,
        )


def derive(candidate_root: Path, policy: Policy) -> Derivation:
    """Derive with no candidate manifest access; caller already selected policy."""
    if Path(policy.selected_path).resolve().is_relative_to(candidate_root.resolve()):
        raise PolicyError("CANDIDATE_POLICY_NOT_AUTHORITATIVE")
    if Path(__file__).resolve().is_relative_to(candidate_root.resolve()):
        raise PolicyError("VERIFIER_INSIDE_CANDIDATE")
    if body_digest(policy.body) != policy.digest:
        raise PolicyError("POLICY_DIGEST_MISMATCH")
    return _Deriver(candidate_root, policy).run()


def compare(derivation: Derivation, manifest: dict) -> Verification:
    """Extra records never enter derivation; unknown proof never passes."""
    root_status = derivation.root_status
    reasons = list(derivation.reasons)
    errors = []
    records = {}
    try:
        _keys(manifest, {"source_root", "policy_digest", "records"}, "MANIFEST_FIELDS")
        if manifest["source_root"] != derivation.source_root:
            errors.append("SOURCE_ROOT_MISMATCH")
        if manifest["policy_digest"] != derivation.policy_digest:
            errors.append("POLICY_DIGEST_MISMATCH")
        if not isinstance(manifest["records"], list):
            raise PolicyError("MANIFEST_RECORDS_TYPE")
        for record in manifest["records"]:
            if not isinstance(record, dict) or "module" not in record:
                raise PolicyError("MANIFEST_RECORD_FIELDS")
            name = _module(record["module"])
            if name in records:
                errors.append("DUPLICATE_IDENTITY:" + name)
            records[name] = record
        required = set(derivation.required)
        missing = required - records.keys()
        if missing:
            root_status = "ROOT_INVALID"
            errors.extend(
                "MISSING_REQUIRED_IDENTITY:" + name for name in sorted(missing)
            )
        for expected in derivation.records:
            actual = records.get(expected["module"])
            if actual is not None and actual != expected:
                errors.append("MODULE_RECORD_MISMATCH:" + expected["module"])
    except (PolicyError, TypeError, ValueError) as exc:
        errors.append("MANIFEST_INVALID:" + str(exc))
    if errors:
        module_status = "MODULE_IDENTITY_INVALID"
    elif derivation.root_status != "ROOT_VALID":
        module_status = "MODULE_IDENTITY_INCONCLUSIVE"
    else:
        module_status = "MODULE_IDENTITY_VALID"
    if any("POLICY_DIGEST_MISMATCH" in reason for reason in errors):
        root_status = "ROOT_INVALID"
    if any(r.startswith("LIMIT_EXCEEDED") for r in derivation.reasons):
        root_status = "ROOT_INCONCLUSIVE"
    return Verification(
        root_status,
        module_status,
        tuple(sorted(set(reasons + errors))),
        tuple(sorted(records.keys() - set(derivation.required))),
        derivation,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--trusted-policy", type=Path, required=True)
    parser.add_argument(
        "--policy-digest", required=True, help="independently selected caller pin"
    )
    parser.add_argument(
        "--manifest", type=Path, help="candidate evidence, read AFTER derivation"
    )
    args = parser.parse_args()
    try:
        policy = load_policy(
            args.trusted_policy,
            candidate_root=args.candidate_root,
            expected_digest=args.policy_digest,
        )
        result = derive(args.candidate_root, policy)
        verification = (
            compare(result, read_json(args.manifest)) if args.manifest else None
        )
        output = asdict(verification) if verification else asdict(result)
        output["finite_gate_passed"] = bool(
            verification and verification.finite_gate_passed
        )
        if verification is None:
            output["module_status"] = "MODULE_IDENTITY_INCONCLUSIVE"
        output["policy_authorization"] = policy.authorization_status
        output["production_acceptance"] = False
        print(json.dumps(output, sort_keys=True))
        return 0 if (verification.finite_gate_passed if verification else False) else 2
    except (PolicyError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "root_status": "ROOT_INCONCLUSIVE",
                    "reason": type(exc).__name__ + ":" + str(exc),
                    "production_acceptance": False,
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
