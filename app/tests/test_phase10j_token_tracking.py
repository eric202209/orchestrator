"""Phase 10J-c — Token Usage Tracking tests.

Covers:
- tokens_in, tokens_out, token_source fields on TaskExecution (model persistence)
- RuntimeBackendResult defaults tokens to None
- runtime_result_from_mapping extracts usage.input_tokens / usage.output_tokens
- runtime_result_from_mapping extracts prompt_tokens / completion_tokens (alt naming)
- runtime_result_from_mapping leaves tokens None when usage absent
- _persist_runtime_backend_result writes token fields to TaskExecution
- _persist_runtime_backend_result emits [TOKEN_USAGE_RECORDED] LogEntry when tokens present
- [TOKEN_USAGE_RECORDED] LogEntry metadata contains expected fields
- session_instance_id propagated to LogEntry
- No LogEntry written when no tokens on result
- Existing task execution flow unaffected when tokens absent
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.agents.interfaces import RuntimeBackendResult
from app.services.agents.runtime_adapters.base import runtime_result_from_mapping
from app.services.orchestration.phases.execution_loop import (
    _persist_runtime_backend_result,
)


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def project(db_session: Session) -> Project:
    p = Project(name="token-tracking-project", workspace_path=None)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def session(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="token-tracking-session",
        status="running",
        is_active=True,
        instance_id="token-test-instance-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def task(db_session: Session, project: Project) -> Task:
    t = Task(
        project_id=project.id,
        title="token-tracking-task",
        status=TaskStatus.RUNNING,
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


def _make_execution(
    db: Session,
    session: SessionModel,
    task: Task,
    *,
    attempt_number: int = 1,
) -> TaskExecution:
    ex = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=attempt_number,
        status=TaskStatus.RUNNING,
    )
    db.add(ex)
    db.commit()
    db.refresh(ex)
    return ex


def _make_result(
    *,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    token_source: str | None = None,
) -> RuntimeBackendResult:
    return RuntimeBackendResult(
        backend_id="test-backend",
        role="execution",
        success=True,
        exit_reason="completed",
        output="some output",
        duration_seconds=1.0,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        token_source=token_source,
    )


# ── 1. Model-level field persistence ─────────────────────────────────────────


class TestTokenFieldModelPersistence:
    def test_token_fields_persist(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task)
        ex.tokens_in = 100
        ex.tokens_out = 200
        ex.token_source = "openai_usage"
        db_session.commit()

        fetched = (
            db_session.query(TaskExecution).filter(TaskExecution.id == ex.id).one()
        )
        assert fetched.tokens_in == 100
        assert fetched.tokens_out == 200
        assert fetched.token_source == "openai_usage"

    def test_token_fields_null_by_default(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task)
        fetched = (
            db_session.query(TaskExecution).filter(TaskExecution.id == ex.id).one()
        )
        assert fetched.tokens_in is None
        assert fetched.tokens_out is None
        assert fetched.token_source is None


# ── 2. RuntimeBackendResult defaults ─────────────────────────────────────────


class TestRuntimeBackendResultDefaults:
    def test_tokens_default_none(self):
        result = RuntimeBackendResult(
            backend_id="x",
            role="execution",
            success=True,
            exit_reason="completed",
            output="",
            duration_seconds=0.0,
        )
        assert result.tokens_in is None
        assert result.tokens_out is None
        assert result.token_source is None

    def test_tokens_accepted_when_provided(self):
        result = RuntimeBackendResult(
            backend_id="x",
            role="execution",
            success=True,
            exit_reason="completed",
            output="",
            duration_seconds=0.0,
            tokens_in=50,
            tokens_out=80,
            token_source="openai_usage",
        )
        assert result.tokens_in == 50
        assert result.tokens_out == 80
        assert result.token_source == "openai_usage"


# ── 3. runtime_result_from_mapping extraction ─────────────────────────────────


class TestRuntimeResultFromMappingTokens:
    def test_extracts_input_output_tokens(self):
        result = runtime_result_from_mapping(
            {
                "status": "completed",
                "output": "hello",
                "usage": {"input_tokens": 123, "output_tokens": 456},
            },
            backend_id="openai",
            role="execution",
        )
        assert result.tokens_in == 123
        assert result.tokens_out == 456
        assert result.token_source == "openai_usage"

    def test_extracts_prompt_completion_tokens(self):
        result = runtime_result_from_mapping(
            {
                "status": "completed",
                "output": "hello",
                "usage": {"prompt_tokens": 77, "completion_tokens": 33},
            },
            backend_id="openai",
            role="execution",
        )
        assert result.tokens_in == 77
        assert result.tokens_out == 33
        assert result.token_source == "openai_usage"

    def test_tokens_none_when_usage_absent(self):
        result = runtime_result_from_mapping(
            {"status": "completed", "output": "hello"},
            backend_id="local",
            role="execution",
        )
        assert result.tokens_in is None
        assert result.tokens_out is None
        assert result.token_source is None

    def test_tokens_none_when_usage_not_dict(self):
        result = runtime_result_from_mapping(
            {"status": "completed", "output": "hello", "usage": "bad"},
            backend_id="local",
            role="execution",
        )
        assert result.tokens_in is None
        assert result.tokens_out is None

    def test_partial_usage_only_tokens_in(self):
        result = runtime_result_from_mapping(
            {
                "status": "completed",
                "output": "hello",
                "usage": {"input_tokens": 10},
            },
            backend_id="openai",
            role="execution",
        )
        assert result.tokens_in == 10
        assert result.tokens_out is None
        assert result.token_source == "openai_usage"


# ── 4. _persist_runtime_backend_result writes tokens and LogEntry ──────────────


class TestPersistRuntimeBackendResultTokens:
    def test_token_fields_written_to_task_execution(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task)
        result = _make_result(
            tokens_in=150, tokens_out=300, token_source="openai_usage"
        )

        _persist_runtime_backend_result(db_session, ex.id, result)
        db_session.commit()

        fetched = (
            db_session.query(TaskExecution).filter(TaskExecution.id == ex.id).one()
        )
        assert fetched.tokens_in == 150
        assert fetched.tokens_out == 300
        assert fetched.token_source == "openai_usage"

    def test_log_entry_emitted_when_tokens_present(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task)
        result = _make_result(
            tokens_in=100, tokens_out=200, token_source="openai_usage"
        )

        _persist_runtime_backend_result(db_session, ex.id, result)
        db_session.commit()

        log = (
            db_session.query(LogEntry)
            .filter(LogEntry.message == "[TOKEN_USAGE_RECORDED]")
            .first()
        )
        assert log is not None

    def test_log_entry_level_is_info(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task)
        result = _make_result(tokens_in=10, tokens_out=20, token_source="openai_usage")

        _persist_runtime_backend_result(db_session, ex.id, result)
        db_session.commit()

        log = (
            db_session.query(LogEntry)
            .filter(LogEntry.message == "[TOKEN_USAGE_RECORDED]")
            .first()
        )
        assert log.level == "INFO"

    def test_log_entry_metadata_contains_token_counts(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task)
        result = _make_result(
            tokens_in=111, tokens_out=222, token_source="openai_usage"
        )

        _persist_runtime_backend_result(db_session, ex.id, result)
        db_session.commit()

        log = (
            db_session.query(LogEntry)
            .filter(LogEntry.message == "[TOKEN_USAGE_RECORDED]")
            .first()
        )
        meta = json.loads(log.log_metadata)
        assert meta["tokens_in"] == 111
        assert meta["tokens_out"] == 222
        assert meta["token_source"] == "openai_usage"
        assert meta["task_execution_id"] == ex.id

    def test_log_entry_session_instance_id_propagated(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task)
        result = _make_result(tokens_in=5, tokens_out=10, token_source="openai_usage")

        _persist_runtime_backend_result(db_session, ex.id, result)
        db_session.commit()

        log = (
            db_session.query(LogEntry)
            .filter(LogEntry.message == "[TOKEN_USAGE_RECORDED]")
            .first()
        )
        assert log.session_instance_id == "token-test-instance-uuid"

    def test_no_log_entry_when_no_tokens(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task)
        result = _make_result()

        _persist_runtime_backend_result(db_session, ex.id, result)
        db_session.commit()

        log = (
            db_session.query(LogEntry)
            .filter(LogEntry.message == "[TOKEN_USAGE_RECORDED]")
            .first()
        )
        assert log is None

    def test_existing_fields_still_written_when_tokens_absent(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task)
        result = _make_result()

        _persist_runtime_backend_result(db_session, ex.id, result)
        db_session.commit()

        fetched = (
            db_session.query(TaskExecution).filter(TaskExecution.id == ex.id).one()
        )
        assert fetched.backend_id == "test-backend"

    def test_log_entry_emitted_when_only_tokens_out_present(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task)
        result = _make_result(tokens_out=50, token_source="openai_usage")

        _persist_runtime_backend_result(db_session, ex.id, result)
        db_session.commit()

        log = (
            db_session.query(LogEntry)
            .filter(LogEntry.message == "[TOKEN_USAGE_RECORDED]")
            .first()
        )
        assert log is not None

    def test_persist_noop_when_execution_id_none(self, db_session: Session):
        result = _make_result(tokens_in=10, tokens_out=20)
        _persist_runtime_backend_result(db_session, None, result)

    def test_persist_noop_when_result_none(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task)
        _persist_runtime_backend_result(db_session, ex.id, None)
