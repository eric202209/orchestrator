"""Bounded, fail-closed operation-level Planning repair contract."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from app.services.orchestration.planning.operation_repair_anchors import (
    MAX_ANCHORS_PER_OPERATION,
    SourceAnchor,
    derive_operation_anchors,
)
from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_EXISTING,
    SOURCE_STATUS_NEW,
    materialized_source_file,
)
from app.services.orchestration.planning.source_operation_verification import (
    resolve_version_fenced_source,
    verify_replace_operation,
)


MAX_REJECTED_OPERATIONS = 3
MAX_OPERATION_REPAIR_PROMPT_CHARS = 16_000
MAX_OPERATION_REPAIR_RESPONSE_CHARS = 12_000
MAX_OPERATION_REPAIR_TASK_CONSTRAINT_CHARS = 2_000

RelativePath = Annotated[str, StringConstraints(min_length=1, max_length=500)]
OperationText = Annotated[str, StringConstraints(max_length=8_000)]
AnchorId = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class OperationRepairError(ValueError):
    """The bounded repair contract failed closed."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WriteFileOperation(_StrictModel):
    op: Literal["write_file"]
    path: RelativePath
    content: OperationText


class AppendFileOperation(_StrictModel):
    op: Literal["append_file"]
    path: RelativePath
    content: OperationText


class ReplaceInFileOperation(_StrictModel):
    op: Literal["replace_in_file"]
    path: RelativePath
    old: Annotated[str, StringConstraints(min_length=1, max_length=8_000)]
    new: OperationText


class DeleteFileOperation(_StrictModel):
    op: Literal["delete_file"]
    path: RelativePath


ReplacementOperation = Annotated[
    Union[
        WriteFileOperation,
        AppendFileOperation,
        ReplaceInFileOperation,
        DeleteFileOperation,
    ],
    Field(discriminator="op"),
]


class OperationRepairEntry(_StrictModel):
    step_number: StrictInt = Field(ge=1)
    operation_index: StrictInt = Field(ge=1)
    replacement_operation: ReplacementOperation


class AnchoredRepairEntry(_StrictModel):
    """A ``replace_in_file`` repair that cites an Orchestrator-owned anchor.

    There is deliberately no ``old`` field anywhere in this model.  Combined
    with ``extra="forbid"``, that makes provider-authored anchor text
    unrepresentable rather than merely discouraged.
    """

    step_number: StrictInt = Field(ge=1)
    operation_index: StrictInt = Field(ge=1)
    anchor_id: AnchorId
    new: OperationText


RepairEntry = Union[AnchoredRepairEntry, OperationRepairEntry]


class OperationRepairResponse(_StrictModel):
    repairs: list[RepairEntry] = Field(min_length=1, max_length=MAX_REJECTED_OPERATIONS)


@dataclass(frozen=True)
class OperationRepairRoute:
    lane: Literal["operation_level", "complete_plan"]
    reason: str
    rejected_identities: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class OperationRepairMergeResult:
    plan: list[dict[str, Any]]
    validator_verdict: Any


def parse_operation_repair_response(response_text: str) -> OperationRepairResponse:
    """Parse the exact wrapper schema; reject prose, fences, duplicates and excess."""

    if not isinstance(response_text, str) or not response_text:
        raise OperationRepairError("operation repair response is empty")
    if len(response_text) > MAX_OPERATION_REPAIR_RESPONSE_CHARS:
        raise OperationRepairError("operation repair response exceeds bounded size")
    if response_text != response_text.strip():
        raise OperationRepairError("operation repair response has surrounding content")
    try:
        parsed = OperationRepairResponse.model_validate_json(response_text)
    except Exception as exc:
        raise OperationRepairError("invalid operation repair response schema") from exc
    identities = [
        (entry.step_number, entry.operation_index) for entry in parsed.repairs
    ]
    if len(identities) != len(set(identities)):
        raise OperationRepairError("duplicate operation repair identity")
    return parsed


def _coerce_finding(finding: Any) -> dict[str, Any]:
    if isinstance(finding, dict):
        return finding
    if hasattr(finding, "model_dump"):
        return finding.model_dump()
    raise OperationRepairError("operation finding is not structured")


def _finding_identity(finding: dict[str, Any]) -> tuple[int, int]:
    step_number = finding.get("step_number")
    operation_index = finding.get("operation_index")
    if (
        isinstance(step_number, bool)
        or not isinstance(step_number, int)
        or step_number < 1
        or isinstance(operation_index, bool)
        or not isinstance(operation_index, int)
        or operation_index < 1
    ):
        raise OperationRepairError("operation finding identity is incomplete")
    return step_number, operation_index


def select_operation_repair_route(
    *,
    findings: list[Any],
    source_materialization: Any,
    project_dir: Path | None = None,
    structural_failure: bool = False,
) -> OperationRepairRoute:
    """Choose the bounded lane only when every blocking finding is attributable."""

    if structural_failure:
        return OperationRepairRoute("complete_plan", "structural_plan_failure")
    if not findings:
        return OperationRepairRoute("complete_plan", "no_operation_findings")
    if len(findings) > MAX_REJECTED_OPERATIONS:
        return OperationRepairRoute("complete_plan", "operation_count_exceeds_bound")
    try:
        structured = [_coerce_finding(item) for item in findings]
        identities = tuple(_finding_identity(item) for item in structured)
    except OperationRepairError:
        return OperationRepairRoute("complete_plan", "unattributed_finding")
    if len(identities) != len(set(identities)):
        return OperationRepairRoute("complete_plan", "duplicate_operation_identity")
    for finding in structured:
        path = finding.get("relative_path")
        failure_code = finding.get("failure_code")
        source_version = finding.get("source_version_identity")
        visibility = finding.get("visibility")
        if not all(isinstance(value, str) and value for value in (path, failure_code)):
            return OperationRepairRoute("complete_plan", "unattributed_finding")
        if not isinstance(visibility, str) or not visibility:
            return OperationRepairRoute("complete_plan", "missing_source_evidence")
        record = materialized_source_file(source_materialization, path)
        if record is None:
            return OperationRepairRoute("complete_plan", "missing_source_evidence")
        if record.status == SOURCE_STATUS_EXISTING:
            if not source_version or record.version_identity != source_version:
                return OperationRepairRoute("complete_plan", "missing_source_evidence")
            if not isinstance(record.content, str):
                return OperationRepairRoute("complete_plan", "missing_source_evidence")
            if project_dir is None:
                return OperationRepairRoute("complete_plan", "missing_source_evidence")
            resolved = resolve_version_fenced_source(
                source_materialization, path, Path(project_dir)
            )
            if resolved.failure_code is not None:
                return OperationRepairRoute("complete_plan", "source_version_mismatch")
        elif record.status == SOURCE_STATUS_NEW:
            if not record.creation_authorized:
                return OperationRepairRoute("complete_plan", "missing_source_evidence")
        else:
            return OperationRepairRoute("complete_plan", "missing_source_evidence")
    return OperationRepairRoute("operation_level", "all_findings_bounded", identities)


@dataclass(frozen=True)
class OperationAnchorRegistry:
    """The complete set of anchors this repair request is allowed to cite."""

    by_id: dict[str, SourceAnchor]
    by_identity: dict[tuple[int, int], tuple[SourceAnchor, ...]]


def build_operation_anchor_registry(
    *,
    original_plan: list[dict[str, Any]],
    rejected_findings: list[Any],
    source_materialization: Any,
    project_dir: Path,
) -> OperationAnchorRegistry:
    """Derive every authorized anchor deterministically from the pinned source.

    The same registry is rebuilt when the response is merged.  Derivation is a
    pure function of the plan, the findings and the version-fenced file, so the
    rebuild is byte-identical or the version fence has already failed closed.
    """

    steps = {
        step.get("step_number"): step
        for step in original_plan
        if isinstance(step, dict)
    }
    by_id: dict[str, SourceAnchor] = {}
    by_identity: dict[tuple[int, int], tuple[SourceAnchor, ...]] = {}
    for raw_finding in rejected_findings:
        finding = _coerce_finding(raw_finding)
        identity = _finding_identity(finding)
        step_number, operation_index = identity
        step = steps.get(step_number)
        operations = step.get("ops") if isinstance(step, dict) else None
        if not isinstance(operations, list) or operation_index > len(operations):
            raise OperationRepairError("rejected operation identity does not exist")
        operation = operations[operation_index - 1]
        if not isinstance(operation, dict):
            raise OperationRepairError("rejected operation is not structured")
        path = finding.get("relative_path")
        if operation.get("path") != path:
            raise OperationRepairError("finding path does not match rejected operation")
        if operation.get("op") != "replace_in_file":
            by_identity[identity] = ()
            continue
        resolved = resolve_version_fenced_source(
            source_materialization, path, Path(project_dir)
        )
        if resolved.failure_code is not None:
            raise OperationRepairError(
                f"anchor source is not version fenced: {resolved.failure_code}"
            )
        if resolved.recorded_version_identity != finding.get("source_version_identity"):
            raise OperationRepairError("anchor source version mismatch")
        anchors = derive_operation_anchors(
            step_number=step_number,
            operation_index=operation_index,
            relative_path=path,
            version_identity=resolved.recorded_version_identity or "",
            original_old=operation.get("old"),
            original_new=operation.get("new"),
            full_source=resolved.full_content,
        )
        if not anchors:
            raise OperationRepairError(
                "no exact source anchor is derivable for the rejected operation"
            )
        by_identity[identity] = anchors
        for anchor in anchors:
            by_id[anchor.anchor_id] = anchor
    return OperationAnchorRegistry(by_id=by_id, by_identity=by_identity)


def build_operation_repair_prompt(
    *,
    task_constraints: str,
    original_plan: list[dict[str, Any]],
    rejected_findings: list[Any],
    source_materialization: Any,
    project_dir: Path,
) -> str:
    """Render only rejected operations, their findings and path-scoped R0 evidence."""

    del task_constraints  # The rejected operation carries the bounded task intent.
    route = select_operation_repair_route(
        findings=rejected_findings,
        source_materialization=source_materialization,
        project_dir=project_dir,
    )
    if route.lane != "operation_level":
        raise OperationRepairError(f"operation repair is ineligible: {route.reason}")
    registry = build_operation_anchor_registry(
        original_plan=original_plan,
        rejected_findings=rejected_findings,
        source_materialization=source_materialization,
        project_dir=project_dir,
    )
    steps = {step.get("step_number"): step for step in original_plan}
    items: list[dict[str, Any]] = []
    for raw_finding in rejected_findings:
        finding = _coerce_finding(raw_finding)
        step_number, operation_index = _finding_identity(finding)
        step = steps.get(step_number)
        operations = step.get("ops") if isinstance(step, dict) else None
        if not isinstance(operations, list) or operation_index > len(operations):
            raise OperationRepairError("rejected operation identity does not exist")
        operation = operations[operation_index - 1]
        if not isinstance(operation, dict):
            raise OperationRepairError("rejected operation is not structured")
        path = finding["relative_path"]
        if operation.get("path") != path:
            raise OperationRepairError("finding path does not match rejected operation")
        record = materialized_source_file(source_materialization, path)
        anchors = registry.by_identity.get((step_number, operation_index), ())
        # The rejected anchor is withheld: it is exactly the text the model
        # copied verbatim in Phase 32N-3C.  Only the intended change is shown.
        intent = {key: value for key, value in operation.items() if key != "old"}
        items.append(
            {
                "identity": {
                    "step_number": step_number,
                    "operation_index": operation_index,
                    "relative_path": path,
                },
                "authorized_anchors": [
                    {"anchor_id": anchor.anchor_id, "old": anchor.text}
                    for anchor in anchors
                ],
                "original_rejected_operation": intent,
                "validator_finding": finding,
                "source_evidence": {
                    "relative_path": record.relative_path,
                    "status": record.status,
                    "content": record.content,
                    "content_hash": record.content_hash,
                    "version_identity": record.version_identity,
                    "visibility": finding["visibility"],
                    "visible_text_verified": finding.get(
                        "visible_text_verified", False
                    ),
                    "full_file_same_version_verified": finding.get(
                        "full_file_same_version_verified", False
                    ),
                    "truncated": record.truncated,
                    "visible_spans": [span.to_dict() for span in record.spans],
                    "creation_authorized": record.creation_authorized,
                    "source_version_currently_verified": True,
                },
                "allowed_path": path,
                "allowed_operation_type": operation.get("op"),
            }
        )
    schema_repairs = []
    for item in items:
        original = item["original_rejected_operation"]
        identity_fields = {
            "step_number": item["identity"]["step_number"],
            "operation_index": item["identity"]["operation_index"],
        }
        if original["op"] == "replace_in_file":
            anchor_ids = [entry["anchor_id"] for entry in item["authorized_anchors"]]
            schema_repairs.append(
                {
                    **identity_fields,
                    "anchor_id": anchor_ids[0],
                    "new": "replacement text",
                }
            )
            continue
        replacement_example = {
            "op": original["op"],
            "path": original["path"],
        }
        if original["op"] in {"write_file", "append_file"}:
            replacement_example["content"] = "replacement content"
        schema_repairs.append(
            {**identity_fields, "replacement_operation": replacement_example}
        )
    schema = {"repairs": schema_repairs}
    envelope = {
        "instruction": (
            "Return only one bare JSON object with exactly the key repairs. "
            "Return one replacement for every rejected identity and no others. "
            "Do not reconstruct, paraphrase, widen or normalize source anchors. "
            "Select exactly one supplied anchor_id. "
            "Return replacement text only. "
            "Whitespace in the supplied source is authoritative."
        ),
        "task_constraints": (
            "Correct only the listed rejected operations. Preserve the intended "
            "change expressed by each original operation and do not change its "
            "identity, path, or operation type."
        )[:MAX_OPERATION_REPAIR_TASK_CONSTRAINT_CHARS],
        "rejected_operations": items,
        "response_schema": schema,
    }
    prompt = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(prompt) > MAX_OPERATION_REPAIR_PROMPT_CHARS:
        raise OperationRepairError("operation repair prompt exceeds bounded size")
    return prompt


def merge_operation_repairs(
    *,
    original_plan: list[dict[str, Any]],
    rejected_operations: list[Any],
    repairs: OperationRepairResponse,
    source_materialization: Any,
    project_dir: Path,
) -> list[dict[str, Any]]:
    """Replace exactly the rejected operations while preserving every other byte."""

    findings = [_coerce_finding(item) for item in rejected_operations]
    required = {_finding_identity(item): item for item in findings}
    if len(required) != len(findings):
        raise OperationRepairError("duplicate rejected operation identity")
    registry = build_operation_anchor_registry(
        original_plan=original_plan,
        rejected_findings=rejected_operations,
        source_materialization=source_materialization,
        project_dir=project_dir,
    )
    returned = {
        (entry.step_number, entry.operation_index): entry for entry in repairs.repairs
    }
    if set(returned) != set(required):
        missing = sorted(set(required).difference(returned))
        extra = sorted(set(returned).difference(required))
        raise OperationRepairError(
            f"repair identity mismatch: missing={missing} extra={extra}"
        )

    step_positions: dict[int, int] = {}
    for position, step in enumerate(original_plan):
        step_number = step.get("step_number") if isinstance(step, dict) else None
        if not isinstance(step_number, int) or step_number < 1:
            raise OperationRepairError("original plan has invalid step identity")
        if step_number in step_positions:
            raise OperationRepairError("original plan has duplicate step identity")
        step_positions[step_number] = position

    merged = copy.deepcopy(original_plan)
    original_canonical = json.dumps(
        original_plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    del original_canonical  # documents canonical-byte preservation authority below
    for identity, entry in returned.items():
        step_number, operation_index = identity
        if step_number not in step_positions:
            raise OperationRepairError("returned step identity does not exist")
        original_step = original_plan[step_positions[step_number]]
        operations = original_step.get("ops")
        if not isinstance(operations, list) or operation_index > len(operations):
            raise OperationRepairError("returned operation identity does not exist")
        original_operation = operations[operation_index - 1]
        if not isinstance(original_operation, dict):
            raise OperationRepairError("original operation is not structured")
        finding = required[identity]
        original_path = original_operation.get("path")
        anchor: SourceAnchor | None = None
        if original_operation.get("op") == "replace_in_file":
            if not isinstance(entry, AnchoredRepairEntry):
                raise OperationRepairError(
                    "replace_in_file repair must cite an authorized anchor_id"
                )
            anchor = registry.by_id.get(entry.anchor_id)
            if anchor is None:
                raise OperationRepairError("unknown repair anchor")
            if (anchor.step_number, anchor.operation_index) != identity:
                raise OperationRepairError("anchor belongs to another operation")
            if anchor.relative_path != original_path:
                raise OperationRepairError("anchor belongs to another path")
            if anchor.version_identity != finding.get("source_version_identity"):
                raise OperationRepairError("anchor source version mismatch")
            replacement = {
                "op": "replace_in_file",
                "path": anchor.relative_path,
                "old": anchor.text,
                "new": entry.new,
            }
        else:
            if isinstance(entry, AnchoredRepairEntry):
                raise OperationRepairError(
                    "anchored repair is only valid for replace_in_file"
                )
            replacement = entry.replacement_operation.model_dump()
        if finding.get("relative_path") != original_path:
            raise OperationRepairError("rejected finding path mismatch")
        if replacement.get("path") != original_path:
            raise OperationRepairError("replacement path changed")
        if replacement.get("op") != original_operation.get("op"):
            raise OperationRepairError("replacement operation type changed")
        record = materialized_source_file(source_materialization, original_path)
        if record is None:
            raise OperationRepairError("replacement path lacks source evidence")
        if record.version_identity != finding.get("source_version_identity"):
            raise OperationRepairError("pinned source version mismatch")
        resolved = None
        if record.status == SOURCE_STATUS_EXISTING:
            resolved = resolve_version_fenced_source(
                source_materialization, original_path, Path(project_dir)
            )
            if resolved.failure_code is not None:
                raise OperationRepairError(
                    f"pinned source version no longer matches: {resolved.failure_code}"
                )
        if replacement["op"] == "replace_in_file":
            verdict = verify_replace_operation(
                resolved,
                replacement["old"],
                relative_path=original_path,
                step_index=step_number,
                operation_index=operation_index,
            )
            if not verdict.verified:
                raise OperationRepairError(
                    f"replacement old text is not verified: {verdict.failure_code}"
                )
            # Re-check uniqueness against the freshly fenced read, not only
            # against the source the anchor was derived from.
            full_content = resolved.full_content if resolved is not None else None
            if full_content is None or full_content.count(replacement["old"]) != 1:
                raise OperationRepairError("anchor occurrence is ambiguous or absent")
        if replacement["op"] == "write_file" and record.status == SOURCE_STATUS_NEW:
            if not record.creation_authorized:
                raise OperationRepairError("new-file creation is not authorized")
        merged[step_positions[step_number]]["ops"][operation_index - 1] = replacement

    if [step.get("step_number") for step in merged] != [
        step.get("step_number") for step in original_plan
    ]:
        raise OperationRepairError("step ordering changed")
    for step_position, (original_step, merged_step) in enumerate(
        zip(original_plan, merged, strict=True), start=1
    ):
        original_ops = original_step.get("ops") or []
        merged_ops = merged_step.get("ops") or []
        if len(original_ops) != len(merged_ops):
            raise OperationRepairError("operation ordering changed")
        for operation_position, (original_op, merged_op) in enumerate(
            zip(original_ops, merged_ops, strict=True), start=1
        ):
            if (original_step["step_number"], operation_position) in required:
                continue
            if json.dumps(
                original_op, sort_keys=True, separators=(",", ":")
            ) != json.dumps(merged_op, sort_keys=True, separators=(",", ":")):
                raise OperationRepairError(
                    f"accepted operation changed at step {step_position} op {operation_position}"
                )
        original_without_ops = {k: v for k, v in original_step.items() if k != "ops"}
        merged_without_ops = {k: v for k, v in merged_step.items() if k != "ops"}
        if json.dumps(
            original_without_ops, sort_keys=True, separators=(",", ":")
        ) != json.dumps(merged_without_ops, sort_keys=True, separators=(",", ":")):
            raise OperationRepairError(
                f"step structure changed at step {step_position}"
            )
    return merged


def merge_and_validate_operation_repairs(
    *,
    original_plan: list[dict[str, Any]],
    rejected_findings: list[Any],
    response_text: str,
    source_materialization: Any,
    project_dir: Path,
    validate_complete_plan: Callable[[list[dict[str, Any]]], Any],
) -> OperationRepairMergeResult:
    parsed = parse_operation_repair_response(response_text)
    merged = merge_operation_repairs(
        original_plan=original_plan,
        rejected_operations=rejected_findings,
        repairs=parsed,
        source_materialization=source_materialization,
        project_dir=project_dir,
    )
    verdict = validate_complete_plan(merged)
    if not getattr(verdict, "accepted", False):
        raise OperationRepairError("merged complete plan failed validation")
    return OperationRepairMergeResult(plan=merged, validator_verdict=verdict)
