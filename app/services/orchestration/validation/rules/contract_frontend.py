"""Workload-contract frontend / static-site workload rules.

Moved verbatim from validator.py in Phase 20K (validator rule split,
slice 1). Functions here cover JS identifier soundness, static-site
off-root mutation detection, and frontend/Python stack-consistency
checks.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.orchestration.workflow_profiles import get_multi_stack_pair_markers

from .contract_placeholders import _plan_materialized_file_targets


def _existing_static_site_roots(project_dir: Optional[Path]) -> List[str]:
    if project_dir is None or not Path(project_dir).exists():
        return []
    root = Path(project_dir)
    roots: List[str] = []
    if (root / "index.html").is_file() and (root / "css" / "style.css").is_file():
        roots.append("")
    public_dir = root / "public"
    if public_dir.is_dir():
        for child in sorted(public_dir.iterdir()):
            if not child.is_dir():
                continue
            if (child / "index.html").is_file() and (
                child / "css" / "style.css"
            ).is_file():
                roots.append(f"public/{child.name}")
    return roots


def _plan_static_site_off_root_mutations(
    plan: List[Dict[str, Any]],
    project_dir: Optional[Path],
    task_prompt: str,
) -> List[str]:
    prompt = str(task_prompt or "").lower()
    if not any(marker in prompt for marker in ("static site", "status site")):
        return []
    roots = _existing_static_site_roots(project_dir)
    if not roots:
        return []
    allowed_roots = [f"{root}/" for root in roots if root]
    suffixes = {".css", ".html", ".js", ".svg"}
    off_root: List[str] = []
    for path in sorted(_plan_materialized_file_targets(plan)):
        normalized = path.strip().lstrip("./")
        if Path(normalized).suffix.lower() not in suffixes:
            continue
        if "" in roots and "/" not in normalized:
            continue
        if any(normalized.startswith(prefix) for prefix in allowed_roots):
            continue
        off_root.append(normalized)
    return off_root


def _frontend_wrong_stack_materializations(
    plan: List[Dict[str, Any]],
    workflow_profile: Optional[str],
) -> List[str]:
    if workflow_profile != "frontend_only":
        return []
    wrong_paths: List[str] = []
    for step in plan:
        for operation in step.get("ops", []) or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "") not in {
                "write_file",
                "append_file",
                "replace_in_file",
            }:
                continue
            path_text = str(operation.get("path") or "").strip().lstrip("./")
            suffix = Path(path_text).suffix.lower()
            content = str(operation.get("content") or operation.get("new") or "")
            if not suffix or suffix == ".py" or re.search(r"(?m)^def\s+\w+\(", content):
                wrong_paths.append(path_text or "(missing path)")
    return sorted(set(wrong_paths))


def _plan_writes_obvious_undefined_js_identifiers(
    plan: List[Dict[str, Any]],
) -> List[str]:
    bad_paths: List[str] = []
    allowed_globals = {
        "array",
        "boolean",
        "date",
        "json",
        "math",
        "number",
        "object",
        "string",
        "undefined",
    }
    for step in plan:
        for operation in step.get("ops", []) or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "") not in {"write_file", "append_file"}:
                continue
            path_text = str(operation.get("path") or "").strip().lstrip("./")
            if Path(path_text).suffix.lower() not in {".js", ".jsx", ".ts", ".tsx"}:
                continue
            content = str(operation.get("content") or "")
            function_match = re.search(
                r"function\s+\w+\s*\((?P<params>[^)]*)\)\s*\{(?P<body>.*?)\}",
                content,
                flags=re.DOTALL,
            )
            if not function_match:
                continue
            declared = {
                part.strip().split("=")[0].split(":")[0].strip()
                for part in function_match.group("params").split(",")
                if part.strip()
            }
            body = function_match.group("body")
            declared.update(
                re.findall(
                    r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
                    body,
                )
            )
            for return_match in re.finditer(r"\breturn\s+([^;\n]+)", body):
                return_expression = return_match.group(1)
                identifier_expression = re.sub(
                    r"(['\"])(?:\\.|(?!\1).)*\1",
                    "",
                    return_expression,
                )
                identifiers = [
                    match.group(1)
                    for match in re.finditer(
                        r"\b([A-Za-z_$][A-Za-z0-9_$]*)\b",
                        identifier_expression,
                    )
                    if match.start() == 0
                    or identifier_expression[match.start() - 1] != "."
                ]
                if any(
                    identifier not in declared
                    and identifier.lower() not in allowed_globals
                    and identifier not in {"true", "false", "null"}
                    for identifier in identifiers
                ):
                    bad_paths.append(path_text)
                    break
    return sorted(set(bad_paths))


def _task_allows_multiple_stacks(
    task_prompt: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> bool:
    combined = " ".join([task_prompt or "", title or "", description or ""]).lower()
    explicit_pairs = get_multi_stack_pair_markers()
    if any(left in combined and right in combined for left, right in explicit_pairs):
        return True
    return any(
        marker in combined
        for marker in ("polyglot", "multi-language", "full stack", "full-stack")
    )


def _infer_stack_from_plan(plan: List[Dict[str, Any]]) -> Optional[str]:
    seen_python = False
    seen_node = False
    for step in plan:
        text = " ".join(
            [
                str(step.get("description") or ""),
                str(step.get("verification") or ""),
            ]
            + [str(command or "") for command in step.get("commands", []) or []]
            + [str(path or "") for path in step.get("expected_files", []) or []]
        ).lower()
        if any(
            token in text
            for token in (
                "requirements.txt",
                "python ",
                ".py",
                "pip ",
                "pytest",
                "pyproject.toml",
            )
        ):
            seen_python = True
        if any(
            token in text
            for token in (
                "package.json",
                "npm ",
                "pnpm ",
                "node ",
                "tsconfig.json",
            )
        ) or re.search(r"\.(?:js|ts)(?![a-z0-9_])", text):
            seen_node = True
    if seen_python and seen_node:
        return "mixed"
    if seen_node:
        return "node"
    if seen_python:
        return "python"
    return None


def _plan_contains_stack_conflict(plan: List[Dict[str, Any]], task_prompt: str) -> bool:
    from ..validator import ValidatorService

    lowered_task = (task_prompt or "").lower()
    if any(
        marker in lowered_task
        for marker in ("python", "node", "javascript", "typescript")
    ):
        return False

    seen_python = False
    seen_node = False
    for step in plan:
        if ValidatorService._step_is_readonly_inspection(step):
            continue
        text_parts = [str(step.get("description") or "")]
        for command in step.get("commands", []) or []:
            command_text = str(command or "").strip()
            lowered_command = command_text.lower()
            if (
                lowered_command.startswith("python -c ")
                and ".py" not in lowered_command
                and "pytest" not in lowered_command
                and "pip " not in lowered_command
                and "requirements.txt" not in lowered_command
            ):
                continue
            text_parts.append(command_text)
        text = " ".join(text_parts).lower()
        if any(
            token in text
            for token in ("requirements.txt", "python ", ".py", "pip ", "pytest")
        ):
            seen_python = True
        if any(
            token in text for token in ("package.json", "npm ", "pnpm ", "node ")
        ) or re.search(r"\.(?:js|ts)(?![a-z0-9_])", text):
            seen_node = True
    return seen_python and seen_node
