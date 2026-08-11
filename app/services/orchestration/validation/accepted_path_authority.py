"""Accepted Path Authority construction at Plan acceptance (Phase 33C-3).

The single architectural rule this module implements:

.. code-block:: text

    task text and source grounding define the MAXIMUM scope
    the Plan REQUESTS a concrete scope
    Plan Validator GRANTS it, deterministically, only after acceptance
    AcceptedPathAuthority freezes the grant

Nothing here derives authority from a Change Set, from Execution output, from
candidate paths, or from any observation.  Those all happen later and must stay
incapable of granting authority, so ``execution_observed`` and
``change_set_observed`` are not representable at all (see
:class:`~.path_authority.GrantProvenance`).

This module owns the *construction* of the authority; the value object, its
identity, and its serialization live in :mod:`.path_authority` and are not
duplicated here.  Phase 33C-3 adds no runtime consumer: the record is written
into the accepted plan verdict and nothing reads it to make a decision.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_EXISTING,
    SOURCE_STATUS_NEW,
)

from .path_authority import (
    AcceptedPathAuthority,
    GrantClass,
    GrantProvenance,
    PathDeclarationError,
    PathGrant,
    PathGrantError,
    declare,
)

# Statuses at which a Plan is authoritative enough to enter Execution.  This
# mirrors ``CandidateValidationResult.accepted`` and the ``PlanAccepted`` branch
# of ``ValidatorService.validate_plan``; a plan that cannot enter Execution must
# never mint an authority.
ACCEPTED_PLAN_STATUSES = frozenset({"accepted", "warning"})

MAXIMUM_SCOPE_SCHEMA_VERSION = "maximum-scope/1"

# The structured file operations through which an accepted Plan requests
# mutation of an existing source-grounded path.  ``delete_file`` is deliberately
# absent: Plan validation grants no deletion authority today (see
# ``build_accepted_path_authority``).  ``mkdir`` is absent because grants are
# file-granular and there are no directory grants.
PLAN_MUTATION_OPS = frozenset(
    {"write_file", "append_file", "replace_in_file", "create_file"}
)


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def plan_identity_text(plan: Any) -> str:
    """Canonical in-memory identity text for an accepted Plan.

    This is the *single* plan-identity authority in the system.  It was
    extracted verbatim from ``completion_coordinator._completion_plan_identity``,
    which now delegates here, so there is exactly one canonical serialization
    rather than two competing notions of "the same plan".
    """

    return json.dumps(plan, sort_keys=True, separators=(",", ":"))


def accepted_plan_identity(plan: Any) -> str:
    """Digest of :func:`plan_identity_text`.

    ``AcceptedPathAuthority`` bounds identity fields to 1024 characters, and the
    canonical plan text is routinely far longer, so the authority binds the
    digest of that same canonical text.  It is the same equivalence class, not a
    second notion of plan identity: two plans share this digest exactly when
    they share :func:`plan_identity_text`.
    """

    try:
        text = plan_identity_text(plan)
    except (TypeError, ValueError) as exc:
        raise PathGrantError(
            "accepted_plan_identity_invalid",
            f"plan is not canonically serializable: {exc}",
        ) from exc
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalized_plan_path(value: Any) -> str:
    """Normalize a plan-declared path exactly as existing plan validation does."""

    return str(value or "").strip().replace("\\", "/").lstrip("./")


def plan_mutation_paths(plan: Any) -> set[str]:
    """Paths the Plan requests a mutating structured file operation on."""

    paths: set[str] = set()
    for step in plan or []:
        if not isinstance(step, Mapping):
            continue
        for operation in step.get("ops") or []:
            if not isinstance(operation, Mapping):
                continue
            if str(operation.get("op") or "").strip() not in PLAN_MUTATION_OPS:
                continue
            path = _normalized_plan_path(operation.get("path"))
            if path:
                paths.add(path)
    return paths


def _source_grounding_scope(source_materialization: Any) -> list[dict[str, Any]]:
    entries = [
        {
            "path": str(getattr(item, "relative_path", "") or ""),
            "status": str(getattr(item, "status", "") or ""),
            "creation_authorized": bool(getattr(item, "creation_authorized", False)),
            "content_hash": getattr(item, "content_hash", None) or None,
        }
        for item in getattr(source_materialization, "files", ()) or ()
    ]
    entries.sort(key=lambda entry: (entry["path"], entry["status"]))
    return entries


def maximum_scope_digest(
    *,
    task_explicit_scope_paths: Iterable[Any],
    source_materialization: Any,
) -> str:
    """Deterministic digest of the maximum scope immediately before acceptance.

    Included: the operator's explicit task scope paths, and one record per
    source-grounding fact carrying its path, status, creation eligibility, and
    source-grounding content hash.  Deliberately excluded: timestamps, inode and
    device identity, ``version_identity`` (which is ``dev:ino:size:mtime_ns`` and
    therefore not stable across workspace re-hydration), object reprs, and any
    Python set or dict ordering — the payload is sorted and canonically
    serialized before hashing.

    The digest is audit evidence about the bound.  It is deliberately not part
    of ``authority_identity``, which binds only what was actually granted.
    """

    payload = {
        "schema_version": MAXIMUM_SCOPE_SCHEMA_VERSION,
        "task_explicit_scope_paths": sorted(
            {str(path) for path in (task_explicit_scope_paths or ()) if str(path)}
        ),
        "source_grounding": _source_grounding_scope(source_materialization),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _provenance_for(
    relative_path: str,
    grant_class: GrantClass,
    task_explicit_scope_paths: set[str],
) -> GrantProvenance:
    """Answer "who is the immediate semantic source of this grant?".

    Precedence, documented and pinned by test:

    1. the operator named the path in the task's explicit hard scope;
    2. the accepted Plan requested this concrete operation and the validator
       granted it within the maximum;
    3. deterministic source materialization alone established the path.

    A single grant is never encoded with a compound value; the
    existing-versus-creation distinction is carried by the grant class and is
    not duplicated in provenance.
    """

    if relative_path in task_explicit_scope_paths:
        return GrantProvenance.TASK_EXPLICIT_SCOPE
    if grant_class in {GrantClass.EXISTING_MUTABLE, GrantClass.CREATION_AUTHORIZED}:
        return GrantProvenance.ACCEPTED_PLAN
    return GrantProvenance.SOURCE_GROUNDING


def build_accepted_path_authority(
    *,
    plan: Any,
    source_materialization: Any,
    task_explicit_scope_paths: Iterable[Any] = (),
    creation_requested_paths: Iterable[Any] = (),
) -> tuple[AcceptedPathAuthority, tuple[str, ...]]:
    """Freeze the grant an accepted Plan carries.

    Returns the authority plus the bounded list of candidate paths that could
    not be lexically declared.  An undeclarable path is **not** granted: absence
    of a grant is denial, which is the fail-closed default.

    Grant construction, entirely from facts that already exist at acceptance:

    ``existing_mutable``
        the record is source-grounded ``SOURCE_STATUS_EXISTING`` **and** the
        accepted Plan requests a mutating structured file operation on it.  The
        authorization decision itself is not re-implemented here: acceptance is
        the decision.  ``_source_operation_contract_issues`` raises
        ``existing_file_write_without_authorization`` (via
        ``_existing_write_authorized``) for an unauthorized existing-file write,
        and that finding is repairable, so a plan carrying it never reaches an
        accepted status and never reaches this function.

    ``existing_readonly``
        source-grounded ``SOURCE_STATUS_EXISTING`` with no requested mutation.
        Materialization admitted the file as readable context the accepted Plan
        has no authority to change.

    ``creation_authorized``
        source-grounded ``SOURCE_STATUS_NEW`` (which the materializer sets only
        when ``creation_authorized`` holds) **and** the accepted Plan actually
        requested the creation.  A path that is merely creation-eligible is not
        granted: the Plan must request, the validator must grant.

    ``deletion_authorized``
        never constructed.  Plan validation performs no deterministic deletion
        authorization today — ``delete_file`` is a supported op shape but no
        source-grounding or task rule grants it, and the class additionally
        requires a baseline content hash that no deletion decision produces.
        Inferring deletion authority from a later observation (a missing file, a
        Change Set ``deleted_files`` entry, Execution behaviour) is exactly the
        category error this contract exists to remove, so the limitation is left
        explicit rather than papered over.
    """

    scope_paths = {str(path) for path in (task_explicit_scope_paths or ()) if str(path)}
    creation_requests = {
        str(path) for path in (creation_requested_paths or ()) if str(path)
    }
    mutation_paths = plan_mutation_paths(plan)

    grants: list[PathGrant] = []
    undeclarable: list[str] = []
    for item in getattr(source_materialization, "files", ()) or ():
        relative_path = str(getattr(item, "relative_path", "") or "")
        if not relative_path:
            continue
        status = getattr(item, "status", None)
        baseline_content_hash: str | None
        if status == SOURCE_STATUS_EXISTING:
            baseline_content_hash = getattr(item, "content_hash", None) or None
            if baseline_content_hash is None:
                # No durable source-grounding hash means no provable baseline,
                # and both existing classes require one.  No grant, no
                # authority.
                continue
            grant_class = (
                GrantClass.EXISTING_MUTABLE
                if relative_path in mutation_paths
                else GrantClass.EXISTING_READONLY
            )
        elif (
            status == SOURCE_STATUS_NEW
            and bool(getattr(item, "creation_authorized", False))
            and relative_path in creation_requests
        ):
            baseline_content_hash = None
            grant_class = GrantClass.CREATION_AUTHORIZED
        else:
            continue

        try:
            path = declare(relative_path)
        except PathDeclarationError as exc:
            undeclarable.append(f"{relative_path}:{exc.code}")
            continue

        grants.append(
            PathGrant(
                path=path,
                grant_class=grant_class,
                provenance=_provenance_for(relative_path, grant_class, scope_paths),
                baseline_content_hash=baseline_content_hash,
            )
        )

    authority = AcceptedPathAuthority.create(
        accepted_plan_identity=accepted_plan_identity(plan),
        workspace_identity=str(
            getattr(source_materialization, "workspace_identity", "") or ""
        ),
        maximum_scope_digest=maximum_scope_digest(
            task_explicit_scope_paths=scope_paths,
            source_materialization=source_materialization,
        ),
        grants=grants,
    )
    return authority, tuple(sorted(undeclarable))


def accepted_path_authority_from_verdict(verdict: Any) -> AcceptedPathAuthority | None:
    """Reconstruct a persisted authority from an accepted plan verdict.

    Accepts a verdict object, a serialized verdict mapping, or the ``details``
    mapping itself.  Returns ``None`` when no authority was recorded, and raises
    the ordinary :mod:`.path_authority` load errors for a malformed or tampered
    record — a forged record never loads as valid authority.

    This is a pure parser.  It performs no database access, and Phase 33C-3
    deliberately leaves it with zero runtime callers.
    """

    details = getattr(verdict, "details", None)
    if details is None and isinstance(verdict, Mapping):
        details = verdict.get("details", verdict)
    if not isinstance(details, Mapping):
        return None
    payload = details.get("accepted_path_authority")
    if payload is None:
        return None
    return AcceptedPathAuthority.from_dict(payload)
