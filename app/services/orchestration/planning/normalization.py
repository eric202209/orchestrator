"""Deterministic plan contract completion for planning/repair output."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Tuple


_STATIC_SITE_EXTENSIONS = {".html", ".css", ".svg"}
_WORKSPACE_TARGET_EXCLUDE_DIRS = {
    ".git",
    ".mypy_cache",
    ".openclaw",
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


def _static_site_linkage_command(paths: list[str]) -> str | None:
    normalized_paths = [_path_text(path) for path in paths if _path_text(path)]
    index_paths = [
        path for path in normalized_paths if PurePosixPath(path).name == "index.html"
    ]
    if not index_paths:
        return None
    index_path = index_paths[0]
    index_parent = PurePosixPath(index_path).parent
    css_paths = [
        path
        for path in normalized_paths
        if PurePosixPath(path).suffix.lower() == ".css"
    ]
    svg_paths = [
        path
        for path in normalized_paths
        if PurePosixPath(path).suffix.lower() == ".svg"
    ]

    content_needles: list[str] = []
    for path in css_paths + svg_paths:
        try:
            relative = PurePosixPath(path).relative_to(index_parent)
        except ValueError:
            relative = PurePosixPath(path).name
        needle = str(relative)
        if needle not in content_needles:
            content_needles.append(needle)

    script = (
        "import pathlib,sys; "
        f"paths={json.dumps(normalized_paths)}; "
        f"index={json.dumps(index_path)}; "
        f"needles={json.dumps(content_needles)}; "
        "ok=all(pathlib.Path(p).is_file() and pathlib.Path(p).stat().st_size > 0 for p in paths); "
        "content=pathlib.Path(index).read_text(encoding='utf-8') if pathlib.Path(index).is_file() else ''; "
        "ok=ok and all(needle in content for needle in needles); "
        "sys.exit(0 if ok else 1)"
    )
    return "python -c " + json.dumps(script)


def _step_static_site_linkage_command(
    write_paths: list[str],
    plan_static_linkage_verification: str | None,
) -> str | None:
    """Return a linkage check only when this step owns the linked file set.

    A plan-wide linkage check is useful for final read-only verification steps,
    but it is too broad for partial materialization steps. A step that only
    writes an SVG must not verify index.html/css before later steps create them.
    """

    if not write_paths:
        return plan_static_linkage_verification
    return _static_site_linkage_command(write_paths)


def _verification_mentions_static_site_linkage(command: Any) -> bool:
    text = str(command or "").lower()
    return "index.html" in text and (
        "link rel" in text
        or "stylesheet" in text
        or "img src" in text
        or ".svg" in text
        or "css/style.css" in text
    )


def _static_html_content_verification_command(command: Any) -> str | None:
    text = str(command or "")
    if ".html" not in text or " in content" not in text:
        return None
    path_match = re.search(
        r"pathlib\.Path\((?P<quote>['\"])(?P<path>[^'\"]+\.html)(?P=quote)\)",
        text,
    )
    if not path_match:
        return None
    path = path_match.group("path")
    needles: list[str] = []
    for match in re.finditer(
        r"(?P<quote>['\"])(?P<needle>[^'\"]+)(?P=quote)\s+in\s+content",
        text,
    ):
        needle = match.group("needle")
        if re.fullmatch(r"\.[A-Za-z0-9_-]+", needle):
            needle = needle[1:]
        if needle and needle not in needles:
            needles.append(needle)
    if not needles:
        return None
    script = (
        "import pathlib,sys; "
        f"path={json.dumps(path)}; "
        f"needles={json.dumps(needles)}; "
        "content=pathlib.Path(path).read_text(encoding='utf-8'); "
        "sys.exit(0 if all(needle in content for needle in needles) else 1)"
    )
    return "python -c " + json.dumps(script)


def _has_malformed_shell_quoting(command: Any) -> bool:
    text = str(command or "").strip()
    if not text:
        return False
    try:
        shlex.split(text, posix=True)
    except ValueError:
        return True
    return False


def _file_contains_command(path: str, needle: str) -> str:
    script = (
        "import pathlib,sys; "
        f"content=pathlib.Path({json.dumps(path)}).read_text(); "
        f"sys.exit(0 if {json.dumps(needle)} in content else 1)"
    )
    return "python -c " + json.dumps(script)


def _static_site_roots_from_plan(
    plan: list[dict[str, Any]], project_dir: Path
) -> list[str]:
    roots: list[str] = []
    for step in plan:
        if not isinstance(step, dict):
            continue
        candidate_paths = [
            _path_text(path)
            for path in (step.get("expected_files") or [])
            if _path_text(path)
        ]
        candidate_paths.extend(_materialized_write_paths(step))
        for op in step.get("ops") or []:
            if not isinstance(op, dict):
                continue
            if str(op.get("op") or "") not in {
                "append_file",
                "replace_in_file",
                "write_file",
            }:
                continue
            path = _path_text(op.get("path"))
            if path:
                candidate_paths.append(path)
        for path in candidate_paths:
            posix_path = PurePosixPath(path)
            parts = posix_path.parts
            if not parts:
                continue
            if parts[-1] == "index.html":
                root = str(PurePosixPath(*parts[:-1])) if len(parts) > 1 else ""
            elif len(parts) >= 2 and parts[-2:] == ("css", "style.css"):
                root = str(PurePosixPath(*parts[:-2])) if len(parts) > 2 else ""
            else:
                continue
            if (
                (project_dir / root / "index.html").is_file()
                and (project_dir / root / "css" / "style.css").is_file()
                and root not in roots
            ):
                roots.append(root)
    if (
        (project_dir / "index.html").is_file()
        and (project_dir / "css" / "style.css").is_file()
        and "" not in roots
    ):
        roots.append("")
    if not roots:
        public_dir = project_dir / "public"
        if public_dir.is_dir():
            for child in sorted(public_dir.iterdir()):
                if not child.is_dir():
                    continue
                if (child / "index.html").is_file() and (
                    child / "css" / "style.css"
                ).is_file():
                    roots.append(str(PurePosixPath("public", child.name)))
    return roots


def _rooted_path(root: str, path: str) -> str:
    if not root:
        return path
    return str(PurePosixPath(root, path))


def _relative_to_static_root(path: str, roots: list[str]) -> str:
    normalized = _path_text(path)
    for root in sorted((r for r in roots if r), key=len, reverse=True):
        prefix = f"{root}/"
        if normalized.startswith(prefix):
            return normalized[len(prefix) :]
    return normalized


def _referenced_static_assets(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    assets = []
    for match in re.findall(r"[\w./-]+\.svg", value, flags=re.IGNORECASE):
        asset_name = PurePosixPath(match).name
        if asset_name and asset_name not in assets:
            assets.append(asset_name)
    return assets


def _looks_like_complete_html_document(content: Any) -> bool:
    text = str(content or "").lower()
    return "<!doctype" in text or "<html" in text


def _html_reference_for_asset(
    root: Path, root_prefix: str, asset_name: str
) -> str | None:
    html_path = root / root_prefix / "index.html"
    if not html_path.is_file():
        return None
    try:
        content = html_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if asset_name not in content:
        return None
    return _rooted_path(root_prefix, "index.html")


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

    scored: list[tuple[int, int, str]] = []
    for candidate_text in existing_files:
        candidate = PurePosixPath(candidate_text)
        if candidate.name != requested.name:
            continue
        suffix_score = _common_suffix_part_count(requested, candidate)
        if suffix_score < 1:
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
        updated["ops"] = ops

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
    indent = current_line[: len(current_line) - len(current_line.lstrip())]
    current_lines[current_index] = indent + new_line.strip()
    trailing_newline = "\n" if current_content.endswith("\n") else ""
    return "\n".join(current_lines) + trailing_newline


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
        updated["ops"] = ops
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
    static_roots = _static_site_roots_from_plan(plan, root)
    if not static_roots:
        return plan, {"changed": False, "reason": "static_site_root_not_present"}

    path_map = {
        "index.html": "index.html",
        "style.css": "css/style.css",
        "styles.css": "css/style.css",
        "css/style.css": "css/style.css",
        "src/index.js": "index.html",
        "src/main.js": "index.html",
        "src/App.js": "index.html",
        "src/App.jsx": "index.html",
        "src/index.css": "css/style.css",
        "src/App.css": "css/style.css",
    }
    rooted_path_map: dict[str, str] = {}
    for static_root in static_roots:
        for old, new in path_map.items():
            rooted_path_map[old] = _rooted_path(static_root, new)
            rooted_path_map[_rooted_path(static_root, old)] = _rooted_path(
                static_root, new
            )
    build_check = _verification_command(
        [
            _rooted_path(static_roots[0], "index.html"),
            _rooted_path(static_roots[0], "css/style.css"),
        ]
    )

    def normalize_asset_verification(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if "background-image" not in value and "url(" not in value:
            return value
        referenced_assets = _referenced_static_assets(value)
        if not referenced_assets:
            return value
        for asset_name in referenced_assets:
            for static_root in static_roots:
                html_path = _html_reference_for_asset(root, static_root, asset_name)
                if html_path:
                    return _file_contains_command(html_path, asset_name)
        return value

    def replace_text(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        updated = value
        for old, new in sorted(
            rooted_path_map.items(), key=lambda item: len(item[0]), reverse=True
        ):
            path_pattern = re.compile(rf"(?<![\w./-])\.?/?{re.escape(old)}(?![\w./-])")
            updated = path_pattern.sub(new, updated)
        if re.fullmatch(r"\s*(npm|pnpm|yarn)\s+run\s+build\s*", updated) or (
            "npm" in updated and "run" in updated and "build" in updated
        ):
            updated = build_check
        return normalize_asset_verification(updated)

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
            relative_path = _relative_to_static_root(path_text, static_roots)
            rewritten_path = rooted_path_map.get(path_text, path_text)
            rewritten_path = (
                _rooted_path(static_roots[0], path_map[relative_path])
                if relative_path in path_map and path_text == relative_path
                else rewritten_path
            )
            if rewritten_path != path_text:
                rewritten_op["path"] = rewritten_path
                rewritten_paths[path_text] = rewritten_path
                changed = True
            if (
                str(rewritten_op.get("op") or "") == "write_file"
                and PurePosixPath(rewritten_path).suffix.lower() == ".html"
                and (root / rewritten_path).is_file()
                and not _looks_like_complete_html_document(rewritten_op.get("content"))
            ):
                rewritten_op["op"] = "append_file"
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
            relative_path = _relative_to_static_root(path_text, static_roots)
            rewritten = rooted_path_map.get(path_text, path_text)
            rewritten = (
                _rooted_path(static_roots[0], path_map[relative_path])
                if relative_path in path_map and path_text == relative_path
                else rewritten
            )
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
    plan_static_linkage_verification = _static_site_linkage_command(
        list(dict.fromkeys(all_write_paths + declared_expected_paths))
    )
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

        verification = str(updated.get("verification") or "").strip()
        linkage_verification = _step_static_site_linkage_command(
            write_paths,
            plan_static_linkage_verification,
        )
        html_verification = _static_html_content_verification_command(verification)
        if html_verification:
            updated["verification"] = html_verification
            verification = html_verification
            changed = True

        if (
            not write_paths
            and not verification
            and linkage_verification
            and all_write_paths
        ):
            updated["verification"] = linkage_verification
            added_verifications.append(step_index)
            changed = True
        elif write_paths and not verification:
            updated["verification"] = linkage_verification or _verification_command(
                write_paths
            )
            added_verifications.append(step_index)
            changed = True
        elif linkage_verification and _verification_mentions_static_site_linkage(
            verification
        ):
            updated["verification"] = linkage_verification
            changed = True
        if linkage_verification:
            commands = []
            commands_changed = False
            for command in updated.get("commands") or []:
                html_command = _static_html_content_verification_command(command)
                if html_command:
                    commands.append(html_command)
                    commands_changed = True
                elif _verification_mentions_static_site_linkage(command):
                    commands.append(linkage_verification)
                    commands_changed = True
                else:
                    commands.append(command)
            if commands_changed:
                updated["commands"] = list(dict.fromkeys(commands))
                changed = True

        if ops and _has_malformed_shell_quoting(updated.get("rollback")):
            updated["rollback"] = "true"
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
