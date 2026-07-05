"""Workspace materialization checks for orchestration validation."""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .placeholder_policy import path_allows_placeholder_fixture_content

SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".scss",
    ".svg",
    ".sh",
}
DOC_NAMES = {"readme.md", "notes.md", "summary.md"}
ROOT_LEVEL_EXPECTED_DIRS = {
    "src",
    "tests",
    "test",
    "fixtures",
    "config",
    "docs",
    "scripts",
    "lib",
    "app",
    "spec",
    "frontend",
    "backend",
    "assets",
    "public",
    "static",
    "images",
    "img",
    "css",
    "js",
    ".github",
}
NESTED_PROJECT_STRUCTURAL_DIRS = {
    "src",
    "app",
    "public",
    "static",
    "assets",
    "frontend",
    "backend",
    "tests",
    "test",
    "docs",
    "scripts",
    "lib",
    "spec",
}


def _path_allows_fixture_placeholder_content(path: Path) -> bool:
    return path_allows_placeholder_fixture_content(path)


def _path_allows_todo_fixme_literals(path: Path) -> bool:
    lowered_name = path.name.lower()
    return "todo" in lowered_name or "fixme" in lowered_name


def _python_has_stub_pass(content: str) -> bool:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return True

    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or len(body) != 1:
            continue
        if isinstance(body[0], ast.Pass) and isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            return True
    return False


def uses_brittle_python_inline_command(command: str) -> bool:
    """Shared brittle-inline-Python detection (validator rule + bootstrap-contract normalization)."""

    raw = str(command or "").strip()
    lowered = raw.lower()
    if "python -c" not in lowered and "python3 -c" not in lowered:
        return False
    if "stdin.read(" in lowered and not re.search(r"(^|[^<>])\|([^|]|$)|<", raw):
        return True

    quote_chars = raw.count('"') + raw.count("'")
    has_nested_python_content = any(
        marker in raw
        for marker in (
            "f'",
            'f"',
            "json.dumps(",
            "assert ",
            "{",
            "}",
        )
    )
    return quote_chars >= 4 and has_nested_python_content


def extract_inline_python_dash_c_script(command: str) -> Optional[str]:
    """Return the script text from a bare `python[3] -c "<script>"` command.

    Returns ``None`` when the command is not exactly `<interpreter> -c <script>`
    (e.g. it has extra CLI arguments after the script, or fails to shell-parse),
    so callers can conservatively skip rewriting anything they cannot losslessly
    reconstruct.
    """

    raw = str(command or "").strip()
    lowered = raw.lower()
    if not lowered.startswith(("python -c", "python3 -c")):
        return None
    try:
        parts = shlex.split(raw)
    except ValueError:
        return None
    if len(parts) != 3 or parts[1] != "-c":
        return None
    return parts[2]


def iter_candidate_files(project_dir: Path, file_paths: Iterable[str]) -> List[Path]:
    candidates: List[Path] = []
    for raw_path in file_paths:
        relative = str(raw_path or "").strip().rstrip("/")
        if not relative:
            continue
        candidate = (project_dir / relative).resolve()
        if candidate.exists() and candidate.is_file():
            candidates.append(candidate)
    return candidates


def find_nested_expected_file_matches(
    project_dir: Path, file_paths: Iterable[str]
) -> Dict[str, List[str]]:
    """Look one project-folder level deeper for misplaced generated files."""

    nested_matches: Dict[str, List[str]] = {}
    expected_top_levels = {
        Path(str(raw_path or "").strip().rstrip("/")).parts[0]
        for raw_path in file_paths
        if str(raw_path or "").strip().rstrip("/")
        and len(Path(str(raw_path or "").strip().rstrip("/")).parts) > 1
    }
    top_level_dirs = (
        [
            child
            for child in project_dir.iterdir()
            if child.is_dir()
            and child.name not in ROOT_LEVEL_EXPECTED_DIRS
            and child.name not in expected_top_levels
            and not child.name.startswith(".")
        ]
        if project_dir.exists()
        else []
    )

    for raw_path in file_paths:
        relative = str(raw_path or "").strip().rstrip("/")
        if not relative:
            continue
        for candidate_root in top_level_dirs:
            nested_candidate = (candidate_root / relative).resolve()
            if nested_candidate.exists() and nested_candidate.is_file():
                nested_matches.setdefault(candidate_root.name, []).append(relative)
    return nested_matches


def detect_placeholder_content(path: Path) -> List[str]:
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return []

    reasons: List[str] = []
    lowered = content.lower()
    fixture_placeholder_content = _path_allows_fixture_placeholder_content(path)
    todo_fixme_literals = _path_allows_todo_fixme_literals(path)
    if re.search(r"^\s*pass\s*$", content, flags=re.MULTILINE) and (
        path.suffix != ".py" or _python_has_stub_pass(content)
    ):
        reasons.append(f"{path.name} still contains `pass` placeholders")
    if (
        (
            "todo" in lowered
            and not fixture_placeholder_content
            and not todo_fixme_literals
        )
        or (
            "fixme" in lowered
            and not fixture_placeholder_content
            and not todo_fixme_literals
        )
        or ("placeholder" in lowered and not fixture_placeholder_content)
    ):
        reasons.append(f"{path.name} still contains TODO or placeholder markers")
    if "notimplemented" in lowered or "raise notimplementederror" in lowered:
        reasons.append(f"{path.name} still contains not-implemented markers")
    has_main_guard = re.search(
        r"if\s+__name__\s*==\s*(['\"])__main__\1\s*:",
        content,
    )
    if "__main__" in content and not has_main_guard:
        reasons.append(f"{path.name} has a broken Python __main__ entrypoint check")
    if path.suffix == ".py":
        try:
            ast.parse(content)
        except SyntaxError as exc:
            reasons.append(f"{path.name} has Python syntax errors: {exc.msg}")
    return reasons


def split_content_issue_severity(
    reasons: List[str],
) -> tuple[List[str], List[str]]:
    repairable: List[str] = []
    rejected: List[str] = []
    for reason in reasons:
        lowered = reason.lower()
        if any(
            marker in lowered
            for marker in (
                "`pass` placeholders",
                "not-implemented markers",
                "syntax errors",
                "broken python __main__",
            )
        ):
            rejected.append(reason)
        elif "todo or placeholder markers" in lowered:
            repairable.append(reason)
        else:
            rejected.append(reason)
    return repairable, rejected


def core_expected_files(plan: List[Dict[str, Any]]) -> List[str]:
    files: List[str] = []
    seen = set()
    for step in plan:
        for raw_path in step.get("expected_files", []) or []:
            path_text = str(raw_path or "").strip()
            if (
                not path_text
                or path_text.endswith("/")
                or path_text.lower() in DOC_NAMES
            ):
                continue
            if Path(path_text).suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            if path_text not in seen:
                seen.add(path_text)
                files.append(path_text)
    return files


def assess_plan_workspace_compatibility(
    *,
    project_dir: Path,
    plan: List[Dict[str, Any]],
    completed_step_count: int = 0,
) -> Dict[str, Any]:
    """Check whether a saved plan's completed portion still matches the current workspace."""

    if completed_step_count <= 0:
        return {
            "compatible": True,
            "completed_step_count": completed_step_count,
            "expected_core_count": 0,
            "matched_core_count": 0,
            "nested_match_count": 0,
            "workspace_source_count": 0,
            "expected_core_files": [],
            "matched_core_files": [],
            "nested_matches": {},
        }

    scoped_plan = plan[:completed_step_count]
    expected_core_files = core_expected_files(scoped_plan)
    candidate_files = iter_candidate_files(project_dir, expected_core_files)
    nested_matches = find_nested_expected_file_matches(project_dir, expected_core_files)

    project_dir = project_dir.resolve()
    workspace_source_files = [
        path
        for path in project_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SOURCE_EXTENSIONS
        and not any(
            part in {"node_modules", "__pycache__", ".openclaw", ".agent"}
            for part in path.relative_to(project_dir).parts
        )
    ]
    nested_match_count = sum(len(matches) for matches in nested_matches.values())
    expected_count = len(expected_core_files)
    matched_count = len(candidate_files)
    compatible = not (
        workspace_source_files
        and expected_count > 0
        and matched_count == 0
        and nested_match_count == 0
    )

    return {
        "compatible": compatible,
        "completed_step_count": completed_step_count,
        "expected_core_count": expected_count,
        "matched_core_count": matched_count,
        "nested_match_count": nested_match_count,
        "workspace_source_count": len(workspace_source_files),
        "expected_core_files": expected_core_files[:20],
        "matched_core_files": [
            str(path.relative_to(project_dir)) for path in candidate_files[:20]
        ],
        "nested_matches": {key: value[:10] for key, value in nested_matches.items()},
    }
