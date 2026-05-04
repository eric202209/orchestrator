"""ObservabilityService — optional, fail-open observability abstraction.

Langfuse (or any tracing backend) is never required for core orchestration.
All calls are non-blocking and safely wrapped so that a misconfigured or
unavailable observability backend cannot affect session execution.

Internal source of truth remains in SQLite logs, KnowledgeUsageLog,
SessionLog, and failure records.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager, nullcontext
from typing import Any, Iterator, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Minimal inline types so callers don't need to import the Langfuse SDK
# ---------------------------------------------------------------------------


class _ObservationHandle:
    """Lightweight handle representing a single observation span.

    When the backend is disabled or unavailable this degrades gracefully
    (all methods become no-ops).
    """

    def __init__(self, raw: Any = None) -> None:
        self._raw = raw

    # -- update helpers -----------------------------------------------------

    def update(
        self,
        *,
        output: Any = None,
        metadata: Optional[dict[str, Any]] = None,
        level: Optional[str] = None,
        status_message: Optional[str] = None,
        usage_details: Optional[dict[str, int]] = None,
    ) -> None:
        if self._raw is None:
            return
        payload: dict[str, Any] = {}
        if output is not None:
            payload["output"] = output
        if metadata is not None:
            payload["metadata"] = metadata
        if level is not None:
            payload["level"] = level
        if status_message is not None:
            payload["status_message"] = status_message
        if usage_details is not None:
            payload["usage_details"] = usage_details
        try:
            self._raw.update(**payload)
        except Exception as exc:
            logger.debug("Observation update failed: %s", exc)


# ---------------------------------------------------------------------------
# ObservabilityService — public API
# ---------------------------------------------------------------------------


class ObservabilityService:
    """Thin, fail-open wrapper around Langfuse (or no-op when disabled).

    Invariants:
    * Never raises exceptions into caller code.
    * Never blocks core orchestration paths.
    * Context managers always work (yield None when backend is off).
    """

    def __init__(self) -> None:
        self._client: Any = None
        self._initialized = False

    # -- lifecycle -----------------------------------------------------------

    def _ensure_client(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        if not self.is_enabled():
            return
        try:
            from langfuse import Langfuse  # type: ignore[import-not-found,no-redef]

            self._client = Langfuse(
                public_key=str(settings.LANGFUSE_PUBLIC_KEY or "").strip(),
                secret_key=str(settings.LANGFUSE_SECRET_KEY or "").strip(),
                base_url=str(settings.LANGFUSE_BASE_URL or "").strip() or None,
                tracing_enabled=True,
                environment=str(settings.LANGFUSE_ENVIRONMENT or "").strip() or None,
                release=settings.VERSION,
            )
        except ImportError:
            logger.warning(
                "Langfuse tracing enabled but SDK is not installed. "
                "Observability will operate in no-op mode."
            )
            self._client = None
        except Exception as exc:
            logger.warning(
                "Langfuse client initialization failed; " "tracing disabled: %s", exc
            )
            self._client = None

    @staticmethod
    def is_enabled() -> bool:
        """Return True when tracing is enabled and minimally configured."""
        return bool(
            settings.ORCHESTRATOR_LANGFUSE_ENABLED
            and str(settings.LANGFUSE_PUBLIC_KEY or "").strip()
            and str(settings.LANGFUSE_SECRET_KEY or "").strip()
        )

    # -- context-manager API -------------------------------------------------

    @contextmanager
    def observation(
        self,
        *,
        name: str,
        as_type: str = "span",
        input: Any = None,
        output: Any = None,
        metadata: Optional[dict[str, Any]] = None,
        status_message: Optional[str] = None,
        model: Optional[str] = None,
        usage_details: Optional[dict[str, int]] = None,
    ) -> Iterator[Optional[_ObservationHandle]]:
        """Start an observation when configured, otherwise yield None."""
        self._ensure_client()
        if self._client is None:
            with nullcontext(None) as _ctx:
                yield None
            return

        try:
            raw_cm = self._client.start_as_current_observation(
                name=name,
                as_type=as_type,
                input=input,
                output=output,
                metadata=metadata,
                status_message=status_message,
                model=model,
                usage_details=usage_details,
                version=settings.VERSION,
            )
        except Exception as exc:
            logger.warning("Observation start failed for %s: %s", name, exc)
            with nullcontext(None) as _ctx:
                yield None
            return

        with raw_cm as raw_observation:
            yield _ObservationHandle(raw_observation)

    # -- flush ----------------------------------------------------------------

    def flush(self) -> None:
        """Flush background trace buffers without raising."""
        self._ensure_client()
        if self._client is None:
            return
        try:
            self._client.flush()
        except Exception as exc:
            logger.debug("Observability flush failed: %s", exc)

    # -- payload helpers ----------------------------------------------------

    _MAX_PREVIEW_CHARS = 600

    def build_text_trace_payload(
        self,
        value: Any,
        *,
        max_preview_chars: int = _MAX_PREVIEW_CHARS,
    ) -> Optional[dict[str, Any]]:
        """Build a compact, low-risk payload for input/output fields."""
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        preview = text[:max_preview_chars]
        if len(text) > max_preview_chars:
            preview = preview.rstrip() + "..."
        return {
            "preview": preview,
            "chars": len(text),
            "lines": text.count("\n") + 1,
        }


# ---------------------------------------------------------------------------
# Module-level singleton (backwards-compatible access pattern)
# ---------------------------------------------------------------------------

_default_service = ObservabilityService()

# Public API — drop-in replacements for existing import patterns.
is_tracing_enabled = _default_service.is_enabled
build_text_trace_payload = _default_service.build_text_trace_payload
start_observation = _default_service.observation
flush = _default_service.flush


def reset_for_tests() -> None:
    """Clear internal state so tests can change settings safely."""
    _default_service._client = None
    _default_service._initialized = False


# ---------------------------------------------------------------------------
# Aliases — keep existing import paths working without modification
# ---------------------------------------------------------------------------

langfuse_tracing_enabled = is_tracing_enabled
start_langfuse_observation = start_observation
flush_langfuse = flush
reset_langfuse_client_for_tests = reset_for_tests


def update_langfuse_observation(
    observation: Any,
    *,
    output: Any = None,
    metadata: Optional[dict[str, Any]] = None,
    level: Optional[str] = None,
    status_message: Optional[str] = None,
    usage_details: Optional[dict[str, int]] = None,
) -> None:
    """Best-effort update for a Langfuse observation."""
    if isinstance(observation, _ObservationHandle):
        observation.update(
            output=output,
            metadata=metadata,
            level=level,
            status_message=status_message,
            usage_details=usage_details,
        )
    elif observation is not None:
        # Legacy raw observation from the old code path
        payload: dict[str, Any] = {}
        if output is not None:
            payload["output"] = output
        if metadata is not None:
            payload["metadata"] = metadata
        if level is not None:
            payload["level"] = level
        if status_message is not None:
            payload["status_message"] = status_message
        if usage_details is not None:
            payload["usage_details"] = usage_details
        try:
            observation.update(**payload)
        except Exception as exc:
            logger.debug("Langfuse observation update failed: %s", exc)
