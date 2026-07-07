"""Deterministic plan-sanitization helpers for PlannerService.

Moved verbatim from ``planner.py`` (Phase 20N). No sanitization logic,
thresholds, reason strings, or return shapes changed — this is a mechanical
extraction. ``PlannerService.sanitize_common_plan_issues`` and the other
externally-referenced names delegate here via ``staticmethod``/``classmethod``
wrappers in ``planner.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.orchestration.operations.file_ops_contract import (
    REPLACE_IN_FILE_NEW_ALIASES,
    REPLACE_IN_FILE_OLD_ALIASES,
    validate_file_op_shape,
)

STRUCTURALLY_EMPTY_FILENAMES = frozenset({"__init__.py", "__init__.pyi", ".gitkeep"})


def _uses_background_process(command: str) -> bool:
    text = str(command or "").strip().lower()
    if not text:
        return False
    if re.search(r"(^|[^&])&(?=[^&]|$)", text):
        return True
    background_markers = (
        "nohup ",
        " disown",
        "tail -f",
        "npm run dev",
        "pnpm dev",
        "yarn dev",
        "vite dev",
        "next dev",
        "webpack serve",
    )
    return any(marker in text for marker in background_markers)


def _command_is_plain_english_file_instruction(command: str) -> bool:
    text = str(command or "").strip().lower()
    if not text:
        return False
    if text.startswith("file ") and " should be " in text:
        return True
    if re.match(
        r"^(create|build|make)\s+(the\s+)?(app|page|site|ui|component)\b", text
    ):
        return True
    return False


def _looks_like_preview_only_step(
    step: Dict[str, Any], *, step_index: int, total_steps: int
) -> bool:
    if step_index != total_steps:
        return False
    description = str(step.get("description") or "").lower()
    commands = [
        str(command or "").strip() for command in step.get("commands", []) or []
    ]
    preview_markers = (
        "final validation",
        "local preview",
        "open the page",
        "confirm rendering",
        "preview",
        "rendering",
    )
    return any(marker in description for marker in preview_markers) and any(
        _uses_background_process(command) for command in commands
    )


def _rewrite_trash_rollback(command: Optional[str]) -> Optional[str]:
    text = str(command or "").strip()
    if not text:
        return command
    match = re.match(r"^\s*trash\s+(.+?)\s*$", text)
    if not match:
        return command
    target = match.group(1).strip()
    return f"rm -f {target}"


def _safe_relative_verification_paths(paths: List[str]) -> List[str]:
    safe_paths: List[str] = []
    for raw_path in paths:
        path_text = str(raw_path or "").strip()
        if not path_text:
            continue
        path = Path(path_text)
        if (
            path.is_absolute()
            or path_text.startswith(("/", "\\"))
            or re.match(r"^[A-Za-z]:[\\/]", path_text)
            or ".." in path.parts
        ):
            continue
        safe_paths.append(path_text)
    return safe_paths


def _failing_verification_command() -> str:
    return 'python -c "import sys; sys.exit(1)"'


def _python_exists_verification_command(paths: List[str]) -> str:
    safe_paths = _safe_relative_verification_paths(paths)
    if not safe_paths:
        return _failing_verification_command()
    encoded_paths = json.dumps(safe_paths)
    script = (
        "import pathlib,sys; "
        f"files={encoded_paths}; "
        "sys.exit(0 if all(pathlib.Path(p).exists() for p in files) else 1)"
    )
    return "python -c " + json.dumps(script)


def _python_file_contains_verification_command(path: str, expected: str) -> str:
    safe_paths = _safe_relative_verification_paths([path])
    if not safe_paths:
        return _failing_verification_command()
    script = (
        "import pathlib,sys; "
        f"path=pathlib.Path({json.dumps(safe_paths[0])}); "
        f"expected={json.dumps(expected)}; "
        "sys.exit(0 if path.exists() and expected in path.read_text() else 1)"
    )
    return "python -c " + json.dumps(script)


def _looks_like_safe_verification_command(command: Any) -> bool:
    text = str(command or "").strip()
    if not text:
        return False
    safe_prefixes = (
        "python -c ",
        "python3 -c ",
        "python -m pytest",
        "python3 -m pytest",
        "pytest",
        "npm test",
        "npm run test",
        "npm run build",
        "test ",
    )
    if not text.startswith(safe_prefixes):
        return False
    # Reject shell chaining — && and || are never needed in a single verification
    if re.search(r"&&|\|\|", text):
        return False
    # Do not promote python -c assertion forms from verification into commands.
    # When a step has structured ops (replace_in_file / write_file), the write
    # intent is already covered; the assertion is a pre-check annotation only.
    # If the model places assert directly in commands[], the validator still rejects it.
    if text.startswith(("python -c ", "python3 -c ")) and re.search(
        r"\bassert\s", text
    ):
        return False
    # For non-python-c commands, also reject ; | > < ` $( to prevent injection
    if not text.startswith(("python -c ", "python3 -c ")):
        if re.search(r";|\|(?!\|)|>|<|`|\$\(", text):
            return False
    return True


def _extract_top_level_file_op(step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw_op_name = str(step.get("op") or step.get("step") or "").strip()
    return _normalize_file_operation(
        raw_op_name=raw_op_name,
        path=str(step.get("path") or step.get("file") or "").strip(),
        source=step,
    )


def _normalize_nested_file_operation(
    operation: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    nested_file_op_names = {"write_file", "append_file", "replace_in_file"}
    if "op" in operation:
        return None

    nested_keys = [key for key in operation if key in nested_file_op_names]
    if not nested_keys:
        return None

    if len(operation) != 1 or len(nested_keys) != 1:
        return dict(operation)

    op_name = nested_keys[0]
    payload = operation.get(op_name)
    if not isinstance(payload, dict):
        return dict(operation)

    if op_name in {"write_file", "append_file"}:
        normalized = {
            "op": op_name,
            "path": payload.get("path"),
            "content": payload.get("content"),
        }
    else:
        normalized = {
            "op": op_name,
            "path": payload.get("path"),
            "old": payload.get("old"),
            "new": payload.get("new"),
        }

    if validate_file_op_shape(normalized):
        return normalized
    return dict(operation)


def _extract_top_level_named_file_ops(step: Dict[str, Any]) -> List[Dict[str, Any]]:
    supported_keys = ("write_file", "append_file", "replace_in_file")
    operations: List[Dict[str, Any]] = []
    for op_name in supported_keys:
        payload = step.get(op_name)
        if not isinstance(payload, dict):
            continue
        if op_name in {"write_file", "append_file"}:
            normalized = {
                "op": op_name,
                "path": payload.get("path"),
                "content": payload.get("content"),
            }
        else:
            normalized = {
                "op": op_name,
                "path": payload.get("path"),
                "old": payload.get("old"),
                "new": payload.get("new"),
            }
            for key in (
                *REPLACE_IN_FILE_OLD_ALIASES,
                *REPLACE_IN_FILE_NEW_ALIASES,
            ):
                if key in payload:
                    normalized[key] = payload[key]
        if validate_file_op_shape(normalized):
            operations.append(normalized)
    return operations


def _normalize_file_op_key_alias(
    operation: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if "o" not in operation:
        return None

    supported_aliases = {"write_file", "append_file", "replace_in_file"}
    alias_name = str(operation.get("o") or "").strip()
    explicit_name = str(operation.get("op") or "").strip()

    if explicit_name and explicit_name != alias_name:
        return dict(operation)
    if alias_name not in supported_aliases:
        return dict(operation)

    normalized = dict(operation)
    normalized.pop("o", None)
    normalized["op"] = alias_name
    if validate_file_op_shape(normalized):
        return normalized
    return dict(operation)


def _normalize_file_operation(
    *,
    raw_op_name: str,
    path: str,
    source: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    op_aliases = {
        "create_file": "write_file",
        "write_file": "write_file",
        "write": "write_file",
        "append_file": "append_file",
        "append": "append_file",
        "replace_in_file": "replace_in_file",
        "replace": "replace_in_file",
        "mkdir": "mkdir",
    }
    op_name = op_aliases.get(raw_op_name)
    if op_name is None:
        return None
    if not path:
        return None
    operation: Dict[str, Any] = {"op": op_name, "path": path}
    if op_name == "replace_in_file":
        for key in (
            "old",
            "new",
            *REPLACE_IN_FILE_OLD_ALIASES,
            *REPLACE_IN_FILE_NEW_ALIASES,
        ):
            if key in source:
                operation[key] = source[key]
    elif op_name != "mkdir":
        for key in ("content", "regex"):
            if key in source:
                operation[key] = source[key]
    return operation


def _extract_top_level_file_verification(step: Dict[str, Any]) -> Optional[str]:
    op_name = str(step.get("op") or step.get("step") or step.get("type") or "").strip()
    if op_name not in {"verify_file", "check"}:
        return None
    path = str(step.get("path") or step.get("file") or "").strip()
    if not path:
        return None
    expected = step.get("expected_content")
    if expected is None and op_name == "check":
        expected = step.get("content")
    if expected is None:
        return _python_exists_verification_command([path])
    expected_text = str(expected)
    return _python_file_contains_verification_command(path, expected_text)


def _path_from_rm_rollback(command: Any) -> Optional[str]:
    text = str(command or "").strip()
    match = re.match(r"^rm\s+-f\s+([A-Za-z0-9_./-]+)\s*$", text)
    if not match:
        return None
    path = match.group(1).strip().lstrip("./")
    if not path or Path(path).is_absolute() or ".." in Path(path).parts:
        return None
    return path


def _infer_unittest_write_op(
    *,
    task_prompt: str,
    description: str,
    rollback: Any,
    prefer_pytest: bool = False,
) -> Optional[Dict[str, Any]]:
    prompt = str(task_prompt or "")
    if "unittest" not in prompt.lower():
        return None
    path = _path_from_rm_rollback(rollback)
    if not path or not path.startswith("tests/") or not path.endswith(".py"):
        return None
    if path not in description and path not in prompt:
        return None

    script_match = re.search(
        r"(?:execute|run)\s+([A-Za-z0-9_./-]+\.py)", prompt, re.IGNORECASE
    )
    expected_match = re.search(
        r"stdout\s+equals\s+[\"']([^\"']+)[\"']", prompt, re.IGNORECASE
    )
    if not script_match or not expected_match:
        return None
    script_path = script_match.group(1).strip().lstrip("./")
    expected = expected_match.group(1)
    if (
        not script_path
        or Path(script_path).is_absolute()
        or ".." in Path(script_path).parts
    ):
        return None

    if prefer_pytest:
        content = (
            "import subprocess\n"
            "import sys\n\n\n"
            "def test_smoke_status_output():\n"
            "    completed = subprocess.run(\n"
            f"        [sys.executable, {json.dumps(script_path)}],\n"
            "        check=True,\n"
            "        capture_output=True,\n"
            "        text=True,\n"
            "    )\n"
            f"    assert completed.stdout.strip() == {json.dumps(expected)}\n"
        )
    else:
        content = (
            "import subprocess\n"
            "import sys\n"
            "import unittest\n\n\n"
            "class SmokeStatusTest(unittest.TestCase):\n"
            "    def test_smoke_status_output(self):\n"
            "        completed = subprocess.run(\n"
            f"            [sys.executable, {json.dumps(script_path)}],\n"
            "            check=True,\n"
            "            capture_output=True,\n"
            "            text=True,\n"
            "        )\n"
            f"        self.assertEqual(completed.stdout.strip(), {json.dumps(expected)})\n\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        )
    return {"op": "write_file", "path": path, "content": content}


def _prompt_has_pytest_project_signal(*texts: str) -> bool:
    combined = "\n".join(str(text or "") for text in texts).lower()
    pytest_markers = (
        "pytest",
        "pytest.ini",
        "[tool.pytest",
        "pytest_plugins",
        "@pytest.mark",
        "conftest.py",
        "python -m pytest",
    )
    return any(marker in combined for marker in pytest_markers)


def _normalize_unittest_write_content(operation: Dict[str, Any]) -> Dict[str, Any]:
    path = str(operation.get("path") or "").strip().lstrip("./")
    content = operation.get("content")
    if (
        str(operation.get("op") or "") != "write_file"
        or not path.startswith("tests/")
        or not path.endswith(".py")
        or not isinstance(content, str)
        or "unittest" not in content
        or "subprocess.run(['python'," not in content
    ):
        return operation
    updated = dict(operation)
    normalized_content = content.replace(
        "subprocess.run(['python',", "subprocess.run([sys.executable,"
    )
    normalized_content = normalized_content.replace(
        "'../scripts/smoke_status.py'", "'scripts/smoke_status.py'"
    ).replace('"../scripts/smoke_status.py"', '"scripts/smoke_status.py"')
    if "import sys" not in normalized_content:
        lines = normalized_content.splitlines()
        insert_at = 0
        while insert_at < len(lines) and lines[insert_at].startswith("import "):
            insert_at += 1
        lines.insert(insert_at, "import sys")
        normalized_content = "\n".join(lines)
    updated["content"] = normalized_content
    return updated


def _exact_line_from_task_prompt(task_prompt: str) -> Optional[str]:
    prompt = str(task_prompt or "")
    patterns = (
        r"exactly\s+this\s+line\s+and\s+no\s+trailing\s+punctuation:\s*([^\n.]+(?:\.[^\n.]+)*?)(?:\.\s|\.$|$)",
        r"exactly\s+this\s+single\s+line:\s*([^\n.]+(?:\.[^\n.]+)*?)(?:\.\s|\.$|$)",
        r"exactly\s+this\s+line:\s*([^\n.]+(?:\.[^\n.]+)*?)(?:\.\s|\.$|$)",
        r"stdout(?:\.strip\(\))?\s+equals\s+([^\n.]+(?:\.[^\n.]+)*?)(?:\.\s|\.$|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, prompt, re.IGNORECASE)
        if match:
            exact_line = match.group(1).strip().strip("\"'")
            if exact_line:
                return exact_line
    return None


def _normalize_exact_line_from_task_prompt(
    operation: Dict[str, Any], task_prompt: str
) -> Dict[str, Any]:
    exact_line = _exact_line_from_task_prompt(task_prompt)
    content = operation.get("content")
    if (
        not exact_line
        or str(operation.get("op") or "") != "write_file"
        or not isinstance(content, str)
    ):
        return operation

    updated = dict(operation)
    updated["content"] = content.replace(f"{exact_line}.", exact_line)
    return updated


def _normalize_exact_line_verification(
    command: Optional[str], task_prompt: str
) -> Optional[str]:
    exact_line = _exact_line_from_task_prompt(task_prompt)
    if not exact_line or not isinstance(command, str):
        return command
    return command.replace(f"{exact_line}.", exact_line)


def _normalize_exact_script_output_verification(
    command: Optional[str], task_prompt: str
) -> Optional[str]:
    exact_line = _exact_line_from_task_prompt(task_prompt)
    text = str(command or "").strip()
    if not exact_line or not text or "scripts/smoke_status.py" not in text:
        return command
    script = (
        "import subprocess,sys; "
        "result=subprocess.run("
        "[sys.executable, 'scripts/smoke_status.py'], "
        "capture_output=True, text=True); "
        f"sys.exit(0 if result.stdout.strip() == {json.dumps(exact_line)} else 1)"
    )
    return "python -c " + json.dumps(script)


def _normalize_python_subprocess_verification(
    command: Optional[str],
) -> Optional[str]:
    text = str(command or "").strip()
    if not text:
        return command
    match = re.search(
        r"subprocess\.run\(\s*\[\s*['\"]python['\"]\s*,\s*['\"]([^'\"]+\.py)['\"]\s*\]\s*,\s*capture_output=True\s*\)\.stdout\.strip\(\)\s*==\s*b['\"]([^'\"]+)['\"]",
        text,
    )
    if not match:
        return command
    script_path = match.group(1).strip().lstrip("./")
    expected = match.group(2)
    if (
        not script_path
        or Path(script_path).is_absolute()
        or ".." in Path(script_path).parts
    ):
        return command
    script = (
        "import subprocess,sys; "
        "result=subprocess.run("
        f"[sys.executable, {json.dumps(script_path)}], "
        "capture_output=True, text=True); "
        f"sys.exit(0 if result.stdout.strip() == {json.dumps(expected)} else 1)"
    )
    return "python -c " + json.dumps(script)


def _directory_creation_preconditions(
    plan: Optional[List[Dict[str, Any]]],
) -> set[str]:
    materialized_files: set[str] = set()
    for step in plan or []:
        if not isinstance(step, dict):
            continue
        materialized_files.update(_file_write_paths_from_step(step))
        for raw_path in step.get("expected_files", []) or []:
            path = str(raw_path or "").strip().lstrip("./")
            if Path(path).suffix and "/" in path:
                materialized_files.add(path)

    dirs: set[str] = set()
    for path in materialized_files:
        parent = str(Path(path).parent).replace("\\", "/")
        if parent and parent != "." and ".." not in Path(parent).parts:
            dirs.add(parent)
    return dirs


def _file_write_paths_from_step(step: Dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for operation in step.get("ops", []) or []:
        if not isinstance(operation, dict):
            continue
        if str(operation.get("op") or "") in {"write_file", "append_file"}:
            path = str(operation.get("path") or "").strip().lstrip("./")
            if path:
                paths.add(path)
    top_level_op = str(
        step.get("op") or step.get("step") or step.get("type") or ""
    ).strip()
    if top_level_op in {"create_file", "write_file", "write", "append_file"}:
        path = str(step.get("path") or step.get("file") or "").strip().lstrip("./")
        if path:
            paths.add(path)
    return paths


def _future_file_write_paths_by_step(
    plan: Optional[List[Dict[str, Any]]],
) -> Dict[int, set[str]]:
    future_by_step: Dict[int, set[str]] = {}
    future: set[str] = set()
    steps = list(plan or [])
    for index in range(len(steps), 0, -1):
        future_by_step[index] = set(future)
        step = steps[index - 1]
        if isinstance(step, dict):
            future.update(_file_write_paths_from_step(step))
    return future_by_step


def _looks_like_read_only_inspection(description: str, commands: List[str]) -> bool:
    text = str(description or "").lower()
    command_text = " && ".join(commands).lower()
    if not any(
        marker in text
        for marker in ("inspect", "review", "check current", "current workspace")
    ):
        return False
    return bool(command_text) and all(
        re.match(r"^\s*(ls|pwd|find\s+\.\s+-maxdepth|rg\s+)", token)
        for token in commands
    )


def sanitize_common_plan_issues(
    plan: Optional[List[Dict[str, Any]]], task_prompt: str = ""
) -> List[Dict[str, Any]]:
    sanitized_plan: List[Dict[str, Any]] = []
    total_steps = len(plan or [])
    directory_creation_preconditions = _directory_creation_preconditions(plan)
    future_file_writes = _future_file_write_paths_by_step(plan)

    for index, raw_step in enumerate(plan or [], start=1):
        step = dict(raw_step or {})
        raw_commands = step.get("commands", [])
        cmd_single = step.get("cmd")
        if not raw_commands and cmd_single:
            raw_commands = [cmd_single]
        if isinstance(raw_commands, str):
            raw_commands = [raw_commands]
        elif not isinstance(raw_commands, list):
            raw_commands = []
        commands = [str(command or "").strip() for command in raw_commands]
        commands = [command for command in commands if command]

        if _looks_like_preview_only_step(
            step, step_index=index, total_steps=total_steps
        ):
            continue

        commands = [
            command
            for command in commands
            if not _command_is_plain_english_file_instruction(command)
        ]

        # Rewrite safe single-expression python -c write_text to ops.write_file.
        # Only the unambiguous form is touched; anything else is left for the
        # validator prefer_typed_ops flag to surface during repair.
        rewritten_commands: List[str] = []
        extra_ops_from_rewrite: List[Dict[str, Any]] = []
        for cmd in commands:
            m = _SAFE_PYTHON_C_WRITE_TEXT_RE.match(cmd.strip())
            if m:
                rel_path = m.group("path").lstrip("./")
                content = m.group("content")
                if rel_path and not Path(rel_path).is_absolute():
                    extra_ops_from_rewrite.append(
                        {"op": "write_file", "path": rel_path, "content": content}
                    )
                    continue
            rewritten_commands.append(cmd)
        commands = rewritten_commands

        raw_ops = []
        if isinstance(raw_step, dict):
            if isinstance(raw_step.get("ops"), list):
                for operation in raw_step["ops"]:
                    if not isinstance(operation, dict):
                        continue
                    normalized_op = _normalize_file_op_key_alias(operation)
                    alias_op_was_invalid = (
                        normalized_op is not None
                        and not validate_file_op_shape(normalized_op)
                    )
                    nested_op_was_invalid = False
                    if normalized_op is None:
                        normalized_op = _normalize_file_operation(
                            raw_op_name=str(
                                operation.get("op") or operation.get("type") or ""
                            ).strip(),
                            path=str(
                                operation.get("path") or operation.get("file") or ""
                            ).strip(),
                            source=operation,
                        )
                    if normalized_op is None:
                        normalized_op = _normalize_nested_file_operation(operation)
                        nested_op_was_invalid = (
                            normalized_op is not None
                            and not validate_file_op_shape(normalized_op)
                        )
                    if (
                        normalized_op
                        and not nested_op_was_invalid
                        and not alias_op_was_invalid
                    ):
                        normalized_op = _normalize_unittest_write_content(normalized_op)
                        normalized_op = _normalize_exact_line_from_task_prompt(
                            normalized_op, task_prompt
                        )
                        raw_ops.append(normalized_op)
                    elif nested_op_was_invalid or alias_op_was_invalid:
                        raw_ops.append(normalized_op)
                    elif top_level_verification := _extract_top_level_file_verification(
                        operation
                    ):
                        step.setdefault("verification", top_level_verification)
            elif top_level_op := _extract_top_level_file_op(raw_step):
                raw_ops.append(top_level_op)
            raw_ops.extend(_extract_top_level_named_file_ops(raw_step))
            if top_level_verification := _extract_top_level_file_verification(raw_step):
                step.setdefault("verification", top_level_verification)

        raw_ops = [
            _normalize_exact_line_from_task_prompt(operation, task_prompt)
            for operation in raw_ops
        ]
        # Append any ops promoted from python -c rewrites.
        raw_ops.extend(extra_ops_from_rewrite)

        raw_expected_files = step.get("expected_files", [])
        if isinstance(raw_expected_files, str):
            raw_expected_files = [raw_expected_files]
        elif raw_expected_files is None:
            raw_expected_files = []
        elif not isinstance(raw_expected_files, list):
            raw_expected_files = []
        expected_files = [
            str(path or "").strip()
            for path in raw_expected_files
            if str(path or "").strip()
        ]
        op_expected_files = [
            str(operation.get("path") or "").strip()
            for operation in raw_ops
            if str(operation.get("op") or "") in {"write_file", "append_file"}
            and str(operation.get("path") or "").strip()
        ]
        raw_path = str(step.get("file") or step.get("path") or "").strip()
        combined_expected_files = expected_files + op_expected_files
        if raw_path:
            combined_expected_files.append(raw_path)
        expected_files = list(dict.fromkeys(combined_expected_files))

        verification = step.get("verification")
        if verification is not None and not isinstance(verification, str):
            verification = None
        if verification is not None:
            verification = str(verification).strip() or None
        verification = _normalize_python_subprocess_verification(verification)
        verification = _normalize_exact_line_verification(verification, task_prompt)
        verification = _normalize_exact_script_output_verification(
            verification, task_prompt
        )
        description_for_intent = str(step.get("description") or "").strip()
        if (
            not raw_ops
            and expected_files
            and set(expected_files).issubset(future_file_writes.get(index, set()))
            and _looks_like_read_only_inspection(description_for_intent, commands)
        ):
            expected_files = []
            verification = "python -c " + json.dumps(
                "import pathlib,sys; "
                "sys.exit(0 if pathlib.Path('.').exists() else 1)"
            )
        if (
            not commands
            and verification
            and _looks_like_safe_verification_command(verification)
        ):
            commands = [verification]
        if not verification and expected_files:
            verification = _python_exists_verification_command(expected_files)

        rollback = _rewrite_trash_rollback(step.get("rollback"))
        if rollback is not None:
            rollback = str(rollback).strip() or None

        description = str(step.get("description") or "").strip()
        if not description:
            description = f"Execute step {index}"

        if not raw_ops:
            prefer_pytest = _prompt_has_pytest_project_signal(
                task_prompt, description, verification or ""
            )
            inferred_unittest_op = _infer_unittest_write_op(
                task_prompt=task_prompt,
                description=description,
                rollback=rollback,
                prefer_pytest=prefer_pytest,
            )
            if inferred_unittest_op:
                raw_ops.append(inferred_unittest_op)
                inferred_path = str(inferred_unittest_op["path"])
                if inferred_path not in expected_files:
                    expected_files.append(inferred_path)
                commands = []
                verification = (
                    "python -m pytest tests/ -q"
                    if prefer_pytest
                    else "python -m unittest discover -s tests"
                )

        if (
            not raw_ops
            and len(expected_files) == 1
            and expected_files[0] in directory_creation_preconditions
            and "exist" in description.lower()
        ):
            directory = expected_files[0]
            commands = [f"mkdir -p {directory}"]
            verification = "python -c " + json.dumps(
                "import pathlib,sys; "
                f"sys.exit(0 if pathlib.Path({json.dumps(directory)}).is_dir() else 1)"
            )
            expected_files = []

        mkdir_paths = [
            str(operation.get("path") or "").strip().lstrip("./")
            for operation in raw_ops
            if str(operation.get("op") or "") == "mkdir"
            and str(operation.get("path") or "").strip()
        ]
        if mkdir_paths and len(mkdir_paths) == len(raw_ops):
            safe_dirs = [
                path
                for path in mkdir_paths
                if not Path(path).is_absolute() and ".." not in Path(path).parts
            ]
            if safe_dirs:
                commands = [f"mkdir -p {' '.join(safe_dirs)}"]
                verification = "python -c " + json.dumps(
                    "import pathlib,sys; "
                    f"dirs={json.dumps(safe_dirs)}; "
                    "sys.exit(0 if all(pathlib.Path(d).is_dir() for d in dirs) else 1)"
                )
                expected_files = []

        step = {
            "step_number": index,
            "description": description,
            "commands": commands,
            "verification": verification,
            "rollback": rollback,
            "expected_files": expected_files,
        }
        if raw_ops:
            step["ops"] = raw_ops

        sanitized_plan.append(step)

    return sanitized_plan


def _command_is_placeholder_only(command: str) -> bool:
    text = str(command or "").strip().lower()
    if not text:
        return True
    placeholder_patterns = (
        r"^mkdir(?:\s|$)",
        r"^install\s+-d(?:\s|$)",
        r"^touch(?:\s|$)",
        r"^truncate\s+-s\s+0(?:\s|$)",
        r"^cp\s+/dev/null(?:\s|$)",
        r"^:\s*>\s*",
        r"^true$",
    )
    if any(re.match(pattern, text) for pattern in placeholder_patterns):
        return True
    empty_write_patterns = (
        r"^echo\s+(['\"]?\s*['\"]?)\s*(>|>>)\s+",
        r"^printf\s+(['\"]?\s*['\"]?)\s*(>|>>)\s+",
    )
    return any(re.match(pattern, text) for pattern in empty_write_patterns)


_PYTHON_C_CONTENT_WRITE_RE = re.compile(
    r"python3?\s+-c\s+.+(?:write_text|write_bytes|open\s*\([^)]+['\"]w['\"])",
    re.IGNORECASE | re.DOTALL,
)

# Matches only the safe single-expression form:
# python[-3] -c ["']...; Path('rel/path').write_text('literal content')["']
# Groups: (1) path string, (2) content string
_SAFE_PYTHON_C_WRITE_TEXT_RE = re.compile(
    r"""^python3?\s+-c\s+(?P<q>["'])"""
    r"""(?:from\s+pathlib\s+import\s+Path\s*;\s*|import\s+pathlib\s*;\s*pathlib\.)?"""
    r"""Path\((?P<pq>["'])(?P<path>[^'"]+)(?P=pq)\)\.write_text\((?P<cq>["'])(?P<content>[^'"\\]*)(?P=cq)\)"""
    r"""\s*(?P=q)$""",
    re.IGNORECASE,
)


def _command_is_python_c_content_write(command: str) -> bool:
    """Return True when a command uses python -c to write file content.

    These should be ops.write_file instead. Only flags actual content-write
    patterns; verification-only python -c commands are left alone.
    """
    return bool(_PYTHON_C_CONTENT_WRITE_RE.search(str(command or "")))


def _step_is_readonly_inspection(step: Dict[str, Any]) -> bool:
    from app.services.orchestration.validation.validator import ValidatorService

    return ValidatorService._step_is_readonly_inspection(step)


def _step_is_implementation_heavy(step: Dict[str, Any]) -> bool:
    if _step_is_readonly_inspection(step):
        return False
    expected_files = [
        str(path or "").strip()
        for path in (step.get("expected_files", []) or [])
        if str(path or "").strip()
    ]
    if any(not path.endswith("/") for path in expected_files):
        return True

    combined = " ".join(
        [
            str(step.get("description") or ""),
            str(step.get("verification") or ""),
        ]
        + [str(command or "") for command in step.get("commands", []) or []]
    ).lower()
    implementation_markers = (
        "create",
        "implement",
        "build",
        "update",
        "modify",
        "wire",
        "scaffold",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".html",
        ".css",
    )
    inspection_markers = (
        "inspect",
        "review",
        "analyze",
        "inventory",
        "audit",
        "list files",
    )
    return any(marker in combined for marker in implementation_markers) and not any(
        marker in combined for marker in inspection_markers
    )


def _step_expected_files_are_structurally_empty(step: Dict[str, Any]) -> bool:
    file_names = [
        Path(str(path or "").strip()).name
        for path in (step.get("expected_files", []) or [])
        if str(path or "").strip()
    ]
    return bool(file_names) and all(
        name in STRUCTURALLY_EMPTY_FILENAMES for name in file_names
    )
