"""Post-repair signature guard for bounded execution debug repair."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BOUNDED_DEBUG_REPAIR_SIGNATURE_VIOLATION_REASON = (
    "bounded_execution_debug_repair_signature_contract_violation"
)
COMPLETION_REPAIR_SIGNATURE_VIOLATION_REASON = (
    "completion_repair_signature_contract_violation"
)


@dataclass(frozen=True)
class SignatureViolation:
    path: str
    qualified_name: str
    violation_type: str
    pre_signature: str | None
    post_signature: str | None


@dataclass(frozen=True)
class CompletionRepairSignatureGuardResult:
    """Pre-apply completion-repair signature-guard outcome."""

    checked: bool
    candidate_unavailable: bool
    violations: list[SignatureViolation]


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


def check_completion_repair_signature_contract(
    *,
    project_dir: Path,
    ops: Any,
) -> CompletionRepairSignatureGuardResult:
    """Strictly check previewable completion-repair Python edits without mutation.

    This deliberately does not execute command-only repairs to obtain a preview.
    E45 continues to use its name-only comparison above; completion repair alone
    uses the strict fingerprint below.
    """
    if not isinstance(ops, list) or not ops:
        return CompletionRepairSignatureGuardResult(
            checked=False, candidate_unavailable=True, violations=[]
        )

    candidate_contents = _candidate_python_contents(project_dir, ops)
    candidate_unavailable = _has_unsimulatable_python_ops(ops)
    violations: list[SignatureViolation] = []
    for rel_path, post_content in candidate_contents.items():
        current_path = project_dir / rel_path
        try:
            pre_content = current_path.read_text(encoding="utf-8")
        except OSError:
            # New files have no existing source contract to preserve.
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
            # There is no reliable existing AST contract to compare.
            continue

        pre_sigs = _collect_strict_function_sigs(pre_tree)
        post_sigs = _collect_strict_function_sigs(post_tree)
        for qualified_name, pre_fingerprints in pre_sigs.items():
            pre_fingerprint = pre_fingerprints[0]
            post_fingerprints = post_sigs.get(qualified_name, [])
            if not post_fingerprints:
                violations.append(
                    SignatureViolation(
                        path=rel_path,
                        qualified_name=qualified_name,
                        violation_type="missing_existing_definition",
                        pre_signature=pre_fingerprint,
                        post_signature=None,
                    )
                )
            elif len(post_fingerprints) > 1:
                violations.append(
                    SignatureViolation(
                        path=rel_path,
                        qualified_name=qualified_name,
                        violation_type="duplicate_definition",
                        pre_signature=pre_fingerprint,
                        post_signature=" | ".join(post_fingerprints),
                    )
                )
            elif post_fingerprints[0] != pre_fingerprint:
                violations.append(
                    SignatureViolation(
                        path=rel_path,
                        qualified_name=qualified_name,
                        violation_type="signature_changed",
                        pre_signature=pre_fingerprint,
                        post_signature=post_fingerprints[0],
                    )
                )

    return CompletionRepairSignatureGuardResult(
        checked=True,
        candidate_unavailable=candidate_unavailable,
        violations=violations,
    )


def completion_repair_signature_violation_event_details(
    result: CompletionRepairSignatureGuardResult,
) -> dict[str, Any]:
    """Return non-sensitive completion-repair guard telemetry."""
    violations = result.violations
    return {
        "completion_repair_signature_guard_checked": result.checked,
        "completion_repair_signature_guard_candidate_unavailable": (
            result.candidate_unavailable
        ),
        "completion_repair_signature_violation_count": len(violations),
        "completion_repair_signature_violation_types": sorted(
            {v.violation_type for v in violations}
        ),
        "completion_repair_signature_violation_paths": sorted(
            {v.path for v in violations}
        ),
        "completion_repair_signature_violations": [
            {
                "path": v.path,
                "qualified_name": v.qualified_name,
                "violation_type": v.violation_type,
                "pre_signature": v.pre_signature,
                "post_signature": v.post_signature,
            }
            for v in violations
        ],
    }


def build_duplicate_definition_retry_instruction(
    violations: list[SignatureViolation],
) -> str:
    """Build a guard-triggered retry instruction for duplicate_definition violations.

    Returns a correction block to append to the repair prompt so the model
    removes wrong-signature duplicate definitions instead of preserving both.
    Only violations with violation_type == "duplicate_definition" are processed.
    """
    dup_violations = [
        v for v in violations if v.violation_type == "duplicate_definition"
    ]
    if not dup_violations:
        return ""

    lines: list[str] = [
        "SIGNATURE GUARD CORRECTION: Your previous repair output contained duplicate"
        " function definitions. This violates the signature contract.",
        "",
        "For each function listed below:",
        "  1. Remove the wrong-signature definition entirely.",
        "  2. Keep exactly one definition with the correct original signature.",
        "  3. Implement only the function body — do not change any parameter list.",
        "",
        "Duplicates to fix:",
    ]
    for v in dup_violations:
        correct_sig = v.pre_signature or ""
        all_post_sigs = [s.strip() for s in (v.post_signature or "").split("|")]
        wrong_sigs = [s for s in all_post_sigs if s != correct_sig]
        lines.append(f"  File:     {v.path}")
        lines.append(f"  Function: {v.qualified_name}")
        lines.append(f"  Keep:     {v.qualified_name}{correct_sig}")
        for ws in wrong_sigs:
            lines.append(f"  Remove:   {v.qualified_name}{ws}")
        lines.append("")
    return "\n".join(lines).rstrip()


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


def _has_unsimulatable_python_ops(ops: list[Any]) -> bool:
    supported = {"write_file", "replace_in_file", "append_file"}
    for op in ops:
        if not isinstance(op, dict):
            continue
        path = str(op.get("path") or "").strip().replace("\\", "/")
        if path.lstrip("./").endswith(".py") and op.get("op") not in supported:
            return True
    return False


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


def _collect_strict_function_sigs(tree: ast.Module) -> dict[str, list[str]]:
    """Return strict signatures for top-level functions and direct class methods."""
    result: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.setdefault(node.name, []).append(_strict_signature_fingerprint(node))
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualified_name = f"{node.name}.{child.name}"
                    result.setdefault(qualified_name, []).append(
                        _strict_signature_fingerprint(child)
                    )
    return result


def _strict_signature_fingerprint(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str:
    """Stable, AST-level source signature representation for completion repair."""
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    positional_defaults = [None] * (len(positional) - len(args.defaults)) + list(
        args.defaults
    )
    parameters: list[dict[str, Any]] = []

    for index, (argument, default) in enumerate(zip(positional, positional_defaults)):
        parameters.append(
            _strict_parameter(
                argument,
                kind=(
                    "positional_only"
                    if index < len(args.posonlyargs)
                    else "positional_or_keyword"
                ),
                default=default,
            )
        )
    if args.vararg:
        parameters.append(_strict_parameter(args.vararg, kind="vararg", default=None))
    for argument, default in zip(args.kwonlyargs, args.kw_defaults):
        parameters.append(
            _strict_parameter(argument, kind="keyword_only", default=default)
        )
    if args.kwarg:
        parameters.append(_strict_parameter(args.kwarg, kind="varkw", default=None))

    return json.dumps(
        {
            "parameters": parameters,
            "return_annotation": _ast_representation(node.returns),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _strict_parameter(
    argument: ast.arg,
    *,
    kind: str,
    default: ast.expr | None,
) -> dict[str, Any]:
    return {
        "annotation": _ast_representation(argument.annotation),
        "default": _ast_representation(default) if default is not None else None,
        "has_default": default is not None,
        "kind": kind,
        "name": argument.arg,
    }


def _ast_representation(node: ast.AST | None) -> str | None:
    return (
        ast.dump(node, annotate_fields=True, include_attributes=False)
        if node is not None
        else None
    )
