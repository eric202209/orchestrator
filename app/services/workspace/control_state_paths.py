"""One authoritative location contract for Orchestrator-owned durable control state.

Durable control state (event journals, fingerprints, change-sets, the project
state manager snapshot, task reports) is written by the Orchestrator *about* a
project; it is not project content.  Today it still lives inside the project
repository under ``<root>/.agent/…``.

This module exists so that identity and location stop being the same thing:

    Project identity  ->  ControlStateLocation  ->  on-disk path

``ControlStateLocation`` carries the durable ``Project.id`` alongside the legacy
on-disk root, so producers/consumers can hand identity across the existing
``project_dir=`` boundary without any caller inferring identity from a path.

This gate deliberately keeps *reads and writes byte- and path-compatible* with
the legacy layout: every resolver below still returns
``<legacy_root>/.agent/<family>``.  ``future_control_state_project_root`` records
the intended post-relocation target (``<runtime_root>/control/projects/<id>``)
and is intentionally not used for any I/O yet.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

#: Legacy control-state directory name inside a project root.
CONTROL_STATE_DIR_NAME = ".agent"

#: Future (relocation-gate) layout, recorded here so exactly one module owns it.
FUTURE_CONTROL_STATE_DIR_NAME = "control"
FUTURE_CONTROL_STATE_PROJECTS_DIR_NAME = "projects"

# Proven Orchestrator-owned durable control-state families.
FAMILY_EVENTS = "events"
FAMILY_FINGERPRINTS = "fingerprints"
FAMILY_CHANGE_SETS = "change-sets"
FAMILY_TASK_REPORTS = "task-reports"
FAMILY_ENGINEERING_CONTEXT = "engineering-context"

#: state_manager.json is a single file directly under the control-state root.
STATE_MANAGER_FILENAME = "state_manager.json"


@dataclass(frozen=True)
class ControlStateLocation(os.PathLike):
    """A control-state root plus the durable Project identity that owns it.

    ``legacy_root`` is the current on-disk directory that contains ``.agent``.
    ``project_id`` is the durable ``Project.id`` and is *never* derived from
    ``legacy_root``; it is threaded in from a caller that already holds it.

    The object is ``os.PathLike`` so it can be passed through the existing
    ``project_dir=`` parameters that only ever coerce with ``Path(...)`` /
    ``str(...)``.
    """

    legacy_root: Path
    project_id: Optional[int] = None

    def __fspath__(self) -> str:
        return str(self.legacy_root)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.legacy_root)

    @property
    def identity(self) -> Optional[str]:
        """Durable, location-independent identity of this control state."""
        if self.project_id is None:
            return None
        return f"project:{self.project_id}"

    def with_project_id(self, project_id: Optional[int]) -> "ControlStateLocation":
        if project_id is None or project_id == self.project_id:
            return self
        return ControlStateLocation(legacy_root=self.legacy_root, project_id=project_id)


ControlStateLocationLike = Union[ControlStateLocation, str, Path, Any]


def _coerce_concrete_legacy_root(value: Any) -> Path:
    """Coerce a real path value without materializing dynamic mock paths.

    ``MagicMock`` implements ``__fspath__`` and therefore looks path-like to
    ``os.fspath``; its generated value is a relative ``MagicMock/...`` path.
    That is a test double, not a valid durable workspace root.  Reject it at
    the single control-state boundary so a best-effort producer fails closed
    instead of writing project control state into the caller's current cwd.
    """
    if value.__class__.__module__ == "unittest.mock":
        raise TypeError("control-state root must be a concrete path")
    try:
        return Path(os.fspath(value))
    except TypeError as exc:
        raise TypeError("control-state root must be a concrete path") from exc


def coerce_control_state_location(
    value: ControlStateLocationLike,
    *,
    project_id: Optional[int] = None,
) -> ControlStateLocation:
    """Normalize any legacy ``project_dir``-shaped value into a location.

    An explicit ``project_id`` argument wins over one already carried by
    ``value`` only when ``value`` does not have one, so a caller can add
    identity to a legacy path without ever overwriting a threaded identity.
    """
    if isinstance(value, ControlStateLocation):
        if value.project_id is None and project_id is not None:
            return value.with_project_id(project_id)
        return value
    return ControlStateLocation(
        legacy_root=_coerce_concrete_legacy_root(value), project_id=project_id
    )


def control_state_identity(
    value: ControlStateLocationLike,
    *,
    project_id: Optional[int] = None,
) -> Optional[str]:
    """Durable identity for this control state, or ``None`` when unthreaded."""
    return coerce_control_state_location(value, project_id=project_id).identity


def control_state_root(
    value: ControlStateLocationLike,
    *,
    project_id: Optional[int] = None,
) -> Path:
    """Current (legacy) control-state root: ``<legacy_root>/.agent``.

    The relocation gate switches this one function to the runtime root; every
    family helper below is defined in terms of it.
    """
    location = coerce_control_state_location(value, project_id=project_id)
    return location.legacy_root / CONTROL_STATE_DIR_NAME


def control_state_family_dir(
    value: ControlStateLocationLike,
    family: str,
    *,
    project_id: Optional[int] = None,
) -> Path:
    """Current (legacy) directory for one control-state family."""
    return control_state_root(value, project_id=project_id) / family


def control_state_of(state: Any) -> ControlStateLocation:
    """Control-state location for an orchestration state object.

    Total by design: several call sites receive duck-typed stand-ins for
    ``OrchestrationState`` that expose ``project_dir`` but not ``project_id``.
    Those callers must still resolve a location rather than raise inside the
    best-effort ``try/except`` blocks that surround event emission, which would
    drop the event silently.
    """
    location = getattr(state, "control_state_location", None)
    if isinstance(location, ControlStateLocation):
        return location
    # Deliberately no default for a missing project_dir: falling back to "." (the
    # process CWD) would write control state into whatever directory the worker
    # happens to run in. A state without a project_dir raises here exactly as
    # ``Path(None)`` did before, inside the caller's best-effort try/except.
    # Routed through the same fail-closed coercion as coerce_control_state_location
    # so a dynamic mock ``project_dir`` cannot slip past this second entry point.
    return ControlStateLocation(
        legacy_root=_coerce_concrete_legacy_root(getattr(state, "project_dir", None)),
        project_id=getattr(state, "project_id", None),
    )


def future_control_state_project_root(
    runtime_root: str | Path, project_id: int
) -> Path:
    """Post-relocation target for a project's durable control state.

    Recorded for the relocation gate only; no read or write path uses it in
    this gate.  It is keyed purely by ``Project.id``, so a project that moves
    between machines or workspace roots keeps the same control-state location.
    """
    if project_id is None:
        raise ValueError("project_id is required to resolve control state by identity")
    return (
        Path(runtime_root)
        / FUTURE_CONTROL_STATE_DIR_NAME
        / FUTURE_CONTROL_STATE_PROJECTS_DIR_NAME
        / str(project_id)
    )
