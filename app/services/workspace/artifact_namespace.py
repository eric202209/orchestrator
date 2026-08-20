"""Provider-neutral names for existing workspace artifact paths."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from app.services.workspace.control_state_paths import (
    FAMILY_EVENTS,
    FAMILY_TASK_REPORTS,
    control_state_family_dir,
)
from app.services.workspace.workspace_paths import (
    AUTO_SNAPSHOT_ROOT,
    PROMOTED_WORKSPACE_ARCHIVE_ROOT,
    REJECTED_CHANGE_ARCHIVE_ROOT,
    REQUESTED_CHANGES_ARCHIVE_ROOT,
    RETAINED_WORKSPACE_ARCHIVE_ROOT,
    TASK_REPORT_ROOT,
)

COMPATIBILITY_NAMESPACE = "openclaw"
CANONICAL_OWNER = "orchestrator"
EVENT_JOURNAL_ROOT = ".agent/events"


@dataclass(frozen=True)
class ArtifactNamespace:
    """Current artifact paths exposed under backend-neutral field names."""

    compatibility_namespace: str = COMPATIBILITY_NAMESPACE
    canonical_owner: str = CANONICAL_OWNER
    event_journal_root: str = EVENT_JOURNAL_ROOT
    auto_snapshot_root: str = AUTO_SNAPSHOT_ROOT
    task_report_root: str = TASK_REPORT_ROOT
    promoted_workspace_archive_root: str = PROMOTED_WORKSPACE_ARCHIVE_ROOT
    rejected_change_archive_root: str = REJECTED_CHANGE_ARCHIVE_ROOT
    retained_workspace_archive_root: str = RETAINED_WORKSPACE_ARCHIVE_ROOT
    requested_changes_archive_root: str = REQUESTED_CHANGES_ARCHIVE_ROOT

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


DEFAULT_ARTIFACT_NAMESPACE = ArtifactNamespace()


def artifact_namespace() -> ArtifactNamespace:
    return DEFAULT_ARTIFACT_NAMESPACE


def artifact_namespace_payload() -> dict[str, str]:
    return DEFAULT_ARTIFACT_NAMESPACE.as_dict()


def event_journal_dir(
    project_root: str | Path, *, project_id: int | None = None
) -> Path:
    return control_state_family_dir(project_root, FAMILY_EVENTS, project_id=project_id)


def task_report_dir(project_root: str | Path, *, project_id: int | None = None) -> Path:
    return control_state_family_dir(
        project_root, FAMILY_TASK_REPORTS, project_id=project_id
    )
