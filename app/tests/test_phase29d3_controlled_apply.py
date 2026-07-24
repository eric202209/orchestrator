"""Focused Phase 29D-3 atomic Controlled Apply coverage."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.db_migrations import _migration_048_controlled_apply_result_authority
from app.models import Base, ExecutionTaskApplyResult
from app.services.execution.apply_execution import (
    ApplyExecutionService,
    ExecuteApplyCommand,
    verify_apply_result_integrity,
)
from app.services.execution.changeset import (
    ChangeSetIngestionService,
    IngestChangeSetCommand,
)
from app.services.execution.controlled_apply import (
    ApplyApprovalService,
    ApplyAttemptService,
    ApplyAuthorizationV2Service,
    AuthorizeApplyV2Command,
    CreateApplyApprovalCommand,
    CreateApplyAttemptCommand,
)
from app.services.execution.execution_evidence import (
    ExecutionEvidenceIngestionService,
    IngestExecutionEvidenceCommand,
)
from app.services.execution.candidate_content import LocalContentAddressedStore
from app.services.execution.workspace_authority import (
    WorkspaceBaseStateService,
    WorkspaceTargetService,
)
from app.services.workspace.project_mutation_lock import project_mutation_lock

from test_phase29d1_changeset_apply_authorization import (
    _accepted,
    _bypass_source_integrity,
    _fabricate_changeset_source_content,
)


def _fake_git(monkeypatch):
    def run_git(root, args):
        if args == ("rev-parse", "--show-toplevel"):
            return str(root).encode()
        if args == ("rev-parse", "HEAD"):
            return ("a" * 40).encode()
        if args == ("status", "--porcelain=v1", "-z"):
            return b""
        raise AssertionError(args)

    monkeypatch.setattr("app.services.execution.workspace_authority._run_git", run_git)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _evidence(db_session, task, created, store, *, content: bytes, key: str) -> str:
    result = ExecutionEvidenceIngestionService(db_session, store=store).ingest(
        IngestExecutionEvidenceCommand(
            execution_plan_id=task.execution_plan_id,
            execution_task_id=task.id,
            execution_task_attempt_id=created.attempt.id,
            attempt_generation=created.attempt.attempt_generation,
            evidence_kind="command",
            producer_id="command-runner",
            producer_version="phase29d3-test",
            content=content,
            media_type="application/octet-stream",
            ingestion_idempotency_key=key,
        )
    )
    return f"execution-evidence://{result.evidence.id}"


def _apply_authority(
    db_session, tmp_path, monkeypatch, *, operations, initial_files=None
):
    _fake_git(monkeypatch)
    root = tmp_path / "workspace"
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "src").mkdir()
    for relative, content in (initial_files or {}).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    monkeypatch.setattr(
        "app.services.execution.workspace_authority.get_effective_workspace_root",
        lambda db=None: tmp_path,
    )
    task, created, outcome, _decision, _ = _accepted(db_session)
    task_project = task.execution_plan.project
    task_project.workspace_path = "workspace"
    store = LocalContentAddressedStore(tmp_path / "content-store")
    evidence_refs = {}
    for key, content in {
        item["content_key"]: item["content"]
        for item in operations
        if item.get("content_key")
    }.items():
        evidence_refs[key] = _evidence(
            db_session, task, created, store, content=content, key=f"evidence-{key}"
        )
    canonical_operations = []
    for item in operations:
        operation = {
            key: evidence_refs.get(value, value)
            for key, value in item.items()
            if key not in {"content_key", "content"}
        }
        if item.get("content_key"):
            operation["content_reference"] = evidence_refs[item["content_key"]]
        canonical_operations.append(operation)
    change_set_payload = {
        "format": "orchestrator-changeset/1",
        "base_state": {"project_id": task_project.id},
        "operations": canonical_operations,
    }
    source = _fabricate_changeset_source_content(
        db_session,
        task,
        created,
        outcome,
        payload=change_set_payload,
        store=store,
        key=f"changeset-source-{task.id}",
    )
    _bypass_source_integrity(monkeypatch)
    change_set = (
        ChangeSetIngestionService(db_session, store=store)
        .ingest(
            IngestChangeSetCommand(
                execution_plan_id=task.execution_plan_id,
                execution_task_id=task.id,
                execution_task_attempt_id=created.attempt.id,
                attempt_generation=created.attempt.attempt_generation,
                candidate_outcome_id=outcome.id,
                acceptance_decision_id=_decision.id,
                source_candidate_content_id=source.id,
                ingestion_idempotency_key=f"changeset-{task.id}",
            )
        )
        .change_set
    )
    target = (
        WorkspaceTargetService(db_session)
        .register(task_project.id, registration_idempotency_key=f"target-{task.id}")
        .target
    )
    base_state = (
        WorkspaceBaseStateService(db_session)
        .inspect(
            workspace_target_id=target.id,
            change_set_id=change_set.id,
            observation_idempotency_key=f"base-{task.id}",
        )
        .base_state
    )
    approval = (
        ApplyApprovalService(db_session)
        .decide(
            CreateApplyApprovalCommand(
                change_set_id=change_set.id,
                workspace_target_id=target.id,
                base_state_id=base_state.id,
                decision="approved",
                reviewed_summary_payload={"operation_count": len(operations)},
                approval_idempotency_key=f"approval-{task.id}",
            )
        )
        .approval
    )
    authorization = (
        ApplyAuthorizationV2Service(db_session, store=store)
        .authorize(
            AuthorizeApplyV2Command(
                change_set_id=change_set.id,
                workspace_target_id=target.id,
                base_state_id=base_state.id,
                approval_id=approval.id,
                authorization_idempotency_key=f"authorization-{task.id}",
            )
        )
        .authorization
    )
    apply_attempt = (
        ApplyAttemptService(db_session)
        .create(
            CreateApplyAttemptCommand(
                authorization_id=authorization.id,
                approval_id=approval.id,
                apply_attempt_idempotency_key=f"apply-attempt-{task.id}",
            )
        )
        .apply_attempt
    )
    db_session.commit()
    return {
        "db": db_session,
        "root": root,
        "store": store,
        "task": task,
        "attempt": apply_attempt,
    }


def test_apply_create_replace_delete_is_atomic_and_replay_safe(
    db_session, tmp_path, monkeypatch
):
    old = b"old\n"
    removed = b"remove me\n"
    context = _apply_authority(
        db_session,
        tmp_path,
        monkeypatch,
        initial_files={"src/replace.txt": old, "src/remove.txt": removed},
        operations=[
            {
                "operation": "create_file",
                "path": "src/create.txt",
                "content_reference": "placeholder",
                "content_key": "create",
                "content": b"created\n",
            },
            {
                "operation": "replace_file",
                "path": "src/replace.txt",
                "expected_previous_sha256": _sha(old),
                "content_reference": "placeholder",
                "content_key": "replace",
                "content": b"replaced\n",
            },
            {
                "operation": "delete_file",
                "path": "src/remove.txt",
                "expected_previous_sha256": _sha(removed),
            },
        ],
    )
    service = ApplyExecutionService(db_session, store=context["store"])
    first = service.execute(ExecuteApplyCommand(context["attempt"].id))
    db_session.commit()

    assert first.result.status == "applied"
    assert (context["root"] / "src/create.txt").read_bytes() == b"created\n"
    assert (context["root"] / "src/replace.txt").read_bytes() == b"replaced\n"
    assert not (context["root"] / "src/remove.txt").exists()
    assert len(first.result.applied_operations) == 3
    assert verify_apply_result_integrity(db_session, first.result.id).verified

    replay = service.execute(ExecuteApplyCommand(context["attempt"].id))
    assert replay.replayed is True
    assert replay.result.id == first.result.id


def test_final_hash_drift_blocks_without_mutation(db_session, tmp_path, monkeypatch):
    original = b"before\n"
    context = _apply_authority(
        db_session,
        tmp_path,
        monkeypatch,
        initial_files={"src/file.txt": original},
        operations=[
            {
                "operation": "replace_file",
                "path": "src/file.txt",
                "expected_previous_sha256": _sha(original),
                "content_reference": "placeholder",
                "content_key": "replacement",
                "content": b"after\n",
            }
        ],
    )
    (context["root"] / "src/file.txt").write_bytes(b"drifted\n")

    result = (
        ApplyExecutionService(db_session, store=context["store"])
        .execute(ExecuteApplyCommand(context["attempt"].id))
        .result
    )
    assert result.status == "blocked"
    assert result.failure_reason == "hash_mismatch"
    assert (context["root"] / "src/file.txt").read_bytes() == b"drifted\n"


def test_missing_file_always_creates_blocked_result(db_session, tmp_path, monkeypatch):
    original = b"before\n"
    context = _apply_authority(
        db_session,
        tmp_path,
        monkeypatch,
        initial_files={"src/file.txt": original},
        operations=[
            {
                "operation": "replace_file",
                "path": "src/file.txt",
                "expected_previous_sha256": _sha(original),
                "content_reference": "placeholder",
                "content_key": "replacement",
                "content": b"after\n",
            }
        ],
    )
    (context["root"] / "src/file.txt").unlink()
    missing = (
        ApplyExecutionService(db_session, store=context["store"])
        .execute(ExecuteApplyCommand(context["attempt"].id))
        .result
    )
    assert missing.status == "blocked"
    assert missing.failure_reason == "missing_file"


def test_io_failure_is_failed_and_compensated(db_session, tmp_path, monkeypatch):
    original = b"before-again\n"
    context = _apply_authority(
        db_session,
        tmp_path / "io",
        monkeypatch,
        initial_files={"src/file.txt": original},
        operations=[
            {
                "operation": "replace_file",
                "path": "src/file.txt",
                "expected_previous_sha256": _sha(original),
                "content_reference": "placeholder",
                "content_key": "replacement-io",
                "content": b"after-again\n",
            }
        ],
    )
    import app.services.execution.apply_execution as apply_execution_module

    real_replace = apply_execution_module.os.replace
    failures_remaining = {"count": 1}

    def fail_target_replace(source, destination):
        if (
            Path(destination) == context["root"] / "src/file.txt"
            and failures_remaining["count"]
        ):
            failures_remaining["count"] -= 1
            raise OSError("injected rename failure")
        return real_replace(source, destination)

    monkeypatch.setattr(apply_execution_module.os, "replace", fail_target_replace)
    failed = (
        ApplyExecutionService(db_session, store=context["store"])
        .execute(ExecuteApplyCommand(context["attempt"].id))
        .result
    )
    assert failed.status == "failed"
    assert failed.failure_reason == "io_failure"
    assert (context["root"] / "src/file.txt").read_bytes() == original
    assert verify_apply_result_integrity(db_session, failed.id).verified


def test_concurrent_apply_attempt_times_out_and_creates_blocked_result(
    db_session, tmp_path, monkeypatch
):
    context = _apply_authority(
        db_session,
        tmp_path,
        monkeypatch,
        operations=[
            {
                "operation": "create_file",
                "path": "src/file.txt",
                "content_reference": "placeholder",
                "content_key": "concurrent",
                "content": b"content\n",
            }
        ],
    )
    with project_mutation_lock(
        project_id=context["attempt"].workspace_target.project_id,
        project_root=context["root"],
        operation="competing-apply",
        owner="test-holder",
        wait_timeout_seconds=0,
    ):
        result = (
            ApplyExecutionService(db_session, store=context["store"])
            .execute(
                ExecuteApplyCommand(context["attempt"].id, lock_wait_timeout_seconds=0)
            )
            .result
        )
    assert result.status == "blocked"
    assert result.failure_reason == "lock_timeout"
    assert not (context["root"] / "src/file.txt").exists()


def test_apply_result_cannot_be_modified_after_creation(
    db_session, tmp_path, monkeypatch
):
    context = _apply_authority(
        db_session,
        tmp_path,
        monkeypatch,
        operations=[
            {
                "operation": "create_file",
                "path": "src/file.txt",
                "content_reference": "placeholder",
                "content_key": "immutable",
                "content": b"content\n",
            }
        ],
    )
    result = (
        ApplyExecutionService(db_session, store=context["store"])
        .execute(ExecuteApplyCommand(context["attempt"].id))
        .result
    )
    db_session.commit()
    result.status = "failed"
    with pytest.raises(RuntimeError, match="immutable"):
        db_session.commit()
    db_session.rollback()
    assert db_session.get(ExecutionTaskApplyResult, result.id).status == "applied"


def test_phase29d3_migration_is_additive_and_replay_safe(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(engine)
    _migration_048_controlled_apply_result_authority(engine)
    _migration_048_controlled_apply_result_authority(engine)
    assert "execution_task_apply_results" in set(inspect(engine).get_table_names())
