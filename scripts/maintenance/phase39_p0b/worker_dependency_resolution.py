"""Offline Worker import inventory and deterministic resolution-input helpers.

This module reads source text and filesystem metadata only. It never imports
Orchestrator modules, starts Celery, resolves distributions, or accesses a
package index. Static analysis is intentionally conservative: dynamic imports
without literal or closed-constant targets are reported unresolved.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from scripts.maintenance.phase39_p0b.runtime_lock_contract import evaluate_marker


TARGET_PLATFORM: dict[str, str] = {
    "distribution": "Ubuntu",
    "release": "24.04",
    "architecture": "aarch64",
    "libc_name": "glibc",
    "libc_baseline": "2.39",
}
TARGET_PYTHON: dict[str, str] = {
    "implementation": "CPython",
    "version": "3.12",
    "abi": "cp312",
    "soabi": "cpython-312-aarch64-linux-gnu",
}
TARGET_MARKERS: dict[str, str] = {
    "implementation_name": "cpython",
    "os_name": "posix",
    "platform_machine": "aarch64",
    "platform_python_implementation": "CPython",
    "platform_system": "Linux",
    "python_version": "3.12",
    "sys_platform": "linux",
}

IMPORT_TO_DISTRIBUTION: dict[str, str] = {
    "bcrypt": "bcrypt",
    "celery": "celery",
    "fastapi": "fastapi",
    "httpx": "httpx",
    "jose": "python-jose",
    "jsonschema": "jsonschema",
    "langfuse": "langfuse",
    "nacl": "pynacl",
    "openai": "openai",
    "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings",
    "qdrant_client": "qdrant-client",
    "redis": "redis",
    "sqlalchemy": "sqlalchemy",
    "typing_extensions": "typing-extensions",
    "tzdata": "tzdata",
    "yaml": "pyyaml",
}

_REQUIREMENT_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[(?P<extras>[A-Za-z0-9][A-Za-z0-9._-]*(?:\s*,\s*"
    r"[A-Za-z0-9][A-Za-z0-9._-]*)*)\])?"
    r"(?P<constraints>[^;@]*)"
    r"(?:;\s*(?P<marker>.+))?$"
)


class WorkerDependencyInventoryError(ValueError):
    """Static inventory input is ambiguous or cannot be classified safely."""


@dataclass(frozen=True)
class WorkerRoots:
    celery_application: str
    explicit_task_modules: tuple[str, ...]
    autodiscovery_candidates: tuple[str, ...]
    existing_autodiscovery_modules: tuple[str, ...]

    @property
    def graph_roots(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    self.celery_application,
                    *self.explicit_task_modules,
                    *self.existing_autodiscovery_modules,
                )
            )
        )


@dataclass(frozen=True)
class ImportSite:
    module: str
    path: str
    line: int
    optional_guard: bool
    deferred_to_runtime: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "path": self.path,
            "line": self.line,
            "optional_guard": self.optional_guard,
            "deferred_to_runtime": self.deferred_to_runtime,
        }


@dataclass(frozen=True)
class DynamicImportSite:
    path: str
    line: int
    expression: str
    static_targets: tuple[str, ...]

    @property
    def classification(self) -> str:
        return "STATIC_TARGETS" if self.static_targets else "UNRESOLVED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "expression": self.expression,
            "static_targets": list(self.static_targets),
            "classification": self.classification,
        }


@dataclass(frozen=True)
class ExternalCommandSite:
    path: str
    line: int
    call: str
    literal_executable: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "call": self.call,
            "literal_executable": self.literal_executable,
        }


@dataclass(frozen=True)
class ImportGraph:
    roots: WorkerRoots
    local_modules: tuple[str, ...]
    local_edges: Mapping[str, tuple[str, ...]]
    third_party_imports: Mapping[str, tuple[ImportSite, ...]]
    standard_library_imports: Mapping[str, tuple[ImportSite, ...]]
    dynamic_imports: tuple[DynamicImportSite, ...]
    external_commands: tuple[ExternalCommandSite, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "phase39-p0b-worker-import-graph",
            "schema_version": 1,
            "celery_application": self.roots.celery_application,
            "explicit_task_modules": list(self.roots.explicit_task_modules),
            "autodiscovery_candidates": list(self.roots.autodiscovery_candidates),
            "existing_autodiscovery_modules": list(
                self.roots.existing_autodiscovery_modules
            ),
            "local_modules": list(self.local_modules),
            "local_edges": {
                key: list(self.local_edges[key]) for key in sorted(self.local_edges)
            },
            "third_party_imports": {
                key: [site.as_dict() for site in self.third_party_imports[key]]
                for key in sorted(self.third_party_imports)
            },
            "standard_library_imports": {
                key: sorted({site.path for site in self.standard_library_imports[key]})
                for key in sorted(self.standard_library_imports)
            },
            "dynamic_imports": [site.as_dict() for site in self.dynamic_imports],
            "external_command_sites": [
                site.as_dict() for site in self.external_commands
            ],
        }


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical compact JSON for reproducible inventory/input digests."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkerDependencyInventoryError("canonical_json_invalid") from exc


def source_digest(source: bytes) -> str:
    """SHA-256 of exact declaration/source bytes, with no newline rewriting."""

    if not isinstance(source, bytes):
        raise WorkerDependencyInventoryError("source_bytes_required")
    return hashlib.sha256(source).hexdigest()


def _module_file(project_root: Path, module: str) -> Path | None:
    if not module or not module.startswith("app.") and module != "app":
        return None
    base = project_root.joinpath(*module.split("."))
    source = base.with_suffix(".py")
    if source.is_file() and not source.is_symlink():
        return source
    package = base / "__init__.py"
    if package.is_file() and not package.is_symlink():
        return package
    return None


def _module_name(project_root: Path, path: Path) -> str:
    relative = path.relative_to(project_root)
    parts = list(relative.parts)
    if parts[-1] == "__init__.py":
        parts.pop()
    else:
        parts[-1] = Path(parts[-1]).stem
    return ".".join(parts)


def _relative_base(module: str, path: Path, level: int, name: str | None) -> str:
    parts = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
    if level:
        parts = parts[: len(parts) - level + 1]
    if name:
        parts.extend(name.split("."))
    return ".".join(parts)


def discover_worker_roots(project_root: Path) -> WorkerRoots:
    """Read Celery's literal include/autodiscovery declarations without import."""

    project_root = Path(project_root)
    entry = project_root / "app" / "celery_app.py"
    try:
        tree = ast.parse(entry.read_text(encoding="utf-8"), filename=str(entry))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise WorkerDependencyInventoryError("celery_entry_source_invalid") from exc

    explicit: list[str] = []
    candidates: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Celery":
            for keyword in node.keywords:
                if keyword.arg != "include" or not isinstance(keyword.value, (ast.List, ast.Tuple)):
                    continue
                for item in keyword.value.elts:
                    if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                        raise WorkerDependencyInventoryError("celery_include_not_static")
                    explicit.append(item.value)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "autodiscover_tasks"
        ):
            if not node.args:
                raise WorkerDependencyInventoryError("celery_autodiscovery_target_missing")
            packages = node.args[0]
            values = packages.elts if isinstance(packages, (ast.List, ast.Tuple)) else [packages]
            for item in values:
                if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                    raise WorkerDependencyInventoryError("celery_autodiscovery_not_static")
                candidates.append(f"{item.value}.tasks")

    if not explicit:
        raise WorkerDependencyInventoryError("celery_explicit_task_modules_missing")
    if len(set(explicit)) != len(explicit) or len(set(candidates)) != len(candidates):
        raise WorkerDependencyInventoryError("celery_task_root_duplicate")
    return WorkerRoots(
        celery_application="app.celery_app",
        explicit_task_modules=tuple(explicit),
        autodiscovery_candidates=tuple(candidates),
        existing_autodiscovery_modules=tuple(
            module for module in candidates if _module_file(project_root, module)
        ),
    )


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    result: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            result[child] = parent
    return result


def _optional_import(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> bool:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.Try):
            for handler in current.handlers:
                if handler.type is None:
                    return True
                names = ast.unparse(handler.type).split(".")
                if any(name in {"ImportError", "ModuleNotFoundError", "Exception"} for name in names):
                    return True
    return False


def _deferred_import(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> bool:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return True
    return False


def _literal_string_constants(tree: ast.AST) -> dict[str, Any]:
    constants: dict[str, Any] = {}
    for node in ast.walk(tree):
        value = None
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
        if isinstance(value, str) or (
            isinstance(value, (tuple, list))
            and all(isinstance(item, str) for item in value)
        ):
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value
    return constants


def _dynamic_targets(argument: ast.AST, constants: Mapping[str, Any]) -> tuple[str, ...]:
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return (argument.value,)
    if isinstance(argument, ast.Name):
        value = constants.get(argument.id)
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (tuple, list)) and all(isinstance(item, str) for item in value):
            return tuple(value)
    return ()


def _function_loop_targets(
    node: ast.Call, parents: Mapping[ast.AST, ast.AST], constants: Mapping[str, Any]
) -> tuple[str, ...]:
    """Resolve ``import_module(item)`` over a function's literal default tuple."""

    argument = node.args[0] if node.args else None
    if not isinstance(argument, ast.Name):
        return ()
    current: ast.AST = node
    function: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = current
            break
    if function is None:
        return ()

    positional = [*function.args.posonlyargs, *function.args.args]
    defaults: dict[str, Any] = {}
    for parameter, default in zip(positional[-len(function.args.defaults):], function.args.defaults):
        defaults[parameter.arg] = _dynamic_targets(default, constants)
    for parameter, default in zip(function.args.kwonlyargs, function.args.kw_defaults):
        if default is not None:
            defaults[parameter.arg] = _dynamic_targets(default, constants)

    for child in ast.walk(function):
        if not isinstance(child, (ast.For, ast.AsyncFor)):
            continue
        if not isinstance(child.target, ast.Name) or child.target.id != argument.id:
            continue
        if isinstance(child.iter, ast.Name):
            return tuple(defaults.get(child.iter.id, ()))
        return _dynamic_targets(child.iter, constants)
    return ()


def _import_sites_for_file(
    project_root: Path,
    path: Path,
    module: str,
    local_edges: set[str],
    third_party: dict[str, list[ImportSite]],
    standard_library: dict[str, list[ImportSite]],
    dynamic_imports: list[DynamicImportSite],
    commands: list[ExternalCommandSite],
) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise WorkerDependencyInventoryError(f"worker_source_parse_failed:{path}") from exc
    parents = _parents(tree)
    constants = _literal_string_constants(tree)
    subprocess_modules = {"subprocess", "asyncio", "shutil", "os"}
    module_aliases: dict[str, str] = {}
    imported_process_functions: dict[str, str] = {}
    for import_node in ast.walk(tree):
        if isinstance(import_node, ast.Import):
            for alias in import_node.names:
                if alias.name in subprocess_modules:
                    module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(import_node, ast.ImportFrom) and import_node.module in subprocess_modules:
            imported_process_functions.update({
                alias.asname or alias.name: import_node.module
                for alias in import_node.names
            })

    def record_import(imported: str, line: int, node: ast.AST) -> None:
        if not imported:
            return
        first = imported.split(".", 1)[0]
        site = ImportSite(
            module=imported,
            path=path.relative_to(project_root).as_posix(),
            line=line,
            optional_guard=_optional_import(node, parents),
            deferred_to_runtime=_deferred_import(node, parents),
        )
        if first == "app":
            if _module_file(project_root, imported):
                local_edges.add(imported)
            return
        if first in sys.stdlib_module_names or first == "__future__":
            standard_library.setdefault(first, []).append(site)
        else:
            third_party.setdefault(first, []).append(site)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                record_import(alias.name, node.lineno, node)
        elif isinstance(node, ast.ImportFrom):
            base = (
                _relative_base(module, path, node.level, node.module)
                if node.level
                else node.module or ""
            )
            if base == "app" or base.startswith("app."):
                if _module_file(project_root, base):
                    local_edges.add(base)
                for alias in node.names:
                    if alias.name != "*":
                        child = f"{base}.{alias.name}" if base else alias.name
                        if _module_file(project_root, child):
                            local_edges.add(child)
            elif base:
                record_import(base, node.lineno, node)

        elif isinstance(node, ast.Call):
            func = node.func
            call_name = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name)
                else ""
            )
            dotted = ast.unparse(func)
            if (
                call_name == "files"
                and isinstance(func, ast.Attribute)
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                # importlib.resources.files("distribution") is a package
                # resource dependency even though it is not imported by name.
                record_import(node.args[0].value, node.lineno, node)
            if call_name in {"import_module", "__import__"} and node.args:
                targets = _dynamic_targets(node.args[0], constants)
                if not targets:
                    targets = _function_loop_targets(node, parents, constants)
                dynamic_imports.append(
                    DynamicImportSite(
                        path=path.relative_to(project_root).as_posix(),
                        line=node.lineno,
                        expression=ast.unparse(node.args[0]),
                        static_targets=targets,
                    )
                )
                for target in targets:
                    if _module_file(project_root, target):
                        local_edges.add(target)
            process_owner = (
                ast.unparse(func.value)
                if isinstance(func, ast.Attribute)
                else None
            )
            owner_module = module_aliases.get(process_owner or "")
            imported_module = imported_process_functions.get(call_name)
            is_external_process_call = (
                owner_module == "shutil" and call_name == "which"
                or owner_module == "subprocess" and call_name in {
                    "Popen", "run", "call", "check_call", "check_output"
                }
                or owner_module == "asyncio" and call_name == "create_subprocess_exec"
                or owner_module == "os" and call_name in {"execv", "execve"}
                or isinstance(func, ast.Name) and (
                    imported_module == "shutil" and call_name == "which"
                    or imported_module == "subprocess" and call_name in {
                        "Popen", "run", "call", "check_call", "check_output"
                    }
                    or imported_module == "asyncio" and call_name == "create_subprocess_exec"
                    or imported_module == "os" and call_name in {"execv", "execve"}
                )
            )
            if is_external_process_call and node.args:
                first_arg = node.args[0]
                literal = first_arg.value if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str) else None
                if call_name == "which" and literal is not None:
                    executable = literal
                elif call_name in {"Popen", "run", "call", "check_call", "check_output", "create_subprocess_exec"}:
                    executable = literal
                else:
                    executable = None
                commands.append(
                    ExternalCommandSite(
                        path=path.relative_to(project_root).as_posix(),
                        line=node.lineno,
                        call=dotted,
                        literal_executable=executable,
                    )
                )


def build_import_graph(project_root: Path) -> ImportGraph:
    """Traverse local imports reachable from Celery's static production roots."""

    project_root = Path(project_root)
    roots = discover_worker_roots(project_root)
    queue = list(roots.graph_roots)
    seen: set[str] = set()
    edges: dict[str, tuple[str, ...]] = {}
    third_party: dict[str, list[ImportSite]] = {}
    standard_library: dict[str, list[ImportSite]] = {}
    dynamic_imports: list[DynamicImportSite] = []
    commands: list[ExternalCommandSite] = []

    while queue:
        module = queue.pop(0)
        if module in seen:
            continue
        path = _module_file(project_root, module)
        if path is None:
            if module in roots.explicit_task_modules:
                raise WorkerDependencyInventoryError(f"celery_included_module_missing:{module}")
            continue
        seen.add(module)
        found: set[str] = set()
        _import_sites_for_file(
            project_root, path, module, found, third_party, standard_library,
            dynamic_imports, commands,
        )
        ordered = tuple(sorted(found))
        edges[module] = ordered
        queue.extend(edge for edge in ordered if edge not in seen)

    return ImportGraph(
        roots=roots,
        local_modules=tuple(sorted(seen)),
        local_edges=edges,
        third_party_imports={
            name: tuple(sorted(sites, key=lambda item: (item.path, item.line, item.module)))
            for name, sites in third_party.items()
        },
        standard_library_imports={
            name: tuple(sorted(sites, key=lambda item: (item.path, item.line, item.module)))
            for name, sites in standard_library.items()
        },
        dynamic_imports=tuple(
            sorted(dynamic_imports, key=lambda item: (item.path, item.line, item.expression))
        ),
        external_commands=tuple(
            sorted(commands, key=lambda item: (item.path, item.line, item.call))
        ),
    )


def map_import_roots_to_distributions(
    imports: Iterable[str],
    mapping: Mapping[str, str] = IMPORT_TO_DISTRIBUTION,
) -> dict[str, str]:
    """Map every third-party import root or fail closed on an unknown root."""

    result: dict[str, str] = {}
    unknown: list[str] = []
    for imported in sorted(set(imports)):
        if imported in sys.stdlib_module_names or imported == "__future__":
            continue
        distribution = mapping.get(imported)
        if distribution is None:
            unknown.append(imported)
        else:
            result[imported] = distribution
    if unknown:
        raise WorkerDependencyInventoryError(
            "third_party_import_unmapped:" + ",".join(unknown)
        )
    return result


def parse_requirement_source(source: bytes) -> tuple[dict[str, Any], ...]:
    """Parse the simple direct declarations used here; reject ambiguous syntax."""

    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkerDependencyInventoryError("requirements_encoding_invalid") from exc
    declarations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(text.splitlines(), 1):
        content = raw.strip()
        if not content or content.startswith("#"):
            continue
        content = content.split(" #", 1)[0].strip()
        match = _REQUIREMENT_LINE.fullmatch(content)
        if not match or content.startswith(("-", ".", "/")):
            raise WorkerDependencyInventoryError(
                f"requirements_line_unsupported:{line_number}"
            )
        name = re.sub(r"[-_.]+", "-", match.group("name")).lower()
        if name in seen:
            raise WorkerDependencyInventoryError(f"requirements_name_duplicate:{name}")
        seen.add(name)
        constraints = match.group("constraints").strip()
        if "@" in constraints or constraints.startswith(("git+", "http:", "https:")):
            raise WorkerDependencyInventoryError(f"requirements_url_forbidden:{line_number}")
        extras = tuple(
            sorted(
                re.sub(r"[-_.]+", "-", extra.strip()).lower()
                for extra in (match.group("extras") or "").split(",")
                if extra.strip()
            )
        )
        declarations.append(
            {
                "name": name,
                "original_declaration": content,
                "line": line_number,
                "extras": list(extras),
                "constraints": constraints,
                "marker": match.group("marker"),
            }
        )
    return tuple(declarations)


def evaluate_target_marker(marker: str | None) -> bool | None:
    """Evaluate only markers fully determined by the declared target."""

    if marker is None or not marker.strip():
        return True
    referenced = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", marker))
    allowed = set(TARGET_MARKERS) | {"and", "or", "not", "in", "True", "False"}
    # String literal contents are not marker variable names.
    stripped = re.sub(r"(['\"]).*?\1", "", marker)
    referenced = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", stripped))
    if referenced - allowed:
        return None
    try:
        return evaluate_marker(marker, TARGET_MARKERS)
    except (ValueError, KeyError):
        return None


def target_document() -> dict[str, Any]:
    """Return the exact declaration target and its supported marker fields."""

    return {
        "platform": dict(TARGET_PLATFORM),
        "python": dict(TARGET_PYTHON),
        "marker_environment": dict(TARGET_MARKERS),
        "python_patch_version": None,
        "runtime_observation_is_not_target_authority": True,
    }

