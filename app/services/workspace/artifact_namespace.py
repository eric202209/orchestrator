"""Provider-neutral names for existing workspace artifact paths."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

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
EVENT_JOURNAL_ROOT = ".openclaw/events"


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


def event_journal_dir(project_root: str | Path) -> Path:
    return Path(project_root) / EVENT_JOURNAL_ROOT


def task_report_dir(project_root: str | Path) -> Path:
    return Path(project_root) / TASK_REPORT_ROOT
