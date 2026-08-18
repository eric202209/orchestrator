from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable

from app.config import settings

MAX_DISCOVERY_QUERY_CHARS = 256
MAX_DISCOVERY_PATHS = 4
MAX_DISCOVERY_PATH_CHARS = 256
MAX_SEARCH_RESULTS = 20
MAX_OBSERVATION_BYTES = 8192
MAX_FILE_BYTES = 4096
MAX_FILE_LINES = 200
MAX_SNIPPET_CHARS = 240
DISCOVERY_PROVIDER_TIMEOUT_SECONDS = 120

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_COMMAND_SYNTAX_RE = re.compile(r"(?:&&|\|\||;|\$\(|`|[<>])")
_SEARCH_LINE_RE = re.compile(r"^(.+?):([0-9]+):(.*)$")


class DiscoveryContractError(ValueError):
    pass


@dataclass(frozen=True)
class DiscoveryRequest:
    action: str
    query: str | None = None
    paths: tuple[str, ...] = ()
    path: str | None = None


@dataclass(frozen=True)
class SearchHit:
    path: str
    line_number: int
    snippet: str


@dataclass(frozen=True)
class DiscoveryObservation:
    action: str
    status: str
    paths: tuple[str, ...] = ()
    hits: tuple[SearchHit, ...] = ()
    content: str | None = None
    truncated: bool = False
    reason: str | None = None

    @property
    def result_count(self) -> int:
        return (
            len(self.hits)
            if self.action == "search_text"
            else (1 if self.content is not None else 0)
        )

    def materialization_paths(self) -> tuple[str, ...]:
        values = (
            [hit.path for hit in self.hits]
            if self.action == "search_text"
            else list(self.paths)
        )
        return tuple(dict.fromkeys(value for value in values if value))


class DiscoveryTurnGuard:
    def __init__(self) -> None:
        self._used = False

    def claim(self) -> None:
        if self._used:
            raise DiscoveryContractError("discovery_turn_already_used")
        self._used = True


def build_discovery_prompt(task_description: str, project_context: str = "") -> str:
    task = _bounded_text(task_description, 1800)
    context = _bounded_text(project_context, 700)
    return (
        "READ-ONLY DISCOVERY ONLY. Return exactly one JSON object and no prose.\n"
        'Allowed: {"action":"search_text","query":"...","paths":["..."]}, '
        '{"action":"read_file","path":"..."}, or {"action":"stop"}.\n'
        "Use relative paths in the admitted workspace. Return no plan, shell "
        "command, mutation, old/new, target ID, selector, offsets, or tests.\n"
        "Choose at most one action; it executes once, then Planning resumes.\n"
        f"TASK:\n{task}\nCURRENT PLANNING CONTEXT:\n{context}"
    )


def parse_discovery_request(output_text: str) -> DiscoveryRequest:
    text = str(output_text or "").strip()
    if not text:
        raise DiscoveryContractError("discovery_output_empty")
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DiscoveryContractError("discovery_output_not_json") from exc
    if not isinstance(payload, dict):
        raise DiscoveryContractError("discovery_output_not_object")

    action = payload.get("action")
    if not isinstance(action, str):
        raise DiscoveryContractError("discovery_action_missing")
    action = action.strip().lower()
    if action == "stop":
        if set(payload) != {"action"}:
            raise DiscoveryContractError("discovery_stop_has_extra_fields")
        return DiscoveryRequest(action=action)
    if action == "search_text":
        if set(payload) != {"action", "query", "paths"}:
            raise DiscoveryContractError("discovery_search_shape_invalid")
        query = _validate_query(payload.get("query"))
        raw_paths = payload.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            raise DiscoveryContractError("discovery_search_paths_invalid")
        if len(raw_paths) > MAX_DISCOVERY_PATHS:
            raise DiscoveryContractError("discovery_search_path_count_exceeded")
        paths = tuple(_validate_declared_path(value) for value in raw_paths)
        if len(set(paths)) != len(paths):
            raise DiscoveryContractError("discovery_search_paths_duplicated")
        return DiscoveryRequest(action=action, query=query, paths=paths)
    if action == "read_file":
        if set(payload) != {"action", "path"}:
            raise DiscoveryContractError("discovery_read_shape_invalid")
        return DiscoveryRequest(
            action=action, path=_validate_declared_path(payload.get("path"))
        )
    raise DiscoveryContractError("discovery_action_unsupported")


def execute_discovery_request(
    project_dir: Path,
    request: DiscoveryRequest,
) -> DiscoveryObservation:
    root = Path(project_dir).resolve()
    if request.action == "stop":
        return DiscoveryObservation("stop", "stopped", reason="provider_stop")
    if request.action == "search_text":
        return _execute_search(root, request)
    if request.action == "read_file":
        return _execute_read_file(root, request)
    raise DiscoveryContractError("discovery_action_unsupported")


def run_discovery_stage(
    *,
    ctx: Any,
    planning_timeout_seconds: int,
    extract_structured_text: Callable[[Any], str],
    planner_service: Any,
    emit_phase_event: Callable[..., Any],
) -> DiscoveryObservation:
    if getattr(ctx, "read_only_discovery_completed", False):
        raise DiscoveryContractError("discovery_turn_already_used")
    if ctx.runtime_service is None:
        raise DiscoveryContractError("read_only_discovery_runtime_unavailable")
    ctx.read_only_discovery_completed = True
    prompt = build_discovery_prompt(
        ctx.prompt, ctx.orchestration_state.project_context or ""
    )
    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="INFO",
        phase="planning",
        message="[ORCHESTRATION] Running one bounded read-only discovery action",
        details={
            "stage": "read_only_discovery",
            "max_discovery_turns": 1,
            "prompt_chars": len(prompt),
        },
    )
    result = asyncio.run(
        planner_service._execute_task_with_planning_lock(
            ctx.runtime_service,
            prompt,
            timeout_seconds=min(
                planning_timeout_seconds, DISCOVERY_PROVIDER_TIMEOUT_SECONDS
            ),
            reuse_task_session=False,
            direct_planning_state={"direct_unavailable": True},
            diagnostic_label="PLANNING_DISCOVERY",
            diagnostic_metadata={
                "session_id": ctx.session_id,
                "task_id": ctx.task_id,
                "task_execution_id": ctx.task_execution_id,
                "stage": "read_only_discovery",
                "max_discovery_turns": 1,
            },
        )
    )
    request = parse_discovery_request(
        discovery_output_text(result, extract_structured_text)
    )
    observation = execute_discovery_request(
        Path(ctx.orchestration_state.project_dir), request
    )
    ctx.read_only_observation = observation
    return observation


def fail_closed_discovery(
    *,
    ctx: Any,
    reason: str,
    detail: str,
    aborted_status: Any,
    emit_phase_event: Callable[..., Any],
    finalize_failure: Callable[..., Any],
) -> dict[str, Any]:
    ctx.orchestration_state.status = aborted_status
    ctx.orchestration_state.abort_reason = f"{reason}: {detail}"[:1000]
    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="ERROR",
        phase="planning",
        message="[ORCHESTRATION] Read-only discovery failed closed",
        details={
            "reason": reason,
            "detail": detail[:500],
            "max_discovery_turns": 1,
            "discovery_repair_count": 0,
            "discovery_reflection_count": 0,
        },
    )
    finalize_failure(ctx=ctx, failure_type=reason, failure_reason=detail)
    if ctx.restore_workspace_snapshot_if_needed:
        ctx.restore_workspace_snapshot_if_needed("read-only discovery failed closed")
    return {"status": "failed", "reason": reason}


def materialize_observation_source_context(
    *,
    project_dir: Path,
    prompt: str,
    planner_contract: Any,
    observation: DiscoveryObservation,
    materialize: Callable[..., Any],
) -> Any:
    paths = observation.materialization_paths()
    hints = (
        "\n".join(hit.snippet for hit in observation.hits)
        if observation.action == "search_text"
        else str(observation.content or "")
    )[:2000]
    return materialize(
        project_dir,
        task_description=f"{prompt}\n\n{hints}" if hints else prompt,
        planner_contract=planner_contract,
        supporting_paths=paths,
    )


def render_discovery_observation(observation: DiscoveryObservation | None) -> str:
    if observation is None:
        return ""
    lines = [
        "## READ-ONLY OBSERVATION",
        "This is bounded, current-workspace evidence from one pre-planning read-only action.",
        "It is advisory context only; current source materialization and all existing validation remain authoritative.",
        f"action: {observation.action}",
        f"status: {observation.status}",
    ]
    if observation.reason:
        lines.append(f"reason: {observation.reason}")
    if observation.action == "search_text":
        lines.append(f"result_count: {observation.result_count}")
        for hit in observation.hits:
            lines.append(f"- {hit.path}:{hit.line_number}: {hit.snippet}")
    elif observation.action == "read_file":
        for path in observation.paths:
            lines.append(f"path: {path}")
        lines.append("content:")
        lines.append(observation.content or "(empty)")
    if observation.truncated:
        lines.append("truncated: true")
    return _bounded_text("\n".join(lines), MAX_OBSERVATION_BYTES)


def discovery_output_text(
    result: Any, extract_structured_text: Callable[[Any], str]
) -> str:
    if not isinstance(result, dict):
        raise DiscoveryContractError("discovery_provider_result_invalid")
    raw = result.get("output")
    if isinstance(raw, dict) and "action" in raw:
        return json.dumps(raw, separators=(",", ":"))
    if isinstance(raw, str):
        return raw
    text = extract_structured_text(raw)
    if isinstance(text, str) and text.strip():
        return text
    raise DiscoveryContractError("discovery_provider_output_missing")


def _validate_query(value: Any) -> str:
    if not isinstance(value, str):
        raise DiscoveryContractError("discovery_query_invalid")
    query = value.strip()
    if not query or len(query) > MAX_DISCOVERY_QUERY_CHARS:
        raise DiscoveryContractError("discovery_query_bound_exceeded")
    if _CONTROL_RE.search(query) or _COMMAND_SYNTAX_RE.search(query):
        raise DiscoveryContractError("discovery_query_command_syntax")
    return query


def _validate_declared_path(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_DISCOVERY_PATH_CHARS:
        raise DiscoveryContractError("discovery_path_bound_exceeded")
    import app.services.orchestration.validation.path_authority as path_authority

    try:
        return path_authority.declare(value).value
    except (path_authority.PathAuthorityError, TypeError, ValueError) as exc:
        raise DiscoveryContractError("discovery_path_unsafe") from exc


def _observe_entry(
    root: Path, relative_path: str, *, require_file: bool = False
) -> Any:
    import app.services.orchestration.validation.path_authority as path_authority

    try:
        canonical = path_authority.declare(relative_path)
        evidence = path_authority.observe(root, canonical)
    except path_authority.PathAuthorityError as exc:
        raise DiscoveryContractError("discovery_path_observation_failed") from exc
    if evidence.symlink_segment:
        raise DiscoveryContractError("discovery_path_symlink")
    if not evidence.exists:
        raise DiscoveryContractError("discovery_path_missing")
    if require_file and evidence.entry_type != path_authority.EntryType.REGULAR_FILE:
        raise DiscoveryContractError("discovery_path_not_regular_file")
    if not require_file and evidence.entry_type not in {
        path_authority.EntryType.REGULAR_FILE,
        path_authority.EntryType.DIRECTORY,
    }:
        raise DiscoveryContractError("discovery_scope_not_readable")
    return evidence


def _execute_search(root: Path, request: DiscoveryRequest) -> DiscoveryObservation:
    assert request.query is not None
    for relative_path in request.paths:
        _observe_entry(root, relative_path)
    executable = shutil.which("rg") or "rg"
    argv = [
        executable,
        "-n",
        "--no-heading",
        "--with-filename",
        "--color",
        "never",
        "--max-count",
        str(MAX_SEARCH_RESULTS),
        "--max-columns",
        str(MAX_SNIPPET_CHARS),
        "--max-columns-preview",
        "--",
        request.query,
        *request.paths,
    ]
    try:
        completed = subprocess.run(
            argv,
            cwd=str(root),
            capture_output=True,
            shell=False,
            timeout=settings.READ_ONLY_INSPECTION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiscoveryContractError("discovery_search_execution_failed") from exc
    if completed.returncode not in (0, 1):
        raise DiscoveryContractError("discovery_search_failed")
    raw = bytes(completed.stdout or b"")
    truncated = len(raw) > MAX_OBSERVATION_BYTES
    text = raw[:MAX_OBSERVATION_BYTES].decode("utf-8", errors="replace")
    hits: list[SearchHit] = []
    for line in text.splitlines():
        match = _SEARCH_LINE_RE.match(line)
        if not match:
            if line.strip():
                raise DiscoveryContractError("discovery_search_output_invalid")
            continue
        relative_path, raw_line_number, snippet = match.groups()
        normalized_path = _validate_declared_path(relative_path)
        _observe_entry(root, normalized_path, require_file=True)
        try:
            line_number = int(raw_line_number)
        except ValueError as exc:
            raise DiscoveryContractError("discovery_search_line_invalid") from exc
        if line_number < 1:
            raise DiscoveryContractError("discovery_search_line_invalid")
        hits.append(
            SearchHit(
                path=normalized_path,
                line_number=line_number,
                snippet=_bounded_text(snippet, MAX_SNIPPET_CHARS),
            )
        )
        if len(hits) >= MAX_SEARCH_RESULTS:
            truncated = True
            break
    return DiscoveryObservation(
        action="search_text",
        status="completed",
        paths=tuple(request.paths),
        hits=tuple(hits),
        truncated=truncated,
        reason="no_matches" if not hits else None,
    )


def _execute_read_file(root: Path, request: DiscoveryRequest) -> DiscoveryObservation:
    assert request.path is not None
    _observe_entry(root, request.path, require_file=True)
    full_path = root.joinpath(*request.path.split("/"))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(full_path, flags)
    except OSError as exc:
        raise DiscoveryContractError("discovery_file_open_failed") from exc
    truncated = False
    try:
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_FILE_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                truncated = True
                break
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            before.st_size,
        ):
            raise DiscoveryContractError("discovery_source_changed_during_read")
    except OSError as exc:
        raise DiscoveryContractError("discovery_file_read_failed") from exc
    finally:
        os.close(descriptor)
    content = b"".join(chunks)[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
    lines = content.splitlines(keepends=True)
    if len(lines) > MAX_FILE_LINES:
        content = "".join(lines[:MAX_FILE_LINES])
        truncated = True
    return DiscoveryObservation(
        action="read_file",
        status="completed",
        paths=(request.path,),
        content=content,
        truncated=truncated,
    )


def _bounded_text(value: Any, maximum: int) -> str:
    text = str(value or "")
    if len(text) <= maximum:
        return text
    return text[:maximum]
