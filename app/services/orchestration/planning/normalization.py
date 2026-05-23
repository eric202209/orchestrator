"""Deterministic plan contract completion for planning/repair output."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
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


def _file_contains_command(path: str, needle: str) -> str:
    script = (
        "import pathlib,sys; "
        f"content=pathlib.Path({json.dumps(path)}).read_text(); "
        f"sys.exit(0 if {json.dumps(needle)} in content else 1)"
    )
    return "python -c " + json.dumps(script)


def _referenced_static_assets(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    assets = []
    for match in re.findall(r"[\w./-]+\.svg", value, flags=re.IGNORECASE):
        asset_name = PurePosixPath(match).name
        if asset_name and asset_name not in assets:
            assets.append(asset_name)
    return assets


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


def normalize_existing_static_site_plan(
    plan: list[dict[str, Any]],
    *,
    project_dir: Path,
) -> Tuple[list[dict[str, Any]], Dict[str, Any]]:
    """Map common framework drift back onto an existing plain static site.

    Lower-capability lanes sometimes see `index.html` + `css/style.css` and
    still plan React/Vite-style edits against `src/index.js`, `src/index.css`,
    followed by `npm run build`. For a plain static-site workspace that already
    has canonical root files, this is a deterministic path mistake, not a new
    implementation stack request.
    """

    root = Path(project_dir)
    if not (root / "index.html").is_file() or not (root / "css/style.css").is_file():
        return plan, {"changed": False, "reason": "static_site_root_not_present"}

    path_map = {
        "src/index.js": "index.html",
        "src/main.js": "index.html",
        "src/App.js": "index.html",
        "src/App.jsx": "index.html",
        "src/index.css": "css/style.css",
        "src/App.css": "css/style.css",
    }
    build_check = _verification_command(["index.html", "css/style.css"])

    def replace_text(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        updated = value
        for old, new in path_map.items():
            updated = updated.replace(old, new)
            updated = updated.replace("./" + old, new)
        if re.fullmatch(r"\s*(npm|pnpm|yarn)\s+run\s+build\s*", updated) or (
            "npm" in updated and "run" in updated and "build" in updated
        ):
            updated = build_check
        return updated

    changed = False
    rewritten_paths: dict[str, str] = {}
    removed_node_build_steps: list[int] = []
    normalized: list[dict[str, Any]] = []
    for index, step in enumerate(plan, start=1):
        updated = dict(step)

        for field in ("description", "verification", "rollback"):
            original = updated.get(field)
            rewritten = replace_text(original)
            if rewritten != original:
                updated[field] = rewritten
                changed = True

        commands = []
        for command in updated.get("commands") or []:
            rewritten = replace_text(command)
            if rewritten != command:
                changed = True
            commands.append(rewritten)
            if rewritten == build_check:
                removed_node_build_steps.append(int(updated.get("step_number", index)))
        ops = []
        for op in updated.get("ops") or []:
            if not isinstance(op, dict):
                continue
            rewritten_op = dict(op)
            path_text = _path_text(rewritten_op.get("path"))
            rewritten_path = path_map.get(path_text, path_text)
            if rewritten_path != path_text:
                rewritten_op["path"] = rewritten_path
                rewritten_paths[path_text] = rewritten_path
                changed = True
            ops.append(rewritten_op)
        if (
            ops
            and commands
            and all(
                str(command or "").strip().startswith("python -c ")
                for command in commands
            )
        ):
            commands = []
            changed = True
        updated["commands"] = commands

        expected_files = []
        for path in updated.get("expected_files") or []:
            path_text = _path_text(path)
            rewritten = path_map.get(path_text, path_text)
            if rewritten != path_text:
                rewritten_paths[path_text] = rewritten
                changed = True
            if rewritten not in expected_files:
                expected_files.append(rewritten)
        updated["expected_files"] = expected_files

        updated["ops"] = ops
        for op in ops:
            if str(op.get("op") or "") not in {
                "append_file",
                "write_file",
                "replace_in_file",
            }:
                continue
            op_path = _path_text(op.get("path"))
            if PurePosixPath(op_path).suffix.lower() not in {".html", ".css"}:
                continue
            referenced_assets = _referenced_static_assets(op.get("content"))
            if not referenced_assets:
                continue
            asset_name = referenced_assets[0]
            normalized_verification = _file_contains_command(op_path, asset_name)
            if updated.get("verification") != normalized_verification:
                updated["verification"] = normalized_verification
                changed = True
            break
        normalized.append(updated)

    return normalized, {
        "changed": changed,
        "reason": (
            "existing_static_site_path_normalization"
            if changed
            else "no_framework_drift"
        ),
        "rewritten_paths": rewritten_paths,
        "removed_node_build_steps": sorted(set(removed_node_build_steps)),
    }


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
    declared_expected_paths = [
        _path_text(path)
        for step in plan
        if isinstance(step, dict)
        for path in (step.get("expected_files") or [])
        if _path_text(path)
    ]
    all_write_paths = [
        path
        for step in plan
        if isinstance(step, dict)
        for path in _materialized_write_paths(step)
    ]
    if not prompt_mentions_static_site and not any(
        _is_static_site_path(path) for path in all_write_paths + declared_expected_paths
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
