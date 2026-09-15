"""Bounded evidence for one Planning provider invocation.

This module deliberately owns only planning-call evidence.  The append-only
orchestration event journal remains the lifecycle index; the per-attempt files
hold the bounded prompt/response material needed to interpret that index.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from app.services.orchestration.events.event_types import EventType
from app.services.workspace.control_state_paths import (
    control_state_root,
    project_control_state_location,
)
from app.services.workspace.permissions import ensure_shared_permissions
from app.services.workspace.system_settings import get_effective_runtime_root

logger = logging.getLogger(__name__)

PLANNING_EVIDENCE_DIRECTORY = "planning-evidence"
MAX_RETAINED_PROMPT_CHARS = 100_000
MAX_RETAINED_VISIBLE_CHARS = 100_000
MAX_RETAINED_REASONING_CHARS = 20_000
MAX_RETAINED_PARTIAL_CHARS = 8_000


def append_orchestration_event(**kwargs: Any) -> dict[str, Any]:
    """Lazy bridge to the existing event journal, avoiding import cycles."""

    from app.services.orchestration.state.persistence import (
        append_orchestration_event as append_event,
    )

    return append_event(**kwargs)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _token_estimate(text: str) -> int:
    return (len(text) + 3) // 4


def safe_endpoint_class(value: Any) -> str | None:
    """Return an endpoint class with credentials and query data removed."""

    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return f"{parsed.scheme}://{host}{parsed.path}".rstrip("/")
    return raw[:255]


def _text_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [item.get("text", "") for item in value if isinstance(item, Mapping)]
        if all(isinstance(part, str) for part in parts):
            return "".join(parts)
    return None


def _normalized_usage(value: Any) -> dict[str, int | None] | None:
    if not isinstance(value, Mapping):
        return None
    prompt = value.get("prompt_tokens", value.get("input_tokens"))
    completion = value.get("completion_tokens", value.get("output_tokens"))
    total = value.get("total_tokens")
    if prompt is None and completion is None and total is None:
        return None
    return {
        "prompt_tokens": prompt if isinstance(prompt, int) else None,
        "completion_tokens": completion if isinstance(completion, int) else None,
        "total_tokens": total if isinstance(total, int) else None,
    }


def inspect_chat_completion_response(body: Any) -> dict[str, Any]:
    """Extract only normal OpenAI-compatible response fields for evidence."""

    result: dict[str, Any] = {
        "response_received": True,
        "visible_content": None,
        "reasoning_content": None,
        "usage": (
            _normalized_usage(body.get("usage")) if isinstance(body, Mapping) else None
        ),
        "finish_reason": None,
        "provider_request_correlation_id": (
            str(body.get("id"))[:255]
            if isinstance(body, Mapping) and body.get("id") is not None
            else None
        ),
    }
    choices = body.get("choices") if isinstance(body, Mapping) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, Mapping) else None
    if isinstance(message, Mapping):
        result["visible_content"] = _text_value(message.get("content"))
        result["reasoning_content"] = _text_value(
            message.get("reasoning_content") or message.get("reasoning")
        )
    if isinstance(first, Mapping) and first.get("finish_reason") is not None:
        result["finish_reason"] = str(first["finish_reason"])[:255]
    return result


def _bounded_text(value: Any, limit: int) -> tuple[str | None, bool]:
    text = _text_value(value)
    if text is None and value is not None:
        text = str(value)
    if text is None:
        return None, False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _runtime_identity(runtime_service: Any) -> dict[str, Any]:
    task = getattr(runtime_service, "task_model", None)
    project_id = getattr(runtime_service, "project_id", None)
    if project_id is None:
        project_id = getattr(task, "project_id", None)
    return {
        "db": getattr(runtime_service, "db", None),
        "project_id": project_id,
        "session_id": getattr(runtime_service, "session_id", None),
        "task_id": getattr(runtime_service, "task_id", None),
        "task_execution_id": getattr(runtime_service, "task_execution_id", None),
    }


def _task_attempt_number(db: Any, task_execution_id: Any) -> int | None:
    if db is None or task_execution_id is None:
        return None
    try:
        from app.models import TaskExecution

        execution = (
            db.query(TaskExecution)
            .filter(TaskExecution.id == task_execution_id)
            .first()
        )
        value = getattr(execution, "attempt_number", None)
        return int(value) if value is not None else None
    except Exception:
        return None


def _resolve_control_state_location(
    *, db: Any, project_id: int | None, control_state_location: Any
) -> Any:
    if control_state_location is not None:
        return control_state_location
    if project_id is None:
        return None
    return project_control_state_location(
        get_effective_runtime_root(db), project_id, db=db
    )


def begin_planning_provider_evidence(
    *,
    control_state_location: Any = None,
    db: Any = None,
    project_id: int | None,
    task_id: int | None,
    session_id: int | None,
    task_execution_id: int | None,
    attempt: int | None,
    model: str | None,
    provider_endpoint_class: str | None,
    effective_timeout_seconds: float | int,
    transport_timeout_seconds: float | int,
    prompt: str,
    invocation_kind: str,
    provider_api_streaming: bool | None,
    partial_response_available_in_current_nonstreaming_path: bool | None = None,
) -> "PlanningProviderEvidence":
    """Persist request evidence and the provider-start event before I/O."""

    location = _resolve_control_state_location(
        db=db, project_id=project_id, control_state_location=control_state_location
    )
    recorder = PlanningProviderEvidence(
        control_state_location=location,
        project_id=project_id,
        task_id=task_id,
        session_id=session_id,
        task_execution_id=task_execution_id,
        attempt=attempt,
        model=model,
        provider_endpoint_class=safe_endpoint_class(provider_endpoint_class),
        effective_timeout_seconds=effective_timeout_seconds,
        transport_timeout_seconds=transport_timeout_seconds,
        prompt=prompt,
        invocation_kind=invocation_kind,
        provider_api_streaming=provider_api_streaming,
        partial_response_available_in_current_nonstreaming_path=(
            partial_response_available_in_current_nonstreaming_path
        ),
    )
    recorder.start()
    return recorder


def begin_planning_provider_evidence_from_runtime(
    runtime_service: Any,
    *,
    prompt: str,
    model: str | None,
    provider_endpoint_class: str | None,
    effective_timeout_seconds: float | int,
    transport_timeout_seconds: float | int,
    invocation_kind: str,
    provider_api_streaming: bool | None,
    partial_response_available_in_current_nonstreaming_path: bool | None = None,
) -> "PlanningProviderEvidence":
    identity = _runtime_identity(runtime_service)
    return begin_planning_provider_evidence(
        db=identity["db"],
        project_id=identity["project_id"],
        task_id=identity["task_id"],
        session_id=identity["session_id"],
        task_execution_id=identity["task_execution_id"],
        attempt=_task_attempt_number(identity["db"], identity["task_execution_id"]),
        model=model,
        provider_endpoint_class=provider_endpoint_class,
        effective_timeout_seconds=effective_timeout_seconds,
        transport_timeout_seconds=transport_timeout_seconds,
        prompt=prompt,
        invocation_kind=invocation_kind,
        provider_api_streaming=provider_api_streaming,
        partial_response_available_in_current_nonstreaming_path=(
            partial_response_available_in_current_nonstreaming_path
        ),
    )


@dataclass
class PlanningProviderEvidence:
    control_state_location: Any
    project_id: int | None
    task_id: int | None
    session_id: int | None
    task_execution_id: int | None
    attempt: int | None
    model: str | None
    provider_endpoint_class: str | None
    effective_timeout_seconds: float | int
    transport_timeout_seconds: float | int
    prompt: str
    invocation_kind: str
    provider_api_streaming: bool | None
    partial_response_available_in_current_nonstreaming_path: bool | None
    attempt_id: str = field(default_factory=lambda: f"planning-{uuid.uuid4().hex}")
    provider_started_at: str = field(default_factory=_utc_now)
    _monotonic_started_at: float = field(init=False, repr=False)
    _metadata: dict[str, Any] = field(init=False, repr=False)
    _terminalized: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        import time

        self._monotonic_started_at = time.monotonic()
        prompt_hash = hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()
        self._metadata = {
            "schema": "planning_provider_evidence.v1",
            "status": "started",
            "attempt_id": self.attempt_id,
            "attempt": self.attempt,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "task_execution_id": self.task_execution_id,
            "model": self.model,
            "provider_endpoint_class": self.provider_endpoint_class,
            "invocation_kind": self.invocation_kind,
            "prompt_sha256": prompt_hash,
            "prompt_chars": len(self.prompt),
            "prompt_token_estimate": _token_estimate(self.prompt),
            "provider_started_at": self.provider_started_at,
            "effective_timeout_seconds": self.effective_timeout_seconds,
            "transport_timeout_seconds": self.transport_timeout_seconds,
            "provider_api_streaming": self.provider_api_streaming,
            "partial_response_available_in_current_nonstreaming_path": (
                self.partial_response_available_in_current_nonstreaming_path
            ),
            "prompt_reconstructable": len(self.prompt) <= MAX_RETAINED_PROMPT_CHARS,
            "response_received": False,
            "visible_chars": 0,
            "reasoning_chars": 0,
            "partial_content_chars": 0,
        }

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    @property
    def artifact_directory(self) -> Path:
        if self.control_state_location is None:
            return Path(tempfile.gettempdir()) / "orchestrator-no-evidence"
        return (
            control_state_root(self.control_state_location)
            / PLANNING_EVIDENCE_DIRECTORY
            / self.attempt_id
        )

    def _artifact_reference(self) -> str:
        return str(self.artifact_directory / "evidence.json")

    def _event_details(self, *, terminal: bool = False) -> dict[str, Any]:
        details = {
            key: value
            for key, value in self._metadata.items()
            if key
            not in {
                "schema",
                "status",
                "response_received",
                "visible_chars",
                "reasoning_chars",
                "partial_content_chars",
            }
        }
        details["evidence_artifact"] = self._artifact_reference()
        if terminal:
            details.update(
                {
                    "elapsed_seconds": self._metadata.get("elapsed_seconds"),
                    "response_received": self._metadata.get("response_received"),
                    "visible_chars": self._metadata.get("visible_chars", 0),
                    "reasoning_chars": self._metadata.get("reasoning_chars", 0),
                    "usage": self._metadata.get("usage"),
                    "finish_reason": self._metadata.get("finish_reason"),
                    "exception_type": self._metadata.get("exception_type"),
                }
            )
        return details

    def _append_event(self, event_type: str, *, terminal: bool = False) -> None:
        if self.control_state_location is None:
            return
        try:
            append_orchestration_event(
                project_dir=self.control_state_location,
                session_id=self.session_id or 0,
                task_id=self.task_id or 0,
                event_type=event_type,
                details=self._event_details(terminal=terminal),
            )
        except Exception as exc:
            logger.warning(
                "Planning provider evidence event could not be persisted: %s", exc
            )

    def start(self) -> None:
        if self.control_state_location is None:
            self._metadata["evidence_persistence"] = "unavailable_no_control_state"
            return
        self.artifact_directory.mkdir(parents=True, exist_ok=True)
        ensure_shared_permissions(self.artifact_directory)
        if len(self.prompt) <= MAX_RETAINED_PROMPT_CHARS:
            _write_text(self.artifact_directory / "planning-request.txt", self.prompt)
        self._metadata["evidence_artifact"] = self._artifact_reference()
        self._metadata["request_artifact"] = str(
            self.artifact_directory / "planning-request.txt"
        )
        _write_json(self.artifact_directory / "planning-response.json", {})
        _write_json(self.artifact_directory / "evidence.json", self._metadata)
        self._append_event(EventType.PLANNING_PROVIDER_STARTED)

    def _elapsed(self) -> float:
        import time

        return round(time.monotonic() - self._monotonic_started_at, 3)

    def _finish(
        self,
        *,
        status: str,
        response_received: bool,
        visible_content: Any = None,
        reasoning_content: Any = None,
        usage: Any = None,
        finish_reason: Any = None,
        provider_request_correlation_id: Any = None,
        partial_content_snapshot: Any = None,
        exception_type: str | None = None,
        partial_response_available: bool | None = None,
    ) -> dict[str, Any]:
        if self._terminalized:
            return self.metadata
        self._terminalized = True
        visible, visible_truncated = _bounded_text(
            visible_content, MAX_RETAINED_VISIBLE_CHARS
        )
        reasoning, reasoning_truncated = _bounded_text(
            reasoning_content, MAX_RETAINED_REASONING_CHARS
        )
        partial, partial_truncated = _bounded_text(
            partial_content_snapshot, MAX_RETAINED_PARTIAL_CHARS
        )
        ended_at = _utc_now()
        self._metadata.update(
            {
                "status": status,
                "provider_ended_at": ended_at,
                "response_timestamp": ended_at,
                "elapsed_seconds": self._elapsed(),
                "response_received": bool(response_received),
                "visible_chars": len(visible or ""),
                "reasoning_chars": len(reasoning or ""),
                "partial_content_chars": len(partial or ""),
                "visible_content_truncated": visible_truncated,
                "reasoning_content_truncated": reasoning_truncated,
                "partial_content_truncated": partial_truncated,
                "usage": _normalized_usage(usage),
                "finish_reason": (
                    str(finish_reason)[:255] if finish_reason is not None else None
                ),
                "provider_request_correlation_id": (
                    str(provider_request_correlation_id)[:255]
                    if provider_request_correlation_id is not None
                    else None
                ),
                "exception_type": exception_type,
            }
        )
        if partial_response_available is not None:
            self._metadata["partial_response_available"] = partial_response_available
        response_payload = {
            "response_received": bool(response_received),
            "visible_content": visible,
            "reasoning_content": reasoning,
            "usage": _normalized_usage(usage),
            "finish_reason": self._metadata["finish_reason"],
            "provider_request_correlation_id": self._metadata[
                "provider_request_correlation_id"
            ],
            "partial_content_snapshot": partial,
        }
        if self.control_state_location is None:
            return self.metadata
        _write_json(
            self.artifact_directory / "planning-response.json", response_payload
        )
        _write_json(self.artifact_directory / "evidence.json", self._metadata)
        self._append_event(
            (
                EventType.PLANNING_PROVIDER_COMPLETED
                if status == "completed"
                else EventType.PLANNING_PROVIDER_FAILED
            ),
            terminal=True,
        )
        return self.metadata

    def complete(self, **kwargs: Any) -> dict[str, Any]:
        return self._finish(status="completed", response_received=True, **kwargs)

    def fail(
        self,
        exception: BaseException,
        *,
        response_received: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self._finish(
            status="failed",
            response_received=response_received,
            exception_type=type(exception).__name__,
            **kwargs,
        )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_shared_permissions(path.parent)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    ensure_shared_permissions(temporary)
    temporary.replace(path)
    ensure_shared_permissions(path)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


_EVIDENCE_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|authorization|bearer|cookie|credential|"
    r"password|secret|token)",
    re.IGNORECASE,
)
_EVIDENCE_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|password|secret|token)\s*[:=]\s*)"
        r"([^\s,;\"']+)"
    ),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def _redact_evidence_value(value: Any) -> tuple[Any, bool]:
    """Return a deterministic evidence copy without credential-like values."""

    if isinstance(value, Mapping):
        redacted = False
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _EVIDENCE_SECRET_KEY_RE.search(key_text):
                result[key_text] = "<redacted>"
                redacted = True
                continue
            safe_item, item_redacted = _redact_evidence_value(item)
            result[key_text] = safe_item
            redacted = redacted or item_redacted
        return result, redacted
    if isinstance(value, (list, tuple)):
        items = []
        redacted = False
        for item in value:
            safe_item, item_redacted = _redact_evidence_value(item)
            items.append(safe_item)
            redacted = redacted or item_redacted
        return items, redacted
    if isinstance(value, bytes):
        return "<redacted-bytes>", True
    if isinstance(value, str):
        result = value
        redacted = False
        for pattern in _EVIDENCE_SECRET_TEXT_PATTERNS:
            updated = pattern.sub(
                lambda match: (
                    match.group(1) + "<redacted>"
                    if match.lastindex and match.lastindex >= 1
                    else "<redacted>"
                ),
                result,
            )
            redacted = redacted or updated != result
            result = updated
        return result, redacted
    return value, False


def _evidence_serialized(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return str(value)


def _evidence_representation(value: Any, *, retained: bool = True) -> dict[str, Any]:
    serialized = _evidence_serialized(value)
    safe_value, redacted = _redact_evidence_value(value)
    safe_serialized = _evidence_serialized(safe_value)
    representation = {
        "retained": retained,
        "redacted": redacted,
        "length": len(serialized),
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "retained_length": len(safe_serialized) if retained else 0,
        "retained_sha256": (
            hashlib.sha256(safe_serialized.encode("utf-8")).hexdigest()
            if retained
            else None
        ),
    }
    if retained:
        representation["value"] = safe_value
    return representation


class PlanningRepairResponseEvidence:
    """Explicit, bounded response capture for one Planning repair call.

    This is intentionally separate from the existing always-on lifecycle
    evidence recorder. A caller supplies a unique path to opt in. Every write
    is best-effort so evidence persistence cannot alter provider output,
    parser behavior, or Planning authority; persistence errors are exposed in
    the diagnostic summary and mark the artifact incomplete when writable.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        correlation_id: str | None,
        backend: str | None,
        model: str | None,
        role: str | None,
        endpoint: str | None,
        logical_timeout_seconds: float | int | None,
        transport_timeout_seconds: float | int | None,
    ) -> None:
        self.path = Path(path)
        self._persistence_errors: list[str] = []
        self.document: dict[str, Any] = {
            "schema_version": "planning_repair_provider_response_evidence.v1",
            "capture_enabled": True,
            "capture_scope": "planning_repair",
            "correlation": {
                "provider_call_id": str(correlation_id or uuid.uuid4().hex),
            },
            "provider": {
                "backend": backend,
                "model": model,
                "role": role,
                "endpoint_class": safe_endpoint_class(endpoint),
                "http_status": None,
                "finish_reason": None,
                "usage": None,
                "provider_request_correlation_id": None,
            },
            "request": {},
            "representations": {
                "raw_http_bytes": {
                    "retained": False,
                    "redacted": False,
                    "sha256": None,
                    "length": 0,
                    "representation": "RAW_HTTP_BYTES",
                },
                "provider_envelope": {
                    "retained": True,
                    "representation": "REDACTED_PROVIDER_ENVELOPE_METADATA",
                },
                "assistant_content": {
                    "retained": False,
                    "representation": "RAW_ASSISTANT_CONTENT",
                },
                "extracted_content": {
                    "retained": False,
                    "representation": "ADAPTER_EXTRACTED_CONTENT",
                },
                "adapter_normalized_content": {
                    "retained": False,
                    "representation": "ADAPTER_NORMALIZED_CONTENT",
                },
                "planner_visible_content": {
                    "retained": False,
                    "representation": "PLANNER_VISIBLE_CONTENT",
                },
            },
            "normalization": {
                "transformed": False,
                "classification": "not_reached",
            },
            "planner_output_contract": {
                "status": "not_reached",
            },
            "timing": {
                "request_started_utc": _utc_now(),
                "response_received_utc": None,
                "request_finished_utc": None,
                "logical_timeout_seconds": logical_timeout_seconds,
                "transport_timeout_seconds": transport_timeout_seconds,
            },
            "error": None,
            "evidence_persistence": {
                "status": "pending",
                "errors": [],
            },
        }

    @classmethod
    def load(cls, path: str | Path) -> "PlanningRepairResponseEvidence":
        """Load one adapter-created artifact for the Planner verdict update."""

        instance = cls.__new__(cls)
        instance.path = Path(path)
        instance._persistence_errors = []
        instance.document = json.loads(instance.path.read_text(encoding="utf-8"))
        return instance

    @property
    def correlation_id(self) -> str:
        return str(self.document["correlation"]["provider_call_id"])

    @property
    def persistence_errors(self) -> list[str]:
        return list(self._persistence_errors)

    def _persist(self) -> None:
        self.document["evidence_persistence"] = {
            "status": "incomplete" if self._persistence_errors else "complete",
            "errors": list(self._persistence_errors),
        }
        try:
            _write_json(self.path, self.document)
        except Exception as exc:  # pragma: no cover - exercised through callers
            message = f"{type(exc).__name__}: {str(exc)[:240]}"
            if message not in self._persistence_errors:
                self._persistence_errors.append(message)

    def start(self, *, prompt: str, payload_keys: list[str]) -> None:
        self.document["request"] = {
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt_chars": len(prompt),
            "payload_keys": sorted(str(key) for key in payload_keys),
        }
        self._persist()

    def record_http_response(
        self, *, status_code: int | None, content_type: str | None, raw_body: bytes
    ) -> None:
        self.document["provider"]["http_status"] = status_code
        self.document["timing"]["response_received_utc"] = _utc_now()
        self.document["representations"]["raw_http_bytes"] = {
            "retained": False,
            "redacted": False,
            "representation": "RAW_HTTP_BYTES",
            "sha256": hashlib.sha256(raw_body).hexdigest(),
            "length": len(raw_body),
            "content_type": str(content_type or "")[:255] or None,
            "retention_reason": "raw HTTP bytes are not durably retained; parsed evidence is captured separately",
        }
        self._persist()

    def record_envelope(self, body: Any, *, request_correlation_id: Any = None) -> None:
        observed = inspect_chat_completion_response(body)
        self.document["provider"].update(
            {
                "finish_reason": observed.get("finish_reason"),
                "usage": observed.get("usage"),
                "provider_request_correlation_id": (
                    str(
                        request_correlation_id
                        or observed.get("provider_request_correlation_id")
                    )[:255]
                    if (
                        request_correlation_id
                        or observed.get("provider_request_correlation_id")
                    )
                    else None
                ),
            }
        )
        choices = body.get("choices") if isinstance(body, Mapping) else None
        self.document["representations"]["provider_envelope"] = {
            "retained": True,
            "representation": "REDACTED_PROVIDER_ENVELOPE_METADATA",
            "keys": (
                sorted(str(key) for key in body) if isinstance(body, Mapping) else []
            ),
            "choice_count": len(choices) if isinstance(choices, list) else 0,
            "assistant_roles": [
                str((choice.get("message") or {}).get("role"))
                for choice in (choices or [])
                if isinstance(choice, Mapping)
                and isinstance(choice.get("message"), Mapping)
            ],
            "model": (
                str(body.get("model"))[:255]
                if isinstance(body, Mapping) and body.get("model") is not None
                else None
            ),
            "id": (
                str(body.get("id"))[:255]
                if isinstance(body, Mapping) and body.get("id") is not None
                else None
            ),
            "usage": observed.get("usage"),
            "finish_reason": observed.get("finish_reason"),
        }
        self._persist()

    def record_content_stages(
        self,
        *,
        assistant_content: Any,
        extracted_content: Any,
        adapter_normalized_content: Any,
        planner_visible_content: Any,
        transformed: bool,
        classification: str,
    ) -> None:
        representations = self.document["representations"]
        representations["assistant_content"] = {
            **_evidence_representation(assistant_content),
            "source_representation": "RAW_ASSISTANT_CONTENT",
            "representation": "REDACTED_ASSISTANT_CONTENT",
        }
        representations["extracted_content"] = {
            **_evidence_representation(extracted_content),
            "source_representation": "ADAPTER_EXTRACTED_CONTENT",
            "representation": "REDACTED_ADAPTER_EXTRACTED_CONTENT",
        }
        representations["adapter_normalized_content"] = {
            **_evidence_representation(adapter_normalized_content),
            "source_representation": "ADAPTER_NORMALIZED_CONTENT",
            "representation": "REDACTED_ADAPTER_NORMALIZED_CONTENT",
        }
        representations["planner_visible_content"] = {
            **_evidence_representation(planner_visible_content),
            "source_representation": "PLANNER_VISIBLE_CONTENT",
            "representation": "REDACTED_PLANNER_VISIBLE_CONTENT",
        }
        self.document["normalization"] = {
            "transformed": bool(transformed),
            "classification": str(classification),
        }
        self._persist()

    def record_planner_contract(
        self,
        *,
        status: str,
        input_content: Any,
        normalized_content: Any = None,
        reason: str | None = None,
        fenced: bool | None = None,
    ) -> None:
        self.document["planner_output_contract"] = {
            "status": str(status),
            "reason": str(reason)[:500] if reason else None,
            "fenced": fenced,
            "input": _evidence_representation(input_content),
            "normalized": (
                _evidence_representation(normalized_content)
                if normalized_content is not None
                else None
            ),
        }
        self._persist()

    def complete(self) -> None:
        self.document["timing"]["request_finished_utc"] = _utc_now()
        if not self._persistence_errors:
            self.document["evidence_persistence"]["status"] = "complete"
        self._persist()

    def fail(self, exception: BaseException, *, response_received: bool) -> None:
        self.document["timing"]["request_finished_utc"] = _utc_now()
        self.document["error"] = {
            "type": type(exception).__name__,
            "message": _redact_evidence_value(str(exception))[0],
            "response_received": bool(response_received),
        }
        self._persist()

    def diagnostics(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "path": str(self.path),
            "provider_call_id": self.correlation_id,
            "persistence_status": (
                "incomplete" if self._persistence_errors else "complete"
            ),
            "persistence_errors": list(self._persistence_errors),
        }


__all__ = [
    "MAX_RETAINED_PROMPT_CHARS",
    "PlanningProviderEvidence",
    "PlanningRepairResponseEvidence",
    "begin_planning_provider_evidence",
    "begin_planning_provider_evidence_from_runtime",
    "inspect_chat_completion_response",
    "safe_endpoint_class",
]
