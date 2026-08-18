"""Provider-free grounded execution adapter.

This module is intentionally an internal seam.  It accepts untrusted,
path/quote/content grounded intent, derives the existing Phase 33 selector and
Accepted Path Authority, and projects the result into the legacy execution
checkpoint/runtime.  It does not create a job model, expose target IDs, call a
provider, or perform review/publication.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.models import TaskExecution, TaskStatus
from app.services.orchestration.planning.semantic_selector_construction import (
    CONSTRUCTED_UNIQUE,
    construct_source_region_identity,
)
from app.services.orchestration.planning.semantic_target_inventory import (
    build_semantic_target_inventory,
)
from app.services.orchestration.planning.source_materialization import (
    HINT_TYPE_QUOTED_SNIPPET,
    SOURCE_STATUS_EXISTING,
    SOURCE_STATUS_NEW,
    SPAN_PRIMARY_TARGET,
    MaterializedSourceFile,
    MaterializedSourceSpan,
    PlannerSourceMaterialization,
    current_source_version_identity,
)
from app.services.orchestration.state.persistence import (
    accepted_authority_for_verdict,
    append_orchestration_event,
    load_accepted_path_authority,
    record_validation_verdict,
    save_orchestration_checkpoint,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_plan_identity,
)
from app.services.orchestration.validation.candidate_checks import (
    candidate_delta_identity,
    validate_candidate_delta,
)
from app.services.orchestration.validation.path_authority import (
    EntryType,
    PathAuthorityError,
    declare,
    observe,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.run_state import (
    mark_task_attempt_done,
    mark_task_attempt_failed,
)
from app.services.orchestration.state.session_state import (
    mark_session_completed,
    mark_session_failed,
)


GROUNDED_EXECUTION_PROFILE = "grounded_external_submission"
GROUNDED_EXECUTION_KIND = "grounded_external_submission"
GROUNDED_ENVELOPE_SCHEMA_VERSION = "grounded-execution/1"
MAX_GROUNDED_OPERATIONS = 8
MAX_GROUNDED_TEST_PATHS = 8
MAX_GROUNDED_CONTENT_CHARS = 200_000


class GroundedExecutionError(RuntimeError):
    """Bounded, public-safe grounded admission/runtime failure."""

    def __init__(
        self,
        family: str,
        subcode: str,
        message: str,
        *,
        evidence: Mapping[str, Any] | None = None,
        partial_work: bool = False,
    ) -> None:
        self.family = family
        self.subcode = subcode
        self.evidence = _safe_evidence(evidence or {})
        self.partial_work = bool(partial_work)
        super().__init__(message)

    def to_result(self) -> dict[str, Any]:
        return {
            "status": "failed",
            "public_state": "FAILED",
            "failure": {
                "family": self.family,
                "subcode": self.subcode,
                "partial_work": self.partial_work,
                "evidence": self.evidence,
            },
            "partial_work": self.partial_work,
            "publication_status": "not_published",
        }


def _safe_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep evidence bounded and exclude selector/target implementation facts."""

    allowed = {"path", "operation_index", "step_index", "kind", "count", "status"}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key not in allowed:
            continue
        if isinstance(item, (str, int, bool)):
            result[key] = str(item)[:240] if isinstance(item, str) else item
    return result


def _canonical_path(root: Path, raw_path: Any) -> str:
    try:
        path = declare(raw_path)
        observation = observe(root, path)
    except (PathAuthorityError, TypeError, ValueError) as exc:
        raise GroundedExecutionError(
            "OUT_OF_SCOPE",
            getattr(exc, "code", "path_not_relative"),
            "Grounded path is outside the task workspace scope",
        ) from exc
    if observation.symlink_segment:
        raise GroundedExecutionError(
            "OUT_OF_SCOPE",
            "path_symlink_rejected",
            "Grounded path contains a symlink segment",
            evidence={"path": path.value},
        )
    return path.value


def _line_number(source: bytes, offset: int) -> int:
    return source[:offset].count(b"\n") + 1


def _grounded_source_record(
    root: Path,
    path: str,
    *,
    quote: str | None,
    creation: bool,
) -> MaterializedSourceFile:
    """Build one typed source fact from the current workspace only."""

    target = (root / path).resolve()
    try:
        target.relative_to(root.resolve())
        observation = observe(root, declare(path))
    except (OSError, PathAuthorityError, ValueError) as exc:
        raise GroundedExecutionError(
            "OUT_OF_SCOPE",
            getattr(exc, "code", "source_observation_failed"),
            "Grounded source path could not be safely observed",
            evidence={"path": path},
        ) from exc
    if observation.symlink_segment:
        raise GroundedExecutionError(
            "OUT_OF_SCOPE",
            "path_symlink_rejected",
            "Grounded source path contains a symlink segment",
            evidence={"path": path},
        )

    if creation:
        if observation.exists:
            raise GroundedExecutionError(
                "CONFLICT",
                "new_file_exists_at_admission",
                "New-file creation requires the destination to be absent at admission",
                evidence={"path": path},
            )
        return MaterializedSourceFile(
            relative_path=path,
            workspace_identity=str(root.resolve()),
            content=None,
            content_hash=None,
            version_identity=None,
            status=SOURCE_STATUS_NEW,
            truncated=False,
            source_length=None,
            source_length_chars=None,
            included_prompt_length=0,
            expected=True,
            creation_authorized=True,
            selection_strategy="new_file_no_source",
        )

    if not observation.exists or observation.entry_type is not EntryType.REGULAR_FILE:
        raise GroundedExecutionError(
            "STALE_GROUNDING",
            "source_file_unavailable",
            "Existing-file grounding no longer names a readable regular file",
            evidence={"path": path},
        )
    if not isinstance(quote, str) or not quote:
        raise GroundedExecutionError(
            "INVALID_GROUNDING",
            "exact_anchor_required",
            "Existing-file replacement requires a non-empty exact anchor",
            evidence={"path": path},
        )
    try:
        source_bytes = target.read_bytes()
        source = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GroundedExecutionError(
            "INVALID_GROUNDING",
            "source_not_safe_utf8",
            "Existing-file grounding source is not safe UTF-8",
            evidence={"path": path},
        ) from exc
    count = source.count(quote)
    if count == 0:
        raise GroundedExecutionError(
            "STALE_GROUNDING",
            "exact_anchor_not_current",
            "Exact grounding anchor is absent from current source",
            evidence={"path": path, "count": count},
        )
    if count != 1:
        raise GroundedExecutionError(
            "AMBIGUOUS_GROUNDING",
            "exact_anchor_not_unique",
            "Exact grounding anchor must match current source exactly once",
            evidence={"path": path, "count": count},
        )
    version_before = current_source_version_identity(target)
    start_char = source.index(quote)
    end_char = start_char + len(quote)
    start_byte = len(source[:start_char].encode("utf-8"))
    end_byte = len(source[:end_char].encode("utf-8"))
    version_after = current_source_version_identity(target)
    if version_before != version_after:
        raise GroundedExecutionError(
            "STALE_GROUNDING",
            "source_changed_during_admission",
            "Source changed while grounded admission was reading it",
            evidence={"path": path},
        )
    return MaterializedSourceFile(
        relative_path=path,
        workspace_identity=str(root.resolve()),
        content=source,
        content_hash=hashlib.sha256(source_bytes).hexdigest(),
        version_identity=version_after,
        status=SOURCE_STATUS_EXISTING,
        truncated=False,
        source_length=len(source_bytes),
        source_length_chars=len(source),
        included_prompt_length=len(source),
        expected=True,
        selection_strategy="target_centered_exact_match",
        full_source_bytes=len(source_bytes),
        included_source_bytes=len(source_bytes),
        start_byte=0,
        end_byte=len(source_bytes),
        start_line=1,
        end_line=_line_number(source_bytes, len(source_bytes)),
        target_hint=quote,
        target_hint_type=HINT_TYPE_QUOTED_SNIPPET,
        target_hint_authority="grounded_external_submission",
        target_hint_status="target_hint_matched",
        target_match_count=1,
        target_match_start=start_byte,
        target_match_end=end_byte,
        target_included=True,
        spans=(
            MaterializedSourceSpan(
                kind=SPAN_PRIMARY_TARGET,
                start_byte=0,
                end_byte=len(source_bytes),
                start_line=1,
                end_line=_line_number(source_bytes, len(source_bytes)),
                included_source_bytes=len(source_bytes),
            ),
        ),
    )


def _normalize_verification(value: Any, root: Path) -> dict[str, Any]:
    if value is None:
        return {"kind": "none", "paths": []}
    if not isinstance(value, Mapping):
        raise GroundedExecutionError(
            "UNSUPPORTED_OPERATION",
            "verification_policy_invalid",
            "Grounded verification policy must be a structured object",
        )
    kind = str(value.get("kind") or "").strip()
    if kind in {"project_checks", "raw_command", "command", "shell"} or value.get(
        "command"
    ):
        raise GroundedExecutionError(
            "COMMAND_REJECTED",
            "raw_command_not_supported",
            "Grounded execution accepts only Orchestrator-owned verification policies",
        )
    if kind not in {"focused_tests", "derived_compile_static", "none"}:
        raise GroundedExecutionError(
            "UNSUPPORTED_OPERATION",
            "verification_kind_unsupported",
            "Grounded verification kind is unsupported",
            evidence={"kind": kind},
        )
    raw_paths = value.get("paths") or []
    if not isinstance(raw_paths, list) or len(raw_paths) > MAX_GROUNDED_TEST_PATHS:
        raise GroundedExecutionError(
            "UNSUPPORTED_OPERATION",
            "verification_path_bound_exceeded",
            "Grounded verification paths exceed the bounded policy",
        )
    paths: list[str] = []
    for raw_path in raw_paths:
        path = _canonical_path(root, raw_path)
        if kind == "focused_tests":
            candidate = root / path
            if (
                not candidate.is_file()
                or not (
                    candidate.name.startswith("test_")
                    or candidate.name.endswith("_test.py")
                )
                or candidate.suffix != ".py"
            ):
                raise GroundedExecutionError(
                    "COMMAND_REJECTED",
                    "focused_test_path_invalid",
                    "focused_tests accepts only existing project Python test paths",
                    evidence={"path": path},
                )
        if path not in paths:
            paths.append(path)
    if kind == "focused_tests" and not paths:
        raise GroundedExecutionError(
            "COMMAND_REJECTED",
            "focused_test_path_required",
            "focused_tests requires at least one normalized project test path",
        )
    return {"kind": kind, "paths": paths}


def _operation_digest(operation: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(operation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def admit_grounded_submission(
    submission: Mapping[str, Any],
    *,
    project_dir: Path,
    project_id: int,
    task_id: int,
    session_id: int,
    task_execution_id: int,
    attempt_number: int,
    session_instance_id: str | None,
    prompt: str,
    title: str | None,
    description: str | None,
    validation_severity: str = "standard",
) -> tuple[dict[str, Any], Any]:
    """Admit and normalize one provider-free grounded submission."""

    if not isinstance(submission, Mapping):
        raise GroundedExecutionError(
            "INVALID_GROUNDING",
            "submission_object_required",
            "Grounded execution submission must be an object",
        )
    if (
        submission.get("execution_kind", GROUNDED_EXECUTION_KIND)
        != GROUNDED_EXECUTION_KIND
    ):
        raise GroundedExecutionError(
            "UNSUPPORTED_OPERATION",
            "execution_kind_unsupported",
            "Grounded execution submission has an unsupported execution kind",
        )
    review = submission.get("review")
    if isinstance(review, Mapping) and str(review.get("mode") or "none") not in {
        "none",
        "omit",
    }:
        raise GroundedExecutionError(
            "REVIEW_REQUIRED",
            "review_not_supported_in_v1b",
            "V1-B does not implement durable review or Publication",
        )
    operations = submission.get("operations")
    if not isinstance(operations, list) or not operations:
        raise GroundedExecutionError(
            "INVALID_GROUNDING",
            "operations_required",
            "Grounded execution requires at least one ordered operation",
        )
    if len(operations) > MAX_GROUNDED_OPERATIONS:
        raise GroundedExecutionError(
            "UNSUPPORTED_OPERATION",
            "operation_bound_exceeded",
            "Grounded execution operation count exceeds the bounded V1-B limit",
        )
    root = Path(project_dir).resolve()
    verification = _normalize_verification(submission.get("verification"), root)
    descriptors: list[dict[str, Any]] = []
    source_records: dict[str, MaterializedSourceFile] = {}
    normalized_operations: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for index, raw_operation in enumerate(operations, start=1):
        if not isinstance(raw_operation, Mapping):
            raise GroundedExecutionError(
                "UNSUPPORTED_OPERATION",
                "operation_object_required",
                "Each grounded operation must be an object",
                evidence={"operation_index": index},
            )
        op_name = str(raw_operation.get("op") or "").strip()
        if op_name not in {"replace_in_file", "create_file", "write_file"}:
            raise GroundedExecutionError(
                "UNSUPPORTED_OPERATION",
                "operation_kind_unsupported",
                "V1-B supports only exact replace and explicit new-file creation",
                evidence={"operation_index": index},
            )
        path = _canonical_path(root, raw_operation.get("path"))
        if path in seen_paths:
            raise GroundedExecutionError(
                "UNSUPPORTED_OPERATION",
                "duplicate_path_operation",
                "V1-B requires one grounded operation per path in an ordered job",
                evidence={"path": path, "operation_index": index},
            )
        seen_paths.add(path)
        if op_name == "replace_in_file":
            new_content = raw_operation.get("new")
            if (
                not isinstance(new_content, str)
                or len(new_content) > MAX_GROUNDED_CONTENT_CHARS
            ):
                raise GroundedExecutionError(
                    "INVALID_GROUNDING",
                    "replacement_content_invalid",
                    "Replacement content must be a bounded string",
                    evidence={"path": path, "operation_index": index},
                )
            record = _grounded_source_record(
                root, path, quote=raw_operation.get("quote"), creation=False
            )
            source_records[path] = record
            inventory = build_semantic_target_inventory(
                PlannerSourceMaterialization(
                    workspace_identity=str(root), files=(record,)
                ),
                task_scope=(path,),
            )
            handles = [handle for handle in inventory.handles if handle.path == path]
            if len(handles) != 1:
                raise GroundedExecutionError(
                    "INVALID_GROUNDING",
                    "target_inventory_unavailable",
                    "Orchestrator could not issue one internal target for grounded source",
                    evidence={"path": path, "operation_index": index},
                )
            construction = construct_source_region_identity(
                root=root,
                canonical_path=path,
                semantic_target=handles[0].semantic_target,
                accepted_source_materialization=PlannerSourceMaterialization(
                    workspace_identity=str(root), files=(record,)
                ),
                eligible_existing_mutable_paths=(path,),
            )
            if (
                construction.status != CONSTRUCTED_UNIQUE
                or construction.selector is None
            ):
                family = (
                    "AMBIGUOUS_GROUNDING"
                    if construction.status == "AMBIGUOUS"
                    else "STALE_GROUNDING"
                )
                raise GroundedExecutionError(
                    family,
                    construction.diagnostic_code or "selector_construction_failed",
                    "Orchestrator could not construct one current exact selector",
                    evidence={"path": path, "operation_index": index},
                )
            normalized_operation = {
                "op": "replace_in_file",
                "path": path,
                "selector": construction.selector.to_dict(),
                "new": new_content,
            }
            descriptors.append(
                {
                    "path": path,
                    "kind": "existing_file_replace",
                    "quote": str(raw_operation.get("quote")),
                    "expected_source_version": record.version_identity,
                    "operation_digest": _operation_digest(normalized_operation),
                }
            )
        else:
            content = raw_operation.get("content")
            if (
                not isinstance(content, str)
                or len(content) > MAX_GROUNDED_CONTENT_CHARS
            ):
                raise GroundedExecutionError(
                    "INVALID_GROUNDING",
                    "new_file_content_invalid",
                    "New-file content must be a bounded string",
                    evidence={"path": path, "operation_index": index},
                )
            record = _grounded_source_record(root, path, quote=None, creation=True)
            source_records[path] = record
            normalized_operation = {
                "op": "write_file",
                "path": path,
                "content": content,
            }
            descriptors.append(
                {
                    "path": path,
                    "kind": "new_file_creation",
                    "expected_absence": True,
                    "operation_digest": _operation_digest(normalized_operation),
                }
            )
        normalized_operations.append(normalized_operation)

    steps = [
        {
            "step_number": index,
            "description": f"Apply grounded operation {index}",
            "commands": [],
            "verification": None,
            "rollback": None,
            "expected_files": [operation["path"]],
            "ops": [operation],
        }
        for index, operation in enumerate(normalized_operations, start=1)
    ]
    materialization = PlannerSourceMaterialization(
        workspace_identity=str(root),
        files=tuple(source_records[path] for path in sorted(source_records)),
    )
    outcome = ValidatorService.validate_plan(
        steps,
        output_text=json.dumps(steps),
        task_prompt=prompt,
        execution_profile=GROUNDED_EXECUTION_PROFILE,
        project_dir=root,
        title=title,
        description=description,
        validation_severity=validation_severity,
        source_materialization=materialization,
    )
    if not outcome.accepted:
        details = getattr(outcome.verdict, "details", {}) or {}
        family = "INVALID_GROUNDING"
        if details.get("stale_replace_materialization"):
            family = "STALE_GROUNDING"
        elif details.get("semantic_replace_contract_issues"):
            family = "INVALID_GROUNDING"
        raise GroundedExecutionError(
            family,
            "plan_validation_failed",
            "Grounded operations failed deterministic Plan validation",
            evidence={"status": getattr(outcome.verdict, "status", "rejected")},
        )
    authority = accepted_authority_for_verdict(outcome.verdict.to_dict())
    if authority is None:
        raise GroundedExecutionError(
            "VALIDATION_FAILED",
            "accepted_path_authority_missing",
            "Accepted Plan did not produce an Accepted Path Authority",
        )
    plan_id = accepted_plan_identity(steps)
    envelope = {
        "schema_version": GROUNDED_ENVELOPE_SCHEMA_VERSION,
        "execution_kind": GROUNDED_EXECUTION_KIND,
        "project_id": project_id,
        "task_id": task_id,
        "session_id": session_id,
        "task_execution_id": task_execution_id,
        "attempt_number": attempt_number,
        "session_instance_id": session_instance_id,
        "normalized_plan": steps,
        "accepted_plan_identity": plan_id,
        "accepted_path_authority": authority.to_dict(),
        "grounding": descriptors,
        "verification": verification,
        "current_step_index": 0,
        "execution_results": [],
        "changed_files": [],
        "partial_work": False,
        "provenance": {
            "provider_free": True,
            "planning_provider": None,
            "provider_request_id": None,
            "source": "grounded_external_submission",
        },
        "step_identities": [
            hashlib.sha256(
                f"{plan_id}:{index}:{descriptor['operation_digest']}".encode()
            ).hexdigest()
            for index, descriptor in enumerate(descriptors, start=1)
        ],
    }
    return envelope, outcome.verdict


def revalidate_grounded_step(
    envelope: Mapping[str, Any], *, project_dir: Path, step_index: int
) -> None:
    grounding = list(envelope.get("grounding") or [])
    if step_index < 0 or step_index >= len(grounding):
        raise GroundedExecutionError(
            "VALIDATION_FAILED",
            "grounded_step_index_invalid",
            "Grounded checkpoint step index is outside the accepted envelope",
            evidence={"step_index": step_index + 1},
            partial_work=step_index > 0,
        )
    descriptor = grounding[step_index]
    path = str(descriptor.get("path") or "")
    target = (Path(project_dir).resolve() / path).resolve()
    if descriptor.get("kind") == "new_file_creation":
        if target.exists():
            raise GroundedExecutionError(
                "CONFLICT",
                "new_file_exists_before_write",
                "New-file destination appeared after grounded admission",
                evidence={"path": path, "step_index": step_index + 1},
                partial_work=step_index > 0,
            )
        return
    try:
        version = current_source_version_identity(target)
        source = target.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise GroundedExecutionError(
            "STALE_GROUNDING",
            "source_unavailable_before_step",
            "Grounded source could not be revalidated before its step",
            evidence={"path": path, "step_index": step_index + 1},
            partial_work=step_index > 0,
        ) from exc
    quote = str(descriptor.get("quote") or "")
    count = source.count(quote)
    if count == 0 or version != descriptor.get("expected_source_version"):
        raise GroundedExecutionError(
            "STALE_GROUNDING",
            "source_drift_before_step",
            "Grounded source changed after admission",
            evidence={"path": path, "step_index": step_index + 1},
            partial_work=step_index > 0,
        )
    if count != 1:
        raise GroundedExecutionError(
            "AMBIGUOUS_GROUNDING",
            "source_anchor_ambiguous_before_step",
            "Grounded source anchor is no longer unique",
            evidence={"path": path, "step_index": step_index + 1, "count": count},
            partial_work=step_index > 0,
        )


class GroundedRuntimeService:
    """Provider-free runtime object satisfying the existing execution context."""

    def __init__(self, *, session_id: int, task_id: int) -> None:
        self.session_id = session_id
        self.task_id = task_id
        self.task_execution_id: int | None = None
        self.runtime_configuration = None
        self.runtime_executor_context = None

    async def get_session_context(self) -> dict[str, Any]:
        return {"execution_kind": GROUNDED_EXECUTION_KIND}

    def get_backend_metadata(self) -> dict[str, Any]:
        return {
            "backend": "provider_free_grounded",
            "model_family": None,
            "provider_free": True,
        }

    def reports_context_overflow(self, _result: Any) -> bool:
        return False

    def bind_runtime_workspace(self, runtime_context: Any) -> None:
        self.runtime_executor_context = runtime_context

    def release_runtime_workspace_binding(self) -> None:
        self.runtime_executor_context = None


def run_grounded_verification(
    *,
    project_dir: Path,
    change_set: Mapping[str, Any],
    plan: Sequence[Mapping[str, Any]],
    task_prompt: str,
    policy: Mapping[str, Any],
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Run only the three V1-B verification policies."""

    kind = str(policy.get("kind") or "none")
    paths = tuple(str(path) for path in policy.get("paths") or ())
    if kind == "none":
        return {"status": "skipped", "kind": "none", "commands": [], "findings": []}
    authorized_paths = {
        str(path)
        for step in plan
        for path in (
            list(step.get("expected_files") or [])
            + [op.get("path") for op in (step.get("ops") or [])]
        )
        if path
    }
    observed_scope = tuple(
        str(path)
        for key in ("added_files", "modified_files")
        for path in (change_set.get(key) or [])
        if str(path) in authorized_paths
    )
    checks = validate_candidate_delta(
        project_dir=project_dir,
        change_set=change_set,
        plan=plan,
        task_prompt=task_prompt,
        include_static_checks=kind == "derived_compile_static",
        allow_broad_fallback=False,
        timeout_seconds=timeout_seconds,
        observed_scope=observed_scope,
        verification_scope=paths if kind == "focused_tests" else (),
        run_focused_tests=kind == "focused_tests",
    )
    return {
        "status": "passed" if not checks.findings else "failed",
        "kind": kind,
        "commands": list(checks.commands_run),
        "findings": [finding.to_dict() for finding in checks.findings[:20]],
    }


def grounded_failure_result(
    *, ctx: Any, error: GroundedExecutionError
) -> dict[str, Any]:
    """Persist terminal grounded failure without entering a repair path."""

    state = ctx.orchestration_state
    envelope = getattr(state, "grounded_execution_envelope", None)
    completed = len(getattr(state, "execution_results", []) or [])
    partial = bool(
        error.partial_work or completed or getattr(state, "changed_files", [])
    )
    if isinstance(envelope, dict):
        envelope["current_step_index"] = int(
            getattr(state, "current_step_index", 0) or 0
        )
        envelope["execution_results"] = [
            {
                "step_number": result.step_number,
                "status": result.status,
                "files_changed": list(result.files_changed or []),
            }
            for result in getattr(state, "execution_results", [])
        ]
        envelope["changed_files"] = list(
            dict.fromkeys(getattr(state, "changed_files", []))
        )
        envelope["partial_work"] = partial
        envelope["failure"] = {
            "family": error.family,
            "subcode": error.subcode,
            "evidence": error.evidence,
        }
    state.status = type(state.status).ABORTED
    state.abort_reason = f"{error.family}:{error.subcode}"
    save_orchestration_checkpoint(
        ctx.db, ctx.session_id, ctx.task_id, ctx.prompt, state
    )
    task_execution = (
        ctx.db.query(TaskExecution).filter_by(id=ctx.task_execution_id).first()
        if ctx.task_execution_id
        else None
    )
    mark_task_attempt_failed(
        task=ctx.task,
        session_task_link=ctx.session_task_link,
        task_execution=task_execution,
        error_message=f"{error.family}:{error.subcode}",
        completed_at=datetime.now(UTC),
        workspace_status="blocked" if partial else "isolated",
    )
    mark_session_failed(
        ctx.session,
        failed_at=datetime.now(UTC),
        alert_level="error",
        alert_message=f"{error.family}:{error.subcode}",
    )
    ctx.db.commit()
    result = error.to_result()
    result.update(
        {
            "task_id": ctx.task_id,
            "session_id": ctx.session_id,
            "task_execution_id": ctx.task_execution_id,
        }
    )
    return result


def complete_grounded_task(ctx: Any) -> dict[str, Any]:
    """Validate the isolated candidate and finish without review/publication."""

    state = ctx.orchestration_state
    envelope = getattr(state, "grounded_execution_envelope", None) or {}
    try:
        authority = load_accepted_path_authority(
            ctx.db,
            task_id=ctx.task_id,
            session_id=ctx.session_id,
            task_execution_id=ctx.task_execution_id,
            plan=state.plan,
            workspace_identity=str(Path(state.project_dir).resolve()),
        )
        change_set = ctx.task_service.build_task_execution_change_set(
            ctx.project,
            ctx.task,
            task_execution_id=ctx.task_execution_id,
            snapshot_key=f"task-{ctx.task_id}-execution-{ctx.task_execution_id}-pre-run",
            target_dir=Path(state.project_dir),
            preserve_project_root_rules=False,
            status=TaskStatus.DONE.value,
        )
        if not isinstance(change_set, dict):
            raise GroundedExecutionError(
                "VALIDATION_FAILED",
                "candidate_change_set_unavailable",
                "Candidate change-set evidence was unavailable",
                partial_work=bool(state.changed_files),
            )
        verification = run_grounded_verification(
            project_dir=Path(state.project_dir),
            change_set=change_set,
            plan=state.plan,
            task_prompt=ctx.prompt,
            policy=envelope.get("verification") or {"kind": "none", "paths": []},
            timeout_seconds=min(int(ctx.timeout_seconds or 180), 180),
        )
        completion = ValidatorService.validate_task_completion(
            project_dir=Path(state.project_dir),
            plan=state.plan,
            task_prompt=ctx.prompt,
            execution_profile=GROUNDED_EXECUTION_PROFILE,
            workspace_consistency=None,
            title=getattr(ctx.task, "title", None),
            description=getattr(ctx.task, "description", None),
            completion_evidence={
                "summary_generated": True,
                "execution_results_count": len(state.execution_results),
                "reported_changed_files": list(state.changed_files),
                "candidate_delta_required": True,
                "change_set": change_set,
                "run_candidate_checks": False,
                "include_static_checks": False,
                "grounded_verification": verification,
            },
            validation_severity=ctx.validation_severity,
            workflow_stage=getattr(ctx.task, "workflow_stage", None),
            is_first_ordered_task=bool(getattr(ctx.task, "plan_position", None) == 1),
            accepted_path_authority=authority,
            require_accepted_path_authority=True,
        )
        completion.candidate_identity = candidate_delta_identity(
            change_set, project_dir=Path(state.project_dir)
        )
        record_validation_verdict(
            ctx.db, ctx.session_id, ctx.task_id, state, completion
        )
        if not completion.accepted:
            raise GroundedExecutionError(
                "VALIDATION_FAILED",
                "candidate_validation_rejected",
                "Candidate validation rejected the grounded workspace delta",
                evidence={"status": completion.status},
                partial_work=bool(state.changed_files),
            )
        if verification.get("status") == "failed":
            raise GroundedExecutionError(
                "VALIDATION_FAILED",
                "grounded_verification_failed",
                "Safe grounded verification failed",
                evidence={"kind": verification.get("kind"), "status": "failed"},
                partial_work=bool(state.changed_files),
            )
        envelope["candidate_binding"] = {
            "task_execution_id": ctx.task_execution_id,
            "accepted_plan_identity": envelope.get("accepted_plan_identity"),
            "accepted_path_authority_identity": authority.authority_identity,
            "workspace_identity": str(Path(state.project_dir).resolve()),
            "changed_files": list(state.changed_files),
            "candidate_identity": completion.candidate_identity,
        }
        envelope["verification_evidence"] = verification
        envelope["partial_work"] = False
        state.status = type(state.status).DONE
        ctx.task.current_step = len(state.plan)
        ctx.task.summary = "Provider-free grounded candidate validated"
        ctx.task.workspace_status = "ready"
        mark_task_attempt_done(
            task=ctx.task,
            session_task_link=ctx.session_task_link,
            task_execution=ctx.db.query(TaskExecution)
            .filter_by(id=ctx.task_execution_id)
            .first(),
            completed_at=datetime.now(UTC),
        )
        mark_session_completed(ctx.session, completed_at=datetime.now(UTC))
        append_orchestration_event(
            project_dir=state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type="task_completed",
            details={
                "execution_kind": GROUNDED_EXECUTION_KIND,
                "steps_completed": len(state.plan),
                "publication_status": "not_published",
            },
        )
        save_orchestration_checkpoint(
            ctx.db, ctx.session_id, ctx.task_id, ctx.prompt, state
        )
        ctx.db.commit()
        return {
            "status": "completed",
            "public_state": "SUCCEEDED",
            "task_id": ctx.task_id,
            "session_id": ctx.session_id,
            "task_execution_id": ctx.task_execution_id,
            "steps_completed": len(state.plan),
            "candidate_identity": completion.candidate_identity,
            "publication_status": "not_published",
            "canonical_baseline_mutated": False,
            "verification": verification,
        }
    except GroundedExecutionError as exc:
        return grounded_failure_result(ctx=ctx, error=exc)
    except Exception as exc:
        return grounded_failure_result(
            ctx=ctx,
            error=GroundedExecutionError(
                "EXECUTION_FAILED",
                "grounded_completion_failed",
                "Grounded completion failed closed",
                partial_work=bool(state.changed_files),
            ),
        )


__all__ = [
    "GROUNDED_EXECUTION_KIND",
    "GROUNDED_EXECUTION_PROFILE",
    "GroundedExecutionError",
    "GroundedRuntimeService",
    "admit_grounded_submission",
    "complete_grounded_task",
    "grounded_failure_result",
    "revalidate_grounded_step",
    "run_grounded_verification",
]
