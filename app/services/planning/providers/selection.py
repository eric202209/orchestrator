"""Configured Planning Provider selection at the composition seam."""

from __future__ import annotations

from typing import Any, Callable

from app.config import settings
from app.services.planning.providers.base import PlanningProvider
from app.services.planning.providers.openclaw import OpenClawPlanningProvider


class UnsupportedPlanningProviderError(ValueError):
    """Raised when no Planning Provider adapter owns the configured name."""


_PROVIDER_FACTORIES: dict[str, Callable[[Any], PlanningProvider]] = {
    "openclaw": OpenClawPlanningProvider,
}


def configured_planning_provider_name() -> str:
    return str(getattr(settings, "PLANNING_PROVIDER", "openclaw") or "").strip().lower()


def create_planning_provider(
    db: Any, *, provider_name: str | None = None
) -> PlanningProvider:
    """Create the sole configured adapter without exposing it to stages."""

    name = str(provider_name or configured_planning_provider_name()).strip().lower()
    factory = _PROVIDER_FACTORIES.get(name)
    if factory is None:
        supported = ", ".join(sorted(_PROVIDER_FACTORIES))
        raise UnsupportedPlanningProviderError(
            f"Unsupported planning provider: {name or '<empty>'}; supported: {supported}"
        )
    return factory(db)


def list_planning_provider_names() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDER_FACTORIES))


__all__ = [
    "UnsupportedPlanningProviderError",
    "configured_planning_provider_name",
    "create_planning_provider",
    "list_planning_provider_names",
]
