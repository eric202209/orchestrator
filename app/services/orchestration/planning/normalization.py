"""Deterministic plan contract completion for planning/repair output."""

from __future__ import annotations

import ast
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Tuple

from app.services.orchestration.operations.file_ops_contract import (
    REPLACE_IN_FILE_OLD_ALIASES,
    ReplaceOperationMode,
    classify_replace_operation,
)
from app.services.orchestration.planning.operation_repair_anchors import (
    DERIVATION_BLANK_LINE_TOLERANT,
    derive_operation_anchors,
)
from app.services.orchestration.planning.source_operation_verification import (
    resolve_version_fenced_source,
)


_WORKSPACE_TARGET_EXCLUDE_DIRS = {
    ".git",
    ".mypy_cache",
    ".agent",
    ".pytest_cache",
    "__pycache__",
    "dist",
    "node_modules",
    "venv",
}


def _path_text(value: Any) -> str:
    return str(value or "").strip().lstrip("./")


def _safe_relative_file_path(path: str) -> bool:
    if not path:
        return False
    posix_path = PurePosixPath(path)
    if posix_path.is_absolute():
        return False
    return ".." not in posix_path.parts


def _set_ops_preserving_absence(
    step: dict[str, Any], ops: list[dict[str, Any]]
) -> None:
    """Store ops only when the source step had ops or normalization created ops."""

    if ops or "ops" in step:
        step["ops"] = ops
    else:
        step.pop("ops", None)


def _workspace_file_paths(project_dir: Path) -> list[str]:
    paths: list[str] = []
    for path in project_dir.rglob("*"):
        try:
            relative = path.relative_to(project_dir)
        except ValueError:
            continue
        if any(part in _WORKSPACE_TARGET_EXCLUDE_DIRS for part in relative.parts):
            continue
        if path.is_file():
            paths.append(relative.as_posix())
    return sorted(paths)


def _common_suffix_part_count(left: PurePosixPath, right: PurePosixPath) -> int:
    count = 0
    for left_part, right_part in zip(reversed(left.parts), reversed(right.parts)):
        if left_part != right_part:
            break
        count += 1
    return count


def _unique_existing_workspace_target(
    requested_path: str,
    *,
    project_dir: Path,
    existing_files: list[str],
) -> str | None:
    normalized = _path_text(requested_path)
    if not _safe_relative_file_path(normalized):
        return None
    if (project_dir / normalized).exists():
        return None
    requested = PurePosixPath(normalized)
    if not requested.name or not requested.suffix:
        return None

    # Require at least one matching directory component when the requested path
    # has an explicit directory prefix. A pure basename match (score=1) when the
    # directories differ is a false positive: tests/__init__.py must not become
    # src/__init__.py just because both share the __init__.py filename.
    min_suffix_score = 2 if len(requested.parts) > 1 else 1

    scored: list[tuple[int, int, str]] = []
    for candidate_text in existing_files:
        candidate = PurePosixPath(candidate_text)
        if candidate.name != requested.name:
            continue
        suffix_score = _common_suffix_part_count(requested, candidate)
        if suffix_score < min_suffix_score:
            continue
        scored.append((suffix_score, len(candidate.parts), candidate_text))
    if not scored:
        return None
    best_score = max(score for score, _, _ in scored)
    best = [item for item in scored if item[0] == best_score]
    if len(best) != 1:
        return None
    return best[0][2]


def _replace_plan_path_text(value: Any, path_map: dict[str, str]) -> Any:
    if not isinstance(value, str) or not path_map:
        return value
    updated = value
    for old, new in sorted(
        path_map.items(), key=lambda item: len(item[0]), reverse=True
    ):
        path_pattern = re.compile(rf"(?<![\w./-])\.?/?{re.escape(old)}(?![\w./-])")
        updated = path_pattern.sub(new, updated)
    return updated


def normalize_existing_file_target_plan(
    plan: list[dict[str, Any]],
    *,
    project_dir: Path,
) -> Tuple[list[dict[str, Any]], Dict[str, Any]]:
    """Rewrite missing plan file targets to unique existing workspace files.

    This is a guarded workspace-evidence normalizer. It does not infer project
    content or create new target names; it only corrects path root drift when a
    planned missing file has exactly one best suffix match among existing files.
    """

    root = Path(project_dir)
    existing_files = _workspace_file_paths(root)
    if not existing_files:
        return plan, {"changed": False, "reason": "workspace_has_no_files"}

    requested_paths: list[str] = []
    for step in plan:
        if not isinstance(step, dict):
            continue
        for path in step.get("expected_files") or []:
            normalized = _path_text(path)
            if normalized:
                requested_paths.append(normalized)
        for op in step.get("ops") or []:
            if not isinstance(op, dict):
                continue
            if classify_replace_operation(op) is ReplaceOperationMode.SEMANTIC_REPLACE:
                # A version-bound selector is already an explicit target. Do
                # not rewrite its path using basename heuristics.
                continue
            if str(op.get("op") or "") not in {
                "append_file",
                "replace_in_file",
                "write_file",
            }:
                continue
            normalized = _path_text(op.get("path"))
            if normalized:
                requested_paths.append(normalized)

    path_map: dict[str, str] = {}
    for requested_path in requested_paths:
        rewritten = _unique_existing_workspace_target(
            requested_path,
            project_dir=root,
            existing_files=existing_files,
        )
        if rewritten and rewritten != requested_path:
            path_map[requested_path] = rewritten

    if not path_map:
        return plan, {"changed": False, "reason": "no_unique_existing_file_target"}

    normalized_plan: list[dict[str, Any]] = []
    changed = False
    for step in plan:
        if not isinstance(step, dict):
            normalized_plan.append(step)
            continue
        updated = dict(step)

        for field in ("description", "verification", "rollback"):
            original = updated.get(field)
            rewritten = _replace_plan_path_text(original, path_map)
            if rewritten != original:
                updated[field] = rewritten
                changed = True

        commands = []
        for command in updated.get("commands") or []:
            rewritten = _replace_plan_path_text(command, path_map)
            if rewritten != command:
                changed = True
            commands.append(rewritten)
        updated["commands"] = commands

        ops = []
        for op in updated.get("ops") or []:
            if not isinstance(op, dict):
                continue
            rewritten_op = dict(op)
            path_text = _path_text(rewritten_op.get("path"))
            rewritten_path = path_map.get(path_text, path_text)
            if rewritten_path != path_text:
                rewritten_op["path"] = rewritten_path
                changed = True
            ops.append(rewritten_op)
        _set_ops_preserving_absence(updated, ops)

        expected_files = []
        for path in updated.get("expected_files") or []:
            path_text = _path_text(path)
            rewritten = path_map.get(path_text, path_text)
            if rewritten != path_text:
                changed = True
            if rewritten and rewritten not in expected_files:
                expected_files.append(rewritten)
        updated["expected_files"] = expected_files
        normalized_plan.append(updated)

    return normalized_plan, {
        "changed": changed,
        "reason": (
            "existing_file_target_path_normalization"
            if changed
            else "path_map_not_referenced"
        ),
        "rewritten_paths": path_map,
    }


def _single_function_names_for_path(path: str, content: str) -> list[str]:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".py":
        return re.findall(r"(?m)^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", content)
    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        return re.findall(
            r"(?m)^(?:export\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
            content,
        )
    return []


def _can_convert_stale_replace_to_small_file_write(
    *,
    path: str,
    current_content: str,
    old_text: str,
    new_text: str,
) -> bool:
    if old_text in current_content:
        return False
    if not new_text.strip():
        return False
    if len(current_content) > 20_000 or len(new_text) > 20_000:
        return False
    if len(current_content.splitlines()) > 80:
        return False
    current_names = _single_function_names_for_path(path, current_content)
    new_names = _single_function_names_for_path(path, new_text)
    if len(current_names) != 1 or len(new_names) != 1:
        return False
    if current_names[0] != new_names[0]:
        return False
    current_imports = [
        line.strip()
        for line in current_content.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    return all(line in new_text for line in current_imports)


def _single_return_line(lines: list[str]) -> tuple[int, str] | None:
    matches = [
        (index, line)
        for index, line in enumerate(lines)
        if line.strip().startswith("return ")
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _python_name_identifiers(text: str) -> set[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def _return_line_uses_only_known_names(
    *,
    current_content: str,
    new_return_line: str,
) -> bool:
    new_names = _python_name_identifiers(
        "def _openclaw_return_probe():\n    " + new_return_line.strip() + "\n"
    )
    if not new_names:
        return True
    current_names = _python_name_identifiers(current_content)
    new_names.discard("_openclaw_return_probe")
    allowed_builtins = {
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "float",
        "int",
        "len",
        "list",
        "max",
        "min",
        "round",
        "set",
        "str",
        "sum",
        "tuple",
    }
    unknown_names = new_names - current_names - allowed_builtins
    return not unknown_names


def _synthesize_single_return_file_write(
    *,
    path: str,
    current_content: str,
    old_text: str,
    new_text: str,
) -> str | None:
    if old_text in current_content:
        return None
    if len(current_content) > 20_000 or len(new_text) > 4_000:
        return None
    if len(current_content.splitlines()) > 80:
        return None
    current_names = _single_function_names_for_path(path, current_content)
    if len(current_names) != 1:
        return None
    new_names = _single_function_names_for_path(path, new_text)
    if new_names and new_names != current_names:
        return None
    current_lines = current_content.splitlines()
    current_return = _single_return_line(current_lines)
    new_return = _single_return_line(new_text.splitlines())
    if not current_return or not new_return:
        return None
    current_index, current_line = current_return
    _, new_line = new_return
    if PurePosixPath(path).suffix.lower() == ".py" and not (
        _return_line_uses_only_known_names(
            current_content=current_content,
            new_return_line=new_line,
        )
    ):
        return None
    indent = current_line[: len(current_line) - len(current_line.lstrip())]
    current_lines[current_index] = indent + new_line.strip()
    trailing_newline = "\n" if current_content.endswith("\n") else ""
    return "\n".join(current_lines) + trailing_newline


MAX_ANCHOR_REALIGNMENT_SOURCE_CHARS = 500_000


def _significant_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _legacy_old_key(operation: dict[str, Any]) -> str | None:
    """Return the key this operation actually stores its legacy anchor under."""

    if isinstance(operation.get("old"), str):
        return "old"
    present = [
        key
        for key in REPLACE_IN_FILE_OLD_ALIASES
        if isinstance(operation.get(key), str)
    ]
    return present[0] if len(present) == 1 else None


def _blank_line_realigned_anchor(
    *,
    step_number: int,
    operation_index: int,
    relative_path: str,
    version_identity: str,
    old_text: str,
    new_text: Any,
    full_source: str,
) -> str | None:
    """Return the exact source region that differs from ``old_text`` only by blank lines.

    ``derive_operation_anchors`` is the existing Orchestrator-owned anchor
    authority, but only its blank-line-tolerant derivation is substitutable
    here.  The minimal-divergent derivation deliberately returns a *smaller*
    region than the operation addressed, which is sound only in operation
    repair, where the model re-authors ``new`` for the chosen anchor.  Injecting
    it while keeping the model's original ``new`` would replace a trimmed region
    with the full replacement text and corrupt the file, so it is excluded.
    """

    anchors = derive_operation_anchors(
        step_number=step_number,
        operation_index=operation_index,
        relative_path=relative_path,
        version_identity=version_identity,
        original_old=old_text,
        original_new=new_text,
        full_source=full_source,
    )
    for anchor in anchors:
        if anchor.derivation != DERIVATION_BLANK_LINE_TOLERANT:
            continue
        # Re-checked here rather than trusted: no non-blank line outside the
        # model's own ``old`` may enter the anchor, and the anchor must occur
        # exactly once in the version-fenced source.
        if _significant_lines(anchor.text) != _significant_lines(old_text):
            continue
        if full_source.count(anchor.text) != 1:
            continue
        return anchor.text
    return None


def normalize_blank_line_divergent_replace_anchors(
    plan: list[dict[str, Any]],
    *,
    project_dir: Path,
    source_materialization: Any,
) -> Tuple[list[dict[str, Any]], Dict[str, Any]]:
    """Restore the file's own blank lines into legacy ``replace_in_file`` anchors.

    The model is asked for *which* region to change; it is not a reliable
    serializer of the exact current bytes of that region, and the whitespace
    between a file's own line groups is the part it most often drops.  Where the
    version-fenced current source contains exactly one region whose significant
    lines are precisely the model's ``old``, that region — not the model's
    reconstruction — is the authoritative anchor, and it is substituted here.

    Everything else fails closed exactly as before: no fuzzy or first match, no
    path change, no whole-file escalation, no anchor invented when the model
    supplied none, and no operation touched once an earlier operation in the
    same plan has already mutated that path, because the in-plan buffer rather
    than the disk is the authority there.
    """

    root = Path(project_dir).resolve()
    changed = False
    normalized_anchors: list[dict[str, Any]] = []
    mutated_paths: set[str] = set()
    normalized: list[dict[str, Any]] = []

    for step_index, step in enumerate(plan, start=1):
        if not isinstance(step, dict):
            normalized.append(step)
            continue
        step_number = step.get("step_number")
        if isinstance(step_number, bool) or not isinstance(step_number, int):
            step_number = step_index
        updated = dict(step)
        ops: list[dict[str, Any]] = []
        for operation_index, op in enumerate(updated.get("ops") or [], start=1):
            if not isinstance(op, dict):
                continue
            rewritten_op = dict(op)
            ops.append(rewritten_op)
            path_text = _path_text(rewritten_op.get("path"))
            op_name = str(rewritten_op.get("op") or "").strip()
            if op_name != "replace_in_file":
                if op_name in {"write_file", "append_file"} and path_text:
                    mutated_paths.add(path_text)
                continue
            already_mutated = path_text in mutated_paths
            if path_text:
                mutated_paths.add(path_text)
            if already_mutated:
                continue
            if (
                classify_replace_operation(rewritten_op)
                is not ReplaceOperationMode.LEGACY_REPLACE
            ):
                continue
            # A stray `target_id` alongside a legacy anchor is a Phase 33
            # contract failure heading for its own deterministic rejection.
            # It must never be rescued into a working legacy replace here.
            if "target_id" in rewritten_op or "selector" in rewritten_op:
                continue
            if not _safe_relative_file_path(path_text):
                continue
            old_key = _legacy_old_key(rewritten_op)
            if old_key is None:
                continue
            old_text = rewritten_op[old_key]
            if not old_text:
                continue
            resolved = resolve_version_fenced_source(
                source_materialization, path_text, root
            )
            if resolved.failure_code is not None:
                continue
            full_source = resolved.full_content
            if not full_source or len(full_source) > (
                MAX_ANCHOR_REALIGNMENT_SOURCE_CHARS
            ):
                continue
            if old_text in full_source:
                continue
            anchor_text = _blank_line_realigned_anchor(
                step_number=step_number,
                operation_index=operation_index,
                relative_path=path_text,
                version_identity=resolved.recorded_version_identity or "",
                old_text=old_text,
                new_text=rewritten_op.get("new"),
                full_source=full_source,
            )
            if anchor_text is None:
                continue
            rewritten_op[old_key] = anchor_text
            changed = True
            normalized_anchors.append(
                {
                    "step_number": step_number,
                    "operation_index": operation_index,
                    "path": path_text,
                    "derivation": DERIVATION_BLANK_LINE_TOLERANT,
                    "source_version_identity": resolved.recorded_version_identity,
                }
            )
        _set_ops_preserving_absence(updated, ops)
        normalized.append(updated)

    return normalized, {
        "changed": changed,
        "reason": (
            "blank_line_divergent_replace_anchor_realigned"
            if changed
            else "no_blank_line_divergent_replace_anchor"
        ),
        "normalized_anchors": normalized_anchors,
    }


def normalize_stale_replace_ops_to_small_file_writes(
    plan: list[dict[str, Any]],
    *,
    project_dir: Path,
) -> Tuple[list[dict[str, Any]], Dict[str, Any]]:
    """Convert safe stale exact-replace ops into full small-file writes.

    This handles stale patch output for tiny single-function modules. It only
    fires when the target exists, the exact old text is absent, and the new
    content contains the same complete function as the current file.
    """

    root = Path(project_dir).resolve()
    changed = False
    converted_paths: list[str] = []
    normalized: list[dict[str, Any]] = []
    for step in plan:
        if not isinstance(step, dict):
            normalized.append(step)
            continue
        updated = dict(step)
        ops = []
        for op in updated.get("ops") or []:
            if not isinstance(op, dict):
                continue
            rewritten_op = dict(op)
            if str(rewritten_op.get("op") or "") != "replace_in_file":
                ops.append(rewritten_op)
                continue
            if (
                classify_replace_operation(rewritten_op)
                is not ReplaceOperationMode.LEGACY_REPLACE
            ):
                # Semantic and mixed replace operations never enter the
                # stale-old-to-write compatibility normalizer.
                ops.append(rewritten_op)
                continue
            path_text = _path_text(rewritten_op.get("path"))
            if not _safe_relative_file_path(path_text):
                ops.append(rewritten_op)
                continue
            target = (root / path_text).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                ops.append(rewritten_op)
                continue
            if not target.is_file():
                ops.append(rewritten_op)
                continue
            try:
                current_content = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                ops.append(rewritten_op)
                continue
            old_text = str(rewritten_op.get("old") or "")
            new_text = str(rewritten_op.get("new") or "")
            if _can_convert_stale_replace_to_small_file_write(
                path=path_text,
                current_content=current_content,
                old_text=old_text,
                new_text=new_text,
            ):
                content = new_text
            else:
                content = _synthesize_single_return_file_write(
                    path=path_text,
                    current_content=current_content,
                    old_text=old_text,
                    new_text=new_text,
                )
            if content is None:
                ops.append(rewritten_op)
                continue
            rewritten_op = {
                "op": "write_file",
                "path": path_text,
                "content": content,
            }
            converted_paths.append(path_text)
            changed = True
            ops.append(rewritten_op)
        _set_ops_preserving_absence(updated, ops)
        normalized.append(updated)

    return normalized, {
        "changed": changed,
        "reason": (
            "stale_replace_small_file_write_fallback"
            if changed
            else "no_safe_stale_replace_fallback"
        ),
        "converted_paths": list(dict.fromkeys(converted_paths)),
    }
