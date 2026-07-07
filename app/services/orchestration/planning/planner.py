"""Planner-stage helpers for orchestration."""

from __future__ import annotations

import asyncio
import ast
from contextlib import asynccontextmanager, contextmanager
import errno
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.services.workspace.file_lock import fcntl
from ..policy import (
    MINIMAL_PLANNING_TIMEOUT_SECONDS,
    PLANNING_REPAIR_NO_OUTPUT_TIMEOUT_SECONDS,
    PLANNING_REPAIR_TIMEOUT_SECONDS,
    STRICT_JSON_RETRY_TIMEOUT_SECONDS,
    ULTRA_MINIMAL_PLANNING_TIMEOUT_SECONDS,
)
from app.config import settings
from app.services.orchestration.operations.file_ops_contract import (
    operation_has_file_op_path,
)
from app.services.orchestration.planning.planning_prompts import (
    PLANNING_VALID_MINIMAL_JSON_EXAMPLE,
    VERIFICATION_PROFILE_PLANNING_CONTRACT_LINE,
    build_minimal_planning_prompt as _build_minimal_planning_prompt,
    build_ultra_minimal_planning_prompt as _build_ultra_minimal_planning_prompt,
)
from app.services.orchestration.planning.repair_prompts import (
    PLANNING_REPAIR_COMPACT_MALFORMED_OUTPUT_CHARS,
    PLANNING_REPAIR_MAX_MALFORMED_OUTPUT_CHARS,
    PLANNING_REPAIR_PROMPT_MAX_CHARS,
    REPAIR_PROMPT_MAX_CHARS,
    build_compact_planning_repair_prompt as _build_compact_planning_repair_prompt,
    build_planning_repair_prompt as _build_planning_repair_prompt,
    build_planning_repair_prompt_with_metadata as _build_planning_repair_prompt_with_metadata,
    compact_invalid_output_excerpt as _compact_invalid_output_excerpt,
    render_repair_knowledge_block as _render_repair_knowledge_block,
)
from app.services.orchestration.planning.repair_evidence import (
    record_pending_planning_repair_triplet,
)
from app.services.project.index_service import (
    build_project_index,
    render_project_structure_capsule,
)
from .plan_sanitizer import (
    _command_is_placeholder_only,
    _command_is_plain_english_file_instruction,
    _command_is_python_c_content_write,
    _looks_like_safe_verification_command,
    _step_expected_files_are_structurally_empty,
    _step_is_implementation_heavy,
    _step_is_readonly_inspection,
    _uses_background_process,
    sanitize_common_plan_issues as _sanitize_common_plan_issues,
)

_logger = logging.getLogger(__name__)

MINIMAL_PLANNING_PROMPT_TOKEN_DIAGNOSTIC_THRESHOLD = 6000
DIRECT_PLANNING_PROMPT_CHAR_CAP = 12000
OPENCLAW_SESSION_LOCK_MARKERS = (
    "session file locked",
    "sessions.json.lock",
)
OPENCLAW_PLANNING_LOCK_PATH = Path(
    os.environ.get(
        "ORCHESTRATOR_OPENCLAW_PLANNING_LOCK",
        str(Path(tempfile.gettempdir()) / "orchestrator-openclaw-planning.lock"),
    )
)
OPENCLAW_PLANNING_LOCK_ACQUIRE_TIMEOUT_SECONDS = float(
    os.environ.get("ORCHESTRATOR_OPENCLAW_PLANNING_LOCK_ACQUIRE_TIMEOUT_SECONDS", "30")
)
OPENCLAW_PLANNING_LOCK_POLL_SECONDS = float(
    os.environ.get("ORCHESTRATOR_OPENCLAW_PLANNING_LOCK_POLL_SECONDS", "0.05")
)
STALE_REPLACE_REPAIR_DIAGNOSTIC_DIR = Path(
    "docs/roadmap/reports/runtime/planning-stale-replace-repair"
)
STALE_REPLACE_REPAIR_TIMEOUT_SECONDS = 120.0
WORKSPACE_PLAN_REFERENCE_RE = re.compile(
    r"(?i)(?:^|[\s`'\"(])(?:[A-Za-z0-9_./-]*/)?plan\.json(?:$|[\s`'\":,.)])"
)
PLANNING_STEP_REQUIRED_KEYS = (
    "step_number",
    "description",
    "commands",
    "verification",
    "rollback",
    "expected_files",
)


def _estimate_prompt_tokens(prompt: str) -> int:
    return max(0, (len(prompt or "") + 3) // 4)


def _run_coroutine_from_sync(coro):
    """Run a coroutine for sync callers, including callers already inside an event loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: Dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner, name="planner-sync-async-runner")
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


class PlanningRepairBudgetExceeded(RuntimeError):
    """Raised when the repair prompt exceeds the safe repair budget."""


class PlanningRepairNoOutputTimeout(TimeoutError):
    """Raised when a repair call produces no output before the no-output guard."""

    def __init__(self, message: str, diagnostics: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.runtime_diagnostics = diagnostics or {}


class PlanningRepairOutputContractViolation(RuntimeError):
    """Raised when repair returns prose or markdown-fenced JSON instead of a bare JSON array."""

    def __init__(self, message: str, diagnostics: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.runtime_diagnostics = diagnostics or {}


class PlannerService:
    """Planning-stage fallback and repair helpers."""

    _NON_RUNNABLE_COMMAND_PREFIXES = (
        "write ",
        "edit ",
        "create files",
        "create file",
        "set up ",
        "setup ",
        "implement ",
        "add component",
        "update component",
        "verify ",
        "check ",
        "ensure ",
        "confirm ",
    )

    @staticmethod
    def is_openclaw_lock_contention(value: Any) -> bool:
        if isinstance(value, dict):
            candidates = [
                value.get("error"),
                value.get("output"),
                value.get("stderr"),
                value.get("message"),
            ]
            diagnostics = value.get("diagnostics")
            if isinstance(diagnostics, dict):
                candidates.extend(
                    [
                        diagnostics.get("error"),
                        diagnostics.get("stderr"),
                        diagnostics.get("message"),
                    ]
                )
            return any(
                PlannerService.is_openclaw_lock_contention(candidate)
                for candidate in candidates
                if candidate is not None
            )

        text = str(value or "").lower()
        return any(marker in text for marker in OPENCLAW_SESSION_LOCK_MARKERS)

    @staticmethod
    @contextmanager
    def _openclaw_planning_lock():
        OPENCLAW_PLANNING_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OPENCLAW_PLANNING_LOCK_PATH.open("a", encoding="utf-8") as handle:
            wait_started_at = time.monotonic()
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            lock_diagnostics = {
                "planning_lock_path": str(OPENCLAW_PLANNING_LOCK_PATH),
                "planning_lock_wait_seconds": round(
                    time.monotonic() - wait_started_at, 3
                ),
            }
            try:
                yield lock_diagnostics
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    @asynccontextmanager
    async def _openclaw_planning_lock_async():
        OPENCLAW_PLANNING_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = OPENCLAW_PLANNING_LOCK_PATH.open("a", encoding="utf-8")
        wait_started_at = time.monotonic()
        acquired = False
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise

                elapsed = time.monotonic() - wait_started_at
                if elapsed >= OPENCLAW_PLANNING_LOCK_ACQUIRE_TIMEOUT_SECONDS:
                    diagnostics = {
                        "planning_lock_path": str(OPENCLAW_PLANNING_LOCK_PATH),
                        "planning_lock_wait_seconds": round(elapsed, 3),
                        "planning_lock_acquire_timeout_seconds": (
                            OPENCLAW_PLANNING_LOCK_ACQUIRE_TIMEOUT_SECONDS
                        ),
                        "timeout_boundary": "planning_lock_wait",
                    }
                    timeout_exc = TimeoutError(
                        "OpenClaw planning lock wait timed out after "
                        f"{OPENCLAW_PLANNING_LOCK_ACQUIRE_TIMEOUT_SECONDS:g}s: "
                        f"{OPENCLAW_PLANNING_LOCK_PATH}"
                    )
                    timeout_exc.runtime_diagnostics = diagnostics  # type: ignore[attr-defined]
                    raise timeout_exc

                remaining = OPENCLAW_PLANNING_LOCK_ACQUIRE_TIMEOUT_SECONDS - elapsed
                await asyncio.sleep(
                    min(OPENCLAW_PLANNING_LOCK_POLL_SECONDS, max(remaining, 0))
                )

            lock_diagnostics = {
                "planning_lock_path": str(OPENCLAW_PLANNING_LOCK_PATH),
                "planning_lock_wait_seconds": round(
                    time.monotonic() - wait_started_at, 3
                ),
            }
            try:
                yield lock_diagnostics
            finally:
                if acquired:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    @staticmethod
    def _attach_planning_lock_diagnostics(
        result: Dict[str, Any],
        lock_diagnostics: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not lock_diagnostics:
            return result
        diagnostics = result.get("diagnostics")
        if isinstance(diagnostics, dict):
            diagnostics.update(lock_diagnostics)
        result["_planning_lock_diagnostics"] = dict(lock_diagnostics)
        return result

    @staticmethod
    def _attach_planning_lock_exception_diagnostics(
        exc: Exception,
        lock_diagnostics: Dict[str, Any],
    ) -> None:
        if not lock_diagnostics:
            return
        diagnostics = getattr(exc, "runtime_diagnostics", None)
        if isinstance(diagnostics, dict):
            diagnostics.update(lock_diagnostics)
            return
        exc.runtime_diagnostics = dict(lock_diagnostics)  # type: ignore[attr-defined]

    @staticmethod
    def _should_try_direct_no_thinking_planning(
        runtime_service: Any, prompt_chars: int
    ) -> bool:
        if bool(getattr(runtime_service, "_disable_direct_planning", False)):
            _logger.info(
                "[PLANNING_DIRECT] skip: direct planning disabled for selected runtime"
            )
            return False
        if not settings.PLANNING_REPAIR_ENABLED:
            return False
        if not settings.PLANNING_REPAIR_BASE_URL.strip():
            return False
        if not settings.PLANNING_REPAIR_MODEL.strip():
            return False
        backend_metadata: Dict[str, Any] = {}
        get_backend_metadata = getattr(runtime_service, "get_backend_metadata", None)
        if callable(get_backend_metadata):
            try:
                backend_metadata = get_backend_metadata() or {}
            except Exception:
                backend_metadata = {}
        backend_name = str(backend_metadata.get("backend") or "").strip()
        if prompt_chars > DIRECT_PLANNING_PROMPT_CHAR_CAP:
            _logger.info(
                "[PLANNING_DIRECT] skip: prompt_chars=%d > cap=%d",
                prompt_chars,
                DIRECT_PLANNING_PROMPT_CHAR_CAP,
            )
            return False
        local_openclaw_skip_threshold = int(
            getattr(settings, "PLANNING_DIRECT_SKIP_PROMPT_CHAR_THRESHOLD", 0) or 0
        )
        if (
            backend_name == "local_openclaw"
            and local_openclaw_skip_threshold > 0
            and prompt_chars >= local_openclaw_skip_threshold
        ):
            _logger.info(
                "[PLANNING_DIRECT] skip: local_openclaw prompt_chars=%d >= threshold=%d",
                prompt_chars,
                local_openclaw_skip_threshold,
            )
            return False
        if (
            backend_name == "direct_ollama"
            and not settings.PLANNING_DIRECT_NO_THINKING_FOR_DIRECT_OLLAMA
        ):
            _logger.info(
                "[PLANNING_DIRECT] skip: direct no-thinking planning disabled "
                "for direct_ollama"
            )
            return False
        if backend_name not in {"local_openclaw", "direct_ollama"}:
            _logger.info(
                "[PLANNING_DIRECT] skip: backend_name=%r (not direct-capable)",
                backend_name,
            )
            return False
        return True

    @staticmethod
    def _is_no_model_output_planning_timeout(exc: Exception) -> bool:
        diagnostics = PlannerService._get_runtime_diagnostics(exc)
        if not diagnostics:
            return False
        if diagnostics.get("timed_out") is not True:
            return False
        stdout_chars = int(diagnostics.get("stdout_chars") or 0)
        stderr_has_model_content = diagnostics.get("stderr_contains_model_content")
        output_channel = str(diagnostics.get("output_channel_used") or "").lower()
        return (
            stdout_chars == 0
            and stderr_has_model_content is False
            and output_channel in {"", "none"}
        )

    @staticmethod
    def _monotonic() -> float:
        return time.monotonic()

    @classmethod
    async def _invoke_direct_no_thinking_planning(
        cls,
        runtime_service: Any,
        planning_prompt: str,
        *,
        timeout_budget_seconds: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        import time as _time

        import httpx

        base_url = settings.PLANNING_REPAIR_BASE_URL.rstrip("/")
        model = cls._direct_no_thinking_model(runtime_service)
        api_key = settings.PLANNING_REPAIR_API_KEY
        configured_direct_timeout = settings.PLANNING_REPAIR_TIMEOUT_SECONDS
        backend_metadata: Dict[str, Any] = {}
        get_backend_metadata = getattr(runtime_service, "get_backend_metadata", None)
        if callable(get_backend_metadata):
            try:
                backend_metadata = get_backend_metadata() or {}
            except Exception:
                backend_metadata = {}
        backend_name = str(backend_metadata.get("backend") or "").strip()
        local_openclaw_timeout = int(
            getattr(settings, "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", 0) or 0
        )
        if backend_name == "local_openclaw" and local_openclaw_timeout > 0:
            configured_direct_timeout = local_openclaw_timeout
        direct_timeout = configured_direct_timeout
        if timeout_budget_seconds is not None:
            direct_timeout = max(1, min(direct_timeout, int(timeout_budget_seconds)))
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": planning_prompt}],
            "temperature": 0.0,
            "max_tokens": 2048,
            "stream": False,
            "think": False,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        started_at = _time.monotonic()
        _logger.info(
            "[PLANNING_DIRECT] attempting direct no-thinking planning url=%s model=%s"
            " prompt_chars=%d timeout=%ds",
            f"{base_url}/chat/completions",
            model,
            len(planning_prompt),
            direct_timeout,
        )

        async def _do_request() -> Optional[str]:
            async with httpx.AsyncClient(timeout=direct_timeout) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
            resp.raise_for_status()
            return PlannerService._extract_chat_completion_content(resp.json())

        try:
            output = await asyncio.wait_for(
                _do_request(), timeout=float(direct_timeout)
            )
        except asyncio.TimeoutError:
            _logger.warning(
                "[PLANNING_DIRECT] wall-clock timeout after %ds; falling back to runtime",
                direct_timeout,
            )
            return None
        except Exception as exc:
            _logger.warning(
                "[PLANNING_DIRECT] failed after %.1fs (%s: %s); falling back to runtime",
                _time.monotonic() - started_at,
                type(exc).__name__,
                str(exc)[:200],
            )
            return None
        if not output.strip():
            _logger.warning(
                "[PLANNING_DIRECT] empty output after %.1fs; falling back to runtime",
                _time.monotonic() - started_at,
            )
            return None
        duration_seconds = _time.monotonic() - started_at
        _logger.info(
            "[PLANNING_DIRECT] success planning_direct=True backend=direct_chat_completions"
            " duration=%.1fs output_chars=%d",
            duration_seconds,
            len(output),
        )
        return {
            "output": output,
            "planning_direct": True,
            "planning_backend": "direct_chat_completions",
            "direct_planning_seconds": round(duration_seconds, 3),
            "direct_planning_prompt_chars": len(planning_prompt),
            "direct_planning_timeout_seconds": direct_timeout,
        }

    @classmethod
    async def _execute_task_with_planning_lock(
        cls,
        runtime_service: Any,
        prompt: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        direct_planning_state = kwargs.pop("direct_planning_state", None)
        if direct_planning_state is None:
            direct_planning_state = {}
        timeout_budget = kwargs.get("timeout_seconds")
        timeout_budget_seconds: Optional[float]
        if isinstance(timeout_budget, (int, float)) and timeout_budget > 0:
            timeout_budget_seconds = float(timeout_budget)
        else:
            timeout_budget_seconds = None

        should_try_direct = cls._should_try_direct_no_thinking_planning(
            runtime_service, len(prompt)
        )
        if should_try_direct and direct_planning_state.get("direct_unavailable"):
            _logger.info(
                "[PLANNING_DIRECT] skip: direct planning already unavailable in this planning phase"
            )
            should_try_direct = False

        if should_try_direct:
            direct_started_at = cls._monotonic()
            direct = await cls._invoke_direct_no_thinking_planning(
                runtime_service,
                prompt,
                timeout_budget_seconds=timeout_budget_seconds,
            )
            if direct is not None:
                return direct
            direct_planning_state["direct_unavailable"] = True
            direct_planning_state["direct_unavailable_after_prompt_chars"] = len(prompt)
            if timeout_budget_seconds is not None:
                elapsed_seconds = cls._monotonic() - direct_started_at
                direct_planning_state["direct_elapsed_seconds"] = round(
                    elapsed_seconds, 3
                )
                remaining_seconds = timeout_budget_seconds - elapsed_seconds
                if remaining_seconds <= 0:
                    raise TimeoutError(
                        "Direct planning fallback budget exhausted before OpenClaw "
                        f"fallback could start after {elapsed_seconds:.1f}s"
                    )
                adjusted_timeout = max(1, int(remaining_seconds))
                if adjusted_timeout < int(timeout_budget_seconds):
                    kwargs = {**kwargs, "timeout_seconds": adjusted_timeout}
                    _logger.info(
                        "[PLANNING_DIRECT] fallback budget adjusted timeout=%ds "
                        "after direct_elapsed=%.1fs original_timeout=%ss",
                        adjusted_timeout,
                        elapsed_seconds,
                        timeout_budget,
                    )
        async with cls._openclaw_planning_lock_async() as lock_diagnostics:
            try:
                result = await runtime_service.execute_task(prompt, **kwargs)
            except Exception as exc:
                cls._attach_planning_lock_exception_diagnostics(exc, lock_diagnostics)
                raise
            return cls._attach_planning_lock_diagnostics(result, lock_diagnostics)

    @staticmethod
    def select_prompt_profile(
        backend_name: Optional[str],
        model_family: Optional[str],
    ) -> str:
        backend = (backend_name or "").strip().lower()
        model = (model_family or "").strip().lower()
        capability = PlannerService.model_capability_label(backend, model)
        if backend == "local_openclaw" and capability == "local_qwen_small_strict":
            return "local_qwen_small_json_array"
        if backend == "local_openclaw" and ("qwen" in model or model == "local"):
            return "local_qwen_json_array"
        return "default"

    @staticmethod
    def model_capability_label(
        backend_name: Optional[str],
        model_family: Optional[str],
    ) -> str:
        backend = (backend_name or "").strip().lower()
        model = (model_family or "").strip().lower()
        if backend == "local_openclaw" and (
            model == "local"
            or "14b" in model
            or "q5_k_m" in model
            or "qwen2.5-coder" in model
        ):
            return "local_qwen_small_strict"
        if backend == "local_openclaw" and "qwen" in model:
            return "local_qwen_capable"
        if "gpt" in model:
            return "remote_structured_capable"
        return "standard"

    @staticmethod
    def apply_prompt_profile(prompt: str, prompt_profile: str = "default") -> str:
        if prompt_profile not in {
            "local_qwen_json_array",
            "local_qwen_small_json_array",
        }:
            return prompt

        profiled = (
            f"{prompt.rstrip()}\n\n"
            "Output discipline for this model:\n"
            "11. Return only a JSON array of steps. Do not wrap it in an object.\n"
            "12. Do not include `payloads`, `text`, `finalAssistantVisibleText`, markdown prose, or commentary.\n"
            "13. The first non-whitespace character must be `[` and the last must be `]`.\n"
            "14. Do not describe the file contents outside the JSON fields for each step.\n"
        )
        if prompt_profile == "local_qwen_small_json_array":
            profiled += (
                "15. Use the smallest valid plan shape for the workflow profile; prefer 2-3 concrete steps over broad multi-step choreography.\n"
                "16. Prefer typed `ops` for file writes and one bounded Python verification command per implementation step.\n"
                "17. Do not include speculative future files in `expected_files`; list only files materialized by typed ops or already present in the workspace.\n"
            )
        return profiled

    @staticmethod
    def looks_salvageable_planning_output(output_text: str) -> bool:
        """Heuristic for whether a failed planning response still contains useful plan content."""

        text = (output_text or "").strip()
        if not text:
            return False
        lowered = text.lower()
        if '"aborted": true' in lowered and "finalassistantvisibletext" not in lowered:
            return False
        planning_markers = (
            '"step_number"',
            '"commands"',
            '"expected_files"',
            '"description"',
            "finalassistantvisibletext",
            "```json",
            "| # | step |",
        )
        return any(
            marker in lowered for marker in planning_markers
        ) or lowered.startswith("[")

    @staticmethod
    def should_retry_with_minimal_prompt(
        planning_result: Dict[str, Any], output_text: str = ""
    ) -> bool:
        error_text = (planning_result.get("error") or "").lower()
        combined_text = f"{error_text}\n{(output_text or '').lower()}"
        retry_markers = (
            "context window exceeded",
            "request timed out before a response was generated",
            "timed out",
            "timeout",
        )
        return any(marker in combined_text for marker in retry_markers)

    @staticmethod
    def should_start_with_minimal_prompt(
        task_prompt: str,
        project_context: str,
    ) -> bool:
        combined = f"{task_prompt or ''}\n{project_context or ''}"
        lowered_context = (project_context or "").lower()
        lowered_task = (task_prompt or "").lower()
        implementation_markers = (
            "set up",
            "setup",
            "build",
            "create",
            "implement",
            "frontend",
            "backend",
            "fastapi",
            "node.js",
            "react",
            "vite",
            "clean architecture",
        )
        dense_context_markers = (
            "hydrated baseline sources available directly in this workspace",
            "canonical baseline available",
            "earlier ordered tasks already completed and can be reused",
            "promoted workspaces already accepted into the project baseline",
        )
        compact_task_markers = (
            "regression test",
            "test suite",
            "integration test",
            "spec file",
            "unit test",
            "inspection",
            "analyze",
            "review",
        )
        task_looks_implementation_heavy = any(
            marker in lowered_task for marker in implementation_markers
        )
        return (
            len(combined) > 8000
            or len(project_context or "") > 3500
            or any(marker in lowered_context for marker in dense_context_markers)
            or (
                any(marker in lowered_task for marker in compact_task_markers)
                and not task_looks_implementation_heavy
            )
        )

    _uses_background_process = staticmethod(_uses_background_process)
    _command_is_plain_english_file_instruction = staticmethod(
        _command_is_plain_english_file_instruction
    )
    _looks_like_safe_verification_command = staticmethod(
        _looks_like_safe_verification_command
    )
    _command_is_placeholder_only = staticmethod(_command_is_placeholder_only)
    _command_is_python_c_content_write = staticmethod(
        _command_is_python_c_content_write
    )
    _step_is_readonly_inspection = staticmethod(_step_is_readonly_inspection)
    _step_is_implementation_heavy = staticmethod(_step_is_implementation_heavy)
    _step_expected_files_are_structurally_empty = staticmethod(
        _step_expected_files_are_structurally_empty
    )

    @classmethod
    def sanitize_common_plan_issues(
        cls, plan: Optional[List[Dict[str, Any]]], task_prompt: str = ""
    ) -> List[Dict[str, Any]]:
        return _sanitize_common_plan_issues(plan, task_prompt)

    @staticmethod
    def find_immediate_repair_step_issues(
        plan: Optional[List[Dict[str, Any]]],
        project_dir: Optional[Path] = None,
    ) -> Dict[str, List[int]]:
        from app.services.orchestration.validation.validator import ValidatorService

        issues: Dict[str, List[int]] = {
            "non_runnable_steps": [],
            "background_process_steps": [],
            "placeholder_only_steps": [],
            "weak_verification_steps": [],
            "prefer_typed_ops_steps": [],
            "stale_replace_ops_steps": [],
            "empty_replace_old_text_steps": [],
            "test_assertion_loss_ops_steps": [],
            "test_deletion_ops_steps": [],
            "fake_verification_artifact_steps": [],
        }
        for index, step in enumerate(plan or [], start=1):
            step_number = int(step.get("step_number") or index)
            commands = step.get("commands", []) or []
            expected_files = step.get("expected_files", []) or []
            ops = step.get("ops") or []
            has_file_ops = isinstance(ops, list) and any(
                operation_has_file_op_path(operation) for operation in ops
            )
            ops_only = has_file_ops and not any(
                str(command or "").strip() for command in commands
            )
            for command in commands:
                rendered = str(command or "").strip()
                lowered = rendered.lower()
                if lowered.startswith(PlannerService._NON_RUNNABLE_COMMAND_PREFIXES):
                    issues["non_runnable_steps"].append(step_number)
                    break
                if PlannerService._command_is_plain_english_file_instruction(rendered):
                    issues["non_runnable_steps"].append(step_number)
                    break
                if PlannerService._uses_background_process(rendered):
                    issues["background_process_steps"].append(step_number)
                    break
            if expected_files and PlannerService._step_is_implementation_heavy(step):
                if (
                    commands
                    and not PlannerService._step_expected_files_are_structurally_empty(
                        step
                    )
                    and all(
                        PlannerService._command_is_placeholder_only(command)
                        for command in commands
                    )
                ):
                    issues["placeholder_only_steps"].append(step_number)
                if (
                    not ops_only or step.get("verification") is not None
                ) and ValidatorService._verification_is_weak(step.get("verification")):
                    issues["weak_verification_steps"].append(step_number)
            # Flag commands that use python -c to write file content alongside
            # expected_files — these should use ops.write_file instead.
            if expected_files and any(
                PlannerService._command_is_python_c_content_write(str(cmd or ""))
                for cmd in commands
            ):
                issues["prefer_typed_ops_steps"].append(step_number)
            if project_dir and PlannerService._step_has_stale_replace_ops(
                step, Path(project_dir)
            ):
                issues["stale_replace_ops_steps"].append(step_number)
            if PlannerService._step_has_empty_replace_old_text_ops(step):
                issues["empty_replace_old_text_steps"].append(step_number)
            if project_dir and PlannerService._step_has_test_assertion_loss_ops(
                step, Path(project_dir)
            ):
                issues["test_assertion_loss_ops_steps"].append(step_number)
            if project_dir and PlannerService._step_deletes_existing_python_tests(
                step, Path(project_dir)
            ):
                issues["test_deletion_ops_steps"].append(step_number)
            if ValidatorService._step_uses_fake_verification_artifact(step):
                issues["fake_verification_artifact_steps"].append(step_number)
        return {key: sorted(set(value)) for key, value in issues.items() if value}

    @staticmethod
    def _step_has_empty_replace_old_text_ops(step: Dict[str, Any]) -> bool:
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "").strip() != "replace_in_file":
                continue
            old_present = "old" in operation or "old_text" in operation
            old_value = (
                operation.get("old")
                if "old" in operation
                else operation.get("old_text")
            )
            if not old_present or not isinstance(old_value, str) or not old_value:
                return True
        return False

    @staticmethod
    def _step_has_stale_replace_ops(step: Dict[str, Any], project_dir: Path) -> bool:
        ops = step.get("ops") or []
        if not isinstance(ops, list):
            return False
        for operation in ops:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "").strip() != "replace_in_file":
                continue
            rel_path = str(operation.get("path") or "").strip().lstrip("./")
            old_text = operation.get("old")
            if not rel_path or not isinstance(old_text, str) or not old_text:
                continue
            path = (project_dir / rel_path).resolve()
            try:
                path.relative_to(project_dir.resolve())
            except ValueError:
                return True
            if not path.is_file():
                return True
            try:
                if path.stat().st_size > 500_000:
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return True
            if old_text not in content:
                return True
        return False

    @staticmethod
    def _step_has_test_assertion_loss_ops(
        step: Dict[str, Any], project_dir: Path
    ) -> bool:
        ops = step.get("ops") or []
        if not isinstance(ops, list):
            return False
        root = Path(project_dir).resolve()
        for operation in ops:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "").strip() != "write_file":
                continue
            rel_path = str(operation.get("path") or "").strip().lstrip("./")
            if not PlannerService._is_python_test_path(rel_path):
                continue
            new_content = operation.get("content")
            if not isinstance(new_content, str):
                continue
            path = (root / rel_path).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                return True
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > 500_000:
                    continue
                old_content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return True
            old_count = PlannerService._python_test_assertion_count(old_content)
            new_count = PlannerService._python_test_assertion_count(new_content)
            if old_count > 0 and new_count < old_count:
                return True
        return False

    @staticmethod
    def _step_deletes_existing_python_tests(
        step: Dict[str, Any], project_dir: Path
    ) -> bool:
        ops = step.get("ops") or []
        if not isinstance(ops, list):
            return False
        root = Path(project_dir).resolve()
        for operation in ops:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "").strip() != "delete_file":
                continue
            rel_path = str(operation.get("path") or "").strip().lstrip("./")
            if not PlannerService._is_python_test_path(rel_path):
                continue
            path = (root / rel_path).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                return True
            if path.is_file():
                return True
        return False

    @staticmethod
    def _is_python_test_path(path_text: str) -> bool:
        normalized = str(path_text or "").replace("\\", "/").lstrip("./")
        name = Path(normalized).name
        return normalized.endswith(".py") and (
            name.startswith("test_")
            or name.endswith("_test.py")
            or "/tests/" in f"/{normalized}"
        )

    @staticmethod
    def _python_test_assertion_count(content: str) -> int:
        try:
            tree = ast.parse(str(content or ""))
        except SyntaxError:
            return 0
        count = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                count += 1
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr.startswith("assert")
            ):
                count += 1
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "raises"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "pytest"
            ):
                count += 1
        return count

    @staticmethod
    def stale_replace_repair_hints(
        plan: Optional[List[Dict[str, Any]]],
        project_dir: Path,
        *,
        max_hints: int = 2,
        max_excerpt_chars: int = 900,
    ) -> List[str]:
        hints: List[str] = []
        seen_targets: set[str] = set()
        root = Path(project_dir).resolve()
        for index, step in enumerate(plan or [], start=1):
            if len(hints) >= max_hints or not isinstance(step, dict):
                break
            for operation in step.get("ops") or []:
                if len(hints) >= max_hints or not isinstance(operation, dict):
                    break
                if str(operation.get("op") or "").strip() != "replace_in_file":
                    continue
                rel_path = str(operation.get("path") or "").strip().lstrip("./")
                old_text = operation.get("old")
                if not rel_path or not isinstance(old_text, str) or not old_text:
                    continue
                if rel_path in seen_targets:
                    continue
                seen_targets.add(rel_path)
                path = (root / rel_path).resolve()
                try:
                    path.relative_to(root)
                except ValueError:
                    hints.append(
                        f"step {index} replace_in_file path escapes workspace: {rel_path}"
                    )
                    continue
                if not path.is_file():
                    hints.append(
                        f"step {index} replace_in_file target missing: {rel_path}"
                    )
                    continue
                try:
                    if path.stat().st_size > 500_000:
                        continue
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    hints.append(
                        f"step {index} replace_in_file target unreadable: {rel_path}"
                    )
                    continue
                if old_text in content:
                    continue
                excerpt = content[:max_excerpt_chars].replace("\n", "\\n")
                hints.append(
                    "step "
                    f"{index} replace_in_file old text not found in {rel_path}. "
                    "Use exact text from current file excerpt or choose a different "
                    f"operation. Current file excerpt: {excerpt}"
                )
        return hints

    @staticmethod
    def stale_replace_fallback_hints(
        plan: Optional[List[Dict[str, Any]]],
        project_dir: Path,
        *,
        max_hints: int = 2,
        max_excerpt_chars: int = 1200,
    ) -> List[str]:
        hints: List[str] = []
        root = Path(project_dir).resolve()
        for index, step in enumerate(plan or [], start=1):
            if len(hints) >= max_hints or not isinstance(step, dict):
                break
            for operation in step.get("ops") or []:
                if len(hints) >= max_hints or not isinstance(operation, dict):
                    break
                if str(operation.get("op") or "").strip() != "replace_in_file":
                    continue
                rel_path = str(operation.get("path") or "").strip().lstrip("./")
                old_text = operation.get("old")
                if not rel_path or not isinstance(old_text, str) or not old_text:
                    continue
                path = (root / rel_path).resolve()
                try:
                    path.relative_to(root)
                except ValueError:
                    hints.append(
                        "patch_strategy_fallback_required: "
                        f"step {index} target escapes workspace ({rel_path}); "
                        "do not retry this replace_in_file operation"
                    )
                    continue
                if not path.is_file():
                    hints.append(
                        "patch_strategy_fallback_required: "
                        f"step {index} target is missing ({rel_path}); "
                        "do not retry this replace_in_file operation"
                    )
                    continue
                try:
                    if path.stat().st_size > 500_000:
                        continue
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    hints.append(
                        "patch_strategy_fallback_required: "
                        f"step {index} target is unreadable ({rel_path}); "
                        "do not retry this replace_in_file operation"
                    )
                    continue
                if old_text in content:
                    continue
                excerpt = content[:max_excerpt_chars].replace("\n", "\\n")
                test_preservation = (
                    " If this is a test file, preserve existing tests and assertion "
                    "intent; do not replace assertions with pass, stubs, or tautologies."
                    if "/test" in f"/{rel_path}" or rel_path.endswith("_test.py")
                    else ""
                )
                hints.append(
                    "patch_strategy_fallback_required: "
                    f"step {index} replace_in_file old text is still absent in "
                    f"{rel_path}. Exact-text patching is exhausted for this target; "
                    "do not emit another replace_in_file for the same missing old "
                    "text or same target. Use ops.write_file with complete preserved "
                    "file content grounded in the current excerpt. write_file.content "
                    "must be a JSON string; escape newline characters as \\n; do not "
                    "use raw triple-quoted Python blocks; do not place bare multiline "
                    "code outside JSON string quotes; the output must remain a valid "
                    "JSON array."
                    f"{test_preservation} Current file excerpt: {excerpt}"
                )
        return hints

    @staticmethod
    def describe_planning_contract_violations(
        *,
        output_text: str = "",
        parse_success: Optional[bool] = None,
        strategy_info: str = "",
        plan_data: Any = None,
        extracted_plan: Optional[List[Dict[str, Any]]] = None,
        immediate_repair_issues: Optional[Dict[str, List[int]]] = None,
    ) -> List[str]:
        violations: List[str] = []
        text = str(output_text or "").strip()
        lowered = text.lower()
        if parse_success is False:
            if text.startswith("```") or "```json" in lowered:
                violations.append("markdown-wrapped JSON")
            elif text and not text.startswith(("[", "{")):
                violations.append("non-JSON prose")
            else:
                violations.append(f"json_parse_failed: {strategy_info[:160]}")
        if isinstance(plan_data, dict):
            if any(
                key in plan_data for key in ("payloads", "finalAssistantVisibleText")
            ):
                violations.append("OpenClaw wrapper fields instead of top-level plan")
            elif "steps" in plan_data or "plan" in plan_data:
                violations.append("object wrapper instead of top-level JSON array")
            else:
                violations.append("object instead of top-level JSON array")
        if isinstance(plan_data, list) and not plan_data:
            violations.append("empty JSON array")
        for index, step in enumerate(extracted_plan or [], start=1):
            if not isinstance(step, dict):
                violations.append(f"non-object step at position {index}")
                continue
            missing = [key for key in PLANNING_STEP_REQUIRED_KEYS if key not in step]
            allowed_step_keys = set(PLANNING_STEP_REQUIRED_KEYS)
            allowed_step_keys.add("ops")
            extra = [key for key in step.keys() if key not in allowed_step_keys]
            if missing:
                violations.append(
                    f"step {index} missing required keys: {', '.join(missing)}"
                )
            if extra:
                violations.append(f"step {index} has extra keys: {', '.join(extra)}")
        for issue_key, steps in (immediate_repair_issues or {}).items():
            if not steps:
                continue
            label = {
                "non_runnable_steps": "non-runnable pseudo-command",
                "background_process_steps": "background process command",
                "placeholder_only_steps": "placeholder-only implementation step",
                "weak_verification_steps": "weak verification command",
                "prefer_typed_ops_steps": "python -c content write should use ops.write_file",
                "stale_replace_ops_steps": "replace_in_file old text not found in workspace",
                "empty_replace_old_text_steps": "replace_in_file old text is empty or missing",
                "test_assertion_loss_ops_steps": "test rewrite would remove existing assertions",
                "test_deletion_ops_steps": "test file deletion requires explicit preservation review",
            }.get(issue_key, issue_key)
            violations.append(f"{label} in steps {steps[:5]}")
        return list(dict.fromkeys(violations))

    @staticmethod
    def build_minimal_planning_prompt(
        task_description: str,
        project_dir: Path,
        prompt_profile: str = "default",
        workflow_profile: str = "default",
        workflow_phases: Optional[List[str]] = None,
        workspace_has_existing_files: bool = False,
        knowledge_context: Any = None,
        project_structure_capsule: Optional[str] = None,
        validation_profile: Optional[str] = None,
        project_context: Optional[str] = None,
    ) -> str:
        return _build_minimal_planning_prompt(
            task_description,
            project_dir,
            prompt_profile=prompt_profile,
            workflow_profile=workflow_profile,
            workflow_phases=workflow_phases,
            workspace_has_existing_files=workspace_has_existing_files,
            knowledge_context=knowledge_context,
            project_structure_capsule=project_structure_capsule,
            validation_profile=validation_profile,
            project_context=project_context,
            apply_prompt_profile=PlannerService.apply_prompt_profile,
        )

    @staticmethod
    def build_ultra_minimal_planning_prompt(
        task_description: str,
        project_dir: Path,
        prompt_profile: str = "default",
        workflow_profile: str = "default",
        workflow_phases: Optional[List[str]] = None,
        workspace_has_existing_files: bool = False,
        validation_profile: Optional[str] = None,
        project_context: Optional[str] = None,
    ) -> str:
        return _build_ultra_minimal_planning_prompt(
            task_description,
            project_dir,
            prompt_profile=prompt_profile,
            workflow_profile=workflow_profile,
            workflow_phases=workflow_phases,
            workspace_has_existing_files=workspace_has_existing_files,
            validation_profile=validation_profile,
            project_context=project_context,
            apply_prompt_profile=PlannerService.apply_prompt_profile,
        )

    @staticmethod
    def _looks_like_timeout_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "timed out" in message or "timeout" in message

    @staticmethod
    def maybe_load_workspace_plan(
        output_text: str,
        project_dir: Path,
        logger: logging.Logger,
    ) -> Optional[Any]:
        if not WORKSPACE_PLAN_REFERENCE_RE.search(str(output_text or "")):
            return None

        plan_path = project_dir / "plan.json"
        if not plan_path.is_file():
            logger.warning(
                "[ORCHESTRATION] Planner output referenced plan.json but no workspace file was found at %s",
                plan_path,
            )
            return None

        try:
            plan_text = plan_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning(
                "[ORCHESTRATION] Failed reading workspace plan file %s: %s",
                plan_path,
                exc,
            )
            return None

        if not plan_text:
            logger.warning(
                "[ORCHESTRATION] Workspace plan file %s was empty despite planner reference",
                plan_path,
            )
            return None

        try:
            parsed = json.loads(plan_text)
        except json.JSONDecodeError as exc:
            logger.warning(
                "[ORCHESTRATION] Workspace plan file %s was not valid JSON: %s",
                plan_path,
                exc,
            )
            return None

        logger.info(
            "[ORCHESTRATION] Recovered planning payload from workspace plan file %s",
            plan_path,
        )
        return parsed

    @staticmethod
    def _build_repair_prompt_budget_error(
        *,
        repair_prompt_chars: int,
        malformed_output_chars: int,
        validation_error_chars: int,
        knowledge_context_chars: int,
    ) -> str:
        return (
            "Planning repair prompt exceeded safe budget "
            f"({repair_prompt_chars} > {PLANNING_REPAIR_PROMPT_MAX_CHARS} chars). "
            "Repair prompts may include only malformed output, validation error, "
            "schema guidance, and small knowledge references. "
            f"Components: malformed_output={malformed_output_chars}, "
            f"validation_error={validation_error_chars}, "
            f"knowledge_context={knowledge_context_chars}."
        )

    @staticmethod
    def _is_stale_replace_repair_reason(reason: str) -> bool:
        return str(reason or "").startswith("post_repair_stale_replace_fallback")

    @staticmethod
    def _is_first_pass_stale_replace_repair_reason(reason: str) -> bool:
        reason_text = str(reason or "")
        return (
            reason_text.startswith("plan_contains_immediate_repair_issues")
            and "replace_in_file old text not found" in reason_text
        )

    @classmethod
    def _planning_repair_timeout_margin_reason(cls, reason: str) -> Optional[str]:
        if cls._is_stale_replace_repair_reason(reason):
            return "post_repair_stale_replace_fallback"
        if cls._is_first_pass_stale_replace_repair_reason(reason):
            return "first_pass_stale_replace_old_text"
        return None

    @staticmethod
    def _stale_replace_repair_diagnostic_path(prompt_hash: str) -> Path:
        return STALE_REPLACE_REPAIR_DIAGNOSTIC_DIR / prompt_hash

    @classmethod
    def _capture_stale_replace_repair_prompt(
        cls,
        *,
        repair_prompt: str,
        reason: str,
        session_id: Optional[int],
        task_id: Optional[int],
        repair_attempt_number: int,
        repair_prompt_build_seconds: float,
        malformed_output_chars: int,
        validation_error_chars: int,
        knowledge_context_chars: int,
        includes_project_context: bool,
        includes_non_project_context: bool,
    ) -> Dict[str, Any]:
        prompt_hash = hashlib.sha256(
            str(repair_prompt or "").encode("utf-8")
        ).hexdigest()[:12]
        diagnostic_dir = cls._stale_replace_repair_diagnostic_path(prompt_hash)
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(diagnostic_dir, 0o777)

        prompt_path = diagnostic_dir / f"planning_repair_prompt_{prompt_hash}.txt"
        metadata_path = diagnostic_dir / "prompt_metadata.json"
        prompt_path.write_text(repair_prompt, encoding="utf-8")
        metadata = {
            "kind": "planning_stale_replace_repair_prompt",
            "prompt_sha256_12": prompt_hash,
            "prompt_chars": len(repair_prompt or ""),
            "session_id": session_id,
            "task_id": task_id,
            "repair_attempt_number": repair_attempt_number,
            "repair_reason": str(reason or "")[:1000],
            "repair_prompt_build_seconds": round(repair_prompt_build_seconds, 3),
            "malformed_output_chars": malformed_output_chars,
            "validation_error_chars": validation_error_chars,
            "knowledge_context_chars": knowledge_context_chars,
            "includes_project_context": includes_project_context,
            "includes_non_project_context": includes_non_project_context,
            "captured_at": datetime.now(UTC).isoformat(),
            "prompt_path": str(prompt_path),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        os.chmod(prompt_path, 0o666)
        os.chmod(metadata_path, 0o666)
        return {
            "prompt_sha256_12": prompt_hash,
            "diagnostic_dir": str(diagnostic_dir),
            "prompt_path": str(prompt_path),
            "metadata_path": str(metadata_path),
        }

    @classmethod
    def _write_stale_replace_repair_gateway_diagnostic(
        cls,
        prompt_hash: str,
        filename: str,
        payload: Dict[str, Any],
    ) -> None:
        diagnostic_dir = cls._stale_replace_repair_diagnostic_path(prompt_hash)
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(diagnostic_dir, 0o777)
        path = diagnostic_dir / filename
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.chmod(path, 0o666)

    @staticmethod
    def _normalize_repair_json_array_output(output_text: str) -> Optional[str]:
        """Normalize fenced JSON arrays before the main planning parser runs."""
        stripped = str(output_text or "").strip()
        if not stripped.startswith("```"):
            return stripped if stripped.startswith("[") else None

        match = re.match(
            r"^```(?:json)?\s*(?P<body>.*?)\s*```\s*.*$",
            stripped,
            flags=re.DOTALL,
        )
        if not match:
            return None

        body = match.group("body").strip()
        array_start = body.find("[")
        if array_start < 0:
            return None
        decoder = json.JSONDecoder()
        try:
            parsed, end_index = decoder.raw_decode(body[array_start:])
        except json.JSONDecodeError:
            return None
        candidate = body[array_start : array_start + end_index].strip()
        return candidate if isinstance(parsed, list) else None

    @staticmethod
    async def _invoke_repair_prompt(
        runtime_service: Any,
        repair_prompt: str,
        repair_timeout: int,
        lock_diagnostics_out: Optional[Dict[str, Any]] = None,
        diagnostic_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        direct_result = await PlannerService._invoke_direct_no_thinking_repair(
            runtime_service,
            repair_prompt,
            repair_timeout,
            diagnostic_context=diagnostic_context,
        )
        if direct_result is not None:
            return direct_result

        invoke_prompt = getattr(runtime_service, "invoke_prompt", None)
        if callable(invoke_prompt):
            async with PlannerService._openclaw_planning_lock_async() as lock_diagnostics:
                if lock_diagnostics_out is not None:
                    lock_diagnostics_out.update(lock_diagnostics)
                try:
                    result = await invoke_prompt(
                        repair_prompt,
                        timeout_seconds=repair_timeout,
                        source_brain="local",
                        session_prefix="planning-repair",
                        isolate_workspace_context=False,
                        no_output_timeout_seconds=PlannerService._effective_planning_repair_no_output_timeout(
                            repair_timeout
                        ),
                    )
                except Exception as exc:
                    PlannerService._attach_planning_lock_exception_diagnostics(
                        exc, lock_diagnostics
                    )
                    raise
                return PlannerService._attach_planning_lock_diagnostics(
                    result, lock_diagnostics
                )

        return await PlannerService._execute_task_with_planning_lock(
            runtime_service,
            repair_prompt,
            timeout_seconds=repair_timeout,
            reuse_task_session=False,
        )

    @staticmethod
    def _effective_planning_repair_timeout(timeout_seconds: int) -> float:
        configured_timeout = float(
            getattr(settings, "PLANNING_REPAIR_TIMEOUT_SECONDS", 0)
            or PLANNING_REPAIR_TIMEOUT_SECONDS
        )
        return max(
            0.01,
            min(
                float(timeout_seconds or PLANNING_REPAIR_TIMEOUT_SECONDS),
                configured_timeout,
                float(PLANNING_REPAIR_TIMEOUT_SECONDS),
            ),
        )

    @classmethod
    def _effective_planning_repair_timeout_for_reason(
        cls, timeout_seconds: int, reason: str
    ) -> float:
        if cls._planning_repair_timeout_margin_reason(reason):
            return STALE_REPLACE_REPAIR_TIMEOUT_SECONDS
        return cls._effective_planning_repair_timeout(timeout_seconds)

    @staticmethod
    def _effective_planning_repair_no_output_timeout(repair_timeout: int) -> int:
        return max(
            1, min(int(repair_timeout), PLANNING_REPAIR_NO_OUTPUT_TIMEOUT_SECONDS)
        )

    @staticmethod
    def _should_try_direct_no_thinking_repair(runtime_service: Any) -> bool:
        if not settings.PLANNING_REPAIR_ENABLED:
            _logger.info("[REPAIR_DIRECT] skip: " "PLANNING_REPAIR_ENABLED=false")
            return False
        if not settings.PLANNING_REPAIR_BASE_URL.strip():
            _logger.info("[REPAIR_DIRECT] skip: base_url empty")
            return False
        if not settings.PLANNING_REPAIR_MODEL.strip():
            _logger.info("[REPAIR_DIRECT] skip: model empty")
            return False
        backend_metadata = {}
        get_backend_metadata = getattr(runtime_service, "get_backend_metadata", None)
        if callable(get_backend_metadata):
            try:
                backend_metadata = get_backend_metadata() or {}
            except Exception:
                backend_metadata = {}
        backend_name = str(backend_metadata.get("backend") or "").strip()
        if backend_name not in {"local_openclaw", "direct_ollama"}:
            _logger.info(
                "[REPAIR_DIRECT] skip: backend_name=%r (not direct-capable)",
                backend_name,
            )
            return False
        has_db = hasattr(runtime_service, "db")
        if not has_db:
            _logger.info("[REPAIR_DIRECT] skip: runtime_service has no db attr")
        return has_db

    @staticmethod
    def _direct_no_thinking_model(runtime_service: Any) -> str:
        configured_model = (settings.PLANNING_REPAIR_MODEL or "").strip()
        backend_metadata: Dict[str, Any] = {}
        get_backend_metadata = getattr(runtime_service, "get_backend_metadata", None)
        if callable(get_backend_metadata):
            try:
                backend_metadata = get_backend_metadata() or {}
            except Exception:
                backend_metadata = {}

        backend_name = str(backend_metadata.get("backend") or "").strip()
        runtime_model = str(backend_metadata.get("model_family") or "").strip()
        if (
            backend_name == "direct_ollama"
            and configured_model
            and runtime_model
            and ":" in runtime_model
            and ":" not in configured_model
            and runtime_model.replace(":", "-", 1) == configured_model
        ):
            return runtime_model
        return configured_model

    @staticmethod
    async def _invoke_direct_no_thinking_repair(
        runtime_service: Any,
        repair_prompt: str,
        repair_timeout: int,
        diagnostic_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not PlannerService._should_try_direct_no_thinking_repair(runtime_service):
            return None

        base_url = settings.PLANNING_REPAIR_BASE_URL.rstrip("/")
        model = PlannerService._direct_no_thinking_model(runtime_service)
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": repair_prompt}],
            "temperature": 0,
            "max_tokens": 2048,
            "stream": False,
        }
        if settings.PLANNING_REPAIR_DISABLE_THINKING:
            payload["think"] = False
            payload["enable_thinking"] = False
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        headers = {"Content-Type": "application/json"}
        api_key = settings.PLANNING_REPAIR_API_KEY.strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        started_at = time.monotonic()
        started_wall = datetime.now(UTC)
        direct_timeout = max(1, repair_timeout)
        prompt_hash = str((diagnostic_context or {}).get("prompt_sha256_12") or "")
        stale_replace_diagnostic = bool(
            (diagnostic_context or {}).get("stale_replace_repair_diagnostic")
            and prompt_hash
        )
        _logger.info(
            "[REPAIR_DIRECT] attempting direct no-thinking repair "
            "url=%s model=%s timeout=%ds",
            f"{base_url}/chat/completions",
            model,
            direct_timeout,
        )

        async def _do_request() -> tuple[str, Dict[str, Any]]:
            async with httpx.AsyncClient(timeout=direct_timeout) as client:
                response = await client.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            response.raise_for_status()
            body = response.json()
            return PlannerService._extract_chat_completion_content(body), body

        try:
            output, response_body = await asyncio.wait_for(
                _do_request(), timeout=float(direct_timeout)
            )
        except asyncio.TimeoutError:
            duration_seconds = time.monotonic() - started_at
            if stale_replace_diagnostic:
                PlannerService._write_stale_replace_repair_gateway_diagnostic(
                    prompt_hash,
                    "direct_no_thinking_result.json",
                    {
                        "kind": "planning_stale_replace_direct_no_thinking_result",
                        "status": "timeout",
                        "prompt_sha256_12": prompt_hash,
                        "prompt_chars": len(repair_prompt or ""),
                        "request_started_at": started_wall.isoformat(),
                        "request_finished_at": datetime.now(UTC).isoformat(),
                        "duration_seconds": round(duration_seconds, 3),
                        "timeout_seconds": direct_timeout,
                        "model": model,
                        "base_url": base_url,
                        "disable_thinking": settings.PLANNING_REPAIR_DISABLE_THINKING,
                        "content_chars": 0,
                        "content_null": True,
                        "finish_reason": None,
                        "gateway_error": "asyncio_timeout",
                        **(diagnostic_context or {}),
                    },
                )
            _logger.warning(
                "[REPAIR_DIRECT] wall-clock timeout after %ds; falling back to runtime",
                direct_timeout,
            )
            return None
        except Exception as exc:
            duration_seconds = time.monotonic() - started_at
            if stale_replace_diagnostic:
                PlannerService._write_stale_replace_repair_gateway_diagnostic(
                    prompt_hash,
                    "direct_no_thinking_result.json",
                    {
                        "kind": "planning_stale_replace_direct_no_thinking_result",
                        "status": "error",
                        "prompt_sha256_12": prompt_hash,
                        "prompt_chars": len(repair_prompt or ""),
                        "request_started_at": started_wall.isoformat(),
                        "request_finished_at": datetime.now(UTC).isoformat(),
                        "duration_seconds": round(duration_seconds, 3),
                        "timeout_seconds": direct_timeout,
                        "model": model,
                        "base_url": base_url,
                        "disable_thinking": settings.PLANNING_REPAIR_DISABLE_THINKING,
                        "content_chars": 0,
                        "content_null": True,
                        "finish_reason": None,
                        "gateway_error": f"{type(exc).__name__}: {str(exc)[:500]}",
                        **(diagnostic_context or {}),
                    },
                )
            _logger.warning(
                "[REPAIR_DIRECT] failed after %.1fs (%s: %s); falling back to runtime",
                time.monotonic() - started_at,
                type(exc).__name__,
                str(exc)[:200],
            )
            return None

        choices = (
            response_body.get("choices") if isinstance(response_body, dict) else None
        )
        first_choice = choices[0] if isinstance(choices, list) and choices else {}
        message = first_choice.get("message") if isinstance(first_choice, dict) else {}
        content_value = message.get("content") if isinstance(message, dict) else None
        finish_reason = (
            first_choice.get("finish_reason")
            if isinstance(first_choice, dict)
            else None
        )

        if not output.strip():
            duration_seconds = time.monotonic() - started_at
            if stale_replace_diagnostic:
                PlannerService._write_stale_replace_repair_gateway_diagnostic(
                    prompt_hash,
                    "direct_no_thinking_result.json",
                    {
                        "kind": "planning_stale_replace_direct_no_thinking_result",
                        "status": "empty_content",
                        "prompt_sha256_12": prompt_hash,
                        "prompt_chars": len(repair_prompt or ""),
                        "request_started_at": started_wall.isoformat(),
                        "request_finished_at": datetime.now(UTC).isoformat(),
                        "duration_seconds": round(duration_seconds, 3),
                        "timeout_seconds": direct_timeout,
                        "model": model,
                        "base_url": base_url,
                        "disable_thinking": settings.PLANNING_REPAIR_DISABLE_THINKING,
                        "content_chars": 0,
                        "content_null": content_value is None,
                        "finish_reason": finish_reason,
                        "usage": (
                            response_body.get("usage")
                            if isinstance(response_body, dict)
                            else None
                        ),
                        "gateway_error": None,
                        **(diagnostic_context or {}),
                    },
                )
            _logger.warning(
                "[REPAIR_DIRECT] empty output from direct call; "
                "falling back to runtime"
            )
            return None

        duration_seconds = time.monotonic() - started_at
        if stale_replace_diagnostic:
            PlannerService._write_stale_replace_repair_gateway_diagnostic(
                prompt_hash,
                "direct_no_thinking_result.json",
                {
                    "kind": "planning_stale_replace_direct_no_thinking_result",
                    "status": "completed",
                    "prompt_sha256_12": prompt_hash,
                    "prompt_chars": len(repair_prompt or ""),
                    "request_started_at": started_wall.isoformat(),
                    "request_finished_at": datetime.now(UTC).isoformat(),
                    "duration_seconds": round(duration_seconds, 3),
                    "timeout_seconds": direct_timeout,
                    "model": model,
                    "base_url": base_url,
                    "disable_thinking": settings.PLANNING_REPAIR_DISABLE_THINKING,
                    "content_chars": len(output),
                    "content_null": content_value is None,
                    "finish_reason": finish_reason,
                    "usage": (
                        response_body.get("usage")
                        if isinstance(response_body, dict)
                        else None
                    ),
                    "gateway_error": None,
                    **(diagnostic_context or {}),
                },
            )
        _logger.info(
            "[REPAIR_DIRECT] success planning_repair_direct=True "
            "backend=direct_chat_completions duration=%.1fs output_chars=%s",
            duration_seconds,
            len(output),
        )
        return {
            "status": "completed",
            "output": output,
            "backend": "direct_chat_completions",
            "model_family": model,
            "diagnostics": {
                "planning_repair_direct": True,
                "disable_thinking": (settings.PLANNING_REPAIR_DISABLE_THINKING),
                "duration_seconds": round(duration_seconds, 3),
                "timeout_seconds": direct_timeout,
            },
        }

    @staticmethod
    def _extract_chat_completion_content(body: Any) -> str:
        if not isinstance(body, dict):
            return ""
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts)
        return ""

    @staticmethod
    def _get_runtime_diagnostics(exc: Exception) -> Dict[str, Any]:
        diagnostics = getattr(exc, "runtime_diagnostics", None)
        return diagnostics if isinstance(diagnostics, dict) else {}

    @classmethod
    def _is_no_output_repair_timeout(cls, exc: Exception) -> bool:
        diagnostics = cls._get_runtime_diagnostics(exc)
        if diagnostics.get("no_output_timeout") is True:
            return True
        if diagnostics.get("timeout_boundary") == "repair_no_output":
            return True
        message = str(exc).lower()
        return "no output" in message and "openclaw" in message

    @classmethod
    def build_planning_repair_prompt(
        cls,
        task_description: str,
        malformed_output: str,
        project_dir: Path,
        rejection_reasons: Optional[List[str]] = None,
        prompt_profile: str = "default",
        workflow_profile: str = "default",
        workflow_phases: Optional[List[str]] = None,
        workspace_has_existing_files: bool = False,
        knowledge_context: Any = None,
    ) -> str:
        del workflow_profile, workflow_phases, workspace_has_existing_files
        return _build_planning_repair_prompt(
            task_description=task_description,
            malformed_output=malformed_output,
            project_dir=project_dir,
            rejection_reasons=rejection_reasons,
            prompt_profile=prompt_profile,
            apply_prompt_profile=PlannerService.apply_prompt_profile,
            knowledge_context=knowledge_context,
            project_structure_capsule=cls._build_project_structure_capsule(project_dir),
        )

    @classmethod
    def build_planning_repair_prompt_with_metadata(
        cls,
        task_description: str,
        malformed_output: str,
        project_dir: Path,
        rejection_reasons: Optional[List[str]] = None,
        prompt_profile: str = "default",
        workflow_profile: str = "default",
        workflow_phases: Optional[List[str]] = None,
        workspace_has_existing_files: bool = False,
        knowledge_context: Any = None,
        guidance_block: str = "",
    ):
        del workflow_profile, workflow_phases, workspace_has_existing_files
        return _build_planning_repair_prompt_with_metadata(
            task_description=task_description,
            malformed_output=malformed_output,
            project_dir=project_dir,
            rejection_reasons=rejection_reasons,
            prompt_profile=prompt_profile,
            apply_prompt_profile=PlannerService.apply_prompt_profile,
            knowledge_context=knowledge_context,
            project_structure_capsule=cls._build_project_structure_capsule(project_dir),
            guidance_block=guidance_block,
        )

    @staticmethod
    def build_compact_planning_repair_prompt(
        malformed_output: str,
        rejection_reasons: Optional[List[str]] = None,
        prompt_profile: str = "default",
        guidance_block: str = "",
    ) -> str:
        return _build_compact_planning_repair_prompt(
            malformed_output=malformed_output,
            rejection_reasons=rejection_reasons,
            prompt_profile=prompt_profile,
            apply_prompt_profile=PlannerService.apply_prompt_profile,
            guidance_block=guidance_block,
        )

    @staticmethod
    def _build_project_structure_capsule(project_dir: Path) -> str:
        try:
            return render_project_structure_capsule(build_project_index(project_dir))
        except Exception:
            return ""

    @classmethod
    def retry_with_minimal_prompt(
        cls,
        runtime_service: Any,
        task_description: str,
        project_dir: Path,
        timeout_seconds: int,
        logger: logging.Logger,
        emit_live: Any,
        reason: str,
        rejection_reasons: Optional[List[str]] = None,
        prompt_profile: str = "default",
        workflow_profile: str = "default",
        workflow_phases: Optional[List[str]] = None,
        workspace_has_existing_files: bool = False,
        knowledge_context: Any = None,
        validation_profile: Optional[str] = None,
        project_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        can_store_retry_guard = hasattr(runtime_service, "__dict__")
        if can_store_retry_guard:
            if getattr(runtime_service, "_minimal_prompt_retry_used", False):
                raise RuntimeError(
                    "Minimal planning retry already attempted for this runtime"
                )
            setattr(runtime_service, "_minimal_prompt_retry_used", True)

        minimal_first = reason == "dense_planning_context"
        logger.warning(
            (
                "[ORCHESTRATION] Planning context selected minimal prompt first"
                if minimal_first
                else "[ORCHESTRATION] Planning output was not machine-parseable; retrying with minimal prompt"
            )
            + f" ({reason})"
        )
        minimal_timeout_limit = (
            MINIMAL_PLANNING_TIMEOUT_SECONDS
            if minimal_first
            else STRICT_JSON_RETRY_TIMEOUT_SECONDS
        )
        minimal_timeout = min(timeout_seconds, minimal_timeout_limit)
        retry_message = (
            "[ORCHESTRATION] Planning context is dense; starting minimal prompt attempt"
            if minimal_first
            else "[ORCHESTRATION] Planning output needed a strict JSON retry; starting minimal prompt attempt"
        )
        minimal_prompt = cls.build_minimal_planning_prompt(
            task_description,
            project_dir,
            prompt_profile=prompt_profile,
            workflow_profile=workflow_profile,
            workflow_phases=workflow_phases,
            workspace_has_existing_files=workspace_has_existing_files,
            knowledge_context=knowledge_context,
            project_structure_capsule=PlannerService._build_project_structure_capsule(
                project_dir
            ),
            validation_profile=validation_profile,
            project_context=project_context,
        )
        minimal_prompt_chars = len(minimal_prompt)
        minimal_prompt_estimated_tokens = _estimate_prompt_tokens(minimal_prompt)
        ultra_dense_planning_context = (
            minimal_prompt_estimated_tokens
            > MINIMAL_PLANNING_PROMPT_TOKEN_DIAGNOSTIC_THRESHOLD
        )
        minimal_prompt_diagnostics = {
            "minimal_prompt_chars": minimal_prompt_chars,
            "minimal_prompt_estimated_tokens": minimal_prompt_estimated_tokens,
            "minimal_prompt_token_threshold": (
                MINIMAL_PLANNING_PROMPT_TOKEN_DIAGNOSTIC_THRESHOLD
            ),
            "ultra_dense_planning_context": ultra_dense_planning_context,
        }
        logger.warning(
            "[ORCHESTRATION] Minimal planning prompt size diagnostics "
            "(chars=%s estimated_tokens=%s threshold=%s ultra_dense=%s)",
            minimal_prompt_chars,
            minimal_prompt_estimated_tokens,
            MINIMAL_PLANNING_PROMPT_TOKEN_DIAGNOSTIC_THRESHOLD,
            ultra_dense_planning_context,
        )
        emit_live(
            "WARN",
            f"{retry_message} (timeout: {minimal_timeout}s)",
            metadata={
                "phase": "planning",
                "retry": "minimal_prompt_first" if minimal_first else "minimal_prompt",
                "reason": reason[:240],
                "timeout_seconds": minimal_timeout,
                **minimal_prompt_diagnostics,
            },
        )
        if ultra_dense_planning_context:
            emit_live(
                "WARN",
                "[ORCHESTRATION] Minimal planning prompt is still above the diagnostic token threshold",
                metadata={
                    "phase": "planning",
                    "reason": "ultra_dense_planning_context",
                    "strategy": "minimal_prompt",
                    **minimal_prompt_diagnostics,
                },
            )
        emit_live(
            "INFO",
            (
                "[ORCHESTRATION] Planning attempt 2 is now running with the minimal "
                f"prompt (timeout: {minimal_timeout}s)"
            ),
            metadata={
                "phase": "planning",
                "attempt": 2,
                "strategy": "minimal_prompt",
                "timeout_seconds": minimal_timeout,
                **minimal_prompt_diagnostics,
            },
        )
        planning_attempt_state: Dict[str, Any] = {}
        try:
            return _run_coroutine_from_sync(
                cls._execute_task_with_planning_lock(
                    runtime_service,
                    minimal_prompt,
                    timeout_seconds=minimal_timeout,
                    reuse_task_session=False,
                    diagnostic_label="MINIMAL_PLANNING",
                    diagnostic_metadata={
                        "planning_attempt": "minimal",
                        **minimal_prompt_diagnostics,
                    },
                    direct_planning_state=planning_attempt_state,
                )
            )
        except Exception as exc:
            if not cls._looks_like_timeout_error(exc):
                raise
            if cls._is_no_model_output_planning_timeout(exc):
                diagnostics = cls._get_runtime_diagnostics(exc)
                diagnostics["planning_failure_class"] = "planner_no_model_output"
                emit_live(
                    "ERROR",
                    (
                        "[ORCHESTRATION] Planning backend timed out without model "
                        "output; skipping equivalent ultra-minimal retry"
                    ),
                    metadata={
                        "phase": "planning",
                        "reason": "planner_no_model_output",
                        "timeout_seconds": diagnostics.get("timeout_seconds"),
                        "duration_seconds": diagnostics.get("duration_seconds"),
                        "output_channel_used": diagnostics.get("output_channel_used"),
                        "stderr_contains_model_content": diagnostics.get(
                            "stderr_contains_model_content"
                        ),
                    },
                )
                raise
            ultra_minimal_timeout = min(
                timeout_seconds, ULTRA_MINIMAL_PLANNING_TIMEOUT_SECONDS
            )
            logger.warning(
                "[ORCHESTRATION] Minimal planning prompt timed out; retrying with ultra-minimal prompt"
            )
            emit_live(
                "WARN",
                (
                    "[ORCHESTRATION] Minimal planning timed out; retrying with "
                    f"ultra-minimal prompt (timeout: {ultra_minimal_timeout}s)"
                ),
                metadata={
                    "phase": "planning",
                    "retry": "ultra_minimal_prompt",
                    "reason": str(exc)[:240],
                    "timeout_seconds": ultra_minimal_timeout,
                },
            )
            emit_live(
                "INFO",
                (
                    "[ORCHESTRATION] Planning attempt 3 is now running with the "
                    f"ultra-minimal prompt (timeout: {ultra_minimal_timeout}s)"
                ),
                metadata={
                    "phase": "planning",
                    "attempt": 3,
                    "strategy": "ultra_minimal_prompt",
                    "timeout_seconds": ultra_minimal_timeout,
                },
            )
            return _run_coroutine_from_sync(
                cls._execute_task_with_planning_lock(
                    runtime_service,
                    cls.build_ultra_minimal_planning_prompt(
                        task_description,
                        project_dir,
                        prompt_profile=prompt_profile,
                        workflow_profile=workflow_profile,
                        workflow_phases=workflow_phases,
                        workspace_has_existing_files=workspace_has_existing_files,
                        validation_profile=validation_profile,
                        project_context=project_context,
                    ),
                    timeout_seconds=ultra_minimal_timeout,
                    reuse_task_session=False,
                    diagnostic_label="ULTRA_MINIMAL_PLANNING",
                    diagnostic_metadata={
                        "planning_attempt": "ultra_minimal",
                        "minimal_prompt_timeout_reason": str(exc)[:240],
                    },
                    direct_planning_state=planning_attempt_state,
                )
            )

    @classmethod
    def repair_output(
        cls,
        runtime_service: Any,
        task_description: str,
        malformed_output: str,
        project_dir: Path,
        timeout_seconds: int,
        logger: logging.Logger,
        emit_live: Any,
        reason: str,
        rejection_reasons: Optional[List[str]] = None,
        prompt_profile: str = "default",
        workflow_profile: str = "default",
        workflow_phases: Optional[List[str]] = None,
        workspace_has_existing_files: bool = False,
        knowledge_context: Any = None,
        session_id: Optional[int] = None,
        task_id: Optional[int] = None,
        lock_wait_seconds: Optional[float] = None,
        _no_output_retry_used: bool = False,
        _repair_attempt_number: int = 1,
        _compact_no_output_retry: bool = False,
        guidance_block: str = "",
    ) -> Dict[str, Any]:
        repair_build_started_at = time.monotonic()
        logger.warning(
            "[ORCHESTRATION] Planning output was malformed but salvageable; "
            f"attempting repair ({reason})"
        )
        timeout_margin_reason = cls._planning_repair_timeout_margin_reason(reason)
        repair_timeout = cls._effective_planning_repair_timeout_for_reason(
            timeout_seconds, reason
        )
        if _compact_no_output_retry:
            repair_prompt = cls.build_compact_planning_repair_prompt(
                malformed_output,
                rejection_reasons=rejection_reasons,
                prompt_profile=prompt_profile,
                guidance_block=guidance_block,
            )
            repair_prompt_metadata: Dict[str, Any] = {
                "source_api_contract_available": False,
                "source_api_contract_included": False,
                "source_api_contract_chars": 0,
                "source_api_contract_compacted": False,
                "source_api_contract_omitted_reason": "compact_no_output_retry",
            }
        else:
            repair_prompt_result = cls.build_planning_repair_prompt_with_metadata(
                task_description,
                malformed_output,
                project_dir,
                rejection_reasons=rejection_reasons,
                prompt_profile=prompt_profile,
                workflow_profile=workflow_profile,
                workflow_phases=workflow_phases,
                workspace_has_existing_files=workspace_has_existing_files,
                knowledge_context=knowledge_context,
                guidance_block=guidance_block,
            )
            repair_prompt = repair_prompt_result.prompt
            repair_prompt_metadata = dict(repair_prompt_result.metadata)
        validation_error_chars = sum(
            len(str(reason_text or "")[:180])
            for reason_text in (rejection_reasons or [])[:5]
        )
        knowledge_context_chars = len(_render_repair_knowledge_block(knowledge_context))
        compact_malformed_output_chars = len(
            _compact_invalid_output_excerpt(malformed_output)
        )
        repair_prompt_build_seconds = time.monotonic() - repair_build_started_at
        includes_project_context = str(project_dir) in repair_prompt or bool(
            re.search(r"\b(project|workspace|baseline)\b", repair_prompt, re.IGNORECASE)
        )
        includes_non_project_context = bool(
            knowledge_context
            or re.search(
                r"\b(knowledge|memory|retrieved)\b", repair_prompt, re.IGNORECASE
            )
        )
        stale_replace_diagnostic_context: Optional[Dict[str, Any]] = None
        if cls._is_stale_replace_repair_reason(reason):
            stale_replace_diagnostic_context = cls._capture_stale_replace_repair_prompt(
                repair_prompt=repair_prompt,
                reason=reason,
                session_id=session_id,
                task_id=task_id,
                repair_attempt_number=_repair_attempt_number,
                repair_prompt_build_seconds=repair_prompt_build_seconds,
                malformed_output_chars=compact_malformed_output_chars,
                validation_error_chars=validation_error_chars,
                knowledge_context_chars=knowledge_context_chars,
                includes_project_context=includes_project_context,
                includes_non_project_context=includes_non_project_context,
            )
            stale_replace_diagnostic_context["stale_replace_repair_diagnostic"] = True
        logger.warning(
            "[ORCHESTRATION] session_id=%s task_id=%s repair_prompt_chars=%s "
            "malformed_output_chars=%s validation_error_chars=%s knowledge_context_chars=%s "
            "includes_project_context=%s includes_non_project_context=%s "
            "source_api_contract_available=%s source_api_contract_included=%s "
            "source_api_contract_chars=%s source_api_contract_compacted=%s "
            "source_api_contract_omitted_reason=%s "
            "repair_reason=%s repair_prompt_build_seconds=%.3f repair_attempts=%s "
            "compact_no_output_retry=%s stale_replace_prompt_hash=%s",
            session_id,
            task_id,
            len(repair_prompt),
            compact_malformed_output_chars,
            validation_error_chars,
            knowledge_context_chars,
            includes_project_context,
            includes_non_project_context,
            repair_prompt_metadata.get("source_api_contract_available"),
            repair_prompt_metadata.get("source_api_contract_included"),
            repair_prompt_metadata.get("source_api_contract_chars"),
            repair_prompt_metadata.get("source_api_contract_compacted"),
            repair_prompt_metadata.get("source_api_contract_omitted_reason"),
            reason[:120],
            repair_prompt_build_seconds,
            _repair_attempt_number,
            _compact_no_output_retry,
            (
                stale_replace_diagnostic_context.get("prompt_sha256_12")
                if stale_replace_diagnostic_context
                else None
            ),
        )
        if len(repair_prompt) > PLANNING_REPAIR_PROMPT_MAX_CHARS:
            budget_error = cls._build_repair_prompt_budget_error(
                repair_prompt_chars=len(repair_prompt),
                malformed_output_chars=compact_malformed_output_chars,
                validation_error_chars=validation_error_chars,
                knowledge_context_chars=knowledge_context_chars,
            )
            logger.warning(
                "[ORCHESTRATION] session_id=%s task_id=%s repair_prompt_exceeds_limit "
                "repair_prompt_chars=%s limit=%s",
                session_id,
                task_id,
                len(repair_prompt),
                PLANNING_REPAIR_PROMPT_MAX_CHARS,
            )
            emit_live(
                "ERROR",
                "[ORCHESTRATION] Planning repair prompt exceeded the safe prompt budget; skipping repair",
                metadata={
                    "phase": "planning",
                    "reason": "planning_repair_prompt_too_large",
                    "repair_prompt_chars": len(repair_prompt),
                    "malformed_output_chars": compact_malformed_output_chars,
                    "validation_error_chars": validation_error_chars,
                    "knowledge_context_chars": knowledge_context_chars,
                    "repair_prompt_build_seconds": round(
                        repair_prompt_build_seconds, 3
                    ),
                    "repair_attempts": 0,
                    **repair_prompt_metadata,
                },
            )
            raise PlanningRepairBudgetExceeded(budget_error)
        emit_live(
            "WARN",
            (
                "[ORCHESTRATION] Planning output was malformed; attempting one "
                f"repair pass (timeout: {repair_timeout}s)"
            ),
            metadata={
                "phase": "planning",
                "retry": "repair_prompt",
                "reason": reason[:240],
                "timeout_seconds": repair_timeout,
                "stale_replace_timeout_margin": timeout_margin_reason is not None,
                "repair_timeout_margin_reason": timeout_margin_reason,
                "repair_prompt_chars": len(repair_prompt),
                "malformed_output_chars": compact_malformed_output_chars,
                "repair_prompt_build_seconds": round(repair_prompt_build_seconds, 3),
                "repair_attempts": _repair_attempt_number,
                **repair_prompt_metadata,
            },
        )
        emit_live(
            "INFO",
            (
                "[ORCHESTRATION] Planning repair attempt is now running "
                f"(timeout: {repair_timeout}s)"
            ),
            metadata={
                "phase": "planning",
                "attempt": "repair",
                "strategy": "repair_prompt",
                "compact_no_output_retry": _compact_no_output_retry,
                "timeout_seconds": repair_timeout,
                "stale_replace_timeout_margin": timeout_margin_reason is not None,
                "repair_timeout_margin_reason": timeout_margin_reason,
                "repair_prompt_chars": len(repair_prompt),
                "malformed_output_chars": compact_malformed_output_chars,
                "repair_prompt_build_seconds": round(repair_prompt_build_seconds, 3),
                "repair_attempts": _repair_attempt_number,
                **repair_prompt_metadata,
            },
        )
        repair_started_at = time.monotonic()
        invoke_started_at = repair_started_at
        repair_lock_diagnostics: Dict[str, Any] = {}
        if lock_wait_seconds is not None:
            repair_lock_diagnostics["planning_lock_wait_seconds"] = lock_wait_seconds
        try:
            result = _run_coroutine_from_sync(
                asyncio.wait_for(
                    cls._invoke_repair_prompt(
                        runtime_service,
                        repair_prompt,
                        repair_timeout,
                        lock_diagnostics_out=repair_lock_diagnostics,
                        diagnostic_context=stale_replace_diagnostic_context,
                    ),
                    timeout=repair_timeout,
                )
            )
            repair_duration_seconds = time.monotonic() - repair_started_at
            parser_started_at = time.monotonic()
            runtime_diagnostics = result.get("diagnostics")
            if not isinstance(runtime_diagnostics, dict):
                runtime_diagnostics = {}
            lock_diagnostics = result.get("_planning_lock_diagnostics")
            if isinstance(lock_diagnostics, dict):
                runtime_diagnostics = {**runtime_diagnostics, **lock_diagnostics}
            planning_lock_wait_seconds = result.get(
                "planning_lock_wait_seconds",
                runtime_diagnostics.get("planning_lock_wait_seconds"),
            )
            repair_output_text = str(result.get("output") or "")
            repair_output_chars = len(repair_output_text)
            repair_output_token_estimate = max(0, (repair_output_chars + 3) // 4)
            repair_truncated = "...<truncated" in repair_output_text.lower()
            parser_validation_seconds = time.monotonic() - parser_started_at
            normalized_repair_output_text = cls._normalize_repair_json_array_output(
                repair_output_text
            )
            if normalized_repair_output_text is None:
                is_fenced = repair_output_text.lstrip().startswith("```")
                contract_reason = (
                    "repair returned markdown-fenced JSON; expected bare JSON array"
                    if is_fenced
                    else "repair returned prose; expected bare JSON array"
                )
                diagnostics = {
                    "output_contract_violated": True,
                    "repair_output_fenced": is_fenced,
                    "stdout_chars": repair_output_chars,
                    "stderr_chars": len(str(result.get("stderr") or "")),
                    "repair_attempts": _repair_attempt_number,
                }
                logger.warning(
                    "[ORCHESTRATION] Planning repair output contract violation: %s "
                    "(session_id=%s task_id=%s output_chars=%s repair_attempts=%s)",
                    contract_reason,
                    session_id,
                    task_id,
                    repair_output_chars,
                    _repair_attempt_number,
                )
                emit_live(
                    "ERROR",
                    f"[ORCHESTRATION] Repair output contract violation: {contract_reason}; stopping repair.",
                    metadata={
                        "phase": "planning",
                        "reason": "repair_output_contract_violation",
                        "contract_reason": contract_reason,
                        "repair_output_fenced": is_fenced,
                        "duration_seconds": round(repair_duration_seconds, 3),
                        "repair_prompt_chars": len(repair_prompt),
                        "malformed_output_chars": compact_malformed_output_chars,
                        "repair_reason": reason[:240],
                        "repair_attempts": _repair_attempt_number,
                        "repair_output_chars": repair_output_chars,
                    },
                )
                raise PlanningRepairOutputContractViolation(
                    contract_reason,
                    diagnostics,
                )
            if normalized_repair_output_text != repair_output_text:
                result["output"] = normalized_repair_output_text
                emit_live(
                    "WARN",
                    "[ORCHESTRATION] Planning repair returned fenced JSON; normalized to a bare JSON array.",
                    metadata={
                        "phase": "planning",
                        "reason": "planning_repair_fenced_json_normalized",
                        "repair_attempts": _repair_attempt_number,
                        "repair_output_chars": repair_output_chars,
                        "normalized_output_chars": len(normalized_repair_output_text),
                    },
                )
            if repair_duration_seconds > repair_timeout:
                raise TimeoutError(
                    f"Planning repair timed out after {repair_timeout:g}s "
                    f"(duration={repair_duration_seconds:.2f}s)"
                )
            logger.info(
                "[ORCHESTRATION] Planning repair completed in %.2fs "
                "(timeout=%ss session_id=%s task_id=%s output_chars=%s "
                "output_token_estimate=%s truncated=%s parser_validation_seconds=%.3f)",
                repair_duration_seconds,
                repair_timeout,
                session_id,
                task_id,
                repair_output_chars,
                repair_output_token_estimate,
                repair_truncated,
                parser_validation_seconds,
            )
            emit_live(
                "INFO",
                (
                    "[ORCHESTRATION] Planning repair completed "
                    f"in {repair_duration_seconds:.2f}s"
                ),
                metadata={
                    "phase": "planning",
                    "attempt": "repair",
                    "strategy": "repair_prompt",
                    "timeout_seconds": repair_timeout,
                    "duration_seconds": round(repair_duration_seconds, 3),
                    "openclaw_request_seconds": round(
                        repair_duration_seconds - parser_validation_seconds,
                        3,
                    ),
                    "repair_prompt_build_seconds": round(
                        repair_prompt_build_seconds, 3
                    ),
                    "parser_validation_seconds": round(parser_validation_seconds, 3),
                    "repair_output_chars": repair_output_chars,
                    "repair_output_token_estimate": repair_output_token_estimate,
                    "repair_output_truncated": repair_truncated,
                    "repair_attempts": _repair_attempt_number,
                    "planning_lock_wait_seconds": planning_lock_wait_seconds,
                    "repair_backend": result.get("backend"),
                    "planning_repair_direct": runtime_diagnostics.get(
                        "planning_repair_direct"
                    )
                    is True,
                    "direct_repair_seconds": (
                        runtime_diagnostics.get("duration_seconds")
                        if runtime_diagnostics.get("planning_repair_direct") is True
                        else None
                    ),
                    "direct_repair_timeout_seconds": (
                        runtime_diagnostics.get("timeout_seconds")
                        if runtime_diagnostics.get("planning_repair_direct") is True
                        else None
                    ),
                    "openclaw_process_seconds": runtime_diagnostics.get(
                        "duration_seconds"
                    ),
                    "openclaw_first_output_after_seconds": runtime_diagnostics.get(
                        "first_output_after_seconds"
                    ),
                },
            )
            record_pending_planning_repair_triplet(
                project_dir=project_dir,
                session_id=session_id,
                task_id=task_id,
                repair_attempt=_repair_attempt_number,
                previous_plan_text=malformed_output,
                repair_prompt=repair_prompt,
                repaired_plan_text=str(result.get("output") or ""),
                metadata={
                    "repair_reason": reason[:240],
                    "repair_backend": result.get("backend"),
                    "repair_prompt_chars": len(repair_prompt),
                    "malformed_output_chars": compact_malformed_output_chars,
                    "repair_output_chars": repair_output_chars,
                    "normalized_output_chars": len(str(result.get("output") or "")),
                    "repair_duration_seconds": round(repair_duration_seconds, 3),
                    "repair_attempts": _repair_attempt_number,
                    "compact_no_output_retry": _compact_no_output_retry,
                    **repair_prompt_metadata,
                },
            )
            result.pop("_planning_lock_diagnostics", None)
            return result
        except PlanningRepairOutputContractViolation:
            raise
        except PlanningRepairNoOutputTimeout:
            raise
        except Exception as exc:
            repair_duration_seconds = time.monotonic() - repair_started_at
            openclaw_request_seconds = time.monotonic() - invoke_started_at
            if cls._is_no_output_repair_timeout(exc):
                diagnostics = cls._get_runtime_diagnostics(exc)
                if not _no_output_retry_used:
                    logger.warning(
                        "[ORCHESTRATION] Planning repair produced no output before %.2fs; "
                        "retrying repair once "
                        "(repair_prompt_chars=%s malformed_output_chars=%s reason=%s)",
                        repair_duration_seconds,
                        len(repair_prompt),
                        compact_malformed_output_chars,
                        reason[:120],
                    )
                    emit_live(
                        "WARN",
                        "[ORCHESTRATION] Planning repair produced no output; retrying once.",
                        metadata={
                            "phase": "planning",
                            "reason": "planning_repair_no_output_retry",
                            "timeout_seconds": PLANNING_REPAIR_NO_OUTPUT_TIMEOUT_SECONDS,
                            "duration_seconds": round(repair_duration_seconds, 3),
                            "repair_prompt_chars": len(repair_prompt),
                            "malformed_output_chars": compact_malformed_output_chars,
                            "repair_reason": reason[:240],
                            "repair_attempts": _repair_attempt_number,
                            "next_repair_attempt": _repair_attempt_number + 1,
                            "next_strategy": "compact_repair_prompt",
                            "timeout_boundary": diagnostics.get("timeout_boundary")
                            or "repair_no_output",
                            "planning_lock_wait_seconds": diagnostics.get(
                                "planning_lock_wait_seconds"
                            ),
                            "openclaw_process_seconds": diagnostics.get(
                                "duration_seconds"
                            ),
                            "process_pid": diagnostics.get("process_pid"),
                        },
                    )
                    return cls.repair_output(
                        runtime_service=runtime_service,
                        task_description=task_description,
                        malformed_output=malformed_output,
                        project_dir=project_dir,
                        timeout_seconds=timeout_seconds,
                        logger=logger,
                        emit_live=emit_live,
                        reason=reason,
                        rejection_reasons=rejection_reasons,
                        prompt_profile=prompt_profile,
                        workflow_profile=workflow_profile,
                        workflow_phases=workflow_phases,
                        workspace_has_existing_files=workspace_has_existing_files,
                        knowledge_context=knowledge_context,
                        session_id=session_id,
                        task_id=task_id,
                        lock_wait_seconds=lock_wait_seconds,
                        _no_output_retry_used=True,
                        _repair_attempt_number=_repair_attempt_number + 1,
                        _compact_no_output_retry=True,
                        guidance_block=guidance_block,
                    )
                timeout_exc = PlanningRepairNoOutputTimeout(
                    (
                        "Planning repair produced no output before "
                        f"{PLANNING_REPAIR_NO_OUTPUT_TIMEOUT_SECONDS:g}s "
                        f"(duration={repair_duration_seconds:.2f}s)"
                    ),
                    diagnostics,
                )
                logger.warning(
                    "[ORCHESTRATION] Planning repair produced no output before %.2fs; "
                    "stopping after one retry "
                    "(first_output_after=%s stdout_chars=%s stderr_chars=%s "
                    "return_code=%s cancelled=%s timeout_boundary=%s "
                    "repair_prompt_chars=%s malformed_output_chars=%s reason=%s "
                    "repair_attempts=%s)",
                    repair_duration_seconds,
                    diagnostics.get("first_output_after_seconds"),
                    diagnostics.get("stdout_chars", 0),
                    diagnostics.get("stderr_chars", 0),
                    diagnostics.get("return_code"),
                    diagnostics.get("cancelled"),
                    diagnostics.get("timeout_boundary") or "repair_no_output",
                    len(repair_prompt),
                    compact_malformed_output_chars,
                    reason[:120],
                    _repair_attempt_number,
                )
                emit_live(
                    "ERROR",
                    (
                        "[ORCHESTRATION] Repair prompt was built, but OpenClaw "
                        "produced no output before timeout."
                    ),
                    metadata={
                        "phase": "planning",
                        "reason": "planning_repair_no_output_timeout",
                        "timeout_seconds": PLANNING_REPAIR_NO_OUTPUT_TIMEOUT_SECONDS,
                        "duration_seconds": round(repair_duration_seconds, 3),
                        "repair_prompt_build_seconds": round(
                            repair_prompt_build_seconds, 3
                        ),
                        "openclaw_request_seconds": round(openclaw_request_seconds, 3),
                        "repair_prompt_chars": len(repair_prompt),
                        "malformed_output_chars": compact_malformed_output_chars,
                        "repair_reason": reason[:240],
                        "repair_attempts": _repair_attempt_number,
                        "first_output_delay": diagnostics.get(
                            "first_output_after_seconds"
                        ),
                        "stdout_chars": diagnostics.get("stdout_chars", 0),
                        "stderr_chars": diagnostics.get("stderr_chars", 0),
                        "return_code": diagnostics.get("return_code"),
                        "cancelled": diagnostics.get("cancelled"),
                        "timeout_boundary": diagnostics.get("timeout_boundary")
                        or "repair_no_output",
                        "parser_validation_seconds": None,
                        "planning_lock_wait_seconds": diagnostics.get(
                            "planning_lock_wait_seconds"
                        ),
                        "openclaw_process_seconds": diagnostics.get("duration_seconds"),
                        "openclaw_no_output_elapsed_seconds": diagnostics.get(
                            "no_output_timeout_elapsed_seconds"
                        ),
                        "process_pid": diagnostics.get("process_pid"),
                    },
                )
                raise timeout_exc from exc
            if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                diagnostics = cls._get_runtime_diagnostics(exc)
                if diagnostics:
                    repair_lock_diagnostics.update(diagnostics)
                timeout_exc = TimeoutError(
                    f"Planning repair timed out after {repair_timeout:g}s "
                    f"(duration={repair_duration_seconds:.2f}s)"
                )
                logger.warning(
                    "[ORCHESTRATION] Planning repair prompt timed out after %.2fs; "
                    "stopping instead of retrying repair "
                    "(repair_prompt_chars=%s malformed_output_chars=%s reason=%s "
                    "repair_prompt_build_seconds=%.3f openclaw_request_seconds=%.3f "
                    "repair_attempts=1 timeout_seconds=%s)",
                    repair_duration_seconds,
                    len(repair_prompt),
                    compact_malformed_output_chars,
                    reason[:120],
                    repair_prompt_build_seconds,
                    openclaw_request_seconds,
                    repair_timeout,
                )
                emit_live(
                    "ERROR",
                    "[ORCHESTRATION] Planning repair diagnostics captured timeout boundary",
                    metadata={
                        "phase": "planning",
                        "reason": "malformed_planning_output_repair_timeout",
                        "timeout_seconds": repair_timeout,
                        "duration_seconds": round(repair_duration_seconds, 3),
                        "repair_prompt_build_seconds": round(
                            repair_prompt_build_seconds, 3
                        ),
                        "openclaw_request_seconds": round(openclaw_request_seconds, 3),
                        "repair_prompt_chars": len(repair_prompt),
                        "malformed_output_chars": compact_malformed_output_chars,
                        "repair_reason": reason[:240],
                        "repair_attempts": _repair_attempt_number,
                        "timeout_boundary": "planner_wait_for",
                        "planning_lock_wait_seconds": repair_lock_diagnostics.get(
                            "planning_lock_wait_seconds"
                        ),
                    },
                )
                raise timeout_exc from exc
            if cls._looks_like_timeout_error(exc):
                logger.warning(
                    "[ORCHESTRATION] Planning repair prompt timed out after %.2fs; "
                    "stopping instead of retrying repair "
                    "(repair_prompt_chars=%s malformed_output_chars=%s reason=%s "
                    "repair_prompt_build_seconds=%.3f openclaw_request_seconds=%.3f "
                    "repair_attempts=1 timeout_seconds=%s)",
                    repair_duration_seconds,
                    len(repair_prompt),
                    compact_malformed_output_chars,
                    reason[:120],
                    repair_prompt_build_seconds,
                    openclaw_request_seconds,
                    repair_timeout,
                )
            else:
                logger.warning(
                    "[ORCHESTRATION] Planning repair failed after %.2fs "
                    "(timeout=%ss session_id=%s task_id=%s "
                    "repair_prompt_build_seconds=%.3f openclaw_request_seconds=%.3f "
                    "repair_attempts=1): %s",
                    repair_duration_seconds,
                    repair_timeout,
                    session_id,
                    task_id,
                    repair_prompt_build_seconds,
                    openclaw_request_seconds,
                    exc,
                )
            raise
