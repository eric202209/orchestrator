"""Durable run <-> ProductRoot <-> provider attribution (§17).

Every future research run must be attributable end to end.  A provider
invocation or workspace mutation that can not be attributed to a run is an
evidence-integrity problem, and is reported as one rather than dropped.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ATTRIBUTION_SCHEMA_VERSION = "rr1-attribution/1.0"

INTEGRITY_OK = "ATTRIBUTED"
INTEGRITY_UNATTRIBUTED_PROVIDER_CALL = "UNATTRIBUTED_PROVIDER_CALL"
INTEGRITY_UNATTRIBUTED_WORKSPACE_MUTATION = "UNATTRIBUTED_WORKSPACE_MUTATION"
INTEGRITY_PRODUCTROOT_UNREADABLE = "PRODUCTROOT_UNREADABLE"


@dataclass(frozen=True)
class GitIdentity:
    path: str
    head: str | None
    tree: str | None
    branch: str | None
    status_clean: bool | None
    error: str | None = None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "head": self.head,
            "tree": self.tree,
            "branch": self.branch,
            "status_clean": self.status_clean,
            "error": self.error,
        }


def git_identity(root: str | Path) -> GitIdentity:
    """Read a ProductRoot's git identity without mutating it."""

    path = Path(root)

    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(path), *args],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    if not path.exists():
        return GitIdentity(str(path), None, None, None, None, "path_missing")
    head = run("rev-parse", "HEAD")
    if head is None:
        return GitIdentity(str(path), None, None, None, None, "git_unreadable")
    status = run("status", "--short", "--untracked-files=all")
    return GitIdentity(
        path=str(path),
        head=head,
        tree=run("rev-parse", "HEAD^{tree}"),
        branch=run("branch", "--show-current") or None,
        status_clean=(status == "") if status is not None else None,
    )


@dataclass
class RunAttribution:
    """Complete attribution record for one research run (§17)."""

    research_run_id: str
    correlation_id: str
    project_id: int | None = None
    session_id: int | None = None
    task_id: int | None = None
    task_execution_ids: list[int] = field(default_factory=list)
    productroot_path: str | None = None
    workspace_path: str | None = None
    baseline_git: GitIdentity | None = None
    final_git: GitIdentity | None = None
    provider_call_ids: list[str] = field(default_factory=list)
    lifecycle_evidence: dict[str, Any] = field(default_factory=dict)
    physical_evidence: dict[str, Any] = field(default_factory=dict)
    integrity_findings: list[str] = field(default_factory=list)
    opened_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    closed_at: str | None = None

    def open_baseline(self) -> None:
        if self.productroot_path:
            self.baseline_git = git_identity(self.productroot_path)
            if self.baseline_git.error:
                self.integrity_findings.append(
                    f"{INTEGRITY_PRODUCTROOT_UNREADABLE}:baseline:"
                    f"{self.baseline_git.error}"
                )

    def close_final(self) -> None:
        self.closed_at = datetime.now(UTC).isoformat()
        if self.productroot_path:
            self.final_git = git_identity(self.productroot_path)
            if self.final_git.error:
                self.integrity_findings.append(
                    f"{INTEGRITY_PRODUCTROOT_UNREADABLE}:final:{self.final_git.error}"
                )

    @property
    def productroot_mutated(self) -> bool | None:
        """Whether the ProductRoot changed, or ``None`` when unreadable."""

        if self.baseline_git is None or self.final_git is None:
            return None
        if self.baseline_git.error or self.final_git.error:
            return None
        if self.baseline_git.head != self.final_git.head:
            return True
        if self.baseline_git.tree != self.final_git.tree:
            return True
        if self.baseline_git.status_clean != self.final_git.status_clean:
            return True
        return False

    def attribute_provider_calls(self, accounting: Any) -> None:
        """Bind captured provider calls to this run and flag orphans."""

        for record in getattr(accounting, "records", []):
            if record.nested:
                continue
            self.provider_call_ids.append(record.call_id)
            if record.correlation_id != self.correlation_id:
                self.integrity_findings.append(
                    f"{INTEGRITY_UNATTRIBUTED_PROVIDER_CALL}:{record.call_id}:"
                    f"correlation={record.correlation_id!r}"
                )

    def note_workspace_mutation(self, path: str, *, attributed: bool) -> None:
        if not attributed:
            self.integrity_findings.append(
                f"{INTEGRITY_UNATTRIBUTED_WORKSPACE_MUTATION}:{path}"
            )

    @property
    def integrity_status(self) -> str:
        return (
            INTEGRITY_OK
            if not self.integrity_findings
            else "EVIDENCE_INTEGRITY_PROBLEM"
        )

    def as_evidence(self) -> dict[str, Any]:
        return {
            "schema_version": ATTRIBUTION_SCHEMA_VERSION,
            "research_run_id": self.research_run_id,
            "correlation_id": self.correlation_id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "task_execution_ids": list(self.task_execution_ids),
            "productroot_path": self.productroot_path,
            "workspace_path": self.workspace_path,
            "productroot_baseline": (
                self.baseline_git.as_evidence() if self.baseline_git else None
            ),
            "productroot_final": (
                self.final_git.as_evidence() if self.final_git else None
            ),
            "productroot_mutated": self.productroot_mutated,
            "provider_call_ids": list(self.provider_call_ids),
            "lifecycle_evidence": dict(self.lifecycle_evidence),
            "physical_evidence": dict(self.physical_evidence),
            "integrity_status": self.integrity_status,
            "integrity_findings": list(self.integrity_findings),
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
        }
