"""Durable Supervisor attempt ledger keyed by the stable attempt key.

One row per ``attempt_idempotency_key``. Registration runs under
``BEGIN IMMEDIATE`` so concurrent first requests serialize; the primary key
makes a second row impossible even across processes. Identity columns are
immutable, rows cannot be deleted, and status only moves forward:
ACCEPTED -> RUNNING -> EXITED | UNKNOWN. RUNNING is written before the
operation begins, so an ACCEPTED row proves the operation never started.
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

LEDGER_SCHEMA_VERSION = 1
FIXED_OPERATION_ID = "phase39-f2-fixed-noop/v1"

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
    status TEXT NOT NULL
        CHECK (status IN ('ACCEPTED', 'RUNNING', 'EXITED', 'UNKNOWN')),
    exit_code INTEGER,
    status_reason TEXT,
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
    OR NEW.created_at IS NOT OLD.created_at
    OR NOT (
        (OLD.status = 'ACCEPTED' AND NEW.status = 'RUNNING')
        OR (OLD.status = 'RUNNING' AND NEW.status IN ('EXITED', 'UNKNOWN'))
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
    authorization_identity_sha256: str
    status: str
    exit_code: int | None
    status_reason: str | None


@dataclass(frozen=True)
class Registration:
    record: AttemptRecord
    # True: this call inserted the row. False: the row already existed.
    created: bool
    # True: the existing row was registered with a different start request.
    conflict: bool = False


def canonical_start_request_sha256(start_request: dict) -> str:
    body = json.dumps(start_request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_COLUMNS = (
    "attempt_idempotency_key, supervisor_attempt_id, start_request_sha256, "
    "request_payload_sha256, authorization_identity_sha256, status, exit_code, "
    "status_reason"
)


class AttemptLedger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def initialize(self) -> None:
        """Create the schema once; the Supervisor's state-dir lock serializes it."""

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
            elif version != LEDGER_SCHEMA_VERSION:
                raise LedgerError(f"unsupported ledger schema {version}")
        self.check()

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
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM supervisor_attempts "
                "WHERE attempt_idempotency_key = ?",
                (key,),
            ).fetchone()
        return AttemptRecord(*row) if row else None

    def register(self, start_request: dict) -> Registration:
        """Insert the first attempt for its key, or return the existing one."""

        key = start_request["attempt_idempotency_key"]
        digest = canonical_start_request_sha256(start_request)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    f"SELECT {_COLUMNS} FROM supervisor_attempts "
                    "WHERE attempt_idempotency_key = ?",
                    (key,),
                ).fetchone()
                if row is not None:
                    connection.execute("COMMIT")
                    existing = AttemptRecord(*row)
                    return Registration(
                        existing,
                        created=False,
                        conflict=existing.start_request_sha256 != digest,
                    )
                now = _now()
                record = AttemptRecord(
                    attempt_idempotency_key=key,
                    supervisor_attempt_id=str(uuid.uuid4()),
                    start_request_sha256=digest,
                    request_payload_sha256=start_request["request_payload_sha256"],
                    authorization_identity_sha256=start_request[
                        "authorization_identity_sha256"
                    ],
                    status="ACCEPTED",
                    exit_code=None,
                    status_reason=None,
                )
                connection.execute(
                    "INSERT INTO supervisor_attempts ("
                    "attempt_idempotency_key, supervisor_attempt_id, "
                    "start_request_sha256, request_payload_sha256, "
                    "authorization_uuid, authorization_identity_sha256, claim_uuid, "
                    "operation_id, start_request_json, status, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACCEPTED', ?, ?)",
                    (
                        key,
                        record.supervisor_attempt_id,
                        digest,
                        record.request_payload_sha256,
                        start_request["authorization_uuid"],
                        record.authorization_identity_sha256,
                        start_request["claim_uuid"],
                        FIXED_OPERATION_ID,
                        json.dumps(start_request, sort_keys=True),
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return Registration(record, created=True)

    def transition(
        self,
        key: str,
        from_status: str,
        to_status: str,
        *,
        exit_code: int | None = None,
        reason: str | None = None,
    ) -> bool:
        """Compare-and-set one forward transition; False if it did not apply."""

        with closing(self.connect()) as connection:
            cursor = connection.execute(
                "UPDATE supervisor_attempts SET status = ?, exit_code = ?, "
                "status_reason = ?, updated_at = ? "
                "WHERE attempt_idempotency_key = ? AND status = ?",
                (to_status, exit_code, reason, _now(), key, from_status),
            )
            return cursor.rowcount == 1

    def keys_with_status(self, status: str) -> list[str]:
        with closing(self.connect()) as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT attempt_idempotency_key FROM supervisor_attempts "
                    "WHERE status = ? ORDER BY created_at",
                    (status,),
                )
            ]
