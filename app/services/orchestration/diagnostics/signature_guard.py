"""Post-repair signature guard for bounded execution debug repair."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BOUNDED_DEBUG_REPAIR_SIGNATURE_VIOLATION_REASON = (
    "bounded_execution_debug_repair_signature_contract_violation"
)


@dataclass(frozen=True)
class SignatureViolation:
    path: str
    qualified_name: str
    violation_type: str
    pre_signature: str | None
    post_signature: str | None


def check_bounded_debug_repair_signature_contract(
    *,
    project_dir: Path,
    ops: Any,
) -> list[SignatureViolation]:
    """Return violations where repair ops change existing Python function signatures.

    Checks for: signature_changed, duplicate_definition, post_parse_error,
    missing_existing_definition.  Does not mutate the workspace.
    """
    candidate_contents = _candidate_python_contents(project_dir, ops)
    if not candidate_contents:
        return []

    violations: list[SignatureViolation] = []
    for rel_path, post_content in candidate_contents.items():
        current_path = project_dir / rel_path
        try:
            pre_content = current_path.read_text(encoding="utf-8")
        except OSError:
            continue

        try:
            post_tree = ast.parse(post_content)
        except SyntaxError as exc:
            violations.append(
                SignatureViolation(
                    path=rel_path,
                    qualified_name="<module>",
                    violation_type="post_parse_error",
                    pre_signature=None,
                    post_signature=str(exc)[:200],
                )
            )
            continue

        try:
            pre_tree = ast.parse(pre_content)
        except SyntaxError:
            continue

        pre_sigs = _collect_function_sigs(pre_tree)
        post_sigs = _collect_function_sigs(post_tree)

        for qualified_name, pre_arglists in pre_sigs.items():
            pre_args = pre_arglists[0]
            post_arglists = post_sigs.get(qualified_name, [])

            if not post_arglists:
                violations.append(
                    SignatureViolation(
                        path=rel_path,
                        qualified_name=qualified_name,
                        violation_type="missing_existing_definition",
                        pre_signature=_sig_str(pre_args),
                        post_signature=None,
                    )
                )
            elif len(post_arglists) > 1:
                violations.append(
                    SignatureViolation(
                        path=rel_path,
                        qualified_name=qualified_name,
                        violation_type="duplicate_definition",
                        pre_signature=_sig_str(pre_args),
                        post_signature=" | ".join(_sig_str(a) for a in post_arglists),
                    )
                )
            elif post_arglists[0] != pre_args:
                violations.append(
                    SignatureViolation(
                        path=rel_path,
                        qualified_name=qualified_name,
                        violation_type="signature_changed",
                        pre_signature=_sig_str(pre_args),
                        post_signature=_sig_str(post_arglists[0]),
                    )
                )

    return violations


def signature_violation_event_details(
    violations: list[SignatureViolation],
) -> dict[str, Any]:
    return {
        "bounded_execution_debug_repair_signature_violations": [
            {
                "path": v.path,
                "qualified_name": v.qualified_name,
                "violation_type": v.violation_type,
                "pre_signature": v.pre_signature,
                "post_signature": v.post_signature,
            }
            for v in violations
        ],
        "bounded_execution_debug_repair_signature_violation_paths": sorted(
            {v.path for v in violations}
        ),
        "bounded_execution_debug_repair_signature_violation_types": sorted(
            {v.violation_type for v in violations}
        ),
    }


def _candidate_python_contents(
    project_dir: Path,
    ops: Any,
) -> dict[str, str]:
    """Simulate post-repair file contents for Python files without mutating the workspace."""
    if not isinstance(ops, list):
        return {}
    contents: dict[str, str] = {}
    for op in ops:
        if not isinstance(op, dict):
            continue
        op_name = str(op.get("op") or "").strip()
        rel_path = str(op.get("path") or "").strip().replace("\\", "/").lstrip("./")
        if not rel_path.endswith(".py"):
            continue
        current_content = contents.get(rel_path)
        if current_content is None:
            try:
                current_content = (project_dir / rel_path).read_text(encoding="utf-8")
            except OSError:
                current_content = ""
        if op_name == "write_file":
            contents[rel_path] = str(op.get("content") or "")
        elif op_name == "replace_in_file":
            old = str(op.get("old") or "")
            new = str(op.get("new") or "")
            contents[rel_path] = (
                current_content.replace(old, new, 1) if old else current_content
            )
        elif op_name == "append_file":
            contents[rel_path] = current_content + str(op.get("content") or "")
    return contents


def _collect_function_sigs(tree: ast.Module) -> dict[str, list[list[str]]]:
    """Return {qualified_name: [arglist, ...]} for top-level functions and class methods."""
    result: dict[str, list[list[str]]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.setdefault(node.name, []).append(_arg_names(node.args))
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qname = f"{node.name}.{child.name}"
                    result.setdefault(qname, []).append(_arg_names(child.args))
    return result


def _arg_names(args: ast.arguments) -> list[str]:
    names = [a.arg for a in args.posonlyargs + args.args]
    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    names.extend(a.arg for a in args.kwonlyargs)
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    return names


def _sig_str(args: list[str]) -> str:
    return f"({', '.join(args)})"
