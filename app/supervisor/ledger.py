"""Durable Supervisor attempt and OCI lifecycle ledger.

The stable attempt key owns one insert-only request identity.  Lifecycle state
is persisted before external runtime operations so restart recovery can inspect
the deterministic container name instead of guessing or relaunching completed
work.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

LEDGER_SCHEMA_VERSION = 2
FIXED_OPERATION_ID = "phase39-f3-oci-python/v1"
ACTIVE_STATUSES = frozenset({"ACCEPTED", "CREATING", "RUNNING"})
TERMINAL_STATUSES = frozenset(
    {"EXITED", "FAILED", "TIMED_OUT", "CANCELLED", "INTERRUPTED", "UNKNOWN"}
)

_SCHEMA = """
CREATE TABLE supervisor_attempts (
    attempt_idempotency_key TEXT PRIMARY KEY
        CHECK (length(attempt_idempotency_key) = 64),
    supervisor_attempt_id TEXT NOT NULL UNIQUE,
    start_request_sha256 TEXT NOT NULL CHECK (length(start_request_sha256) = 64),
    request_payload_sha256 TEXT NOT NULL,
    authorization_uuid TEXT NOT NULL,
    authorization_identity_sha256 TEXT NOT NULL,
    claim_uuid TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    start_request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'ACCEPTED', 'CREATING', 'RUNNING', 'EXITED', 'FAILED',
        'TIMED_OUT', 'CANCELLED', 'INTERRUPTED', 'UNKNOWN'
    )),
    exit_code INTEGER,
    status_reason TEXT,
    container_name TEXT NOT NULL UNIQUE,
    container_id TEXT,
    execution_timeout_seconds INTEGER NOT NULL
        CHECK (execution_timeout_seconds BETWEEN 1 AND 360),
    resource_limits_json TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    cancel_requested_at TEXT,
    stdout_ref TEXT,
    stdout_sha256 TEXT,
    stdout_bytes INTEGER,
    stderr_ref TEXT,
    stderr_sha256 TEXT,
    stderr_bytes INTEGER,
    cleanup_status TEXT NOT NULL DEFAULT 'NOT_STARTED'
        CHECK (cleanup_status IN ('NOT_STARTED', 'SUCCEEDED', 'FAILED', 'NOT_REQUIRED')),
    cleanup_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TRIGGER supervisor_attempts_no_delete
BEFORE DELETE ON supervisor_attempts
BEGIN
    SELECT RAISE(ABORT, 'supervisor attempts are insert-only');
END;
CREATE TRIGGER supervisor_attempts_forward_only
BEFORE UPDATE ON supervisor_attempts
WHEN NEW.attempt_idempotency_key IS NOT OLD.attempt_idempotency_key
    OR NEW.supervisor_attempt_id IS NOT OLD.supervisor_attempt_id
    OR NEW.start_request_sha256 IS NOT OLD.start_request_sha256
    OR NEW.request_payload_sha256 IS NOT OLD.request_payload_sha256
    OR NEW.authorization_uuid IS NOT OLD.authorization_uuid
    OR NEW.authorization_identity_sha256 IS NOT OLD.authorization_identity_sha256
    OR NEW.claim_uuid IS NOT OLD.claim_uuid
    OR NEW.operation_id IS NOT OLD.operation_id
    OR NEW.start_request_json IS NOT OLD.start_request_json
    OR NEW.container_name IS NOT OLD.container_name
    OR NEW.execution_timeout_seconds IS NOT OLD.execution_timeout_seconds
    OR NEW.resource_limits_json IS NOT OLD.resource_limits_json
    OR (OLD.container_id IS NOT NULL AND NEW.container_id IS NOT OLD.container_id)
    OR (OLD.container_id IS NULL AND NEW.container_id IS NOT NULL
        AND OLD.status != 'CREATING')
    OR NOT (
        (
            NEW.status = OLD.status
            AND NEW.exit_code IS OLD.exit_code
            AND NEW.status_reason IS OLD.status_reason
            AND (
                NEW.container_id IS OLD.container_id
                OR (OLD.status = 'CREATING' AND OLD.container_id IS NULL
                    AND NEW.container_id IS NOT NULL)
            )
            AND NEW.started_at IS OLD.started_at
            AND NEW.completed_at IS OLD.completed_at
            AND NEW.stdout_ref IS OLD.stdout_ref
            AND NEW.stdout_sha256 IS OLD.stdout_sha256
            AND NEW.stdout_bytes IS OLD.stdout_bytes
            AND NEW.stderr_ref IS OLD.stderr_ref
            AND NEW.stderr_sha256 IS OLD.stderr_sha256
            AND NEW.stderr_bytes IS OLD.stderr_bytes
            AND (
                NEW.cancel_requested_at IS OLD.cancel_requested_at
                OR (OLD.status IN ('ACCEPTED', 'CREATING', 'RUNNING')
                    AND OLD.cancel_requested_at IS NULL
                    AND NEW.cancel_requested_at IS NOT NULL)
            )
        )
        OR (OLD.status = 'ACCEPTED' AND NEW.status IN
            ('CREATING', 'FAILED', 'CANCELLED', 'UNKNOWN'))
        OR (OLD.status = 'CREATING' AND NEW.status IN
            ('RUNNING', 'FAILED', 'TIMED_OUT', 'CANCELLED', 'INTERRUPTED', 'UNKNOWN'))
        OR (OLD.status = 'RUNNING' AND NEW.status IN
            ('EXITED', 'FAILED', 'TIMED_OUT', 'CANCELLED', 'INTERRUPTED', 'UNKNOWN'))
    )
BEGIN
    SELECT RAISE(ABORT, 'supervisor attempt identity is immutable');
END;
"""


class LedgerError(RuntimeError):
    """The ledger is unavailable or not the expected schema."""


@dataclass(frozen=True)
class AttemptRecord:
    attempt_idempotency_key: str
    supervisor_attempt_id: str
    start_request_sha256: str
    request_payload_sha256: str
    authorization_uuid: str
    authorization_identity_sha256: str
    claim_uuid: str
    operation_id: str
    start_request_json: str
    status: str
    exit_code: int | None
    status_reason: str | None
    container_name: str
    container_id: str | None
    execution_timeout_seconds: int
    resource_limits_json: str
    started_at: str | None
    completed_at: str | None
    cancel_requested_at: str | None
    stdout_ref: str | None
    stdout_sha256: str | None
    stdout_bytes: int | None
    stderr_ref: str | None
    stderr_sha256: str | None
    stderr_bytes: int | None
    cleanup_status: str
    cleanup_reason: str | None
    created_at: str
    updated_at: str

    @property
    def request(self) -> dict:
        return json.loads(self.start_request_json)

    @property
    def resource_limits(self) -> dict:
        return json.loads(self.resource_limits_json)


@dataclass(frozen=True)
class Registration:
    record: AttemptRecord
    created: bool
    conflict: bool = False


def canonical_start_request_sha256(start_request: dict) -> str:
    body = json.dumps(start_request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def deterministic_container_name(attempt_key: str) -> str:
    if len(attempt_key) != 64 or any(c not in "0123456789abcdef" for c in attempt_key):
        raise ValueError("invalid attempt key")
    return "orc-one-shot-" + attempt_key[:32]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(connection: sqlite3.Connection, key: str) -> AttemptRecord | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT * FROM supervisor_attempts WHERE attempt_idempotency_key = ?", (key,)
    ).fetchone()
    return AttemptRecord(**dict(row)) if row else None


def _execute_script_in_transaction(connection: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise LedgerError("ledger schema script is incomplete")


class AttemptLedger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def initialize(self) -> None:
        """Create a new ledger or fail-closed upgrade the F2 ledger schema."""

        with closing(self.connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                try:
                    connection.executescript(
                        "BEGIN IMMEDIATE;"
                        + _SCHEMA
                        + f"PRAGMA user_version = {LEDGER_SCHEMA_VERSION};"
                        + "COMMIT;"
                    )
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
            elif version == 1:
                self._migrate_v1(connection)
            elif version != LEDGER_SCHEMA_VERSION:
                raise LedgerError(f"unsupported ledger schema {version}")
        self.check()

    def _migrate_v1(self, connection: sqlite3.Connection) -> None:
        """Keep F2 history but never replay a pre-OCI ACCEPTED/RUNNING row."""

        try:
            connection.execute("BEGIN IMMEDIATE")
            old_rows = connection.execute(
                "SELECT attempt_idempotency_key, supervisor_attempt_id, "
                "start_request_sha256, request_payload_sha256, authorization_uuid, "
                "authorization_identity_sha256, claim_uuid, operation_id, "
                "start_request_json, status, exit_code, status_reason, created_at, "
                "updated_at FROM supervisor_attempts ORDER BY created_at"
            ).fetchall()
            connection.execute(
                "ALTER TABLE supervisor_attempts RENAME TO supervisor_attempts_v1"
            )
            connection.execute("DROP TRIGGER supervisor_attempts_no_delete")
            connection.execute("DROP TRIGGER supervisor_attempts_forward_only")
            _execute_script_in_transaction(connection, _SCHEMA)
            for row in old_rows:
                (
                    key,
                    attempt_id,
                    request_sha,
                    payload_sha,
                    authorization_uuid,
                    identity_sha,
                    claim_uuid,
                    operation_id,
                    request_json,
                    old_status,
                    exit_code,
                    old_reason,
                    created_at,
                    updated_at,
                ) = row
                status = "EXITED" if old_status == "EXITED" else "UNKNOWN"
                reason = old_reason
                if old_status in {"ACCEPTED", "RUNNING"}:
                    reason = "legacy_f2_attempt_not_replayed"
                request = json.loads(request_json)
                limits = {"cpu_count": 1, "memory_bytes": 536870912, "pids": 64}
                connection.execute(
                    "INSERT INTO supervisor_attempts ("
                    "attempt_idempotency_key, supervisor_attempt_id, "
                    "start_request_sha256, request_payload_sha256, authorization_uuid, "
                    "authorization_identity_sha256, claim_uuid, operation_id, "
                    "start_request_json, status, exit_code, status_reason, "
                    "container_name, execution_timeout_seconds, resource_limits_json, "
                    "started_at, completed_at, cleanup_status, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 120, ?, ?, ?, "
                    "'NOT_REQUIRED', ?, ?)",
                    (
                        key,
                        attempt_id,
                        request_sha,
                        payload_sha,
                        authorization_uuid,
                        identity_sha,
                        claim_uuid,
                        operation_id,
                        request_json,
                        status,
                        exit_code if status == "EXITED" else None,
                        reason,
                        deterministic_container_name(key),
                        json.dumps(limits, sort_keys=True),
                        updated_at if status == "EXITED" else None,
                        updated_at if status == "EXITED" else updated_at,
                        created_at,
                        updated_at,
                    ),
                )
            connection.execute("DROP TABLE supervisor_attempts_v1")
            connection.execute(f"PRAGMA user_version = {LEDGER_SCHEMA_VERSION}")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def check(self) -> None:
        try:
            with closing(self.connect()) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                connection.execute("SELECT 1 FROM supervisor_attempts LIMIT 1")
        except sqlite3.Error as exc:
            raise LedgerError("ledger unavailable") from exc
        if version != LEDGER_SCHEMA_VERSION:
            raise LedgerError(f"unsupported ledger schema {version}")

    def get(self, key: str) -> AttemptRecord | None:
        with closing(self.connect()) as connection:
            return _record(connection, key)

    def register(
        self,
        start_request: dict,
        *,
        execution_timeout_seconds: int,
        resource_limits: dict,
    ) -> Registration:
        """Insert the first attempt for its key, or return the existing one."""

        key = start_request["attempt_idempotency_key"]
        digest = canonical_start_request_sha256(start_request)
        container_name = deterministic_container_name(key)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = _record(connection, key)
                if existing is not None:
                    connection.execute("COMMIT")
                    return Registration(
                        existing,
                        created=False,
                        conflict=existing.start_request_sha256 != digest,
                    )
                now = _now()
                attempt_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO supervisor_attempts ("
                    "attempt_idempotency_key, supervisor_attempt_id, "
                    "start_request_sha256, request_payload_sha256, authorization_uuid, "
                    "authorization_identity_sha256, claim_uuid, operation_id, "
                    "start_request_json, status, container_name, "
                    "execution_timeout_seconds, resource_limits_json, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACCEPTED', ?, ?, ?, ?, ?)",
                    (
                        key,
                        attempt_id,
                        digest,
                        start_request["request_payload_sha256"],
                        start_request["authorization_uuid"],
                        start_request["authorization_identity_sha256"],
                        start_request["claim_uuid"],
                        FIXED_OPERATION_ID,
                        json.dumps(
                            start_request, sort_keys=True, separators=(",", ":")
                        ),
                        container_name,
                        int(execution_timeout_seconds),
                        json.dumps(
                            resource_limits, sort_keys=True, separators=(",", ":")
                        ),
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            record = _record(connection, key)
        assert record is not None
        return Registration(record, created=True)

    def transition(
        self,
        key: str,
        from_status: str,
        to_status: str,
        *,
        exit_code: int | None = None,
        reason: str | None = None,
        container_id: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        stdout_ref: str | None = None,
        stdout_sha256: str | None = None,
        stdout_bytes: int | None = None,
        stderr_ref: str | None = None,
        stderr_sha256: str | None = None,
        stderr_bytes: int | None = None,
    ) -> bool:
        """Compare-and-set one forward lifecycle transition."""

        values = {"status": to_status, "updated_at": _now()}
        optional = {
            "exit_code": exit_code,
            "status_reason": reason,
            "container_id": container_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "stdout_ref": stdout_ref,
            "stdout_sha256": stdout_sha256,
            "stdout_bytes": stdout_bytes,
            "stderr_ref": stderr_ref,
            "stderr_sha256": stderr_sha256,
            "stderr_bytes": stderr_bytes,
        }
        values.update(
            {name: value for name, value in optional.items() if value is not None}
        )
        assignments = ", ".join(f"{name} = ?" for name in values)
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                f"UPDATE supervisor_attempts SET {assignments} "
                "WHERE attempt_idempotency_key = ? AND status = ?",
                (*values.values(), key, from_status),
            )
            return cursor.rowcount == 1

    def mark_cancel_requested(self, key: str) -> AttemptRecord | None:
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE supervisor_attempts SET cancel_requested_at = COALESCE("
                "cancel_requested_at, ?), updated_at = ? "
                "WHERE attempt_idempotency_key = ? AND status IN "
                "('ACCEPTED', 'CREATING', 'RUNNING')",
                (_now(), _now(), key),
            )
            return _record(connection, key)

    def mark_cleanup(self, key: str, status: str, reason: str | None = None) -> None:
        if status not in {"SUCCEEDED", "FAILED", "NOT_REQUIRED"}:
            raise ValueError("invalid cleanup status")
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE supervisor_attempts SET cleanup_status = ?, "
                "cleanup_reason = ?, updated_at = ? WHERE attempt_idempotency_key = ?",
                (status, reason, _now(), key),
            )

    def records_with_status(self, status: str) -> list[AttemptRecord]:
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM supervisor_attempts WHERE status = ? ORDER BY created_at",
                (status,),
            ).fetchall()
        return [AttemptRecord(**dict(row)) for row in rows]

    def records_needing_cleanup(self) -> list[AttemptRecord]:
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM supervisor_attempts WHERE status IN "
                "('EXITED', 'FAILED', 'TIMED_OUT', 'CANCELLED', 'INTERRUPTED', 'UNKNOWN') "
                "AND cleanup_status IN ('NOT_STARTED', 'FAILED') "
                "AND container_id IS NOT NULL ORDER BY created_at"
            ).fetchall()
        return [AttemptRecord(**dict(row)) for row in rows]

    def all_records(self) -> list[AttemptRecord]:
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM supervisor_attempts ORDER BY created_at"
            ).fetchall()
        return [AttemptRecord(**dict(row)) for row in rows]
