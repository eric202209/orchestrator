"""Local execution helpers for simple orchestration steps."""

from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from app.services.orchestration.execution.execution_flow import (
    execute_verification_command,
    workspace_python_command_env,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.validation.workspace_guard import (
    TaskWorkspaceViolationError,
    assert_no_workspace_cd_escape,
    normalize_path_reference,
)
from app.services.workspace.permissions import ensure_shared_permissions


def _verification_can_replace_stale_commands(step: dict[str, Any]) -> bool:
    commands = step.get("commands")
    if not isinstance(commands, list) or not commands:
        return False
    if isinstance(step.get("ops"), list) and step.get("ops"):
        return False
    return True


def _is_read_only_inspection_command(command: str) -> bool:
    normalized = " ".join(str(command or "").strip().split())
    if not normalized:
        return False
    blocked_tokens = (" >", ">>", "&&", ";", "||", "$(", "`")
    if any(token in normalized for token in blocked_tokens):
        if normalized == "rg --files . | sort":
            return True
        return False
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False
    executable = tokens[0]
    if executable in {"grep", "rg", "ripgrep"}:
        path_tokens = [
            token
            for token in tokens[1:]
            if token not in {"--"}
            and not token.startswith("-")
            and "/" in token
            and not token.startswith(("~", "/"))
            and ".." not in Path(token).parts
        ]
        if executable in {"rg", "ripgrep"} and normalized.startswith("rg --files"):
            return True
        return bool(path_tokens)
    allowed_prefixes = (
        "ls",
        "find .",
        "rg --files",
        "cat ",
        "pwd",
        "true",
    )
    if not normalized.startswith(allowed_prefixes):
        return False
    if normalized == "rg --files . | sort":
        return True
    if normalized.startswith("find .") and "| head" in normalized:
        return True
    return "|" not in normalized


def _debug_ops_have_placeholder_content(ops: Any) -> bool:
    if not isinstance(ops, list):
        return False
    for operation in ops:
        if not isinstance(operation, dict):
            continue
        op_name = str(operation.get("op") or "").strip()
        if op_name not in {"write_file", "append_file"}:
            continue
        path = str(operation.get("path") or "").strip()
        content = operation.get("content")
        has_placeholder_content = isinstance(
            content, str
        ) and ValidatorService._write_file_content_has_placeholder_implementation(
            path, content
        )
        if has_placeholder_content:
            return True
    return False


def _is_simple_verification_command(
    command: str, *, project_dir: Path | None = None
) -> bool:
    normalized = " ".join(str(command or "").strip().split())
    if not normalized:
        return False
    if _is_safe_compileall_command(normalized, project_dir=project_dir):
        return True
    if normalized.startswith(("python -c ", "python3 -c ")):
        return (
            _python_inline_verification_script(normalized, project_dir=project_dir)
            is not None
        )
    if normalized.startswith("node -e "):
        return _node_eval_script(normalized) is not None
    allowed_prefixes = (
        "python -m py_compile ",
        "python3 -m py_compile ",
        "python -m unittest ",
        "python3 -m unittest ",
        "npm run build",
        "pytest",
        "python -m pytest",
        "python3 -m pytest",
    )
    if not normalized.startswith(allowed_prefixes):
        return False
    return not any(
        token in normalized for token in (" >", ">>", ";", "&&", "||", "$(", "`")
    )


def _is_safe_compileall_command(
    command: str, *, project_dir: Path | None = None
) -> bool:
    normalized = " ".join(str(command or "").strip().split())
    blocked_fragments = ("<", ">", "|", ";", "&", "$(", "`")
    if any(fragment in normalized for fragment in blocked_fragments):
        return False

    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        return False

    if len(tokens) < 4 or tokens[:3] not in (
        ["python", "-m", "compileall"],
        ["python3", "-m", "compileall"],
    ):
        return False

    operands = tokens[3:]
    if not operands:
        return False
    if any(token.startswith("-") for token in operands):
        return False

    for operand in operands:
        path_text = str(operand or "").strip()
        if not path_text:
            return False
        candidate = Path(path_text)
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or re.match(r"^[A-Za-z]:[\\/]", path_text)
            or path_text.startswith(("~", "\\\\", "//"))
        ):
            return False
        if project_dir is not None:
            try:
                resolved = project_dir / normalize_path_reference(
                    path_text, project_dir
                )
            except TaskWorkspaceViolationError:
                return False
            if resolved.is_dir():
                continue
            if resolved.is_file() and resolved.suffix == ".py":
                continue
            return False
        elif candidate.suffix != ".py":
            return False

    return True


def _open_read_path_is_safe(path_value: str, project_dir: Path | None) -> bool:
    raw = str(path_value or "").strip()
    if not raw:
        return False
    if raw.startswith(("~", "/", "\\\\")) or re.match(r"^[A-Za-z]:[\\/]", raw):
        return False
    if ".." in Path(raw).parts:
        return False
    if project_dir is None:
        return True
    try:
        normalize_path_reference(raw, project_dir)
    except TaskWorkspaceViolationError:
        return False
    return True


def _open_read_calls_are_safe(script: str, project_dir: Path | None) -> bool:
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return False

    open_calls: list[ast.Call] = []
    read_open_calls: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                open_calls.append(node)
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "read"
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "open"
            ):
                read_open_calls.add(id(node.func.value))

    if not open_calls or len(read_open_calls) != len(open_calls):
        return False

    for open_call in open_calls:
        if id(open_call) not in read_open_calls:
            return False
        if not open_call.args or not isinstance(open_call.args[0], ast.Constant):
            return False
        path_value = open_call.args[0].value
        if not isinstance(path_value, str) or not _open_read_path_is_safe(
            path_value, project_dir
        ):
            return False

        mode_value = "r"
        if len(open_call.args) >= 2:
            if not isinstance(open_call.args[1], ast.Constant):
                return False
            mode_value = open_call.args[1].value
        for keyword in open_call.keywords:
            if keyword.arg == "mode":
                if not isinstance(keyword.value, ast.Constant):
                    return False
                mode_value = keyword.value.value
        if not isinstance(mode_value, str) or mode_value not in {"r", "rt"}:
            return False

    return True


def _python_inline_verification_script(
    command: str, *, project_dir: Path | None = None
) -> str | None:
    normalized = " ".join(str(command or "").strip().split())
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        return None
    if len(tokens) != 3 or tokens[0] not in {"python", "python3"} or tokens[1] != "-c":
        return None

    script = tokens[2]
    lowered = script.lower()
    blocked_fragments = (
        "__",
        ".write(",
        ".unlink(",
        ".rename(",
        ".replace(",
        "remove(",
        "rmdir(",
        "mkdir(",
        "shutil",
        "subprocess",
        "socket",
        "requests",
        "urllib",
        "http://",
        "https://",
        "os.",
        "sys.argv",
        "eval(",
        "exec(",
    )
    if any(fragment in lowered for fragment in blocked_fragments):
        return None
    path_check = "pathlib.path(" in lowered and any(
        fragment in lowered for fragment in (".read_text(", ".exists(")
    )
    open_read_check = "open(" in lowered and _open_read_calls_are_safe(
        script, project_dir
    )
    config_check = "configparser" in lowered and ".read(" in lowered
    if not (path_check or open_read_check or config_check):
        return None
    if not any(fragment in lowered for fragment in ("sys.exit(", "print(")):
        return None
    return script


def _patch_python_verification_cmd(command: str) -> str:
    """Prepend 'import sys; ' when a python -c script uses sys.* without importing it."""
    normalized = " ".join(str(command or "").strip().split())
    if not normalized.startswith(("python -c ", "python3 -c ")):
        return command
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        return command
    if len(tokens) != 3:
        return command
    script = tokens[2]
    imports_sys = bool(re.search(r"(^|;)\s*import\s+[^;]*\bsys\b", script))
    if "sys." in script and not imports_sys:
        script = "import sys; " + script
        return f"{tokens[0]} -c {shlex.quote(script)}"
    return command


def _node_eval_script(command: str) -> str | None:
    normalized = " ".join(str(command or "").strip().split())
    if not normalized.startswith("node -e "):
        return None
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        tokens = []
    if len(tokens) == 3 and tokens[0] == "node" and tokens[1] == "-e":
        script = tokens[2]
    else:
        script = normalized[len("node -e ") :].strip()
        if len(script) >= 2 and script[0] == script[-1] and script[0] in {"'", '"'}:
            script = script[1:-1]
    return script.replace('\\\\"', '"').replace('\\"', '"')


def _same_simple_verification_command(command: str, verification: str) -> bool:
    if command == verification:
        return True
    try:
        if shlex.split(command, posix=True) == shlex.split(verification, posix=True):
            return True
    except ValueError:
        pass
    command_script = _node_eval_script(command)
    verification_script = _node_eval_script(verification)
    return bool(command_script and command_script == verification_script)


def _execute_read_only_inspection_step(
    *,
    project_dir: Path,
    commands: list[Any],
) -> dict[str, Any] | None:
    if not commands or not all(
        _is_read_only_inspection_command(str(c)) for c in commands
    ):
        return None

    outputs: list[str] = []
    for command in commands:
        result = execute_verification_command(
            project_dir=project_dir,
            command=str(command),
            timeout_seconds=30,
        )
        if not result.get("success"):
            return {
                "status": "failed",
                "output": result.get("output", ""),
                "error": result.get("output", "read-only inspection command failed"),
                "files_changed": [],
            }
        command_output = str(result.get("output") or "").strip()
        if command_output:
            outputs.append(f"$ {command}\n{command_output}")
    return {
        "status": "completed",
        "output": "\n\n".join(outputs),
        "verification_output": "",
        "skip_declared_verification": True,
        "files_changed": [],
    }


def _is_safe_local_shell_command(command: str) -> bool:
    normalized = " ".join(str(command or "").strip().split())
    if not normalized:
        return False
    blocked_tokens = (
        "$(",
        "`",
        "curl",
        "wget",
        "pip",
        "npm",
        "yarn",
        "apt",
        "yum",
        "brew",
        "rm ",
        "rm\t",
        "sudo",
        "; rm",
        "&&rm",
        "||rm",
        "..",
    )
    if any(t in normalized for t in blocked_tokens):
        return False
    safe_prefixes = (
        "echo ",
        "echo\t",
        "printf ",
        "mkdir ",
        "mkdir\t",
        "touch ",
        "cp ",
        "cp\t",
        "mv ",
        "mv\t",
        "chmod ",
        "chmod\t",
    )
    return any(normalized.startswith(p) for p in safe_prefixes)


def _is_workspace_local_path_token(token: str, project_dir: Path) -> bool:
    raw = str(token or "").strip()
    if not raw:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("\\\\", "//")):
        return False
    try:
        normalize_path_reference(raw, project_dir)
    except TaskWorkspaceViolationError:
        return False
    return True


def _local_shell_command_paths_are_safe(command: str, project_dir: Path) -> bool:
    if re.search(r"(^|[\s>])([A-Za-z]:[\\/]|\\\\)", str(command or "")):
        return False
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False

    executable = tokens[0]
    if executable in {"echo", "printf"}:
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token in {">", ">>"}:
                if index + 1 >= len(tokens):
                    return False
                if not _is_workspace_local_path_token(tokens[index + 1], project_dir):
                    return False
                index += 2
                continue
            if token.startswith((">", ">>")):
                target = token[2:] if token.startswith(">>") else token[1:]
                if not _is_workspace_local_path_token(target, project_dir):
                    return False
            index += 1
        return True

    if executable in {"mkdir", "touch"}:
        operands = [token for token in tokens[1:] if not token.startswith("-")]
        return bool(operands) and all(
            _is_workspace_local_path_token(token, project_dir) for token in operands
        )

    if executable in {"cp", "mv"}:
        operands = [token for token in tokens[1:] if not token.startswith("-")]
        return len(operands) >= 2 and all(
            _is_workspace_local_path_token(token, project_dir) for token in operands
        )

    if executable == "chmod":
        operands = [token for token in tokens[1:] if not token.startswith("-")]
        if len(operands) < 2:
            return False
        mode = operands[0]
        if not re.match(r"^(?:[0-7]{3,4}|[ugoa]*[+-=][rwxXstugo,+-=]+)$", mode):
            return False
        paths = operands[1:]
        return all(
            _is_workspace_local_path_token(token, project_dir) for token in paths
        )

    return False


def _execute_local_shell_commands_step(
    *,
    project_dir: Path,
    commands: list[Any],
    verification_command: Any,
) -> dict[str, Any] | None:
    normalized_cmds = [
        str(c or "").strip() for c in (commands or []) if str(c or "").strip()
    ]
    if not normalized_cmds:
        return None
    if not all(_is_safe_local_shell_command(c) for c in normalized_cmds):
        return None
    for cmd in normalized_cmds:
        try:
            assert_no_workspace_cd_escape(cmd, project_dir)
        except TaskWorkspaceViolationError:
            return None
        if not _local_shell_command_paths_are_safe(cmd, project_dir):
            return None

    files_before = set(
        os.path.relpath(os.path.join(root, f), project_dir)
        for root, _, files in os.walk(project_dir)
        for f in files
    )
    dirs_before = set(
        os.path.relpath(root, project_dir)
        for root, _, _ in os.walk(project_dir)
        if os.path.relpath(root, project_dir) != "."
    )
    outputs: list[str] = []
    for cmd in normalized_cmds:
        try:
            env = workspace_python_command_env(project_dir, cmd)
            result = subprocess.run(
                cmd,
                cwd=str(project_dir),
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "failed",
                "output": f"Command timed out: {cmd}",
                "error": "timeout",
                "files_changed": [],
            }
        if result.returncode != 0:
            err = "\n".join(
                filter(None, [result.stdout.strip(), result.stderr.strip()])
            )
            return {
                "status": "failed",
                "output": err,
                "error": err,
                "files_changed": [],
            }
        out = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
        if out:
            outputs.append(f"$ {cmd}\n{out}")

    files_after = set(
        os.path.relpath(os.path.join(root, f), project_dir)
        for root, _, files in os.walk(project_dir)
        for f in files
    )
    dirs_after = set(
        os.path.relpath(root, project_dir)
        for root, _, _ in os.walk(project_dir)
        if os.path.relpath(root, project_dir) != "."
    )
    files_changed = [
        f for f in files_after - files_before if not f.startswith(".openclaw")
    ]
    for relative_path in files_changed:
        try:
            ensure_shared_permissions(project_dir / relative_path)
        except Exception:
            pass
    for relative_path in dirs_after - dirs_before:
        if str(relative_path).startswith(".openclaw"):
            continue
        try:
            ensure_shared_permissions(project_dir / relative_path)
        except Exception:
            pass

    verification = _patch_python_verification_cmd(
        str(verification_command or "").strip()
    )
    if verification:
        vresult = execute_verification_command(
            project_dir=project_dir,
            command=verification,
            timeout_seconds=60,
        )
        if not vresult.get("success"):
            verr = vresult.get("output", "verification failed")
            return {
                "status": "failed",
                "output": verr,
                "error": verr,
                "files_changed": files_changed,
            }

    return {
        "status": "completed",
        "output": "\n\n".join(outputs),
        "verification_output": "",
        "files_changed": files_changed,
    }


def _execute_simple_verification_step(
    *,
    project_dir: Path,
    commands: list[Any],
    verification_command: Any,
) -> dict[str, Any] | None:
    normalized_commands = [str(command or "").strip() for command in commands or []]
    verification = str(verification_command or "").strip()
    if len(normalized_commands) != 1:
        return None
    command = normalized_commands[0]
    command_is_simple_verification = _is_simple_verification_command(
        command, project_dir=project_dir
    )
    verification_is_simple = _is_simple_verification_command(
        verification, project_dir=project_dir
    )
    if not verification and _is_safe_compileall_command(
        command, project_dir=project_dir
    ):
        command_to_run = command
    elif not verification:
        return None
    elif _same_simple_verification_command(command, verification):
        command_to_run = verification
    elif (
        command.startswith("node -e ")
        and verification.startswith("node -e ")
        and command_is_simple_verification
        and verification_is_simple
    ):
        command_to_run = command
    elif command_is_simple_verification and verification_is_simple:
        command_to_run = command
    else:
        return None
    command_to_run = _patch_python_verification_cmd(command_to_run)
    if not _is_simple_verification_command(command_to_run, project_dir=project_dir):
        return None

    result = execute_verification_command(
        project_dir=project_dir,
        command=command_to_run,
        timeout_seconds=120,
    )
    return {
        "status": "completed" if result.get("success") else "failed",
        "output": result.get("output", ""),
        "verification_output": result.get("output", ""),
        "error": "" if result.get("success") else result.get("output", ""),
        "files_changed": [],
    }
