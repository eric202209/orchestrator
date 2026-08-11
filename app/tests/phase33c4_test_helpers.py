"""Small explicit APA fixtures for legacy provider-free executor tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.services.orchestration.validation.path_authority import (
    AcceptedPathAuthority,
    GrantClass,
    GrantProvenance,
    PathGrant,
    declare,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_plan_identity,
)


def executor_test_authority(
    project_dir: Path,
    ops: list[dict[str, Any]],
    *,
    plan: Any = None,
    existing_paths: list[str] | tuple[str, ...] = (),
) -> AcceptedPathAuthority:
    """Construct test-only grants for an explicit legacy executor fixture.

    Production code never infers these grants.  Existing executor unit tests
    exercise the file-operation mechanics directly, so this fixture supplies
    the authority those tests previously omitted.
    """

    root = Path(project_dir).resolve()
    existing_path_set = set(existing_paths)
    grants: dict[str, PathGrant] = {}
    operations = list(ops)
    for existing_path in existing_paths:
        operations.append(
            {"op": "replace_in_file", "path": existing_path, "old": "", "new": ""}
        )
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        op_name = str(operation.get("op") or "")
        if op_name == "mkdir":
            continue
        try:
            path = declare(operation.get("path"))
        except Exception:
            continue
        target = root / path.value
        if path.value in existing_path_set or op_name == "replace_in_file":
            grant_class = GrantClass.EXISTING_MUTABLE
        elif op_name == "delete_file":
            grant_class = GrantClass.DELETION_AUTHORIZED
        elif target.exists():
            grant_class = GrantClass.EXISTING_MUTABLE
        else:
            grant_class = GrantClass.CREATION_AUTHORIZED
        baseline = None
        if grant_class in {
            GrantClass.EXISTING_MUTABLE,
            GrantClass.DELETION_AUTHORIZED,
        }:
            try:
                baseline = hashlib.sha256(target.read_bytes()).hexdigest()
            except OSError:
                baseline = "0" * 64
        grants[path.value] = PathGrant(
            path=path,
            grant_class=grant_class,
            provenance=GrantProvenance.ACCEPTED_PLAN,
            baseline_content_hash=baseline,
        )
    return AcceptedPathAuthority.create(
        accepted_plan_identity=accepted_plan_identity(
            plan if plan is not None else [{"step_number": 1, "ops": ops}]
        ),
        workspace_identity=str(root),
        maximum_scope_digest="b" * 64,
        grants=tuple(grants.values()),
    )
