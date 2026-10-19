from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api.v1.endpoints import project_logs
from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    TaskExecutionChangeSet,
)
from app.services.observability.log_stream import LogStreamService
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.invocation_provenance import (
    CORRELATED,
    INCONCLUSIVE,
    MISMATCH,
    REQUIRED_LOADED_MODULES,
    InvocationProvenanceReference,
    attach_invocation_provenance,
    bind_invocation_reference,
    build_invocation_projection,
    current_invocation_reference,
    persist_invocation_projection,
    reset_invocation_reference,
)
from app.tasks import worker
from app.services.orchestration.state import persistence
from app.services.workspace.changeset_service import ChangesetService


_SOURCE_DIGEST = "c" * 64
_MODULE_DIGEST = "e" * 64
_BOOT_DIGEST = "d" * 64
_HEAD = "a" * 40
_TREE = "b" * 40
_BOOT_ID = "11111111-2222-3333-4444-555555555555"


def _identity_inputs(*, drift: bool = False, missing: bool = False) -> dict:
    module_source = "f" * 64 if drift else _MODULE_DIGEST
    boot = {
        "record_id": f"{_BOOT_ID}-101-500",
        "identity_status": "IDENTITY_VERIFIED",
        "worker_pid": 101,
        "worker_start_ticks": "500",
        "linux_boot_id": _BOOT_ID,
        "worker_node_identity": "celery@offline-test",
        "candidate_head": _HEAD,
        "candidate_tree": _TREE,
        "source_manifest_digest": _SOURCE_DIGEST,
        "affected_module_provenance": [
            {
                "module_name": name,
                "expected_source_sha256": _MODULE_DIGEST,
                "loaded": True,
            }
            for name in REQUIRED_LOADED_MODULES
        ],
        "provider_response": "synthetic response must never enter projection",
    }
    process = {
        "pid": 101,
        "ppid": 1,
        "linux_boot_id": _BOOT_ID,
        "start_ticks": "500",
    }
    source = {
        "candidate_head": _HEAD if not drift else "9" * 40,
        "candidate_tree": _TREE,
        "source_manifest_digest": _SOURCE_DIGEST,
    }
    modules = [
        {
            "module_name": name,
            "source_sha256": module_source,
            "loaded": True,
        }
        for name in REQUIRED_LOADED_MODULES
    ]
    if missing:
        boot = None
        source = None
        modules = None
        process = None
    return {
        "project_id": 17,
        "task_id": 29,
        "session_id": 31,
        "task_execution_id": 43,
        "celery_request_id": str(uuid.uuid4()),
        "celery_retry_count": 0,
        "delivery_redelivered": False,
        "invocation_started_at": datetime.now(UTC),
        "worker_boot_manifest": boot,
        "worker_boot_manifest_digest": _BOOT_DIGEST if boot else None,
        "worker_process": process,
        "current_source_manifest": source,
        "current_loaded_modules": modules,
    }


def _build(**overrides):
    values = _identity_inputs()
    values.update(overrides)
    return build_invocation_projection(**values)


def _reference(invocation_id: str | None = None) -> InvocationProvenanceReference:
    invocation_id = invocation_id or uuid.uuid4().hex
    return InvocationProvenanceReference(
        invocation_attempt_id=invocation_id,
        projection_journal_reference=invocation_id,
        worker_boot_manifest_id=f"{_BOOT_ID}-101-500",
        worker_boot_manifest_digest=_BOOT_DIGEST,
        attempt_number=1,
        classification=CORRELATED,
    )


def _event_details(project_dir, session_id=31, task_id=29):
    path = persistence._orchestration_event_log_path(
        project_dir, session_id, task_id, project_id=17
    )
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_valid_boot_to_invocation_chain_and_retry_attempts_are_distinct(tmp_path):
    first_projection = _build()
    assert first_projection["classification"] == CORRELATED
    first = persist_invocation_projection(
        project_dir=tmp_path,
        project_id=17,
        session_id=31,
        task_id=29,
        projection=first_projection,
    )
    assert first.classification == CORRELATED
    assert first.projection_journal_reference == first.invocation_attempt_id
    assert first.attempt_number == 1
    assert first_projection["source_identity"]["loaded_code_identity_status"] == (
        "UNAVAILABLE"
    )

    retry_projection = _build(
        task_execution_id=43,
        celery_request_id=first_projection["celery_request_id"],
        celery_retry_count=1,
        delivery_redelivered=True,
    )
    retry = persist_invocation_projection(
        project_dir=tmp_path,
        project_id=17,
        session_id=31,
        task_id=29,
        projection=retry_projection,
    )
    assert retry.invocation_attempt_id != first.invocation_attempt_id
    assert retry.attempt_number == 2

    separate_execution = persist_invocation_projection(
        project_dir=tmp_path,
        project_id=17,
        session_id=31,
        task_id=29,
        projection=_build(task_execution_id=44),
    )
    assert separate_execution.invocation_attempt_id not in {
        first.invocation_attempt_id,
        retry.invocation_attempt_id,
    }
    assert separate_execution.attempt_number == 1
    rows = _event_details(tmp_path)
    projections = [row for row in rows if row["event_type"] == "invocation_projection"]
    assert [row["details"]["attempt_number"] for row in projections] == [1, 2, 1]


def test_duplicate_projection_is_mismatch_and_never_overwrites(tmp_path):
    projection = _build()
    first = persist_invocation_projection(
        project_dir=tmp_path,
        project_id=17,
        session_id=31,
        task_id=29,
        projection=projection,
    )
    duplicate = persist_invocation_projection(
        project_dir=tmp_path,
        project_id=17,
        session_id=31,
        task_id=29,
        projection=projection,
    )
    assert first.projection_journal_reference is not None
    assert duplicate.classification == MISMATCH
    assert duplicate.projection_journal_reference is None
    assert (
        len(
            [
                row
                for row in _event_details(tmp_path)
                if row["event_type"] == "invocation_projection"
            ]
        )
        == 1
    )


def test_missing_boot_or_execution_identity_remains_inconclusive():
    missing_boot = _build(**_identity_inputs(missing=True))
    assert missing_boot["classification"] == INCONCLUSIVE
    assert "worker_boot_manifest_unavailable" in missing_boot["classification_reasons"]

    missing_execution = _build(task_execution_id=None)
    assert missing_execution["classification"] == INCONCLUSIVE
    assert "task_execution_id_missing" in missing_execution["classification_reasons"]

    missing_request = _build(celery_request_id="unreviewed free-form request")
    assert missing_request["classification"] == INCONCLUSIVE
    assert missing_request["celery_request_id"] is None

    inconclusive_boot = _build(
        worker_boot_manifest={
            **_identity_inputs()["worker_boot_manifest"],
            "identity_status": "IDENTITY_INCONCLUSIVE",
        }
    )
    assert inconclusive_boot["classification"] == INCONCLUSIVE

    unloaded_at_boot = _identity_inputs()
    unloaded_at_boot["worker_boot_manifest"] = {
        **unloaded_at_boot["worker_boot_manifest"],
        "affected_module_provenance": [
            {
                "module_name": item["module_name"],
                "expected_source_sha256": item["expected_source_sha256"],
                "loaded": item["module_name"] != REQUIRED_LOADED_MODULES[0],
            }
            for item in unloaded_at_boot["worker_boot_manifest"][
                "affected_module_provenance"
            ]
        ],
    }
    unloaded_projection = build_invocation_projection(**unloaded_at_boot)
    assert unloaded_projection["classification"] == INCONCLUSIVE


def test_source_and_loaded_module_drift_classify_as_mismatch():
    drifted = _build(**_identity_inputs(drift=True))
    assert drifted["classification"] == MISMATCH
    assert "source_manifest_drift" in drifted["classification_reasons"]
    assert "loaded_module_source_mismatch" in drifted["classification_reasons"]


def test_projection_contains_no_free_form_or_sensitive_source_values():
    projection = _build(
        celery_request_id="sk-test-secret-shaped-request-id",
        worker_boot_manifest={
            **_identity_inputs()["worker_boot_manifest"],
            "assistant_response": "synthetic assistant secret canary",
            "environment": {"API_KEY": "synthetic-key-canary"},
        },
    )
    serialized = json.dumps(projection)
    assert "synthetic assistant secret canary" not in serialized
    assert "synthetic-key-canary" not in serialized
    assert "sk-test-secret-shaped-request-id" not in serialized
    assert projection["celery_request_id"] is None
    assert "required_loaded_module_identities" in projection


def test_optional_projection_persistence_failure_is_inconclusive(monkeypatch, tmp_path):
    monkeypatch.setattr(
        persistence,
        "append_invocation_projection_event",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("synthetic journal failure")),
        raising=False,
    )
    reference = persist_invocation_projection(
        project_dir=tmp_path,
        project_id=17,
        session_id=31,
        task_id=29,
        projection=_build(),
    )
    assert reference.classification == INCONCLUSIVE
    assert reference.projection_journal_reference is None
    assert reference.invocation_attempt_id


def test_interrupted_initialization_binds_an_inconclusive_attempt(monkeypatch):
    monkeypatch.setattr(
        worker,
        "_invocation_worker_evidence",
        lambda: (None, None, None, None, None),
    )
    celery_task = SimpleNamespace(
        request=SimpleNamespace(
            id=str(uuid.uuid4()),
            retries=0,
            delivery_info={"redelivered": False},
        )
    )
    projection, token = worker._start_task_invocation_provenance(
        celery_task=celery_task,
        project_id=None,
        session_id=31,
        task_id=29,
        task_execution_id=None,
        project_dir=None,
        invocation_started_at=datetime.now(UTC),
    )
    try:
        assert projection["classification"] == INCONCLUSIVE
        assert current_invocation_reference().classification == INCONCLUSIVE
        assert current_invocation_reference().projection_journal_reference is None
    finally:
        worker._reset_invocation_reference(token)


def test_evaluator_completion_and_publication_events_share_projection_reference(
    tmp_path,
):
    reference = _reference()
    token = bind_invocation_reference(reference)
    try:
        evaluator = persistence.append_orchestration_event(
            project_dir=tmp_path,
            project_id=17,
            session_id=31,
            task_id=29,
            event_type=EventType.EVALUATOR_RESULT,
            details={"verdict": "pass"},
        )
        completion = persistence.append_orchestration_event(
            project_dir=tmp_path,
            project_id=17,
            session_id=31,
            task_id=29,
            event_type=EventType.COMPLETION_EVIDENCE_FAILED,
            details={"decision": "held_for_review"},
        )
        publication = persistence.append_orchestration_event(
            project_dir=tmp_path,
            project_id=17,
            session_id=31,
            task_id=29,
            event_type="changeset_publication_evidence",
            details={"publication_eligible": False},
        )
    finally:
        reset_invocation_reference(token)
    for event in (evaluator, completion, publication):
        assert event["details"]["invocation_provenance"]["invocation_attempt_id"] == (
            reference.invocation_attempt_id
        )


def test_verification_evidence_keeps_same_reference_and_existing_evidence(tmp_path):
    reference = _reference()
    token = bind_invocation_reference(reference)
    try:
        filename = persistence.append_verification_execution_evidence(
            project_dir=tmp_path,
            project_id=17,
            session_id=31,
            task_id=29,
            task_execution_id=43,
            evidence={"process_exit_code": 0, "stdout_byte_count": 0},
        )
    finally:
        reset_invocation_reference(token)
    evidence_file = next((tmp_path / ".agent" / "events").glob(filename))
    evidence = json.loads(evidence_file.read_text())
    assert evidence["process_exit_code"] == 0
    assert evidence["invocation_provenance"]["invocation_attempt_id"] == (
        reference.invocation_attempt_id
    )


def test_changeset_capture_and_review_decision_keep_reference(tmp_path):
    reference = _reference()
    token = bind_invocation_reference(reference)

    class Query:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def first(self):
            return self.rows[0] if self.rows else None

    class Db:
        def __init__(self):
            self.added = []
            self.change_record = SimpleNamespace(
                review_decision={}, disposition_metadata={}
            )

        def query(self, entity):
            return Query(
                [self.change_record] if entity is TaskExecutionChangeSet else []
            )

        def add(self, item):
            self.added.append(item)

        def commit(self):
            pass

    try:
        db = Db()
        service = object.__new__(ChangesetService)
        service.db = db
        change_set = {
            "schema": "openclaw.task_execution_change_set.v1",
            "project_id": 17,
            "task_id": 29,
            "task_execution_id": 43,
            "snapshot_key": "snapshot-43",
            "target_path": str(tmp_path / "not-created"),
            "status": "done",
            "added_files": [],
            "modified_files": [],
            "deleted_files": [],
        }
        service.build_task_execution_change_set = lambda *_args, **_kwargs: dict(
            change_set
        )
        service.upsert_record = lambda **_kwargs: None
        service.persist_task_execution_change_set(
            SimpleNamespace(id=17),
            SimpleNamespace(id=29),
            session_id=31,
            task_execution_id=43,
            snapshot_key="snapshot-43",
            commit=False,
        )
        stored_log = json.loads(db.added[0].log_metadata)
        assert stored_log["invocation_provenance"]["invocation_attempt_id"] == (
            reference.invocation_attempt_id
        )

        service.update_task_execution_change_set_review_decision(
            task_execution_id=43,
            review_decision={"outcome": "accepted", "publication_eligible": True},
            commit=False,
        )
        assert db.change_record.review_decision["publication_eligible"] is True
        assert (
            db.change_record.review_decision["invocation_provenance"][
                "invocation_attempt_id"
            ]
            == reference.invocation_attempt_id
        )
    finally:
        reset_invocation_reference(token)


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def join(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


def test_project_log_api_serializes_safe_compact_invocation_metadata(monkeypatch):
    reference = _reference()
    token = bind_invocation_reference(reference)
    try:
        added = []

        class Db:
            def add(self, row):
                added.append(row)

            def commit(self):
                pass

        persistence.record_live_log(
            Db(),
            session_id=31,
            task_id=29,
            level="INFO",
            message="safe operational event",
            metadata={"event": "execution_started"},
        )
    finally:
        reset_invocation_reference(token)

    row = added[0]
    row.id = 4
    row.session_instance_id = "session-instance-31"
    row.created_at = datetime.now(UTC)
    project = SimpleNamespace(id=17, name="offline-project")

    class DbForApi:
        def query(self, entity):
            if entity is Project:
                return _Query([project])
            if entity is SessionModel.id:
                return _Query([(31,)])
            if entity is LogEntry:
                return _Query([row])
            raise AssertionError(f"unexpected query entity: {entity!r}")

    monkeypatch.setattr(
        LogStreamService,
        "get_project_logs_summary",
        lambda _self, _project_id: {"total_logs": 1, "by_level": {"INFO": 1}},
    )
    result = project_logs.get_project_logs(project_id=17, db=DbForApi(), limit=10)
    encoded = json.dumps(result)
    assert reference.invocation_attempt_id in encoded
    assert "synthetic assistant" not in encoded
    assert (
        result["logs"][0]["metadata"]["invocation_provenance"]["classification"]
        == CORRELATED
    )


def test_historical_events_remain_compatible_without_invocation_metadata(tmp_path):
    event = persistence.append_orchestration_event(
        project_dir=tmp_path,
        project_id=17,
        session_id=31,
        task_id=29,
        event_type="phase_started",
        details={"phase": "planning"},
    )
    assert "invocation_provenance" not in event["details"]
    assert _event_details(tmp_path)[0]["event_type"] == "phase_started"


def test_verdict_and_publication_decision_fields_are_not_rewritten():
    reference = _reference()
    token = bind_invocation_reference(reference)
    try:
        original = {
            "verdict": "pass",
            "outcome": "accepted",
            "publication_eligible": True,
            "automatic_promotion": False,
        }
        projected = attach_invocation_provenance(original)
    finally:
        reset_invocation_reference(token)
    for key, value in original.items():
        assert projected[key] == value
    assert projected["invocation_provenance"]["classification"] == CORRELATED


def test_conflicting_existing_reference_is_explicitly_mismatched():
    current = _reference()
    other = _reference()
    payload = attach_invocation_provenance(
        {"invocation_provenance": other.to_dict()}, reference=current
    )
    assert payload["invocation_provenance_conflict"] is True
    assert payload["invocation_provenance"]["classification"] == MISMATCH
    assert payload["invocation_provenance"]["invocation_attempt_id"] == (
        current.invocation_attempt_id
    )
