"""One authoritative location contract for Orchestrator-owned durable control state.

Durable control state (event journals, fingerprints, change-sets, the project
state manager snapshot, task reports) is written by the Orchestrator *about* a
project; it is not project content.  It therefore belongs under the configured
Orchestrator runtime root, not inside the project repository.

Identity and location are separate concerns:

    Project identity  ->  ControlStateLocation  ->  on-disk path

``ControlStateLocation`` carries the durable ``Project.id`` alongside the legacy
on-disk root, so producers/consumers can hand identity across the existing
``project_dir=`` boundary without any caller inferring identity from a path.

Relocation contract (this gate):

* **Write** goes to ``<runtime_root>/control/projects/<project_id>/<family>``
  whenever the location carries a resolved ``control_root``.
* **Read** prefers that location and falls back to the historical
  ``<legacy_root>/.agent/<family>`` when the new artifact does not exist.
* A location *without* a ``control_root`` is a legacy-scoped location and keeps
  the pre-relocation behaviour exactly.  ``control_root`` is only ever supplied
  by an identity-aware boundary (``project_control_state_location``), never
  inferred from a path and never resolved implicitly on a write.

No historical data is migrated, rewritten, or deleted by this contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional, Union

if False:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

#: Legacy control-state directory name inside a project root.
CONTROL_STATE_DIR_NAME = ".agent"

#: Runtime-root layout, recorded here so exactly one module owns the literal.
CONTROL_STATE_ROOT_DIR_NAME = "control"
CONTROL_STATE_PROJECTS_DIR_NAME = "projects"

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

    ``legacy_root`` is the historical on-disk directory that contains
    ``.agent``.  ``project_id`` is the durable ``Project.id`` and is *never*
    derived from ``legacy_root``; it is threaded in from a caller that already
    holds it.  ``control_root`` is the resolved
    ``<runtime_root>/control/projects/<project_id>`` directory; when it is set
    the location writes outside the project repository.

    The object is ``os.PathLike`` so it can be passed through the existing
    ``project_dir=`` parameters that only ever coerce with ``Path(...)`` /
    ``str(...)``.  ``__fspath__`` deliberately still yields ``legacy_root``:
    those parameters double as the project's on-disk location for callers that
    are not resolving control state.
    """

    legacy_root: Path
    project_id: Optional[int] = None
    control_root: Optional[Path] = None

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
        return replace(self, project_id=project_id)

    def with_legacy_root(self, legacy_root: Any) -> "ControlStateLocation":
        """Same identity and control root, a different historical read root."""
        return replace(self, legacy_root=_coerce_concrete_legacy_root(legacy_root))

    def with_control_root(self, control_root: Optional[Path]) -> "ControlStateLocation":
        if control_root is None:
            return self
        return replace(self, control_root=Path(control_root))


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


def _coerce_concrete_project_id(value: Any) -> int:
    """Reject a non-concrete Project.id before it can name a directory.

    ``MagicMock`` attributes stringify to a unique ``<MagicMock …>`` value, so
    an unconfigured mock ``project_id`` would otherwise become a real directory
    name under the runtime root.  Same fail-closed rule as
    ``_coerce_concrete_legacy_root``, applied to the identity half.
    """
    if value is None:
        raise ValueError("project_id is required to resolve control state by identity")
    if value.__class__.__module__ == "unittest.mock":
        raise TypeError("control-state project_id must be a concrete integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("control-state project_id must be a concrete integer") from exc


def coerce_control_state_location(
    value: ControlStateLocationLike,
    *,
    project_id: Optional[int] = None,
) -> ControlStateLocation:
    """Normalize any legacy ``project_dir``-shaped value into a location.

    An explicit ``project_id`` argument wins over one already carried by
    ``value`` only when ``value`` does not have one, so a caller can add
    identity to a legacy path without ever overwriting a threaded identity.

    Deliberately free of any database or settings access: this runs on every
    event append, and a location that has not been given a ``control_root`` by
    an identity-aware boundary stays legacy-scoped rather than triggering an
    implicit runtime-root lookup here.
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


def project_control_state_root(runtime_root: str | Path, project_id: int) -> Path:
    """A project's durable control-state root under the Orchestrator runtime root.

    Keyed purely by ``Project.id``, so a project that moves between machines or
    workspace roots keeps the same control-state identity and the same location
    relative to that machine's runtime root.
    """
    return (
        _coerce_concrete_legacy_root(runtime_root)
        / CONTROL_STATE_ROOT_DIR_NAME
        / CONTROL_STATE_PROJECTS_DIR_NAME
        / str(_coerce_concrete_project_id(project_id))
    )


def resolve_project_control_root(
    project_id: Optional[int], *, db: Optional["Session"] = None
) -> Path:
    """Resolve ``<runtime_root>/control/projects/<project_id>``.

    Raises rather than degrading to the legacy project-root location: the
    legacy tree is a read-compatibility source, never a write fallback.
    """
    project_id = _coerce_concrete_project_id(project_id)
    # Imported lazily: this module is a leaf that low-level writers import, and
    # system_settings pulls in the database/model layer.
    from app.services.workspace.system_settings import get_effective_runtime_root

    # A mock/stub database yields a mock setting value; that must fail closed
    # here rather than become a real ``MagicMock/...`` directory tree.
    return project_control_state_root(get_effective_runtime_root(db), project_id)


def project_control_state_location(
    legacy_root: ControlStateLocationLike,
    project_id: Optional[int],
    *,
    db: Optional["Session"] = None,
) -> ControlStateLocation:
    """The one identity-aware boundary that puts a location on the new root.

    Callers that hold a ``Project`` row use this instead of constructing a
    ``ControlStateLocation`` directly.  With no ``project_id`` the result is a
    legacy-scoped location, exactly as before relocation.
    """
    location = coerce_control_state_location(legacy_root, project_id=project_id)
    if location.project_id is None:
        return location
    return location.with_control_root(
        resolve_project_control_root(location.project_id, db=db)
    )


def control_state_root(
    value: ControlStateLocationLike,
    *,
    project_id: Optional[int] = None,
) -> Path:
    """Authoritative (write) control-state root for this location.

    ``<runtime_root>/control/projects/<id>`` once identity has been resolved by
    ``project_control_state_location``; the historical ``<legacy_root>/.agent``
    otherwise.
    """
    location = coerce_control_state_location(value, project_id=project_id)
    if location.control_root is not None:
        return location.control_root
    return location.legacy_root / CONTROL_STATE_DIR_NAME


def legacy_control_state_root(
    value: ControlStateLocationLike,
    *,
    project_id: Optional[int] = None,
) -> Path:
    """Historical ``<legacy_root>/.agent`` root, for compatibility reads only."""
    location = coerce_control_state_location(value, project_id=project_id)
    return location.legacy_root / CONTROL_STATE_DIR_NAME


def control_state_family_dir(
    value: ControlStateLocationLike,
    family: str,
    *,
    project_id: Optional[int] = None,
) -> Path:
    """Authoritative (write) directory for one control-state family."""
    return control_state_root(value, project_id=project_id) / family


def legacy_control_state_family_dir(
    value: ControlStateLocationLike,
    family: str,
    *,
    project_id: Optional[int] = None,
) -> Path:
    """Historical directory for one control-state family, for reads only."""
    return legacy_control_state_root(value, project_id=project_id) / family


def control_state_read_path(
    value: ControlStateLocationLike,
    *parts: str,
    project_id: Optional[int] = None,
) -> Path:
    """New-first, legacy-fallback path for one durable control-state artifact.

    Returns the authoritative path when it exists, otherwise the historical
    project-root path.  When the location is legacy-scoped both are identical,
    so pre-relocation behaviour is unchanged.
    """
    location = coerce_control_state_location(value, project_id=project_id)
    current = control_state_root(location).joinpath(*parts)
    if location.control_root is None or current.exists():
        return current
    legacy = legacy_control_state_root(location).joinpath(*parts)
    return legacy if legacy.exists() else current


def control_state_location_for(
    value: ControlStateLocationLike, state: Any
) -> ControlStateLocation:
    """Coerce ``value`` while inheriting identity and root from ``state``.

    For the call sites that were given a bare ``orchestration_state.project_dir``
    as the legacy root: the caller's root is kept, but the durable identity and
    the already-resolved runtime-root location come from the orchestration state
    instead of being re-resolved (or silently lost) here.
    """
    location = coerce_control_state_location(
        value, project_id=getattr(state, "project_id", None)
    )
    if location.control_root is not None:
        return location
    state_location = getattr(state, "control_state_location", None)
    if isinstance(state_location, ControlStateLocation):
        return location.with_control_root(state_location.control_root)
    return location


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
