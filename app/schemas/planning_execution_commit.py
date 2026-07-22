"""Strict public schemas for the Phase 29B-2 execution-commit command."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Hash = Annotated[
    str,
    StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]


class ExecutionCommitSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionCommitRequestPayload(ExecutionCommitSchema):
    idempotency_key: str = Field(min_length=1, max_length=128)
    structured_task_plan_checkpoint_id: int = Field(gt=0)
    structured_task_plan_hash: Hash
    expected_session_generation_id: str = Field(min_length=1, max_length=256)
    expected_review_id: str | None = Field(default=None, max_length=128)
    expected_approval_event_id: str | None = Field(default=None, max_length=128)


class ExecutionCommitResponse(ExecutionCommitSchema):
    planning_session_id: int
    session_generation_id: str
    structured_task_plan_checkpoint_id: int
    structured_task_plan_hash: str
    review_id: str
    approval_event_id: str
    completion_manifest_id: int
    completion_manifest_hash: str
    planning_commit_manifest_id: int
    commit_identity: str
    boundary_state: str
    idempotent_replay: bool
    integrity_status: str
    execution_plan_id: int | None = None
    execution_plan_generation: int | None = None
    execution_plan_status: str | None = None
    task_count: int
    dependency_edge_count: int
    group_count: int
    group_membership_count: int
    execution_failure_reason: str | None = None


__all__ = [
    "ExecutionCommitRequestPayload",
    "ExecutionCommitResponse",
]
