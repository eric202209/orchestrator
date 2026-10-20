"""Bounded invocation-to-worker identity projection and correlation context.

The projection records correlation evidence only. It does not attest that a
worker is trustworthy and must not be used as a publication gate.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence, TypedDict


INVOCATION_PROJECTION_SCHEMA_VERSION = 1
CORRELATED = "CORRELATED"
INCONCLUSIVE = "INCONCLUSIVE"
MISMATCH = "MISMATCH"
_CLASSIFICATIONS = frozenset({CORRELATED, INCONCLUSIVE, MISMATCH})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SOURCE_REVISION = re.compile(r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$|^[a-f0-9]{64}$")
_PROCESS_START = re.compile(r"^[a-fA-F0-9-]{36}:[0-9]+$")

REQUIRED_LOADED_MODULES = (
    "app.celery_app",
    "app.tasks.worker",
    "app.services.orchestration.worker_identity",
    "app.services.orchestration.invocation_provenance",
    "app.services.orchestration.state.persistence",
    "app.services.orchestration.phases.execution_loop",
    "app.services.orchestration.coordinators.completion_coordinator",
    "app.services.orchestration.phases.completion_flow",
    "app.services.tasks.service",
    "app.services.workspace.changeset_service",
)

_REASON_CODES = frozenset(
    {
        "project_id_missing",
        "task_id_missing",
        "session_id_missing",
        "task_execution_id_missing",
        "attempt_number_unavailable",
        "celery_request_id_missing",
        "celery_retry_count_unavailable",
        "redelivery_identity_unavailable",
        "worker_boot_manifest_unavailable",
        "worker_boot_manifest_identity_unavailable",
        "worker_boot_identity_status_unavailable",
        "worker_boot_identity_inconclusive",
        "worker_boot_identity_mismatch",
        "worker_process_identity_unavailable",
        "worker_process_boot_mismatch",
        "source_manifest_unavailable",
        "source_manifest_drift",
        "loaded_module_identity_unavailable",
        "loaded_module_source_mismatch",
    }
)


class InvocationProjectionV1(TypedDict):
    invocation_projection_schema_version: int
    invocation_attempt_id: str
    durable_journal_reference: str
    project_id: int | None
    task_id: int | None
    session_id: int | None
    task_execution_id: int | None
    celery_request_id: str | None
    attempt_number: int | None
    celery_retry_count: int | None
    delivery_redelivered: bool | None
    worker_boot_manifest_id: str | None
    worker_boot_manifest_digest: str | None
    worker_boot_identity_status: str
    worker_process_identity: dict[str, Any]
    source_identity: dict[str, Any]
    required_loaded_module_identities: list[dict[str, Any]]
    invocation_started_at: str
    classification: str
    classification_reasons: list[str]


_INVOCATION_REFERENCE: ContextVar["InvocationProvenanceReference | None"] = ContextVar(
    "orchestrator_invocation_provenance", default=None
)


@dataclass(frozen=True)
class InvocationProvenanceReference:
    invocation_attempt_id: str
    projection_journal_reference: str | None
    worker_boot_manifest_id: str | None
    worker_boot_manifest_digest: str | None
    attempt_number: int | None
    classification: str

    def to_dict(self) -> dict[str, Any]:
        try:
            invocation_attempt_id = uuid.UUID(self.invocation_attempt_id).hex
        except (AttributeError, TypeError, ValueError):
            invocation_attempt_id = ""
        journal_reference = self.projection_journal_reference
        if journal_reference != invocation_attempt_id:
            journal_reference = None
        classification = (
            self.classification
            if self.classification in _CLASSIFICATIONS
            else INCONCLUSIVE
        )
        if journal_reference is None and classification == CORRELATED:
            classification = INCONCLUSIVE
        return {
            "schema_version": INVOCATION_PROJECTION_SCHEMA_VERSION,
            "invocation_attempt_id": invocation_attempt_id,
            "projection_journal_reference": journal_reference,
            "worker_boot_manifest_id": _identifier(self.worker_boot_manifest_id),
            "worker_boot_manifest_digest": _sha256(self.worker_boot_manifest_digest),
            "attempt_number": _positive_int(self.attempt_number),
            "classification": classification,
        }


_REFERENCE_KEYS = frozenset(
    {
        "schema_version",
        "invocation_attempt_id",
        "projection_journal_reference",
        "worker_boot_manifest_id",
        "worker_boot_manifest_digest",
        "attempt_number",
        "classification",
    }
)


def validate_invocation_reference(value: Any) -> dict[str, Any] | None:
    """Return a canonical compact reference, or None when it is not exact.

    Unknown or missing nested fields reject the whole reference rather than
    being silently dropped, so a stored reference is never partially trusted.
    """

    if not isinstance(value, Mapping) or set(value) != _REFERENCE_KEYS:
        return None
    if value.get("schema_version") != INVOCATION_PROJECTION_SCHEMA_VERSION:
        return None
    attempt_id = value.get("invocation_attempt_id")
    if not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", attempt_id):
        return None
    journal_reference = value.get("projection_journal_reference")
    if journal_reference is not None and journal_reference != attempt_id:
        return None
    manifest_id = value.get("worker_boot_manifest_id")
    if manifest_id is not None and _identifier(manifest_id) != manifest_id:
        return None
    manifest_digest = value.get("worker_boot_manifest_digest")
    if manifest_digest is not None and _sha256(manifest_digest) is None:
        return None
    attempt_number = value.get("attempt_number")
    if attempt_number is not None and _positive_int(attempt_number) is None:
        return None
    classification = value.get("classification")
    if classification not in _CLASSIFICATIONS:
        return None
    if classification == CORRELATED and journal_reference is None:
        return None
    return {key: value[key] for key in sorted(_REFERENCE_KEYS)}


def bind_invocation_reference(reference: InvocationProvenanceReference) -> Token:
    return _INVOCATION_REFERENCE.set(reference)


def reset_invocation_reference(token: Token) -> None:
    _INVOCATION_REFERENCE.reset(token)


def current_invocation_reference() -> InvocationProvenanceReference | None:
    return _INVOCATION_REFERENCE.get()


def attach_invocation_provenance(
    payload: Mapping[str, Any],
    *,
    reference: InvocationProvenanceReference | None = None,
) -> dict[str, Any]:
    """Return a payload with the current compact reference, if one is bound.

    Existing conflicting references remain explicitly mismatched instead of
    being silently accepted as the current invocation.
    """

    result = dict(payload)
    reference = reference or current_invocation_reference()
    if reference is None:
        return result

    existing = result.get("invocation_provenance")
    conflict = False
    if existing is not None:
        if not isinstance(existing, Mapping):
            conflict = True
        else:
            expected = reference.to_dict()
            conflict = any(
                key in existing and existing.get(key) != value
                for key, value in expected.items()
            )
    if conflict:
        reference = replace(reference, classification=MISMATCH)
        result["invocation_provenance_conflict"] = True
    result["invocation_provenance"] = reference.to_dict()
    return result


def _positive_int(value: Any) -> int | None:
    if type(value) is not int or value <= 0:
        return None
    return value


def _bounded_nonnegative_int(value: Any, *, maximum: int = 2**31 - 1) -> int | None:
    if type(value) is not int or value < 0 or value > maximum:
        return None
    return value


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip()
    return value if value and _IDENTIFIER.fullmatch(value) else None


def _celery_request_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _sha256(value: Any) -> str | None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        return None
    return value


def _revision(value: Any) -> str | None:
    if not isinstance(value, str) or not _SOURCE_REVISION.fullmatch(value):
        return None
    return value


def _record_map(records: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(records, (list, tuple)):
        return {}
    return {
        record["module_name"]: record
        for record in records
        if isinstance(record, Mapping) and isinstance(record.get("module_name"), str)
    }


def _source_projection(
    boot: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
    reasons: list[str],
) -> tuple[dict[str, Any], str]:
    if boot is None or current is None:
        reasons.append("source_manifest_unavailable")
        return (
            {
                "candidate_head": None,
                "candidate_tree": None,
                "source_manifest_digest": None,
                "status": "UNAVAILABLE",
            },
            "UNAVAILABLE",
        )

    boot_head = _revision(boot.get("candidate_head"))
    boot_tree = _revision(boot.get("candidate_tree"))
    boot_digest = _sha256(boot.get("source_manifest_digest"))
    current_head = _revision(current.get("candidate_head"))
    current_tree = _revision(current.get("candidate_tree"))
    current_digest = _sha256(current.get("source_manifest_digest"))
    if not all(
        (boot_head, boot_tree, boot_digest, current_head, current_tree, current_digest)
    ):
        reasons.append("source_manifest_unavailable")
        return (
            {
                "candidate_head": boot_head,
                "candidate_tree": boot_tree,
                "source_manifest_digest": boot_digest,
                "status": "UNAVAILABLE",
            },
            "UNAVAILABLE",
        )

    drift = (
        boot_head != current_head
        or boot_tree != current_tree
        or boot_digest != current_digest
    )
    if drift:
        reasons.append("source_manifest_drift")
    status = "MISMATCH" if drift else "MATCH"
    return (
        {
            "candidate_head": boot_head,
            "candidate_tree": boot_tree,
            "source_manifest_digest": boot_digest,
            "status": status,
        },
        status,
    )


def _module_projection(
    boot: Mapping[str, Any] | None,
    current_modules: Sequence[Mapping[str, Any]] | None,
    reasons: list[str],
) -> tuple[list[dict[str, Any]], str]:
    boot_values = _record_map((boot or {}).get("affected_module_provenance"))
    current_values = _record_map(current_modules)
    projected: list[dict[str, Any]] = []
    statuses: list[str] = []
    for module_name in REQUIRED_LOADED_MODULES:
        boot_record = boot_values.get(module_name)
        current_record = current_values.get(module_name)
        expected_source = _sha256(
            boot_record.get("expected_source_sha256") if boot_record else None
        )
        current_source = _sha256(
            current_record.get("source_sha256") if current_record else None
        )
        loaded = (
            current_record.get("loaded")
            if current_record and type(current_record.get("loaded")) is bool
            else None
        )
        boot_loaded = (
            boot_record.get("loaded")
            if boot_record and type(boot_record.get("loaded")) is bool
            else None
        )
        if (
            boot_record is None
            or current_record is None
            or not expected_source
            or boot_loaded is not True
        ):
            status = "UNAVAILABLE"
        elif loaded is not True or current_source is None:
            status = "UNAVAILABLE"
        elif current_source != expected_source:
            status = "MISMATCH"
        else:
            status = "MATCH"
        if status == "UNAVAILABLE":
            reasons.append("loaded_module_identity_unavailable")
        elif status == "MISMATCH":
            reasons.append("loaded_module_source_mismatch")
        statuses.append(status)
        projected.append(
            {
                "module_name": module_name,
                "expected_source_sha256": expected_source,
                "observed_source_sha256": current_source,
                "loaded_at_invocation": loaded,
                "loaded_at_worker_boot": boot_loaded,
                "source_identity_status": status,
                "in_memory_code_identity_available": False,
            }
        )
    if "MISMATCH" in statuses:
        return projected, "MISMATCH"
    if not statuses or any(status != "MATCH" for status in statuses):
        return projected, "UNAVAILABLE"
    return projected, "MATCH"


def _process_projection(
    boot: Mapping[str, Any] | None,
    process: Mapping[str, Any] | None,
    reasons: list[str],
) -> tuple[dict[str, Any], str]:
    current_pid = _positive_int((process or {}).get("pid"))
    parent_pid = _positive_int((process or {}).get("ppid"))
    boot_id = _identifier((process or {}).get("linux_boot_id"))
    start_ticks = _identifier((process or {}).get("start_ticks"))
    boot_pid = _positive_int((boot or {}).get("worker_pid"))
    boot_linux_id = _identifier((boot or {}).get("linux_boot_id"))
    boot_start_ticks = _identifier((boot or {}).get("worker_start_ticks"))
    node_identity = _identifier((boot or {}).get("worker_node_identity"))
    facts = {
        "pid": current_pid,
        "ppid": parent_pid,
        "linux_boot_id": boot_id,
        "start_ticks": start_ticks,
        "worker_node_identity": node_identity,
    }
    if boot is None or not all(
        (current_pid, boot_id, start_ticks, boot_pid, boot_linux_id)
    ):
        reasons.append("worker_process_identity_unavailable")
        return facts, "UNAVAILABLE"
    if boot_id != boot_linux_id:
        reasons.append("worker_process_boot_mismatch")
        return facts, "MISMATCH"
    if current_pid == boot_pid and boot_start_ticks == start_ticks:
        return facts, "MATCH"
    child_match = any(
        isinstance(child, Mapping)
        and _positive_int(child.get("pid")) == current_pid
        and _identifier(child.get("linux_boot_id")) == boot_id
        and _identifier(child.get("start_ticks")) == start_ticks
        for child in (boot.get("worker_children") or [])
    )
    if child_match or parent_pid == boot_pid:
        return facts, "MATCH"
    reasons.append("worker_process_boot_mismatch")
    return facts, "MISMATCH"


def build_invocation_projection(
    *,
    project_id: Any,
    task_id: Any,
    session_id: Any,
    task_execution_id: Any,
    celery_request_id: Any,
    celery_retry_count: Any,
    delivery_redelivered: Any,
    invocation_started_at: datetime,
    worker_boot_manifest: Mapping[str, Any] | None,
    worker_boot_manifest_digest: Any,
    worker_process: Mapping[str, Any] | None,
    current_source_manifest: Mapping[str, Any] | None,
    current_loaded_modules: Sequence[Mapping[str, Any]] | None,
    invocation_attempt_id: str | None = None,
) -> dict[str, Any]:
    """Build a content-free projection and its explicit identity classification."""

    invocation_id = invocation_attempt_id or uuid.uuid4().hex
    try:
        invocation_id = uuid.UUID(str(invocation_id)).hex
    except (AttributeError, TypeError, ValueError):
        invocation_id = uuid.uuid4().hex
    reasons: list[str] = []
    project = _positive_int(project_id)
    task = _positive_int(task_id)
    session = _positive_int(session_id)
    execution = _positive_int(task_execution_id)
    request_id = _celery_request_identifier(celery_request_id)
    retry_count = _bounded_nonnegative_int(celery_retry_count)
    redelivered = delivery_redelivered if type(delivery_redelivered) is bool else None
    for value, reason in (
        (project, "project_id_missing"),
        (task, "task_id_missing"),
        (session, "session_id_missing"),
        (execution, "task_execution_id_missing"),
        (request_id, "celery_request_id_missing"),
        (retry_count, "celery_retry_count_unavailable"),
        (redelivered, "redelivery_identity_unavailable"),
    ):
        if value is None:
            reasons.append(reason)

    boot = worker_boot_manifest if isinstance(worker_boot_manifest, Mapping) else None
    if boot is None:
        reasons.append("worker_boot_manifest_unavailable")
    boot_manifest_id = _identifier(boot.get("record_id")) if boot else None
    boot_digest = _sha256(worker_boot_manifest_digest)
    if boot is not None and (boot_manifest_id is None or boot_digest is None):
        reasons.append("worker_boot_manifest_identity_unavailable")
    boot_status_value = (boot or {}).get("identity_status")
    boot_status = (
        boot_status_value
        if boot_status_value
        in {
            "IDENTITY_VERIFIED",
            "IDENTITY_INCONCLUSIVE",
            "IDENTITY_MISMATCH",
            "IDENTITY_UNAVAILABLE",
        }
        else "IDENTITY_UNAVAILABLE"
    )
    if boot is not None and boot_status == "IDENTITY_UNAVAILABLE":
        reasons.append("worker_boot_identity_status_unavailable")
    if boot is not None and boot_status == "IDENTITY_INCONCLUSIVE":
        reasons.append("worker_boot_identity_inconclusive")

    process, process_status = _process_projection(boot, worker_process, reasons)
    source, source_status = _source_projection(boot, current_source_manifest, reasons)
    modules, modules_status = _module_projection(boot, current_loaded_modules, reasons)
    if boot_status == "IDENTITY_MISMATCH":
        reasons.append("worker_boot_identity_mismatch")

    mismatch_reasons = {
        "source_manifest_drift",
        "loaded_module_source_mismatch",
        "worker_process_boot_mismatch",
        "worker_boot_identity_mismatch",
    }
    if mismatch_reasons.intersection(reasons):
        classification = MISMATCH
    elif reasons:
        classification = INCONCLUSIVE
    elif (
        source_status == "MATCH"
        and modules_status == "MATCH"
        and process_status == "MATCH"
    ):
        classification = CORRELATED
    else:
        classification = INCONCLUSIVE

    started_at = invocation_started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return {
        "invocation_projection_schema_version": INVOCATION_PROJECTION_SCHEMA_VERSION,
        "invocation_attempt_id": invocation_id,
        "durable_journal_reference": invocation_id,
        "project_id": project,
        "task_id": task,
        "session_id": session,
        "task_execution_id": execution,
        "celery_request_id": request_id,
        "attempt_number": None,
        "celery_retry_count": retry_count,
        "delivery_redelivered": redelivered,
        "worker_boot_manifest_id": boot_manifest_id,
        "worker_boot_manifest_digest": boot_digest,
        "worker_boot_identity_status": boot_status,
        "worker_process_identity": process,
        "source_identity": {
            **source,
            "loaded_module_identity_status": modules_status,
            "loaded_code_identity_status": "UNAVAILABLE",
        },
        "required_loaded_module_identities": modules,
        "invocation_started_at": started_at.isoformat(),
        "classification": classification,
        "classification_reasons": sorted(set(reasons))[:32],
    }


def validate_invocation_projection(value: Mapping[str, Any]) -> InvocationProjectionV1:
    """Validate and rebuild the projection using only reviewed schema fields."""

    if value.get("invocation_projection_schema_version") != (
        INVOCATION_PROJECTION_SCHEMA_VERSION
    ):
        raise ValueError("unsupported invocation projection schema")
    invocation_id = _identifier(value.get("invocation_attempt_id"))
    if invocation_id is None or not re.fullmatch(r"[a-f0-9]{32}", invocation_id):
        raise ValueError("invalid invocation attempt identifier")
    if value.get("durable_journal_reference") != invocation_id:
        raise ValueError("invalid invocation journal reference")
    classification = value.get("classification")
    if classification not in _CLASSIFICATIONS:
        raise ValueError("invalid invocation classification")

    worker_identity = value.get("worker_process_identity")
    if not isinstance(worker_identity, Mapping):
        raise ValueError("invalid worker process identity")
    process_projection = {
        "pid": _positive_int(worker_identity.get("pid")),
        "ppid": _positive_int(worker_identity.get("ppid")),
        "linux_boot_id": _identifier(worker_identity.get("linux_boot_id")),
        "start_ticks": _identifier(worker_identity.get("start_ticks")),
        "worker_node_identity": _identifier(
            worker_identity.get("worker_node_identity")
        ),
    }

    source = value.get("source_identity")
    if not isinstance(source, Mapping):
        raise ValueError("invalid source identity")
    source_status = source.get("status")
    module_status = source.get("loaded_module_identity_status")
    code_status = source.get("loaded_code_identity_status")
    if source_status not in {"MATCH", "MISMATCH", "UNAVAILABLE"}:
        raise ValueError("invalid source identity status")
    if module_status not in {"MATCH", "MISMATCH", "UNAVAILABLE"}:
        raise ValueError("invalid loaded module identity status")
    if code_status != "UNAVAILABLE":
        raise ValueError("unsupported loaded code identity status")
    source_projection = {
        "candidate_head": _revision(source.get("candidate_head")),
        "candidate_tree": _revision(source.get("candidate_tree")),
        "source_manifest_digest": _sha256(source.get("source_manifest_digest")),
        "status": source_status,
        "loaded_module_identity_status": module_status,
        "loaded_code_identity_status": code_status,
    }

    raw_modules = value.get("required_loaded_module_identities")
    if not isinstance(raw_modules, list) or len(raw_modules) != len(
        REQUIRED_LOADED_MODULES
    ):
        raise ValueError("invalid required loaded-module identities")
    module_projection = []
    for expected_name, item in zip(REQUIRED_LOADED_MODULES, raw_modules):
        if not isinstance(item, Mapping) or item.get("module_name") != expected_name:
            raise ValueError("invalid loaded-module identity name")
        status = item.get("source_identity_status")
        if status not in {"MATCH", "MISMATCH", "UNAVAILABLE"}:
            raise ValueError("invalid loaded-module identity status")
        loaded = item.get("loaded_at_invocation")
        boot_loaded = item.get("loaded_at_worker_boot")
        if loaded is not None and type(loaded) is not bool:
            raise ValueError("invalid loaded-module runtime flag")
        if boot_loaded is not None and type(boot_loaded) is not bool:
            raise ValueError("invalid loaded-module boot flag")
        if type(item.get("in_memory_code_identity_available")) is not bool:
            raise ValueError("invalid in-memory code identity flag")
        module_projection.append(
            {
                "module_name": expected_name,
                "expected_source_sha256": _sha256(item.get("expected_source_sha256")),
                "observed_source_sha256": _sha256(item.get("observed_source_sha256")),
                "loaded_at_invocation": loaded,
                "loaded_at_worker_boot": boot_loaded,
                "source_identity_status": status,
                "in_memory_code_identity_available": item.get(
                    "in_memory_code_identity_available"
                ),
            }
        )

    reasons = value.get("classification_reasons")
    if not isinstance(reasons, list) or any(
        not isinstance(item, str) or item not in _REASON_CODES for item in reasons
    ):
        raise ValueError("invalid invocation classification reasons")
    started_at = value.get("invocation_started_at")
    if not isinstance(started_at, str) or len(started_at) > 40:
        raise ValueError("invalid invocation start timestamp")
    try:
        parsed_started_at = datetime.fromisoformat(started_at)
    except ValueError as exc:
        raise ValueError("invalid invocation start timestamp") from exc
    if parsed_started_at.tzinfo is None:
        raise ValueError("invocation start timestamp must include timezone")

    project_id = _positive_int(value.get("project_id"))
    task_id = _positive_int(value.get("task_id"))
    session_id = _positive_int(value.get("session_id"))
    task_execution_id = _positive_int(value.get("task_execution_id"))
    celery_request_id = _celery_request_identifier(value.get("celery_request_id"))
    retry_count = _bounded_nonnegative_int(value.get("celery_retry_count"))
    redelivered = (
        value.get("delivery_redelivered")
        if type(value.get("delivery_redelivered")) is bool
        else None
    )
    boot_manifest_id = _identifier(value.get("worker_boot_manifest_id"))
    boot_manifest_digest = _sha256(value.get("worker_boot_manifest_digest"))
    boot_identity_status = value.get("worker_boot_identity_status")
    if boot_identity_status not in {
        "IDENTITY_VERIFIED",
        "IDENTITY_INCONCLUSIVE",
        "IDENTITY_MISMATCH",
        "IDENTITY_UNAVAILABLE",
    }:
        boot_identity_status = "IDENTITY_UNAVAILABLE"

    reasons = list(dict.fromkeys(reasons))
    attempt_number = _positive_int(value.get("attempt_number"))
    required_values = (
        (project_id, "project_id_missing"),
        (task_id, "task_id_missing"),
        (session_id, "session_id_missing"),
        (task_execution_id, "task_execution_id_missing"),
        (celery_request_id, "celery_request_id_missing"),
        (retry_count, "celery_retry_count_unavailable"),
        (redelivered, "redelivery_identity_unavailable"),
        (attempt_number, "attempt_number_unavailable"),
        (boot_manifest_id, "worker_boot_manifest_identity_unavailable"),
        (boot_manifest_digest, "worker_boot_manifest_identity_unavailable"),
        (process_projection["pid"], "worker_process_identity_unavailable"),
        (process_projection["linux_boot_id"], "worker_process_identity_unavailable"),
        (process_projection["start_ticks"], "worker_process_identity_unavailable"),
        (source_projection["candidate_head"], "source_manifest_unavailable"),
        (source_projection["candidate_tree"], "source_manifest_unavailable"),
        (source_projection["source_manifest_digest"], "source_manifest_unavailable"),
    )
    for observed, reason in required_values:
        if observed is None and reason not in reasons:
            reasons.append(reason)
    if boot_identity_status == "IDENTITY_UNAVAILABLE":
        reasons.append("worker_boot_identity_status_unavailable")
    if boot_identity_status == "IDENTITY_INCONCLUSIVE":
        reasons.append("worker_boot_identity_inconclusive")
    if boot_identity_status == "IDENTITY_MISMATCH":
        reasons.append("worker_boot_identity_mismatch")
    module_statuses = [item["source_identity_status"] for item in module_projection]
    if any(status == "UNAVAILABLE" for status in module_statuses):
        reasons.append("loaded_module_identity_unavailable")
    if any(status == "MISMATCH" for status in module_statuses):
        reasons.append("loaded_module_source_mismatch")
    if source_status == "UNAVAILABLE":
        reasons.append("source_manifest_unavailable")
    if source_status == "MISMATCH":
        reasons.append("source_manifest_drift")
    if (
        process_projection["pid"] is None
        or process_projection["linux_boot_id"] is None
        or process_projection["start_ticks"] is None
    ):
        reasons.append("worker_process_identity_unavailable")

    mismatch_reasons = {
        "source_manifest_drift",
        "loaded_module_source_mismatch",
        "worker_process_boot_mismatch",
        "worker_boot_identity_mismatch",
    }
    if mismatch_reasons.intersection(reasons):
        classification = MISMATCH
    elif classification == CORRELATED and reasons:
        classification = INCONCLUSIVE
    reasons = sorted(set(reasons))[:32]

    return {
        "invocation_projection_schema_version": INVOCATION_PROJECTION_SCHEMA_VERSION,
        "invocation_attempt_id": invocation_id,
        "durable_journal_reference": invocation_id,
        "project_id": project_id,
        "task_id": task_id,
        "session_id": session_id,
        "task_execution_id": task_execution_id,
        "celery_request_id": celery_request_id,
        "attempt_number": attempt_number,
        "celery_retry_count": retry_count,
        "delivery_redelivered": redelivered,
        "worker_boot_manifest_id": boot_manifest_id,
        "worker_boot_manifest_digest": boot_manifest_digest,
        "worker_boot_identity_status": boot_identity_status,
        "worker_process_identity": process_projection,
        "source_identity": source_projection,
        "required_loaded_module_identities": module_projection,
        "invocation_started_at": parsed_started_at.isoformat(),
        "classification": classification,
        "classification_reasons": reasons,
    }


def reference_from_projection(
    projection: Mapping[str, Any],
    *,
    journal_reference: str | None,
    attempt_number: int | None,
    classification: str | None = None,
) -> InvocationProvenanceReference:
    status = classification or projection.get("classification")
    if status not in _CLASSIFICATIONS:
        status = INCONCLUSIVE
    if journal_reference is None and status == CORRELATED:
        status = INCONCLUSIVE
    return InvocationProvenanceReference(
        invocation_attempt_id=str(projection.get("invocation_attempt_id") or ""),
        projection_journal_reference=journal_reference,
        worker_boot_manifest_id=_identifier(projection.get("worker_boot_manifest_id")),
        worker_boot_manifest_digest=_sha256(
            projection.get("worker_boot_manifest_digest")
        ),
        attempt_number=_positive_int(attempt_number),
        classification=status,
    )


def persist_invocation_projection(
    *,
    project_dir: Any,
    project_id: Any,
    session_id: Any,
    task_id: Any,
    projection: Mapping[str, Any],
) -> InvocationProvenanceReference:
    """Append one immutable projection; persistence gaps remain inconclusive."""

    try:
        from app.services.orchestration.state.persistence import (
            append_invocation_projection_event,
        )

        event = append_invocation_projection_event(
            project_dir=project_dir,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            invocation_attempt_id=str(projection["invocation_attempt_id"]),
            details=dict(projection),
        )
    except Exception as exc:
        from app.services.orchestration.state.persistence import (
            DuplicateInvocationProjectionError,
        )

        status = (
            MISMATCH
            if isinstance(exc, DuplicateInvocationProjectionError)
            else INCONCLUSIVE
        )
        return reference_from_projection(
            projection,
            journal_reference=None,
            attempt_number=None,
            classification=status,
        )
    event_id = event.get("event_id") if isinstance(event, Mapping) else None
    if event_id != projection.get("invocation_attempt_id"):
        return reference_from_projection(
            projection,
            journal_reference=None,
            attempt_number=None,
            classification=MISMATCH,
        )
    details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
    return reference_from_projection(
        details,
        journal_reference=event_id,
        attempt_number=details.get("attempt_number"),
    )


__all__ = [
    "CORRELATED",
    "INCONCLUSIVE",
    "INVOCATION_PROJECTION_SCHEMA_VERSION",
    "InvocationProvenanceReference",
    "MISMATCH",
    "REQUIRED_LOADED_MODULES",
    "attach_invocation_provenance",
    "bind_invocation_reference",
    "build_invocation_projection",
    "current_invocation_reference",
    "persist_invocation_projection",
    "reset_invocation_reference",
    "validate_invocation_reference",
]
