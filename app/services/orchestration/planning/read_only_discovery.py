from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Callable, Mapping

from app.config import settings
from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_EXISTING,
    SOURCE_STATUS_NEW,
    TARGET_HINT_ABSENT,
    PlannerSourceMaterialization,
    extract_source_target_hints,
)
from app.services.workspace.workspace_paths import (
    HYDRATION_EXCLUDED_NAMES,
    is_hydration_excluded_path,
)

MAX_DISCOVERY_QUERY_CHARS = 256
MAX_DISCOVERY_PATHS = 4
MAX_DISCOVERY_PATH_CHARS = 256
MAX_SEARCH_RESULTS = 20
MAX_OBSERVATION_BYTES = 8192
MAX_FILE_BYTES = 4096
MAX_FILE_LINES = 200
MAX_SNIPPET_CHARS = 240
DISCOVERY_PROVIDER_TIMEOUT_SECONDS = 120

DISCOVERY_ADMISSION_SKIPPED = "SKIPPED_SUFFICIENT_GROUNDING"
DISCOVERY_ADMISSION_REQUIRED = "REQUIRED_MISSING_GROUNDING"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_COMMAND_SYNTAX_RE = re.compile(r"(?:&&|\|\||;|\$\(|`|[<>])")
_SEARCH_LINE_RE = re.compile(r"^(.+?):([0-9]+):(.*)$")


class DiscoveryContractError(ValueError):
    """A bounded discovery failure that must terminate the current attempt."""

    failure_category = "discovery_terminal_failure"
    terminal_failure = True


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


@dataclass(frozen=True)
class DiscoveryAdmission:
    status: str
    reason: str


def assess_discovery_admission(
    *,
    prompt: str,
    planner_contract: Mapping[str, Any] | None,
    materialization: PlannerSourceMaterialization,
) -> DiscoveryAdmission:
    """Decide whether current bounded source facts justify one discovery call."""

    expected = tuple(
        item for item in materialization.files if bool(getattr(item, "expected", False))
    )
    if not expected:
        return DiscoveryAdmission(
            DISCOVERY_ADMISSION_REQUIRED, "no_explicit_source_or_creation_path"
        )

    if all(
        getattr(item, "status", None) == SOURCE_STATUS_NEW
        and bool(getattr(item, "creation_authorized", False))
        for item in expected
    ):
        return DiscoveryAdmission(
            DISCOVERY_ADMISSION_SKIPPED, "authorized_new_paths_are_grounded"
        )
    if any(
        getattr(item, "status", None) != SOURCE_STATUS_EXISTING for item in expected
    ):
        return DiscoveryAdmission(
            DISCOVERY_ADMISSION_REQUIRED, "expected_source_status_not_grounded"
        )

    existing = expected
    if any(
        not getattr(item, "version_identity", None)
        or not getattr(item, "content_hash", None)
        or getattr(item, "content", None) is None
        or bool(getattr(item, "truncated", False))
        for item in existing
    ):
        return DiscoveryAdmission(
            DISCOVERY_ADMISSION_REQUIRED, "current_source_is_missing_or_truncated"
        )

    hints = extract_source_target_hints(prompt, planner_contract=planner_contract)
    if hints:
        from app.services.orchestration.planning.semantic_target_inventory import (
            build_semantic_target_inventory,
        )

        hinted_paths = {hint.target_path for hint in hints if hint.target_path}
        target_records = tuple(
            item
            for item in existing
            if (
                not hinted_paths or getattr(item, "relative_path", None) in hinted_paths
            )
            and (
                getattr(item, "target_hint", None)
                or getattr(item, "target_hint_status", None) != TARGET_HINT_ABSENT
            )
        )
        if not target_records:
            return DiscoveryAdmission(
                DISCOVERY_ADMISSION_REQUIRED, "semantic_target_not_materialized"
            )
        inventory = build_semantic_target_inventory(materialization)
        eligible_paths = set(inventory.eligible_existing_mutable_paths)
        for item in target_records:
            if (
                getattr(item, "target_match_count", 0) != 1
                or not bool(getattr(item, "target_included", False))
                or getattr(item, "relative_path", None) not in eligible_paths
            ):
                return DiscoveryAdmission(
                    DISCOVERY_ADMISSION_REQUIRED, "semantic_target_is_not_unique"
                )

    return DiscoveryAdmission(
        DISCOVERY_ADMISSION_SKIPPED, "explicit_current_source_is_sufficient"
    )


def emit_discovery_admission(
    *,
    ctx: Any,
    admission: DiscoveryAdmission,
    emit_phase_event: Callable[..., Any],
) -> None:
    """Record admission without implying that a provider action occurred."""

    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="INFO",
        phase="planning",
        message=f"[ORCHESTRATION] Discovery admission: {admission.status}",
        details={
            "stage": "read_only_discovery_admission",
            "discovery_admission": admission.status,
            "reason": admission.reason,
            "max_discovery_turns": 1,
            "discovery_turns_used": 0,
        },
    )


def prepare_discovery_context(
    *,
    ctx: Any,
    planning_timeout_seconds: int,
    extract_structured_text: Callable[[Any], str],
    planner_service: Any,
    emit_phase_event: Callable[..., Any],
    materialize: Callable[..., Any],
) -> None:
    """Materialize once before admission and refresh only after observation."""

    source_cache: dict[str, str] = {}
    materialization = materialize(
        project_dir=Path(ctx.orchestration_state.project_dir),
        task_description=ctx.prompt,
        planner_contract=ctx.planner_contract,
        supporting_paths=(),
        source_cache=source_cache,
    )
    admission = assess_discovery_admission(
        prompt=ctx.prompt,
        planner_contract=ctx.planner_contract,
        materialization=materialization,
    )
    emit_discovery_admission(
        ctx=ctx, admission=admission, emit_phase_event=emit_phase_event
    )
    if admission.status == DISCOVERY_ADMISSION_REQUIRED:
        observation = run_discovery_stage(
            ctx=ctx,
            planning_timeout_seconds=planning_timeout_seconds,
            extract_structured_text=extract_structured_text,
            planner_service=planner_service,
            emit_phase_event=emit_phase_event,
        )
        materialization = materialize_observation_source_context(
            project_dir=Path(ctx.orchestration_state.project_dir),
            prompt=ctx.prompt,
            planner_contract=ctx.planner_contract,
            observation=observation,
            materialize=materialize,
            source_cache=source_cache,
        )
    ctx.planner_source_materialization = materialization


def build_discovery_prompt(task_description: str, project_context: str = "") -> str:
    task = _bounded_text(task_description, 1800)
    context = _bounded_text(project_context, 700)
    return (
        "READ-ONLY DISCOVERY ONLY. Return exactly one JSON object and no prose.\n"
        'Allowed: {"action":"search_text","query":"...","paths":["..."]}, '
        '{"action":"read_file","path":"..."}, or {"action":"stop"}.\n'
        "For search_text, query is a bounded ripgrep-compatible text/regex "
        "pattern; spaces are literal, and use `|` for alternatives when useful.\n"
        "Use relative paths in the admitted workspace; search_text paths may "
        "name existing files or directories, while read_file requires one "
        "existing regular file. Return no plan, shell "
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
        paths = tuple(_validate_search_scope(value) for value in raw_paths)
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
    try:
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
    except DiscoveryContractError:
        raise
    except Exception as exc:
        error = DiscoveryContractError(f"discovery_provider_failed: {str(exc)[:500]}")
        diagnostics = getattr(exc, "runtime_diagnostics", None)
        if isinstance(diagnostics, dict):
            error.runtime_diagnostics = diagnostics
        provider_classification = getattr(exc, "provider_failure_classification", None)
        if provider_classification:
            error.provider_failure_classification = provider_classification
        raise error from exc
    if not isinstance(result, dict) or result.get("status") != "completed":
        detail = ""
        if isinstance(result, dict):
            detail = str(
                result.get("error")
                or result.get("failure_category")
                or "discovery_provider_failed"
            )
        else:
            detail = "discovery_provider_result_invalid"
        error = DiscoveryContractError(detail[:500])
        if isinstance(result, dict):
            diagnostics = result.get("runtime_diagnostics")
            if isinstance(diagnostics, dict):
                error.runtime_diagnostics = diagnostics
            provider_classification = result.get("provider_failure_classification")
            if provider_classification:
                error.provider_failure_classification = provider_classification
        raise error
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
    return {
        "status": "failed",
        "reason": reason,
        "failure_category": "discovery_terminal_failure",
        "terminal_failure": True,
        "discovery_turns_used": 1,
    }


def materialize_observation_source_context(
    *,
    project_dir: Path,
    prompt: str,
    planner_contract: Any,
    observation: DiscoveryObservation,
    materialize: Callable[..., Any],
    source_cache: dict[str, str] | None = None,
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
        source_cache=source_cache,
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


def _validate_search_scope(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_DISCOVERY_PATH_CHARS:
        raise DiscoveryContractError("discovery_path_bound_exceeded")
    normalized = value.rstrip("/")
    if not normalized:
        raise DiscoveryContractError("discovery_path_unsafe")
    return _validate_declared_path(normalized)


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
    if evidence.trust_class is not path_authority.TrustClass.PRODUCT:
        raise DiscoveryContractError("discovery_path_excluded")
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
    executable = shutil.which("rg")
    if executable is None:
        return _execute_python_search(root, request)
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
        *(
            item
            for name in sorted(HYDRATION_EXCLUDED_NAMES)
            for item in ("--glob", f"!**/{name}", "--glob", f"!**/{name}/**")
        ),
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


def _execute_python_search(
    root: Path, request: DiscoveryRequest
) -> DiscoveryObservation:
    """Search with the standard library when the optional rg binary is absent."""
    assert request.query is not None
    import app.services.orchestration.validation.path_authority as path_authority

    try:
        pattern = re.compile(request.query)
    except re.error as exc:
        raise DiscoveryContractError("discovery_search_failed") from exc

    deadline = time.monotonic() + settings.READ_ONLY_INSPECTION_TIMEOUT_SECONDS
    hits: list[SearchHit] = []
    output_bytes = 0
    truncated = False
    stop = False

    def candidate_files():
        for relative_path in request.paths:
            evidence = _observe_entry(root, relative_path)
            full_path = root.joinpath(*relative_path.split("/"))
            if evidence.entry_type == path_authority.EntryType.REGULAR_FILE:
                yield full_path, relative_path
                continue
            for directory, dir_names, file_names in os.walk(
                full_path, topdown=True, followlinks=False
            ):
                dir_path = Path(directory)
                dir_names[:] = sorted(
                    name
                    for name in dir_names
                    if not (dir_path / name).is_symlink()
                    and not is_hydration_excluded_path(
                        (dir_path / name).relative_to(root)
                    )
                )
                for file_name in sorted(file_names):
                    candidate = dir_path / file_name
                    if candidate.is_symlink() or is_hydration_excluded_path(
                        candidate.relative_to(root)
                    ):
                        continue
                    yield candidate, candidate.relative_to(root).as_posix()

    for full_path, relative_path in candidate_files():
        if time.monotonic() > deadline:
            raise DiscoveryContractError("discovery_search_execution_failed")
        _observe_entry(root, relative_path, require_file=True)
        try:
            handle = full_path.open("rb")
        except OSError as exc:
            raise DiscoveryContractError("discovery_search_execution_failed") from exc
        file_hit_start = len(hits)
        binary_seen = False
        with handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if time.monotonic() > deadline:
                    raise DiscoveryContractError("discovery_search_execution_failed")
                binary_seen = binary_seen or b"\x00" in raw_line
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not pattern.search(line):
                    continue
                if binary_seen:
                    raise DiscoveryContractError("discovery_search_output_invalid")
                snippet = _bounded_text(line, MAX_SNIPPET_CHARS)
                rendered = f"{relative_path}:{line_number}:{snippet}\n".encode(
                    "utf-8", errors="replace"
                )
                if output_bytes + len(rendered) > MAX_OBSERVATION_BYTES:
                    truncated = True
                    stop = True
                    break
                output_bytes += len(rendered)
                hits.append(
                    SearchHit(
                        path=relative_path,
                        line_number=line_number,
                        snippet=snippet,
                    )
                )
                if len(hits) >= MAX_SEARCH_RESULTS:
                    truncated = True
                    stop = True
                    break
        if binary_seen and len(hits) > file_hit_start:
            raise DiscoveryContractError("discovery_search_output_invalid")
        if stop:
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
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum:
        return text
    end = maximum
    while end > 0 and (encoded[end] & 0xC0) == 0x80:
        end -= 1
    return encoded[:end].decode("utf-8", errors="ignore")
