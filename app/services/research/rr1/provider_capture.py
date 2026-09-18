"""All-lane provider evidence capture and deterministic call accounting.

Design constraints this module is built to satisfy:

* **Caller-independent.**  Capture attaches to the provider adapter classes,
  so a provider call from a path nobody expected -- the BR2 certification
  incident shape -- is still recorded and counted.
* **Neutral.**  The wrapper forwards ``*args``/``**kwargs`` unchanged and
  returns the adapter's own result object unchanged.  It adds no prompt,
  option, timeout, retry, parser, or normalizer behavior.  Capture ON/OFF
  behavioral equivalence is therefore a property of the wrapper shape, and is
  asserted by the deterministic regression suite.
* **Never silently zero.**  Incomplete capture is a distinct accounting
  verdict.  ``UNKNOWN`` is never collapsed into ``0``.
"""

from __future__ import annotations

import importlib
import inspect
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Iterator

from app.services.research.rr1.lanes import (
    LANE_UNATTRIBUTED,
    NON_PROVIDER_RUNTIMES,
    PROVIDER_CAPABLE_RUNTIMES,
    PROVIDER_ENTRY_METHODS,
    lane_for_session_prefix,
)

# Accounting verdicts (§15).
CALLS_NONE = "NONE"
CALLS_ONE = "ONE"
CALLS_MANY = "MANY"
CALLS_UNKNOWN = "UNKNOWN_INCOMPLETE_CAPTURE"

# Evidence levels for recorded prompt/output material.
EVIDENCE_LEVEL_METADATA = "metadata_only"
EVIDENCE_LEVEL_BOUNDED_TEXT = "bounded_text"

_MAX_TEXT_CHARS = 4000

_ATTR_ORIGINAL = "_rr1_original"
_ATTR_INSTRUMENTED = "_rr1_instrumented"

#: Correlation identity for the research run currently being observed.
_correlation: ContextVar[str | None] = ContextVar("rr1_correlation", default=None)
#: Declared lane, when a lane entry point announces itself.
_lane: ContextVar[str | None] = ContextVar("rr1_lane", default=None)
#: Re-entrancy guard so an adapter method that delegates to another adapter
#: method on the same instance counts as one provider invocation.
_boundary_depth: ContextVar[int] = ContextVar("rr1_boundary_depth", default=0)
#: The outermost boundary record currently in flight, so a delegating entry
#: method can attach itself to it before that record is finalized.
_outer_record: ContextVar[Any] = ContextVar("rr1_outer_record", default=None)


@dataclass
class ProviderCallRecord:
    """Correlated evidence for one provider boundary invocation (§13)."""

    call_id: str
    correlation_id: str | None
    lane_id: str
    declared_lane: str | None
    session_prefix: str | None
    runtime_class: str
    entry_method: str
    nested: bool
    research_run_id: str | None = None
    session_id: int | None = None
    task_id: int | None = None
    task_execution_id: int | None = None
    project_id: int | None = None
    backend: str | None = None
    model_family: str | None = None
    role: str | None = None
    adaptation_profile: str | None = None
    timeout_seconds: float | None = None
    no_output_timeout_seconds: float | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    reasoning_enabled: bool | None = None
    retry_configuration: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: float | None = None
    transport_completed: bool | None = None
    content_returned: bool | None = None
    result_representation: str | None = None
    parse_verdict: str | None = None
    error_type: str | None = None
    error: str | None = None
    evidence_level: str = EVIDENCE_LEVEL_METADATA
    prompt_chars: int | None = None
    prompt_excerpt: str | None = None
    output_chars: int | None = None
    output_excerpt: str | None = None
    nested_entries: list[str] = field(default_factory=list)

    def as_evidence(self) -> dict[str, Any]:
        return {key: value for key, value in vars(self).items()}


@dataclass
class ProviderCallAccounting:
    """Deterministic 0 / 1 / N / UNKNOWN accounting surface (§15)."""

    records: list[ProviderCallRecord] = field(default_factory=list)
    capture_installed: bool = False
    capture_gaps: list[str] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return sum(1 for record in self.records if not record.nested)

    @property
    def verdict(self) -> str:
        if self.capture_gaps or not self.capture_installed:
            # Incomplete capture is never reported as zero.
            return CALLS_UNKNOWN
        count = self.call_count
        if count == 0:
            return CALLS_NONE
        if count == 1:
            return CALLS_ONE
        return CALLS_MANY

    @property
    def unexpected_calls(self) -> list[ProviderCallRecord]:
        """Boundary calls that no lane entry point declared."""

        return [
            record
            for record in self.records
            if not record.nested and record.declared_lane is None
        ]

    def by_lane(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            if record.nested:
                continue
            counts[record.lane_id] = counts.get(record.lane_id, 0) + 1
        return counts

    def record_gap(self, reason: str) -> None:
        if reason not in self.capture_gaps:
            self.capture_gaps.append(reason)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "call_count": self.call_count,
            "capture_installed": self.capture_installed,
            "capture_gaps": list(self.capture_gaps),
            "by_lane": self.by_lane(),
            "unexpected_call_count": len(self.unexpected_calls),
            "unexpected_calls": [
                record.as_evidence() for record in self.unexpected_calls
            ],
            "records": [record.as_evidence() for record in self.records],
        }


class ProviderCaptureSink:
    """Thread-safe collector for provider boundary records."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.accounting = ProviderCallAccounting()

    def add(self, record: ProviderCallRecord) -> None:
        with self._lock:
            self.accounting.records.append(record)

    def record_gap(self, reason: str) -> None:
        with self._lock:
            self.accounting.record_gap(reason)

    def reset(self) -> None:
        with self._lock:
            self.accounting = ProviderCallAccounting()


@contextmanager
def research_correlation(correlation_id: str) -> Iterator[str]:
    """Bind one stable research correlation identity for the whole run (§13)."""

    token = _correlation.set(str(correlation_id))
    try:
        yield str(correlation_id)
    finally:
        _correlation.reset(token)


@contextmanager
def provider_lane(lane_id: str) -> Iterator[str]:
    """Declare the lane that is about to invoke a provider."""

    token = _lane.set(str(lane_id))
    try:
        yield str(lane_id)
    finally:
        _lane.reset(token)


def current_correlation_id() -> str | None:
    return _correlation.get()


def current_lane() -> str | None:
    return _lane.get()


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _excerpt(value: str | None, evidence_level: str) -> str | None:
    if value is None or evidence_level != EVIDENCE_LEVEL_BOUNDED_TEXT:
        return None
    return value[:_MAX_TEXT_CHARS]


def _configuration_from(runtime: Any, options: Any) -> dict[str, Any]:
    configuration = getattr(runtime, "runtime_configuration", None)
    descriptor = getattr(runtime, "backend_descriptor", None)
    payload: dict[str, Any] = {
        "backend": getattr(configuration, "backend_name", None)
        or getattr(descriptor, "name", None),
        "model_family": getattr(configuration, "model_family", None),
        "adaptation_profile": getattr(configuration, "adaptation_profile", None),
        "role": getattr(runtime, "backend_role", None),
    }
    role = payload["role"]
    payload["role"] = getattr(role, "value", role)
    payload["role"] = str(payload["role"]) if payload["role"] else None
    if options is not None:
        for name in (
            "timeout_seconds",
            "no_output_timeout_seconds",
            "max_output_tokens",
            "temperature",
            "reasoning_enabled",
        ):
            payload[name] = getattr(options, name, None)
    return payload


def _result_shape(result: Any) -> tuple[str, bool | None, str | None, int | None]:
    """Return (representation, content_returned, parse_verdict, output_chars)."""

    representation = type(result).__name__
    if isinstance(result, dict):
        output = result.get("output")
        text = _text(output)
        if text is None and output is not None:
            return representation, True, "non_text_output", None
        if text is None:
            return representation, False, "absent_output_key", None
        return representation, bool(text.strip()), "text_output", len(text)
    text = _text(result)
    if text is not None:
        return representation, bool(text.strip()), "text_output", len(text)
    return representation, None, "unrecognized_representation", None


def _make_wrapper(
    original: Callable[..., Any],
    *,
    sink: ProviderCaptureSink,
    runtime_class: str,
    entry_method: str,
    evidence_level: str,
    research_run_id: str | None,
) -> Callable[..., Any]:
    """Wrap one adapter coroutine method without altering its contract."""

    async def wrapper(self, *args: Any, **kwargs: Any):
        depth = _boundary_depth.get()
        nested = depth > 0
        declared = _lane.get()
        session_prefix = kwargs.get("session_prefix")
        prompt = args[0] if args else kwargs.get("prompt")
        options = kwargs.get("invocation_options")
        configuration = _configuration_from(self, options)
        prompt_text = _text(prompt)
        record = ProviderCallRecord(
            call_id=str(uuid.uuid4()),
            correlation_id=_correlation.get(),
            lane_id=declared or lane_for_session_prefix(session_prefix),
            declared_lane=declared,
            session_prefix=(
                str(session_prefix) if session_prefix is not None else None
            ),
            runtime_class=runtime_class,
            entry_method=entry_method,
            nested=nested,
            research_run_id=research_run_id,
            session_id=getattr(self, "session_id", None),
            task_id=getattr(self, "task_id", None),
            task_execution_id=getattr(self, "task_execution_id", None),
            project_id=getattr(self, "project_id", None),
            backend=configuration.get("backend"),
            model_family=configuration.get("model_family"),
            role=configuration.get("role"),
            adaptation_profile=configuration.get("adaptation_profile"),
            timeout_seconds=_as_float(
                configuration.get("timeout_seconds")
                if configuration.get("timeout_seconds") is not None
                else kwargs.get("timeout_seconds")
            ),
            no_output_timeout_seconds=_as_float(
                configuration.get("no_output_timeout_seconds")
                if configuration.get("no_output_timeout_seconds") is not None
                else kwargs.get("no_output_timeout_seconds")
            ),
            max_output_tokens=configuration.get("max_output_tokens"),
            temperature=configuration.get("temperature"),
            reasoning_enabled=configuration.get("reasoning_enabled"),
            retry_configuration=_retry_configuration(self),
            evidence_level=evidence_level,
            prompt_chars=len(prompt_text) if prompt_text is not None else None,
            prompt_excerpt=_excerpt(prompt_text, evidence_level),
            started_at=datetime.now(UTC).isoformat(),
        )
        outer = _outer_record.get()
        if nested and outer is not None:
            outer.nested_entries.append(f"{runtime_class}.{entry_method}")
        started = datetime.now(UTC)
        token = _boundary_depth.set(depth + 1)
        outer_token = None if nested else _outer_record.set(record)
        try:
            result = await original(self, *args, **kwargs)
        except BaseException as exc:
            record.transport_completed = False
            record.content_returned = False
            record.error_type = type(exc).__name__
            record.error = str(exc)[:500]
            record.parse_verdict = "not_reached"
            raise
        else:
            record.transport_completed = True
            (
                record.result_representation,
                record.content_returned,
                record.parse_verdict,
                record.output_chars,
            ) = _result_shape(result)
            record.output_excerpt = _excerpt(
                _output_text(result), record.evidence_level
            )
            return result
        finally:
            _boundary_depth.reset(token)
            if outer_token is not None:
                _outer_record.reset(outer_token)
            ended = datetime.now(UTC)
            record.ended_at = ended.isoformat()
            record.duration_seconds = round((ended - started).total_seconds(), 6)
            sink.add(record)

    wrapper.__name__ = getattr(original, "__name__", entry_method)
    wrapper.__qualname__ = getattr(original, "__qualname__", entry_method)
    wrapper.__doc__ = getattr(original, "__doc__", None)
    setattr(wrapper, _ATTR_ORIGINAL, original)
    setattr(wrapper, _ATTR_INSTRUMENTED, True)
    return wrapper


def _output_text(result: Any) -> str | None:
    if isinstance(result, dict):
        return _text(result.get("output"))
    return _text(result)


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _retry_configuration(runtime: Any) -> str | None:
    descriptor = getattr(runtime, "backend_descriptor", None)
    strategy = getattr(descriptor, "preferred_retry_strategy", None)
    if strategy is None:
        return None
    to_dict = getattr(strategy, "to_dict", None)
    if callable(to_dict):
        try:
            return str(sorted(to_dict().items()))
        except Exception:  # noqa: BLE001 - evidence must not break a call
            return None
    return None


class ProviderCapture:
    """Install/remove all-lane capture across every provider adapter class."""

    def __init__(
        self,
        *,
        research_run_id: str | None = None,
        evidence_level: str = EVIDENCE_LEVEL_METADATA,
    ) -> None:
        self.sink = ProviderCaptureSink()
        self.research_run_id = research_run_id
        self.evidence_level = evidence_level
        self.active = False
        self._patched: list[tuple[type, str, Any]] = []

    @property
    def accounting(self) -> ProviderCallAccounting:
        return self.sink.accounting

    def install(self) -> ProviderCallAccounting:
        """Wrap every provider entry method that exists at this baseline.

        A class or method that cannot be resolved records a capture gap, so
        the accounting verdict degrades to ``UNKNOWN`` rather than reporting a
        confident zero over a boundary it never watched.
        """

        covered_any = False
        for module_name, class_name in PROVIDER_CAPABLE_RUNTIMES:
            try:
                module = importlib.import_module(module_name)
                runtime_class = getattr(module, class_name)
            except (ImportError, AttributeError) as exc:
                self.sink.record_gap(
                    f"unresolvable_runtime:{module_name}.{class_name}:"
                    f"{type(exc).__name__}"
                )
                continue
            class_covered = False
            for method_name in PROVIDER_ENTRY_METHODS:
                original = getattr(runtime_class, method_name, None)
                if original is None:
                    continue
                if getattr(original, _ATTR_INSTRUMENTED, False):
                    self.sink.record_gap(
                        f"already_instrumented:{class_name}.{method_name}"
                    )
                    continue
                if not inspect.iscoroutinefunction(original):
                    self.sink.record_gap(
                        f"non_coroutine_entry:{class_name}.{method_name}"
                    )
                    continue
                setattr(
                    runtime_class,
                    method_name,
                    _make_wrapper(
                        original,
                        sink=self.sink,
                        runtime_class=f"{module_name}.{class_name}",
                        entry_method=method_name,
                        evidence_level=self.evidence_level,
                        research_run_id=self.research_run_id,
                    ),
                )
                self._patched.append((runtime_class, method_name, original))
                class_covered = True
            if not class_covered:
                self.sink.record_gap(f"no_entry_methods:{class_name}")
            covered_any = covered_any or class_covered
        self.sink.accounting.capture_installed = covered_any
        self.active = covered_any
        return self.sink.accounting

    def remove(self) -> None:
        """Restore the original methods.

        ``capture_installed`` is deliberately *not* cleared: it records that
        this accounting window was watched, so a completed window keeps its
        real verdict instead of degrading to ``UNKNOWN`` on teardown.
        """

        while self._patched:
            runtime_class, method_name, original = self._patched.pop()
            setattr(runtime_class, method_name, original)
        self.active = False

    def __enter__(self) -> "ProviderCapture":
        self.install()
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.remove()
        return False

    def coverage_evidence(self) -> dict[str, Any]:
        return {
            "instrumented": [
                f"{runtime_class.__module__}.{runtime_class.__name__}.{method}"
                for runtime_class, method, _ in self._patched
            ],
            "entry_methods": list(PROVIDER_ENTRY_METHODS),
            "excluded_non_provider_runtimes": [
                f"{module}.{name}" for module, name in NON_PROVIDER_RUNTIMES
            ],
            "capture_gaps": list(self.sink.accounting.capture_gaps),
            "evidence_level": self.evidence_level,
            "unattributed_lane": LANE_UNATTRIBUTED,
        }
