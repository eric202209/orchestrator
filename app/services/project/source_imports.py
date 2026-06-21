"""Dependency-free Python import-to-source discovery helpers."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import re
from pathlib import Path
from typing import Optional

_PYTHON_IMPORT_LINE_RE = re.compile(
    r"^\s*(?:from\s+(?P<from>[A-Za-z_][A-Za-z0-9_.]*)\s+import\b|"
    r"import\s+(?P<import>[A-Za-z_][A-Za-z0-9_.]*))",
    re.MULTILINE,
)

_IGNORED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".agent",
    "dist",
    "build",
}


@dataclass(frozen=True)
class PythonTestContract:
    test_files: tuple[str, ...]
    imports: tuple[str, ...]
    public_calls: tuple[str, ...]
    assertions: tuple[str, ...]
    source_targets: tuple[tuple[str, str], ...]
    missing_source_targets: tuple[tuple[str, str], ...]
    src_layout_detected: bool
    truncated: bool = False


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


def _truncate(text: str, limit: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _truncate_block(text: str, limit: int) -> str:
    cleaned = str(text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _relative_to_root(path: Path, root: Path) -> Optional[str]:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def _iter_python_test_files(project_dir: Path) -> list[Path]:
    if not project_dir.exists():
        return []
    root = project_dir.resolve()
    tests: list[Path] = []
    for path in sorted(project_dir.rglob("*.py")):
        rel = _relative_to_root(path, root)
        if rel is None:
            continue
        rel_path = Path(rel)
        if set(rel_path.parts) & _IGNORED_PARTS:
            continue
        if (
            rel_path.name.startswith("test_")
            or rel_path.name.endswith("_test.py")
            or "tests" in rel_path.parts
        ):
            tests.append(path)
    return tests


def _project_package_roots(project_dir: Path) -> set[str]:
    roots: set[str] = set()
    search_roots = [project_dir / "src", project_dir]
    for base in search_roots:
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.name.startswith(".") or child.name in _IGNORED_PARTS:
                continue
            if child.is_dir() and (
                (child / "__init__.py").is_file() or any(child.rglob("*.py"))
            ):
                roots.add(child.name)
            elif child.is_file() and child.suffix == ".py" and child.stem != "__init__":
                roots.add(child.stem)
    return roots


def _import_is_project_package(module_name: str, package_roots: set[str]) -> bool:
    root = module_name.split(".", 1)[0]
    return bool(root and root in package_roots)


def _infer_src_module_path(project_dir: Path, module_name: str) -> Optional[Path]:
    parts = [part for part in module_name.split(".") if part]
    if not parts:
        return None
    src_root = project_dir / "src"
    base = src_root if src_root.is_dir() else project_dir
    candidate = base.joinpath(*parts).with_suffix(".py")
    try:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(project_dir.resolve()):
            return None
    except ValueError:
        return None
    return candidate


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def _source_import_missing_targets(
    project_dir: Path,
    source_path: Path,
    package_roots: set[str],
) -> list[tuple[str, str]]:
    try:
        tree = ast.parse(_safe_read_text(source_path))
    except SyntaxError:
        return []
    targets: list[tuple[str, str]] = []
    root = project_dir.resolve()
    for node in ast.walk(tree):
        module_name = ""
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            module_name = node.module
            names = [alias.name for alias in node.names if alias.name != "*"]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name
                names = []
                break
        if not module_name or not _import_is_project_package(
            module_name, package_roots
        ):
            continue
        if source_path_for_module(project_dir, module_name) is not None:
            continue
        inferred = _infer_src_module_path(project_dir, module_name)
        rel = _relative_to_root(inferred, root) if inferred is not None else None
        if rel:
            symbol_suffix = f" for {', '.join(names[:3])}" if names else ""
            targets.append((rel, f"missing module {module_name}{symbol_suffix}"))
    return targets


def _call_name(node: ast.Call) -> Optional[str]:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _render_call(node: ast.Call, source: str) -> str:
    segment = ast.get_source_segment(source, node)
    if segment:
        return _truncate(segment, 120)
    name = _call_name(node)
    return f"{name}(...)" if name else "call(...)"


def _render_assertion(node: ast.AST, source: str) -> Optional[str]:
    segment = ast.get_source_segment(source, node)
    if segment:
        rendered = segment.strip()
        if rendered.startswith("assert "):
            rendered = rendered[len("assert ") :]
        return _truncate(rendered, 160)
    return None


def extract_python_test_contract(project_dir: Path) -> Optional[PythonTestContract]:
    """Extract a compact, deterministic source/test contract for Python projects."""

    root = project_dir.resolve()
    test_files = _iter_python_test_files(project_dir)
    if not test_files:
        return None
    package_roots = _project_package_roots(project_dir)
    if not package_roots:
        return None

    imports: list[str] = []
    public_calls: list[str] = []
    assertions: list[str] = []
    source_targets: dict[str, list[str]] = {}
    missing_source_targets: dict[str, list[str]] = {}
    seen_imports: set[str] = set()
    imported_symbols: dict[str, str] = {}
    selected_tests: list[str] = []
    truncated = False

    for test_path in test_files[:5]:
        rel_test = _relative_to_root(test_path, root)
        if not rel_test:
            continue
        selected_tests.append(rel_test)
        text = _safe_read_text(test_path)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                module_name = node.module
                if not _import_is_project_package(module_name, package_roots):
                    continue
                names = tuple(alias.name for alias in node.names if alias.name != "*")
                import_label = (
                    f"from {module_name} import {', '.join(names)}"
                    if names
                    else f"from {module_name} import *"
                )
                if import_label not in seen_imports:
                    imports.append(import_label)
                    seen_imports.add(import_label)
                source_path = source_path_for_module(project_dir, module_name)
                inferred = None
                if source_path is None:
                    inferred = _infer_src_module_path(project_dir, module_name)
                rel_source = (
                    _relative_to_root(source_path, root)
                    if source_path is not None
                    else None
                )
                rel_missing = (
                    _relative_to_root(inferred, root) if inferred is not None else None
                )
                for name in names:
                    imported_symbols[name] = rel_source or rel_missing or module_name
                    if rel_source:
                        source_targets.setdefault(rel_source, []).append(
                            f"{rel_test} imports {module_name}.{name}"
                        )
                    elif rel_missing:
                        missing_source_targets.setdefault(rel_missing, []).append(
                            f"{rel_test} imports missing {module_name}.{name}"
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module_name = alias.name
                    if not _import_is_project_package(module_name, package_roots):
                        continue
                    import_label = f"import {module_name}"
                    if import_label not in seen_imports:
                        imports.append(import_label)
                        seen_imports.add(import_label)
                    source_path = source_path_for_module(project_dir, module_name)
                    rel_source = (
                        _relative_to_root(source_path, root)
                        if source_path is not None
                        else None
                    )
                    bound_name = alias.asname or module_name.split(".", 1)[0]
                    imported_symbols[bound_name] = rel_source or module_name
                    if rel_source:
                        source_targets.setdefault(rel_source, []).append(
                            f"{rel_test} imports {module_name}"
                        )

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name and name in imported_symbols:
                    call = _render_call(node, text)
                    public_calls.append(call)
                    target = imported_symbols.get(name)
                    if target and target.endswith(".py"):
                        source_targets.setdefault(target, []).append(
                            f"{rel_test} calls {call}"
                        )
            elif isinstance(node, ast.Assert):
                rendered = _render_assertion(node, text)
                if rendered:
                    assertions.append(rendered)
            elif isinstance(node, ast.With):
                rendered = ast.get_source_segment(text, node)
                if rendered and "pytest.raises" in rendered:
                    assertions.append(_truncate(rendered, 160))

    for rel_source in list(source_targets):
        source_path = project_dir / rel_source
        for rel_missing, reason in _source_import_missing_targets(
            project_dir, source_path, package_roots
        ):
            missing_source_targets.setdefault(rel_missing, []).append(reason)

    def unique_limited(values: list[str], limit: int) -> tuple[str, ...]:
        seen: set[str] = set()
        result: list[str] = []
        nonlocal_truncated = False
        for value in values:
            cleaned = _truncate(value, 180)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            result.append(cleaned)
            if len(result) >= limit:
                nonlocal_truncated = True
                break
        nonlocal truncated
        truncated = truncated or nonlocal_truncated or len(values) > limit
        return tuple(result)

    def target_tuple(
        data: dict[str, list[str]], limit: int
    ) -> tuple[tuple[str, str], ...]:
        items: list[tuple[str, str]] = []
        for path, reasons in sorted(data.items()):
            unique_reasons = []
            for reason in reasons:
                if reason not in unique_reasons:
                    unique_reasons.append(reason)
            items.append((path, _truncate("; ".join(unique_reasons[:3]), 220)))
            if len(items) >= limit:
                break
        nonlocal_truncated = len(data) > limit
        nonlocal truncated
        truncated = truncated or nonlocal_truncated
        return tuple(items)

    contract = PythonTestContract(
        test_files=tuple(selected_tests[:5]),
        imports=unique_limited(imports, 8),
        public_calls=unique_limited(public_calls, 8),
        assertions=unique_limited(assertions, 8),
        source_targets=target_tuple(source_targets, 8),
        missing_source_targets=target_tuple(missing_source_targets, 8),
        src_layout_detected=(project_dir / "src").is_dir(),
        truncated=truncated,
    )
    if not (
        contract.imports
        or contract.public_calls
        or contract.assertions
        or contract.source_targets
        or contract.missing_source_targets
    ):
        return None
    return contract


def render_python_test_contract_summary(
    contract: PythonTestContract,
    *,
    max_chars: int = 1800,
) -> str:
    """Render a bounded, target-oriented TEST CONTRACT SUMMARY planning block."""

    lines = [
        "## TEST CONTRACT SUMMARY",
        "- Existing tests are the contract. Preserve them.",
        "- Prefer source edits under src/.",
        "- Do not rewrite tests or verifier commands to satisfy imports/behavior.",
        "",
    ]
    required_targets = list(contract.source_targets) + list(
        contract.missing_source_targets
    )
    if required_targets:
        lines.append("Required source targets:")
        for path, reason in required_targets[:8]:
            if (path, reason) in contract.missing_source_targets:
                lines.append(f"- {path} (create missing source module)")
            else:
                lines.append(f"- {path}")
        lines.append("")
    behavior_lines = _expected_behavior_lines(contract)
    if behavior_lines:
        lines.append("Expected behavior:")
        lines.extend(f"- {item}" for item in behavior_lines[:5])
    if contract.truncated:
        lines.append("- Summary truncated to stay within planning budget.")

    rendered = "\n".join(lines).strip()
    return _truncate_block(rendered, max_chars)


def _expected_behavior_lines(contract: PythonTestContract) -> list[str]:
    lines: list[str] = []
    for assertion in contract.assertions:
        rendered = assertion.strip()
        if "capsys.readouterr().out.strip()" in rendered:
            rendered = rendered.replace(
                "capsys.readouterr().out.strip()", "printed output"
            )
        if "==" in rendered:
            left, right = [part.strip() for part in rendered.split("==", 1)]
            rendered = f"{left} should equal {right}"
        lines.append(_truncate(rendered, 140))

    if not lines:
        lines.extend(_truncate(call, 140) for call in contract.public_calls[:3])

    result: list[str] = []
    seen: set[str] = set()
    for line in reversed(lines):
        if line and line not in seen:
            result.append(line)
            seen.add(line)
        if len(result) >= 5:
            break
    return list(reversed(result))


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
        ".agent",
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


def python_test_source_context_from_tests(
    project_dir: Path,
    *,
    max_chars: int = 2200,
) -> str:
    """Return a bounded TEST CONTRACT SUMMARY for Python projects with tests."""

    contract = extract_python_test_contract(project_dir)
    if contract is None:
        return ""
    return render_python_test_contract_summary(contract, max_chars=max_chars)
