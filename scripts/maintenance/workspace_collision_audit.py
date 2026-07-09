#!/usr/bin/env python3
"""Phase 23B: read-only Project workspace collision audit.

Resolves every non-deleted Project's workspace root (the same resolution
used by the live dispatch path, `resolve_project_root`) and reports which
resolved paths are shared by more than one project. Does not modify
anything -- confirms known collisions (e.g. Project rows 2-9 sharing one
physical directory per Phase 22C-0) without remediating them; remediation
is deferred to a later phase
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from app.database import SessionLocal
from app.models import Project
from app.services.workspace.workspace_paths import resolve_project_root


@dataclass
class CollisionGroup:
    resolved_path: str
    project_ids: List[int] = field(default_factory=list)
    project_names: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "resolved_path": self.resolved_path,
            "project_ids": self.project_ids,
            "project_names": self.project_names,
        }


@dataclass
class AuditReport:
    total_projects: int
    resolved_count: int
    unresolved: List[Dict[str, object]]
    collisions: List[CollisionGroup]

    def to_dict(self) -> dict:
        return {
            "total_projects": self.total_projects,
            "resolved_count": self.resolved_count,
            "unresolved": self.unresolved,
            "collision_group_count": len(self.collisions),
            "collisions": [group.to_dict() for group in self.collisions],
        }


def run_audit(db) -> AuditReport:
    projects = (
        db.query(Project)
        .filter(Project.deleted_at.is_(None))
        .order_by(Project.id.asc())
        .all()
    )

    by_resolved_path: Dict[str, CollisionGroup] = {}
    unresolved: List[Dict[str, object]] = []
    resolved_count = 0

    for project in projects:
        try:
            resolved = str(resolve_project_root(project, db))
        except Exception as exc:  # noqa: BLE001 - audit must not crash on one bad row
            unresolved.append(
                {
                    "project_id": project.id,
                    "project_name": project.name,
                    "error": str(exc),
                }
            )
            continue

        resolved_count += 1
        group = by_resolved_path.get(resolved)
        if group is None:
            group = CollisionGroup(resolved_path=resolved)
            by_resolved_path[resolved] = group
        group.project_ids.append(project.id)
        group.project_names.append(project.name)

    collisions = [
        group for group in by_resolved_path.values() if len(group.project_ids) > 1
    ]
    collisions.sort(key=lambda group: group.resolved_path)

    return AuditReport(
        total_projects=len(projects),
        resolved_count=resolved_count,
        unresolved=unresolved,
        collisions=collisions,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        report = run_audit(db)
    finally:
        db.close()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0

    print(f"Projects scanned: {report.total_projects}")
    print(f"Resolved: {report.resolved_count}")
    if report.unresolved:
        print(f"Unresolved (errors): {len(report.unresolved)}")
        for row in report.unresolved:
            print(
                f"  - project_id={row['project_id']} name={row['project_name']!r}: {row['error']}"
            )

    if not report.collisions:
        print("No workspace collisions detected.")
        return 0

    print(f"Collision groups: {len(report.collisions)}")
    for group in report.collisions:
        print(f"  {group.resolved_path}")
        for project_id, project_name in zip(group.project_ids, group.project_names):
            print(f"    - project_id={project_id} name={project_name!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
