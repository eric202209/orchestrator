"""Workload-contract Python-specific workload rules.

Moved verbatim from validator.py in Phase 20K (validator rule split,
slice 1). Functions here cover Python source syntax validity, src-layout
package-root/import contracts, import-time argparse usage, and
undefined test-name/decorator detection.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.orchestration.operations.file_ops_contract import (
    normalize_file_op_shape,
)
from app.services.orchestration.operations.source_region_identity import (
    SourceRegionIdentity,
    SourceRegionIdentityError,
)
from app.services.orchestration.planning.source_materialization import (
    current_source_version_identity,
)
from app.services.project.source_imports import extract_python_test_contract

from ..integrity import is_python_test_path, scan_python_test_text
from ..workspace_guard import TaskWorkspaceViolationError, normalize_path_reference
from .contract_placeholders import _plan_materialized_file_targets


def _plan_python_source_syntax_issues(
    plan: List[Dict[str, Any]],
    project_dir: Optional[Path],
) -> List[Dict[str, Any]]:
    """Compile simulated Python file-op results without touching the workspace."""

    root = Path(project_dir).resolve() if project_dir is not None else None
    simulated_files: Dict[str, str] = {}
    issues: List[Dict[str, Any]] = []
    seen_issue_paths: set[str] = set()

    def _read_current(relative_path: str) -> str:
        if relative_path in simulated_files:
            return simulated_files[relative_path]
        if root is None:
            return ""
        candidate = (root / relative_path).resolve()
        try:
            if not candidate.is_relative_to(root) or not candidate.is_file():
                return ""
        except ValueError:
            return ""
        try:
            return candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""

    def _record_issue(relative_path: str, exc: SyntaxError) -> None:
        if relative_path in seen_issue_paths:
            return
        seen_issue_paths.add(relative_path)
        candidate_content = simulated_files.get(relative_path) or ""
        candidate_excerpt = " ".join(candidate_content.split())[:500]
        issues.append(
            {
                "path": relative_path,
                "line": exc.lineno,
                "offset": exc.offset,
                "message": str(exc.msg or "invalid Python syntax"),
                "candidate_content_excerpt": candidate_excerpt,
                "candidate_content": candidate_content[:12000],
                "candidate_content_truncated": len(candidate_content) > 12000,
            }
        )

    for step in plan:
        for raw_operation in step.get("ops", []) or []:
            if not isinstance(raw_operation, dict):
                continue
            operation = normalize_file_op_shape(raw_operation)
            op_name = str(operation.get("op") or "")
            if op_name not in {"write_file", "append_file", "replace_in_file"}:
                continue
            path_text = str(operation.get("path") or "").strip()
            if not path_text or Path(path_text).suffix.lower() != ".py":
                continue
            if root is not None:
                try:
                    relative_path = normalize_path_reference(path_text, root)
                except TaskWorkspaceViolationError:
                    continue
            else:
                relative_path = path_text.rstrip("/").lstrip("./")
                if (
                    not relative_path
                    or Path(relative_path).is_absolute()
                    or ".." in Path(relative_path).parts
                ):
                    continue

            current = _read_current(relative_path)
            if op_name == "write_file":
                candidate_content = str(operation.get("content") or "")
            elif op_name == "append_file":
                candidate_content = current + str(operation.get("content") or "")
            else:
                if "selector" in operation:
                    if root is None or not isinstance(operation.get("new"), str):
                        continue
                    try:
                        selector = SourceRegionIdentity.from_dict(
                            operation.get("selector")
                        )
                        target = (root / relative_path).resolve()
                        source_bytes = target.read_bytes()
                        if (
                            selector.canonical_path.value != relative_path
                            or current_source_version_identity(target)
                            != selector.expected_source_version
                            or selector.end_byte > len(source_bytes)
                        ):
                            continue
                        source_bytes[: selector.start_byte].decode("utf-8")
                        source_bytes[: selector.end_byte].decode("utf-8")
                        selected = source_bytes[selector.start_byte : selector.end_byte]
                        if (
                            hashlib.sha256(selected).hexdigest()
                            != selector.selected_region_sha256
                        ):
                            continue
                        candidate_content = (
                            source_bytes[: selector.start_byte]
                            + operation["new"].encode("utf-8")
                            + source_bytes[selector.end_byte :]
                        ).decode("utf-8")
                    except (
                        OSError,
                        UnicodeDecodeError,
                        SourceRegionIdentityError,
                    ):
                        continue
                    simulated_files[relative_path] = candidate_content
                    try:
                        compile(candidate_content, relative_path, "exec")
                    except SyntaxError as exc:
                        _record_issue(relative_path, exc)
                    continue
                old = operation.get("old")
                new = operation.get("new")
                if not isinstance(old, str) or not isinstance(new, str) or not old:
                    continue
                if current.count(old) != 1:
                    continue
                candidate_content = current.replace(old, new, 1)

            simulated_files[relative_path] = candidate_content
            try:
                compile(candidate_content, relative_path, "exec")
            except SyntaxError as exc:
                _record_issue(relative_path, exc)

    return issues


def _python_src_package_root(path: str) -> Optional[str]:
    parts = Path(str(path or "").strip().lstrip("./")).parts
    if len(parts) >= 3 and parts[0] == "src" and parts[1].isidentifier():
        return parts[1]
    return None


def _task_prompt_requests_python_package_rename(
    task_prompt: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> bool:
    combined = " ".join(
        str(value or "") for value in (task_prompt, title, description)
    ).lower()
    return bool(
        re.search(
            r"\b(?:rename|renaming|migrate|move|change)\b.{0,80}"
            r"\b(?:package|module|import|namespace)\b",
            combined,
        )
        or re.search(
            r"\b(?:package|module|import|namespace)\b.{0,80}"
            r"\b(?:rename|renaming|migrate|move|change)\b",
            combined,
        )
    )


def _operation_text_import_roots(operation: Dict[str, Any]) -> set[str]:
    text = str(operation.get("content") or operation.get("new") or "")
    if not text:
        return set()
    roots: set[str] = set()
    for match in re.finditer(
        r"^\s*(?:from\s+([A-Za-z_][A-Za-z0-9_]*)"
        r"(?:\.[A-Za-z_][A-Za-z0-9_.]*)?\s+import\b|"
        r"import\s+([A-Za-z_][A-Za-z0-9_]*)"
        r"(?:\.[A-Za-z_][A-Za-z0-9_.]*)?)",
        text,
        re.MULTILINE,
    ):
        root = match.group(1) or match.group(2)
        if root:
            roots.add(root)
    return roots


def _python_package_root_contract_violation(
    plan: List[Dict[str, Any]],
    *,
    project_dir: Optional[Path],
    task_prompt: str,
    title: Optional[str],
    description: Optional[str],
) -> Optional[Dict[str, Any]]:
    if project_dir is None or not project_dir.exists():
        return None
    if _task_prompt_requests_python_package_rename(
        task_prompt, title=title, description=description
    ):
        return None

    try:
        contract = extract_python_test_contract(project_dir)
    except Exception:
        return None
    if contract is None or not contract.source_targets or not contract.imports:
        return None

    required_source_targets = sorted(
        {
            path
            for path, _reason in contract.source_targets
            if str(path or "").startswith("src/")
        }
    )
    existing_roots = sorted(
        {
            root
            for path in required_source_targets
            if (root := _python_src_package_root(path))
        }
    )
    if not required_source_targets or not existing_roots:
        return None

    materialized_targets = sorted(_plan_materialized_file_targets(plan))
    source_write_paths = sorted(
        path
        for path in materialized_targets
        if path.startswith("src/") and Path(path).suffix.lower() == ".py"
    )
    touched_existing_roots = sorted(
        {
            root
            for path in source_write_paths
            if (root := _python_src_package_root(path)) in set(existing_roots)
        }
    )
    introduced_roots = sorted(
        {
            root
            for path in source_write_paths
            if (root := _python_src_package_root(path))
            and root not in set(existing_roots)
        }
    )

    rewritten_test_import_roots: set[str] = set()
    for step in plan:
        for operation in step.get("ops", []) or []:
            if not isinstance(operation, dict):
                continue
            path = str(operation.get("path") or "").strip().rstrip("/").lstrip("./")
            if not path or not is_python_test_path(path):
                continue
            rewritten_test_import_roots.update(
                root
                for root in _operation_text_import_roots(operation)
                if root not in set(existing_roots)
            )

    introduced_without_existing_touch = bool(
        introduced_roots and not touched_existing_roots
    )
    rewrites_tests_to_introduced_root = bool(
        set(rewritten_test_import_roots).intersection(introduced_roots)
    )
    if not introduced_without_existing_touch and not rewrites_tests_to_introduced_root:
        return None

    return {
        "existing_package_roots": existing_roots,
        "required_source_targets": required_source_targets[:12],
        "source_write_paths": source_write_paths[:12],
        "touched_existing_package_roots": touched_existing_roots,
        "introduced_package_roots": introduced_roots,
        "rewritten_test_import_roots": sorted(rewritten_test_import_roots),
        "introduced_without_existing_touch": introduced_without_existing_touch,
        "rewrites_tests_to_introduced_root": rewrites_tests_to_introduced_root,
    }


def _expected_source_files_not_materialized(
    *,
    declared_expected_files: set[str],
    materialized_targets: set[str],
    existing_expected_files: set[str],
) -> List[str]:
    from ..workspace_checks import SOURCE_EXTENSIONS

    missing_sources: List[str] = []
    for path_text in sorted(declared_expected_files):
        normalized = str(path_text or "").strip().rstrip("/").lstrip("./")
        if not normalized or normalized in materialized_targets:
            continue
        if normalized in existing_expected_files:
            continue
        path = Path(normalized)
        if path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        if not normalized.startswith("src/"):
            continue
        missing_sources.append(normalized)
    return missing_sources


def _plan_writes_obvious_undefined_python_test_names(
    plan: List[Dict[str, Any]],
    project_dir: Optional[Path],
) -> List[str]:
    bad_paths: List[str] = []
    root = project_dir.resolve() if project_dir is not None else None
    for step in plan:
        for operation in step.get("ops", []) or []:
            if not isinstance(operation, dict):
                continue
            op_name = str(operation.get("op") or "")
            if op_name not in {"write_file", "append_file"}:
                continue
            path_text = str(operation.get("path") or "").strip().lstrip("./")
            if not is_python_test_path(path_text):
                continue
            content = str(operation.get("content") or "")
            if not content.strip():
                continue
            scan_text = content
            if op_name == "append_file" and project_dir is not None:
                existing_path = (project_dir / path_text).resolve()
                try:
                    if root is not None and existing_path.is_relative_to(root):
                        try:
                            existing_text = existing_path.read_text(encoding="utf-8")
                        except UnicodeDecodeError:
                            existing_text = existing_path.read_text(
                                encoding="utf-8", errors="ignore"
                            )
                        except OSError:
                            existing_text = ""
                        if existing_text:
                            scan_text = f"{existing_text.rstrip()}\n{content}"
                except ValueError:
                    pass
            findings = scan_python_test_text(scan_text, path_text)
            if any(
                finding.code == "undefined_test_name" and finding.severity == "error"
                for finding in findings
            ):
                bad_paths.append(path_text or "(missing path)")
    return sorted(set(bad_paths))


def _plan_writes_obvious_undefined_python_decorators(
    plan: List[Dict[str, Any]],
    project_dir: Optional[Path],
) -> List[str]:
    bad_paths: List[str] = []
    root = project_dir.resolve() if project_dir is not None else None
    for step in plan:
        for operation in step.get("ops", []) or []:
            if not isinstance(operation, dict):
                continue
            op_name = str(operation.get("op") or "")
            if op_name not in {"write_file", "append_file"}:
                continue
            path_text = str(operation.get("path") or "").strip().lstrip("./")
            if Path(path_text).suffix.lower() != ".py":
                continue
            content = str(operation.get("content") or "")
            if not content.strip():
                continue
            scan_text = content
            if op_name == "append_file" and project_dir is not None:
                existing_path = (project_dir / path_text).resolve()
                try:
                    if root is not None and existing_path.is_relative_to(root):
                        try:
                            existing_text = existing_path.read_text(encoding="utf-8")
                        except UnicodeDecodeError:
                            existing_text = existing_path.read_text(
                                encoding="utf-8", errors="ignore"
                            )
                        except OSError:
                            existing_text = ""
                        if existing_text:
                            scan_text = f"{existing_text.rstrip()}\n{content}"
                except ValueError:
                    pass
            try:
                tree = ast.parse(scan_text)
            except SyntaxError:
                continue
            defined = _python_module_defined_names(tree)
            for node in ast.walk(tree):
                decorators = getattr(node, "decorator_list", None)
                if not decorators:
                    continue
                for decorator in decorators:
                    root_name = _python_expression_root_name(decorator)
                    if root_name and root_name not in defined:
                        bad_paths.append(path_text or "(missing path)")
                        break
                if bad_paths and bad_paths[-1] == (path_text or "(missing path)"):
                    break
    return sorted(set(bad_paths))


# PHASE40C5: ``write_file`` replaces the whole file. Workflows A and B returned
# function-only content for existing modules, deleting imports and helpers that
# the new content itself still referenced. Flag only names the replaced
# content bound and the new content uses but no longer binds anywhere.
_PYTHON_MODULE_DUNDERS = frozenset(
    {
        "__name__",
        "__file__",
        "__doc__",
        "__package__",
        "__spec__",
        "__loader__",
        "__builtins__",
        "__path__",
        "__annotations__",
    }
)


def _python_module_scope_bindings(statements: List[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for node in statements:
        if isinstance(node, ast.Import):
            names.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_python_bound_names(target))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            names.update(_python_bound_names(node.target))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            names.update(_python_bound_names(node.target))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    names.update(_python_bound_names(item.optional_vars))
        for field_name in ("body", "orelse", "finalbody"):
            nested = getattr(node, field_name, None)
            if isinstance(nested, list) and not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                names.update(_python_module_scope_bindings(nested))
        for handler in getattr(node, "handlers", None) or []:
            names.update(_python_module_scope_bindings(handler.body))
    return names


def _python_unbound_name_references(tree: ast.Module) -> set[str]:
    """Return loaded names bound nowhere in the module (any scope)."""

    postponed = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )
    annotation_nodes: set[int] = set()
    if postponed:
        for node in ast.walk(tree):
            for annotation in (
                getattr(node, "annotation", None),
                getattr(node, "returns", None),
            ):
                if isinstance(annotation, ast.AST):
                    annotation_nodes.update(id(sub) for sub in ast.walk(annotation))

    bound: set[str] = set(dir(builtins)) | _PYTHON_MODULE_DUNDERS
    loaded: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                if id(node) not in annotation_nodes:
                    loaded.add(node.id)
            else:
                bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
    return loaded - bound


def _plan_write_file_drops_referenced_python_definitions(
    plan: List[Dict[str, Any]],
    project_dir: Optional[Path],
) -> List[Dict[str, Any]]:
    if project_dir is None:
        return []
    root = Path(project_dir).resolve()
    simulated: Dict[str, str] = {}
    findings: List[Dict[str, Any]] = []
    for step_index, step in enumerate(plan or [], start=1):
        if not isinstance(step, dict):
            continue
        for raw_operation in step.get("ops", []) or []:
            if not isinstance(raw_operation, dict):
                continue
            operation = normalize_file_op_shape(raw_operation)
            if str(operation.get("op") or "") != "write_file":
                continue
            content = operation.get("content")
            path_text = str(operation.get("path") or "").strip()
            if not isinstance(content, str) or Path(path_text).suffix != ".py":
                continue
            try:
                relative_path = normalize_path_reference(path_text, root)
            except TaskWorkspaceViolationError:
                continue
            previous = simulated.get(relative_path)
            if previous is None:
                candidate = root / relative_path
                try:
                    if not candidate.is_file():
                        simulated[relative_path] = content
                        continue
                    previous = candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
            simulated[relative_path] = content
            try:
                before = ast.parse(previous)
                after = ast.parse(content)
            except SyntaxError:
                continue
            if any(
                isinstance(node, ast.ImportFrom)
                and any(alias.name == "*" for alias in node.names)
                for node in ast.walk(after)
            ):
                continue
            dropped = sorted(
                _python_unbound_name_references(after)
                & _python_module_scope_bindings(before.body)
            )
            if dropped:
                findings.append(
                    {
                        "path": relative_path,
                        "step_number": step.get("step_number", step_index),
                        "dropped_referenced_names": dropped[:20],
                    }
                )
    return findings


def _plan_writes_import_time_python_parse_args(
    plan: List[Dict[str, Any]],
    project_dir: Optional[Path],
) -> List[str]:
    bad_paths: List[str] = []
    root = project_dir.resolve() if project_dir is not None else None
    for step in plan:
        for operation in step.get("ops", []) or []:
            if not isinstance(operation, dict):
                continue
            op_name = str(operation.get("op") or "")
            if op_name not in {"write_file", "append_file"}:
                continue
            path_text = str(operation.get("path") or "").strip().lstrip("./")
            if Path(path_text).suffix.lower() != ".py":
                continue
            content = str(operation.get("content") or "")
            if not content.strip():
                continue
            scan_text = content
            if op_name == "append_file" and project_dir is not None:
                existing_path = (project_dir / path_text).resolve()
                try:
                    if root is not None and existing_path.is_relative_to(root):
                        try:
                            existing_text = existing_path.read_text(encoding="utf-8")
                        except UnicodeDecodeError:
                            existing_text = existing_path.read_text(
                                encoding="utf-8", errors="ignore"
                            )
                        except OSError:
                            existing_text = ""
                        if existing_text:
                            scan_text = f"{existing_text.rstrip()}\n{content}"
                except ValueError:
                    pass
            try:
                tree = ast.parse(scan_text)
            except SyntaxError:
                continue
            if _python_module_has_import_time_parse_args(tree):
                bad_paths.append(path_text or "(missing path)")
    return sorted(set(bad_paths))


def _plan_appends_contextual_python_fragments(
    plan: List[Dict[str, Any]],
) -> List[str]:
    bad_paths: List[str] = []
    block_continuation_pattern = re.compile(
        r"^(?:elif\b.*:|else\s*:|except\b.*:|finally\s*:|case\b.*:)"
    )
    indented_flow_exit_pattern = re.compile(r"^(?:return\b|break\b|continue\b)")

    for step in plan:
        for operation in step.get("ops", []) or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "") != "append_file":
                continue
            path_text = str(operation.get("path") or "").strip().lstrip("./")
            if Path(path_text).suffix.lower() != ".py":
                continue
            content = str(operation.get("content") or "")
            if not content.strip():
                continue
            try:
                ast.parse(content)
                continue
            except SyntaxError:
                pass

            for raw_line in content.splitlines():
                if not raw_line.strip():
                    continue
                stripped = raw_line.strip()
                is_indented = raw_line[:1].isspace()
                if block_continuation_pattern.match(stripped):
                    bad_paths.append(path_text or "(missing path)")
                    break
                if is_indented and indented_flow_exit_pattern.match(stripped):
                    bad_paths.append(path_text or "(missing path)")
                    break

    return sorted(set(bad_paths))


def _plan_writes_physical_src_python_imports(
    plan: List[Dict[str, Any]],
    project_dir: Optional[Path],
) -> List[str]:
    return sorted(
        {
            str(item.get("path") or "")
            for item in _plan_physical_src_python_import_details(plan, project_dir)
            if str(item.get("path") or "")
        }
    )


def _plan_physical_src_python_import_details(
    plan: List[Dict[str, Any]],
    project_dir: Optional[Path],
) -> List[Dict[str, Any]]:
    if project_dir is None or not _project_has_python_src_layout(project_dir):
        return []

    findings: List[Dict[str, Any]] = []
    package_names = _python_src_layout_package_names(project_dir)
    for step in plan:
        for operation in step.get("ops", []) or []:
            if not isinstance(operation, dict):
                continue
            op_name = str(operation.get("op") or "")
            if op_name not in {"write_file", "append_file", "replace_in_file"}:
                continue
            path_text = str(operation.get("path") or "").strip().lstrip("./")
            if Path(path_text).suffix.lower() != ".py":
                continue
            raw_content = (
                operation.get("new")
                if op_name == "replace_in_file"
                else operation.get("content")
            )
            content = str(raw_content or "")
            if not content.strip():
                continue
            invalid_lines = _python_physical_src_import_lines(
                content,
                package_names=package_names,
            )
            if invalid_lines:
                findings.append(
                    {
                        "path": path_text or "(missing path)",
                        "invalid_imports": invalid_lines[:5],
                    }
                )
    return findings


def _project_has_python_src_layout(project_dir: Path) -> bool:
    root = project_dir.resolve()
    src_dir = root / "src"
    if not src_dir.is_dir():
        return False

    for config_name in ("pyproject.toml", "setup.cfg", "setup.py"):
        config_path = root / config_name
        try:
            config_text = config_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            config_text = config_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            config_text = ""
        lowered = config_text.lower()
        if any(
            marker in lowered
            for marker in (
                'where = ["src"]',
                "where = ['src']",
                "package_dir",
                'pythonpath = ["src"]',
                "pythonpath = ['src']",
            )
        ):
            return True

    return any(
        candidate.is_dir() and (candidate / "__init__.py").exists()
        for candidate in src_dir.iterdir()
    )


def _python_src_layout_package_names(project_dir: Path) -> set[str]:
    src_dir = project_dir.resolve() / "src"
    if not src_dir.is_dir():
        return set()
    try:
        return {
            candidate.name
            for candidate in src_dir.iterdir()
            if candidate.is_dir()
            and candidate.name.isidentifier()
            and (candidate / "__init__.py").exists()
        }
    except OSError:
        return set()


def _python_text_uses_physical_src_import_prefix(
    text: str,
    *,
    package_names: set[str],
) -> bool:
    if _python_physical_src_import_lines(text, package_names=package_names):
        return True
    return False


def _python_physical_src_import_lines(
    text: str,
    *,
    package_names: set[str],
) -> List[str]:
    lines: List[str] = []
    line_pattern = re.compile(
        r"^\s*(?:from\s+src\.([A-Za-z_][A-Za-z0-9_]*)\b.*|import\s+src\.([A-Za-z_][A-Za-z0-9_]*)\b.*)$"
    )
    for raw_line in str(text or "").splitlines():
        match = line_pattern.match(raw_line)
        if not match:
            continue
        package = match.group(1) or match.group(2) or ""
        if not package_names or package in package_names:
            rendered = raw_line.strip()
            if rendered and rendered not in lines:
                lines.append(rendered)

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return lines

    for node in ast.walk(tree):
        package = ""
        if isinstance(node, ast.ImportFrom) and str(node.module or "").startswith(
            "src."
        ):
            package = str(node.module or "").split(".", 2)[1]
            rendered = f"from {node.module} import ..."
        elif isinstance(node, ast.Import):
            rendered = ""
            for alias in node.names:
                name = str(alias.name or "")
                if name.startswith("src."):
                    package = name.split(".", 2)[1]
                    rendered = f"import {name}"
                    break
        if package and (not package_names or package in package_names):
            if isinstance(node, ast.ImportFrom) and any(
                line.startswith(f"from {node.module} import ") for line in lines
            ):
                continue
            if rendered and rendered not in lines:
                lines.append(rendered)
    return lines


def _python_module_has_import_time_parse_args(tree: ast.AST) -> bool:
    for node in getattr(tree, "body", []):
        if _is_main_guard(node):
            continue
        for child in ast.walk(node):
            if child is node and isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                break
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if _is_parse_args_call(child):
                return True
    return False


def _is_parse_args_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "parse_args"


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    return _is_main_guard_compare(node.test)


def _is_main_guard_compare(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
        return False
    if len(node.comparators) != 1:
        return False
    return (
        _is_dunder_name_name(node.left) and _is_main_string(node.comparators[0])
    ) or (_is_main_string(node.left) and _is_dunder_name_name(node.comparators[0]))


def _is_dunder_name_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "__name__"


def _is_main_string(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "__main__"


def _python_module_defined_names(tree: ast.AST) -> set[str]:
    names = set(dir(builtins))
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Import):
            names.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_python_bound_names(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_python_bound_names(node.target))
    return names


def _python_bound_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for child in node.elts:
            names.update(_python_bound_names(child))
        return names
    return set()


def _python_expression_root_name(node: ast.AST) -> Optional[str]:
    current = node
    while isinstance(current, (ast.Call, ast.Attribute, ast.Subscript)):
        if isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Attribute):
            current = current.value
        else:
            current = current.value
    if isinstance(current, ast.Name):
        return current.id
    return None


# --------------------------------------------------------------------------
# PHASE34-VIC1 — Plan-internal verification contradiction.
#
# PHASE34-S2X proved a deterministic validator false negative: a Plan could
# reach `accepted` while mutating an implementation so that an assertion in a
# materialized test file can no longer hold, and then naming that same test file
# as its own verification. Nothing here executes Plan code, imports user
# modules, runs pytest, or evaluates a whole program. The reducer below decides
# exactly one shape -- a function whose body is a single `return` of string
# literals, bound parameters, f-strings and `+` concatenation -- and returns
# None for everything else, so the rule stays silent unless the contradiction is
# provable from evidence already on hand.
# --------------------------------------------------------------------------

_VIC1_MAX_TEST_BYTES = 200_000


def _vic1_literal_value(node: ast.AST, binding: Dict[str, str]) -> Optional[str]:
    """Reduce an expression to a string literal, or None when undecidable."""

    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return binding.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                if not isinstance(value.value, str):
                    return None
                parts.append(value.value)
                continue
            if not isinstance(value, ast.FormattedValue):
                return None
            # A conversion (!r) or format spec is not reduced here.
            if value.conversion not in (-1, None) or value.format_spec is not None:
                return None
            resolved = _vic1_literal_value(value.value, binding)
            if resolved is None:
                return None
            parts.append(resolved)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _vic1_literal_value(node.left, binding)
        right = _vic1_literal_value(node.right, binding)
        if left is None or right is None:
            return None
        return left + right
    return None


def _vic1_single_return_value(
    source_text: str, function_name: str, arguments: List[str]
) -> Optional[str]:
    """Value of ``function_name(*arguments)`` when it is a single literal return."""

    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return None
    target = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            if target is not None:
                return None  # redefined; not decidable
            target = node
    if target is None:
        return None
    body = list(target.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # drop the docstring
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return None
    if body[0].value is None:
        return None
    signature = target.args
    if signature.vararg or signature.kwarg or signature.kwonlyargs:
        return None
    positional = [argument.arg for argument in signature.posonlyargs] + [
        argument.arg for argument in signature.args
    ]
    if len(arguments) > len(positional):
        return None
    binding = dict(zip(positional, arguments))
    return _vic1_literal_value(body[0].value, binding)


def _vic1_apply_plan_ops(
    current_text: str, operations: List[Dict[str, Any]]
) -> Optional[str]:
    """Text after the Plan's own ops, or None when not deterministically applicable."""

    text = current_text
    for operation in operations:
        shape = normalize_file_op_shape(operation) or operation
        op_name = str(shape.get("op") or "").strip()
        if op_name == "write_file":
            content = shape.get("content")
            if not isinstance(content, str):
                return None
            text = content
        elif op_name == "append_file":
            content = shape.get("content")
            if not isinstance(content, str):
                return None
            text = text + content
        elif op_name == "replace_in_file":
            old = shape.get("old")
            new = shape.get("new")
            if not isinstance(old, str) or not isinstance(new, str):
                return None
            if not old or text.count(old) != 1:
                return None
            text = text.replace(old, new, 1)
        else:
            return None
    return text


def _vic1_plan_ops_by_path(
    plan: List[Dict[str, Any]], project_dir: Path
) -> Dict[str, List[Dict]]:
    ops_by_path: Dict[str, List[Dict[str, Any]]] = {}
    for step in plan:
        for operation in step.get("ops", []) or []:
            if not isinstance(operation, dict):
                continue
            raw_path = str(operation.get("path") or "").strip()
            if not raw_path:
                continue
            try:
                path = normalize_path_reference(raw_path, project_dir)
            except TaskWorkspaceViolationError:
                continue
            ops_by_path.setdefault(path, []).append(operation)
    return ops_by_path


def _vic1_verification_test_references(
    plan: List[Dict[str, Any]]
) -> List[tuple[str, str]]:
    """(test_path, command) pairs a Plan's own verification explicitly names."""

    references: List[tuple[str, str]] = []
    for step in plan:
        candidates = [step.get("verification")]
        candidates.extend(step.get("commands", []) or [])
        for candidate in candidates:
            command = str(candidate or "").strip()
            if not command:
                continue
            for token in re.findall(r"[\w./-]+\.py", command):
                path = token.lstrip("./")
                if is_python_test_path(path):
                    references.append((path, command))
    return references


def _vic1_assertion_targets(
    test_text: str,
) -> List[tuple[str, List[str], str, str]]:
    """(function, literal args, expected value, rendered assertion) tuples."""

    try:
        tree = ast.parse(test_text)
    except SyntaxError:
        return []
    imported: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                if alias.name != "*":
                    imported[alias.asname or alias.name] = node.module
    targets: List[tuple[str, List[str], str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert) or node.msg is not None:
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        if not isinstance(test.ops[0], ast.Eq):
            continue
        left, right = test.left, test.comparators[0]
        if isinstance(right, ast.Call) and isinstance(left, ast.Constant):
            left, right = right, left
        if not isinstance(left, ast.Call) or not isinstance(right, ast.Constant):
            continue
        if not isinstance(right.value, str):
            continue
        if not isinstance(left.func, ast.Name) or left.keywords:
            continue
        function_name = left.func.id
        if function_name not in imported:
            continue
        arguments: List[str] = []
        for argument in left.args:
            if not isinstance(argument, ast.Constant) or not isinstance(
                argument.value, str
            ):
                arguments = []
                break
            arguments.append(argument.value)
        if len(arguments) != len(left.args):
            continue
        rendered = f"{function_name}({', '.join(repr(a) for a in arguments)})"
        targets.append((function_name, arguments, right.value, rendered))
    return targets


def _plan_verification_internal_contradiction(
    plan: List[Dict[str, Any]],
    *,
    project_dir: Optional[Path],
) -> Optional[Dict[str, Any]]:
    """Detect a Plan whose own mutation cannot pass its own verification."""

    from app.services.project.source_imports import source_path_for_module

    if project_dir is None or not project_dir.exists():
        return None
    ops_by_path = _vic1_plan_ops_by_path(plan, project_dir)
    if not ops_by_path:
        return None

    for test_path, command in _vic1_verification_test_references(plan):
        if test_path in ops_by_path:
            continue  # the Plan updates the test as well -- nothing to contradict
        test_file = project_dir / test_path
        try:
            if not test_file.is_file():
                continue
            if test_file.stat().st_size > _VIC1_MAX_TEST_BYTES:
                continue
            test_text = test_file.read_text(encoding="utf-8")
        except OSError:
            continue

        for function_name, arguments, expected, rendered in _vic1_assertion_targets(
            test_text
        ):
            module = None
            try:
                tree = ast.parse(test_text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    if any(
                        (alias.asname or alias.name) == function_name
                        for alias in node.names
                    ):
                        module = node.module
            if not module:
                continue
            source_path = source_path_for_module(project_dir, module)
            if source_path is None or not source_path.is_file():
                continue
            try:
                relative = normalize_path_reference(
                    str(source_path.relative_to(project_dir.resolve())),
                    project_dir,
                )
            except (TaskWorkspaceViolationError, ValueError):
                continue
            operations = ops_by_path.get(relative)
            if not operations:
                continue
            try:
                current_text = source_path.read_text(encoding="utf-8")
            except OSError:
                continue

            current_value = _vic1_single_return_value(
                current_text, function_name, arguments
            )
            # Only blame the Plan when the assertion holds before it runs.
            if current_value is None or current_value != expected:
                continue
            planned_text = _vic1_apply_plan_ops(current_text, operations)
            if planned_text is None:
                continue
            planned_value = _vic1_single_return_value(
                planned_text, function_name, arguments
            )
            if planned_value is None or planned_value == expected:
                continue
            return {
                "mutated_implementation_path": relative,
                "unchanged_test_path": test_path,
                "asserted_expression": rendered,
                "expected_value": expected[:120],
                "planned_value": planned_value[:120],
                "verification_command": command[:200],
                "contradiction_reason": (
                    f"plan changes {relative} so {rendered} returns "
                    f"{planned_value[:60]!r}, but {test_path} still asserts "
                    f"{expected[:60]!r} and the plan runs that test as its own "
                    "verification"
                ),
            }
    return None
