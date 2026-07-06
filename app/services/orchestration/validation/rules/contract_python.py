"""Workload-contract Python-specific workload rules.

Moved verbatim from validator.py in Phase 20K (validator rule split,
slice 1). Functions here cover Python source syntax validity, src-layout
package-root/import contracts, import-time argparse usage, and
undefined test-name/decorator detection.
"""

from __future__ import annotations

import ast
import builtins
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.orchestration.operations.file_ops_contract import (
    normalize_file_op_shape,
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
