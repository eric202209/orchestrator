"""Deterministic plan contract completion for planning/repair output."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any, Dict, List, Tuple


_STATIC_SITE_EXTENSIONS = {".html", ".css", ".svg", ".js"}


def _path_text(value: Any) -> str:
    return str(value or "").strip().lstrip("./")


def _is_static_site_path(path: str) -> bool:
    suffix = PurePosixPath(path).suffix.lower()
    return suffix in _STATIC_SITE_EXTENSIONS


def _verification_command(paths: list[str]) -> str:
    script = (
        "import pathlib,sys; "
        f"paths={json.dumps(paths)}; "
        "sys.exit(0 if all(pathlib.Path(p).is_file() and "
        "pathlib.Path(p).stat().st_size > 0 for p in paths) else 1)"
    )
    return "python -c " + json.dumps(script)


def _materialized_write_paths(step: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for op in step.get("ops") or []:
        if not isinstance(op, dict):
            continue
        if str(op.get("op") or "") != "write_file":
            continue
        path = _path_text(op.get("path"))
        if path:
            paths.append(path)
    return list(dict.fromkeys(paths))


def complete_repaired_plan_contract(
    plan: list[dict[str, Any]],
    *,
    task_prompt: str = "",
    repaired: bool = False,
) -> Tuple[list[dict[str, Any]], Dict[str, Any]]:
    """Fill deterministic static-site plan contract gaps.

    This is intentionally narrow. It does not invent task content. It only
    completes structural fields around already-declared typed file writes.
    """

    changed = False
    added_parent_dirs: list[str] = []
    added_expected_files: list[str] = []
    added_verifications: list[int] = []

    prompt = (task_prompt or "").lower()
    prompt_mentions_static_site = any(
        marker in prompt for marker in ("html", "css", "svg", "static site")
    )
    all_write_paths = [
        path
        for step in plan
        if isinstance(step, dict)
        for path in _materialized_write_paths(step)
    ]
    if not prompt_mentions_static_site and not any(
        _is_static_site_path(path) for path in all_write_paths
    ):
        return plan, {"changed": False, "reason": "not_static_site_shape"}

    completed: list[dict[str, Any]] = []
    created_dirs: set[str] = set()
    for step_index, step in enumerate(plan, start=1):
        updated = dict(step)
        ops = [dict(op) for op in (updated.get("ops") or []) if isinstance(op, dict)]
        write_paths = _materialized_write_paths({"ops": ops})

        parent_dirs = []
        for path in write_paths:
            parent = str(PurePosixPath(path).parent)
            if parent and parent != "." and parent not in created_dirs:
                parent_dirs.append(parent)
                created_dirs.add(parent)
        if parent_dirs:
            mkdir_ops = [{"op": "mkdir", "path": parent} for parent in parent_dirs]
            ops = mkdir_ops + ops
            added_parent_dirs.extend(parent_dirs)
            changed = True

        expected_files = [
            _path_text(path)
            for path in (updated.get("expected_files") or [])
            if _path_text(path)
        ]
        for path in write_paths:
            if path not in expected_files:
                expected_files.append(path)
                added_expected_files.append(path)
                changed = True

        if write_paths and not str(updated.get("verification") or "").strip():
            updated["verification"] = _verification_command(write_paths)
            added_verifications.append(step_index)
            changed = True

        updated["ops"] = ops
        updated["expected_files"] = expected_files
        completed.append(updated)

    return completed, {
        "changed": changed,
        "reason": "static_site_contract_completion" if changed else "no_gaps_found",
        "repaired": repaired,
        "added_parent_dirs": list(dict.fromkeys(added_parent_dirs)),
        "added_expected_files": list(dict.fromkeys(added_expected_files)),
        "added_verifications": added_verifications,
    }
