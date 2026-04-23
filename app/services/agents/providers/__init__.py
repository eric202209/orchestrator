"""Provider-specific runtime adapter factories."""

from __future__ import annotations

from typing import Callable, Optional

from .openai_adapter import create_runtime as create_openai_runtime
from .openclaw_adapter import create_runtime as create_openclaw_runtime
from .remote_openclaw_adapter import create_runtime as create_remote_openclaw_runtime

RuntimeFactory = Callable[..., object]

_RUNTIME_FACTORIES: dict[str, RuntimeFactory] = {
    "local_openclaw": create_openclaw_runtime,
    "remote_openclaw_gateway": create_remote_openclaw_runtime,
    "openai_responses_api": create_openai_runtime,
}


def get_runtime_factory(backend_name: str) -> Optional[RuntimeFactory]:
    """Return the registered provider adapter factory for a backend name."""

    return _RUNTIME_FACTORIES.get((backend_name or "").strip())


__all__ = [
    "create_openai_runtime",
    "create_openclaw_runtime",
    "create_remote_openclaw_runtime",
    "get_runtime_factory",
]
