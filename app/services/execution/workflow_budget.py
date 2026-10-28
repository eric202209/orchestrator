"""Provider-free durable workflow budget and provider-request reservations.

Phase 40E-4R3A foundation. A logical workflow owns one immutable policy
snapshot, one absolute ``deadline_at``, and one cumulative reservation
counter. Every write runs on a dedicated SQLite connection whose first
statement is ``BEGIN IMMEDIATE``; the trusted clock is sampled only after the
write lock is held, and each transaction commits before returning. Nothing
here performs network I/O, and no provider adapter path calls this module
(R3B-2 integrates reservations). R3B-1 lifecycle binding reaches it only
through ``workflow_lifecycle`` and only while enforcement is enabled.

Guarantee: at-most-budgeted local dispatch authorization with conservative
accounting. A committed ``DISPATCH_STARTED`` marker cannot prove the provider
received a request, so no reservation is ever refunded and stale ambiguous
dispatches become ``OUTCOME_UNKNOWN``. This is not exactly-once execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import re
import sqlite3
from typing import Callable
import uuid

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

from app.models import (
    PlanningSession,
    ProviderRequestReservation,
    Session as SessionModel,
    TaskExecution,
    TaskStatus,
    WorkflowBudget,
    WorkflowTaskExecutionBinding,
)

MIGRATION_VERSION = "059_workflow_provider_request_budget"
MAX_REQUESTS_CEILING = 1000
MAX_DURATION_SECONDS_CEILING = 604800

_WF = WorkflowBudget.__table__
_BIND = WorkflowTaskExecutionBinding.__table__
_RES = ProviderRequestReservation.__table__
_TE = TaskExecution.__table__
_SESSION = SessionModel.__table__
_PLANNING = PlanningSession.__table__
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6
_POLICY_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_POLICY_VERSION = re.compile(r"^[A-Za-z0-9._:-]{1,32}$")
_LABEL = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class WorkflowKind(str, Enum):
    TASK_EXECUTION = "TASK_EXECUTION"
    PLANNING_SESSION = "PLANNING_SESSION"


class EnforcementMode(str, Enum):
    DISABLED = "DISABLED"
    ENFORCED = "ENFORCED"


class BindingKind(str, Enum):
    ROOT = "ROOT"
    RETRY = "RETRY"
    RECOVERY = "RECOVERY"


class ReservationStatus(str, Enum):
    RESERVED = "RESERVED"
    DISPATCH_STARTED = "DISPATCH_STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


TERMINAL_STATUSES = frozenset(
    {
        ReservationStatus.COMPLETED,
        ReservationStatus.FAILED,
        ReservationStatus.TIMED_OUT,
        ReservationStatus.CANCELLED,
        ReservationStatus.OUTCOME_UNKNOWN,
    }
)
ENFORCED = "ENFORCED"
LEGACY_UNENFORCED = "LEGACY_UNENFORCED"
STALE_NOT_DISPATCHED = "STALE_NOT_DISPATCHED"
STALE_DISPATCH_AMBIGUOUS = "STALE_DISPATCH_AMBIGUOUS"


class BudgetOutcome(str, Enum):
    CREATED = "CREATED"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    BOUND = "BOUND"
    ALREADY_BOUND = "ALREADY_BOUND"
    CLOSED = "CLOSED"
    ALREADY_CLOSED = "ALREADY_CLOSED"
    RESERVED = "RESERVED"
    ALREADY_RESERVED = "ALREADY_RESERVED"
    TRANSITIONED = "TRANSITIONED"
    ALREADY_IN_STATE = "ALREADY_IN_STATE"
    RECONCILED = "RECONCILED"
    PROVIDER_REQUEST_BUDGET_EXHAUSTED = "PROVIDER_REQUEST_BUDGET_EXHAUSTED"
    WORKFLOW_INFERENCE_DEADLINE_EXCEEDED = "WORKFLOW_INFERENCE_DEADLINE_EXCEEDED"
    WORKFLOW_CLOSED = "WORKFLOW_CLOSED"
    WORKFLOW_NOT_FOUND = "WORKFLOW_NOT_FOUND"
    RESERVATION_NOT_FOUND = "RESERVATION_NOT_FOUND"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    TERMINAL_CONFLICT = "TERMINAL_CONFLICT"
    ATTEMPT_ACTIVE = "ATTEMPT_ACTIVE"
    OPEN_WORKFLOW_EXISTS = "OPEN_WORKFLOW_EXISTS"
    DATABASE_BUSY = "DATABASE_BUSY"
    COMMIT_UNCERTAIN = "COMMIT_UNCERTAIN"
    SCHEMA_UNAVAILABLE = "SCHEMA_UNAVAILABLE"
    UNSUPPORTED_DATABASE = "UNSUPPORTED_DATABASE"


class InvalidBudgetPolicy(ValueError):
    """A policy cannot be snapshotted into a workflow budget."""


class BudgetEnforcementDisabled(RuntimeError):
    """A DISABLED policy never creates a budget, so it is never reported as enforced."""


class WorkflowBudgetSchemaUnavailable(RuntimeError):
    """Read-side helpers fail closed when the 059 schema is not verified."""


@dataclass(frozen=True)
class WorkflowBudgetPolicy:
    """Typed, versioned policy; its values are copied into the workflow row."""

    policy_id: str
    policy_version: str
    workflow_kind: WorkflowKind
    max_requests: int
    max_duration_seconds: int
    enforcement_mode: EnforcementMode = EnforcementMode.DISABLED


# The R2 proposal, recorded for operator review only. It is DISABLED, so it
# cannot create a budget; nothing selects it by default.
R2_CANDIDATE_POLICY = WorkflowBudgetPolicy(
    policy_id="phase40e4r2-direct-single-model",
    policy_version="candidate-1",
    workflow_kind=WorkflowKind.TASK_EXECUTION,
    max_requests=24,
    max_duration_seconds=1800,
    enforcement_mode=EnforcementMode.DISABLED,
)


@dataclass(frozen=True)
class WorkflowBudgetState:
    workflow_uuid: str
    workflow_kind: str
    policy_id: str
    policy_version: str
    max_requests: int
    max_duration_seconds: int
    reserved_requests: int
    status: str
    created_at: datetime
    deadline_at: datetime
    root_task_execution_id: int | None
    planning_session_id: int | None


@dataclass(frozen=True)
class WorkflowResult:
    outcome: BudgetOutcome
    workflow: WorkflowBudgetState | None = None
    reconciled: bool = False


@dataclass(frozen=True)
class BindingResult:
    outcome: BudgetOutcome
    task_execution_id: int
    workflow_uuid: str | None = None
    binding_kind: str | None = None
    reconciled: bool = False


@dataclass(frozen=True)
class BindingState:
    task_execution_id: int
    workflow_uuid: str
    binding_kind: str
    parent_task_execution_id: int | None
    session_id: int
    session_instance_id: str
    task_id: int
    attempt_number: int


@dataclass(frozen=True)
class ReservationState:
    reservation_uuid: str
    workflow_uuid: str
    request_ordinal: int
    idempotency_key: str
    stage: str
    runtime_role: str
    task_execution_id: int | None
    retry_of_reservation_uuid: str | None
    status: str
    outcome_code: str | None
    reserved_at: datetime
    dispatch_started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True)
class ReservationResult:
    outcome: BudgetOutcome
    reservation: ReservationState | None = None
    reconciled: bool = False

    @property
    def reserved(self) -> bool:
        """The workflow durably holds this allowance (fresh or replayed)."""

        return self.outcome in (BudgetOutcome.RESERVED, BudgetOutcome.ALREADY_RESERVED)


@dataclass(frozen=True)
class TransitionResult:
    outcome: BudgetOutcome
    reservation: ReservationState | None = None
    remaining_seconds: float | None = None
    reconciled: bool = False

    @property
    def dispatch_permitted(self) -> bool:
        """Only the caller whose own RESERVED→DISPATCH_STARTED committed may send."""

        return (
            self.outcome is BudgetOutcome.TRANSITIONED
            and not self.reconciled
            and self.reservation is not None
            and self.reservation.status == ReservationStatus.DISPATCH_STARTED.value
        )


@dataclass(frozen=True)
class ReconciliationReport:
    outcome: BudgetOutcome
    cancelled_unstarted: tuple[str, ...] = ()
    marked_outcome_unknown: tuple[str, ...] = ()
    observed_at: datetime | None = None


@dataclass(frozen=True)
class WorkflowAccounting:
    workflow: WorkflowBudgetState
    reservation_count: int
    status_counts: dict[str, int]

    @property
    def consistent(self) -> bool:
        return self.reservation_count == self.workflow.reserved_requests


def new_idempotency_key() -> str:
    """A fresh key for one genuinely new provider POST; never reuse it for another."""

    return uuid.uuid4().hex


def validate_budget_policy(policy: WorkflowBudgetPolicy) -> None:
    if not isinstance(policy, WorkflowBudgetPolicy):
        raise InvalidBudgetPolicy("policy must be a WorkflowBudgetPolicy")
    if not isinstance(policy.policy_id, str) or not _POLICY_ID.fullmatch(
        policy.policy_id
    ):
        raise InvalidBudgetPolicy("policy_id is missing or invalid")
    if not isinstance(policy.policy_version, str) or not _POLICY_VERSION.fullmatch(
        policy.policy_version
    ):
        raise InvalidBudgetPolicy("policy_version is missing or invalid")
    if not isinstance(policy.workflow_kind, WorkflowKind):
        raise InvalidBudgetPolicy("workflow_kind is unsupported")
    if not isinstance(policy.enforcement_mode, EnforcementMode):
        raise InvalidBudgetPolicy("enforcement_mode is unsupported")
    for name, value, ceiling in (
        ("max_requests", policy.max_requests, MAX_REQUESTS_CEILING),
        (
            "max_duration_seconds",
            policy.max_duration_seconds,
            MAX_DURATION_SECONDS_CEILING,
        ),
    ):
        if type(value) is not int or not 1 <= value <= ceiling:
            raise InvalidBudgetPolicy(f"{name} must be an integer in [1, {ceiling}]")


def _trusted_utc_now() -> datetime:
    """The only time source for budget decisions.

    Trust boundary: no public function accepts a caller-supplied time. Tests
    replace this function in-process; production callers cannot backdate.
    """

    return datetime.now(timezone.utc)


def trusted_utc_now() -> datetime:
    """Return the service clock used for immutable workflow deadlines."""

    return _utc(_trusted_utc_now())


def _utc(value: datetime | None) -> datetime | None:
    """SQLite stores UTC text without an offset; restore or normalize UTC."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_label(value: str, name: str, pattern: re.Pattern = _LABEL) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{name} is missing or invalid")
    return value


def _require_id(value: int, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _db_error_outcome(exc: BaseException) -> BudgetOutcome | None:
    """Map expected operational failures; anything else stays an exception."""

    if isinstance(exc, PoolTimeoutError):
        return BudgetOutcome.DATABASE_BUSY
    orig = getattr(exc, "orig", exc)
    code = getattr(orig, "sqlite_errorcode", None)
    message = str(orig).lower()
    if code is not None:
        if code & 0xFF in (_SQLITE_BUSY, _SQLITE_LOCKED):
            return BudgetOutcome.DATABASE_BUSY
    elif "database is locked" in message or "database is busy" in message:
        return BudgetOutcome.DATABASE_BUSY
    if "no such table" in message or "no such column" in message:
        return BudgetOutcome.SCHEMA_UNAVAILABLE
    return None


def _engine(db: Session) -> Engine:
    return db.get_bind().engine


def _schema_outcome(engine: Engine) -> BudgetOutcome | None:
    """Fail closed unless this is a file-backed SQLite with verified 059."""

    if engine.dialect.name != "sqlite":
        return BudgetOutcome.UNSUPPORTED_DATABASE
    if engine.url.database in (None, "", ":memory:"):
        return BudgetOutcome.UNSUPPORTED_DATABASE
    try:
        from app.db_migrations import _verify_059_workflow_provider_request_budget

        _verify_059_workflow_provider_request_budget(engine)
        with engine.connect() as connection:
            applied = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                (MIGRATION_VERSION,),
            ).scalar_one()
    except SQLAlchemyError as exc:
        if _db_error_outcome(exc) is BudgetOutcome.DATABASE_BUSY:
            return BudgetOutcome.DATABASE_BUSY
        return BudgetOutcome.SCHEMA_UNAVAILABLE
    except RuntimeError:
        return BudgetOutcome.SCHEMA_UNAVAILABLE
    if applied != 1:
        return BudgetOutcome.SCHEMA_UNAVAILABLE
    return None


def _commit(connection: Connection) -> None:
    connection.exec_driver_sql("COMMIT")


def _rollback_quietly(connection: Connection) -> None:
    try:
        connection.exec_driver_sql("ROLLBACK")
    except (SQLAlchemyError, sqlite3.Error):
        # Closing/invalidating the DBAPI connection rolls back as well.
        connection.invalidate()


def _run_write(
    engine: Engine,
    body: Callable[[Connection, datetime], tuple[object, bool]],
    *,
    failed: Callable[[BudgetOutcome], object],
    reconcile: Callable[[], object],
):
    """Run ``body`` in one short ``BEGIN IMMEDIATE`` transaction.

    ``body`` returns ``(result, commit)``; with ``commit`` false everything is
    rolled back. A COMMIT error discards the connection and returns
    ``reconcile()``, which proves the durable state from a fresh connection.
    """

    unavailable = _schema_outcome(engine)
    if unavailable is not None:
        return failed(unavailable)
    try:
        connection = engine.connect()
    except SQLAlchemyError as exc:
        outcome = _db_error_outcome(exc)
        if outcome is None:
            raise
        return failed(outcome)

    with connection:
        if connection.connection.dbapi_connection.in_transaction:
            # A pooled connection that is not clean cannot give BEGIN IMMEDIATE
            # first position; refuse rather than join someone else's work.
            connection.invalidate()
            return failed(BudgetOutcome.UNSUPPORTED_DATABASE)
        connection.execution_options(isolation_level="AUTOCOMMIT")
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        except SQLAlchemyError as exc:
            outcome = _db_error_outcome(exc)
            if outcome is None:
                raise
            return failed(outcome)
        try:
            # Sampled only once the write lock is held, so lock waiting can
            # never carry a stale pre-deadline timestamp into the decision.
            observed_at = _utc(_trusted_utc_now())
            result, should_commit = body(connection, observed_at)
        except SQLAlchemyError as exc:
            _rollback_quietly(connection)
            outcome = _db_error_outcome(exc)
            if outcome is None:
                raise
            return failed(outcome)
        except BaseException:
            _rollback_quietly(connection)
            raise
        if not should_commit:
            _rollback_quietly(connection)
            return result
        try:
            _commit(connection)
        except (SQLAlchemyError, sqlite3.Error):
            connection.invalidate()
            return reconcile()
    return result


def _fresh_read(engine: Engine, read: Callable[[Connection], object]):
    with engine.connect() as connection:
        return read(connection)


def _workflow_state(row) -> WorkflowBudgetState | None:
    if row is None:
        return None
    return WorkflowBudgetState(
        workflow_uuid=row.workflow_uuid,
        workflow_kind=row.workflow_kind,
        policy_id=row.policy_id,
        policy_version=row.policy_version,
        max_requests=row.max_requests,
        max_duration_seconds=row.max_duration_seconds,
        reserved_requests=row.reserved_requests,
        status=row.status,
        created_at=_utc(row.created_at),
        deadline_at=_utc(row.deadline_at),
        root_task_execution_id=row.root_task_execution_id,
        planning_session_id=row.planning_session_id,
    )


def _reservation_state(row) -> ReservationState | None:
    if row is None:
        return None
    return ReservationState(
        reservation_uuid=row.reservation_uuid,
        workflow_uuid=row.workflow_uuid,
        request_ordinal=row.request_ordinal,
        idempotency_key=row.idempotency_key,
        stage=row.stage,
        runtime_role=row.runtime_role,
        task_execution_id=row.task_execution_id,
        retry_of_reservation_uuid=row.retry_of_reservation_uuid,
        status=row.status,
        outcome_code=row.outcome_code,
        reserved_at=_utc(row.reserved_at),
        dispatch_started_at=_utc(row.dispatch_started_at),
        completed_at=_utc(row.completed_at),
    )


def _load_workflow(connection: Connection, workflow_uuid: str):
    return connection.execute(
        select(_WF).where(_WF.c.workflow_uuid == workflow_uuid)
    ).first()


def _load_binding(connection: Connection, task_execution_id: int):
    return connection.execute(
        select(_BIND).where(_BIND.c.task_execution_id == task_execution_id)
    ).first()


def _load_reservation(connection: Connection, reservation_uuid: str):
    return connection.execute(
        select(_RES).where(_RES.c.reservation_uuid == reservation_uuid)
    ).first()


def _load_reservation_by_key(
    connection: Connection, workflow_uuid: str, idempotency_key: str
):
    return connection.execute(
        select(_RES).where(
            _RES.c.workflow_uuid == workflow_uuid,
            _RES.c.idempotency_key == idempotency_key,
        )
    ).first()


def _policy_matches(row, policy: WorkflowBudgetPolicy) -> bool:
    return (
        row.workflow_kind == policy.workflow_kind.value
        and row.policy_id == policy.policy_id
        and row.policy_version == policy.policy_version
        and row.max_requests == policy.max_requests
        and row.max_duration_seconds == policy.max_duration_seconds
    )


def _require_enforced(policy: WorkflowBudgetPolicy, kind: WorkflowKind) -> None:
    validate_budget_policy(policy)
    if policy.workflow_kind is not kind:
        raise InvalidBudgetPolicy(f"policy is not a {kind.value} policy")
    if policy.enforcement_mode is not EnforcementMode.ENFORCED:
        raise BudgetEnforcementDisabled("a DISABLED policy cannot create a budget")


def _deadline(created_at: datetime, policy: WorkflowBudgetPolicy) -> datetime:
    try:
        return created_at + timedelta(seconds=policy.max_duration_seconds)
    except OverflowError as exc:
        raise InvalidBudgetPolicy("workflow deadline overflows") from exc


def _workflow_values(policy: WorkflowBudgetPolicy, observed_at: datetime) -> dict:
    return {
        "workflow_uuid": str(uuid.uuid4()),
        "workflow_kind": policy.workflow_kind.value,
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "enforcement_mode": EnforcementMode.ENFORCED.value,
        "max_requests": policy.max_requests,
        "max_duration_seconds": policy.max_duration_seconds,
        "reserved_requests": 0,
        "status": "OPEN",
        "created_at": observed_at,
        "deadline_at": _deadline(observed_at, policy),
        "closed_at": None,
    }


def _existing_root(
    row, policy: WorkflowBudgetPolicy, observed_at: datetime | None = None
) -> WorkflowResult:
    if row is not None and _policy_matches(row, policy):
        effective_now = observed_at or _utc(_trusted_utc_now())
        if _utc(row.deadline_at) <= effective_now:
            return WorkflowResult(
                BudgetOutcome.WORKFLOW_INFERENCE_DEADLINE_EXCEEDED,
                _workflow_state(row),
            )
        return WorkflowResult(BudgetOutcome.ALREADY_EXISTS, _workflow_state(row))
    return WorkflowResult(BudgetOutcome.IDENTITY_CONFLICT, _workflow_state(row))


def _workflow_failed(outcome: BudgetOutcome) -> WorkflowResult:
    return WorkflowResult(outcome)


def create_task_execution_workflow(
    db: Session,
    policy: WorkflowBudgetPolicy,
    *,
    root_task_execution_id: int,
    exclusive_open_task: bool = False,
    expected_session_instance_id: str | None = None,
) -> WorkflowResult:
    """Create, or idempotently return, the workflow rooted at one attempt.

    The root is an explicit decision by the caller: the first attempt of a
    new logical Task run. The workflow and its ROOT binding commit together.
    An attempt already bound to any workflow can never become a second root.

    ``exclusive_open_task`` refuses while the same Task has any OPEN workflow,
    checked in the same write transaction. An expired OPEN workflow is reported
    as ``WORKFLOW_INFERENCE_DEADLINE_EXCEEDED`` and cannot be bypassed by
    minting a new root. It never inherits that workflow: refusal leaves the
    attempt unbound.
    """

    _require_enforced(policy, WorkflowKind.TASK_EXECUTION)
    _require_id(root_task_execution_id, "root_task_execution_id")
    engine = _engine(db)
    created: dict = {}

    def _root_workflow(connection: Connection):
        binding = _load_binding(connection, root_task_execution_id)
        if binding is None:
            return None
        return _load_workflow(connection, binding.workflow_uuid)

    def body(connection: Connection, observed_at: datetime):
        attempt = connection.execute(
            select(
                _TE.c.session_id,
                _TE.c.task_id,
                _TE.c.attempt_number,
                _SESSION.c.instance_id,
                _SESSION.c.project_id,
            )
            .select_from(_TE.join(_SESSION, _SESSION.c.id == _TE.c.session_id))
            .where(_TE.c.id == root_task_execution_id)
        ).first()
        if (
            attempt is None
            or not attempt.instance_id
            or (
                expected_session_instance_id is not None
                and attempt.instance_id != expected_session_instance_id
            )
        ):
            return WorkflowResult(BudgetOutcome.IDENTITY_CONFLICT), False
        binding = _load_binding(connection, root_task_execution_id)
        if binding is not None:
            if (
                expected_session_instance_id is not None
                and binding.session_instance_id != expected_session_instance_id
            ):
                return (
                    WorkflowResult(
                        BudgetOutcome.IDENTITY_CONFLICT,
                        _workflow_state(
                            _load_workflow(connection, binding.workflow_uuid)
                        ),
                    ),
                    False,
                )
            row = _load_workflow(connection, binding.workflow_uuid)
            if binding.binding_kind != BindingKind.ROOT.value:
                return (
                    WorkflowResult(
                        BudgetOutcome.IDENTITY_CONFLICT, _workflow_state(row)
                    ),
                    False,
                )
            return _existing_root(row, policy, observed_at), False
        if exclusive_open_task:
            live = connection.execute(
                select(_WF)
                .where(
                    _WF.c.task_id == attempt.task_id,
                    _WF.c.workflow_kind == WorkflowKind.TASK_EXECUTION.value,
                    _WF.c.status == "OPEN",
                )
                .order_by(_WF.c.created_at)
            ).first()
            if live is not None:
                outcome = (
                    BudgetOutcome.OPEN_WORKFLOW_EXISTS
                    if _utc(live.deadline_at) > observed_at
                    else BudgetOutcome.WORKFLOW_INFERENCE_DEADLINE_EXCEEDED
                )
                return (
                    WorkflowResult(outcome, _workflow_state(live)),
                    False,
                )
        values = _workflow_values(policy, observed_at)
        values.update(
            project_id=attempt.project_id,
            session_id=attempt.session_id,
            session_instance_id=attempt.instance_id,
            task_id=attempt.task_id,
            root_task_execution_id=root_task_execution_id,
        )
        try:
            connection.execute(insert(_WF).values(**values))
            connection.execute(
                insert(_BIND).values(
                    task_execution_id=root_task_execution_id,
                    workflow_uuid=values["workflow_uuid"],
                    binding_kind=BindingKind.ROOT.value,
                    parent_task_execution_id=None,
                    session_id=attempt.session_id,
                    session_instance_id=attempt.instance_id,
                    task_id=attempt.task_id,
                    attempt_number=attempt.attempt_number,
                    bound_at=observed_at,
                )
            )
        except IntegrityError:
            return WorkflowResult(BudgetOutcome.IDENTITY_CONFLICT), False
        created["uuid"] = values["workflow_uuid"]
        row = _load_workflow(connection, values["workflow_uuid"])
        return WorkflowResult(BudgetOutcome.CREATED, _workflow_state(row)), True

    def reconcile() -> WorkflowResult:
        try:
            row = _fresh_read(engine, _root_workflow)
        except (SQLAlchemyError, sqlite3.Error):
            return WorkflowResult(BudgetOutcome.COMMIT_UNCERTAIN)
        if row is not None and row.workflow_uuid == created.get("uuid"):
            return WorkflowResult(
                BudgetOutcome.CREATED, _workflow_state(row), reconciled=True
            )
        if row is not None:
            result = _existing_root(row, policy)
            return WorkflowResult(result.outcome, result.workflow, reconciled=True)
        return WorkflowResult(BudgetOutcome.COMMIT_UNCERTAIN, reconciled=True)

    return _run_write(engine, body, failed=_workflow_failed, reconcile=reconcile)


def create_planning_session_workflow(
    db: Session,
    policy: WorkflowBudgetPolicy,
    *,
    planning_session_id: int,
) -> WorkflowResult:
    """Create, or idempotently return, the workflow of one PlanningSession generation."""

    _require_enforced(policy, WorkflowKind.PLANNING_SESSION)
    _require_id(planning_session_id, "planning_session_id")
    engine = _engine(db)
    created: dict = {}

    def _generation_workflow(connection: Connection, generation_id: str):
        return connection.execute(
            select(_WF).where(_WF.c.planning_generation_id == generation_id)
        ).first()

    def body(connection: Connection, observed_at: datetime):
        planning = connection.execute(
            select(_PLANNING.c.generation_id, _PLANNING.c.project_id).where(
                _PLANNING.c.id == planning_session_id
            )
        ).first()
        if planning is None or not planning.generation_id:
            return WorkflowResult(BudgetOutcome.IDENTITY_CONFLICT), False
        created["generation"] = planning.generation_id
        existing = _generation_workflow(connection, planning.generation_id)
        if existing is not None:
            return _existing_root(existing, policy, observed_at), False
        values = _workflow_values(policy, observed_at)
        values.update(
            project_id=planning.project_id,
            planning_session_id=planning_session_id,
            planning_generation_id=planning.generation_id,
        )
        try:
            connection.execute(insert(_WF).values(**values))
        except IntegrityError:
            return WorkflowResult(BudgetOutcome.IDENTITY_CONFLICT), False
        created["uuid"] = values["workflow_uuid"]
        row = _load_workflow(connection, values["workflow_uuid"])
        return WorkflowResult(BudgetOutcome.CREATED, _workflow_state(row)), True

    def reconcile() -> WorkflowResult:
        try:
            row = _fresh_read(
                engine,
                lambda connection: _generation_workflow(
                    connection, created["generation"]
                ),
            )
        except (SQLAlchemyError, sqlite3.Error):
            return WorkflowResult(BudgetOutcome.COMMIT_UNCERTAIN)
        if row is not None and row.workflow_uuid == created.get("uuid"):
            return WorkflowResult(
                BudgetOutcome.CREATED, _workflow_state(row), reconciled=True
            )
        if row is not None:
            result = _existing_root(row, policy)
            return WorkflowResult(result.outcome, result.workflow, reconciled=True)
        return WorkflowResult(BudgetOutcome.COMMIT_UNCERTAIN, reconciled=True)

    return _run_write(engine, body, failed=_workflow_failed, reconcile=reconcile)


def bind_task_execution_attempt(
    db: Session,
    *,
    task_execution_id: int,
    parent_task_execution_id: int,
    binding_kind: BindingKind,
    expected_session_instance_id: str | None = None,
) -> BindingResult:
    """Bind a later retry/recovery attempt to its parent attempt's workflow.

    The child must belong to the same Session generation and Task with a
    larger attempt number, and the workflow must still be OPEN. A CLOSED
    workflow is never inherited; a new logical run needs a new root.
    """

    _require_id(task_execution_id, "task_execution_id")
    _require_id(parent_task_execution_id, "parent_task_execution_id")
    if binding_kind not in (BindingKind.RETRY, BindingKind.RECOVERY):
        raise ValueError("binding_kind must be RETRY or RECOVERY")
    engine = _engine(db)

    def _result(outcome: BudgetOutcome, binding=None, **extra) -> BindingResult:
        return BindingResult(
            outcome,
            task_execution_id,
            workflow_uuid=binding.workflow_uuid if binding is not None else None,
            binding_kind=binding.binding_kind if binding is not None else None,
            **extra,
        )

    def _classify_existing(existing, parent) -> BindingResult:
        if (
            parent is not None
            and existing.workflow_uuid == parent.workflow_uuid
            and existing.parent_task_execution_id == parent_task_execution_id
            and existing.binding_kind == binding_kind.value
            and (
                expected_session_instance_id is None
                or existing.session_instance_id == expected_session_instance_id
            )
        ):
            return _result(BudgetOutcome.ALREADY_BOUND, existing)
        return _result(BudgetOutcome.IDENTITY_CONFLICT, existing)

    def body(connection: Connection, observed_at: datetime):
        parent = _load_binding(connection, parent_task_execution_id)
        existing = _load_binding(connection, task_execution_id)
        if existing is not None:
            return _classify_existing(existing, parent), False
        if parent is None:
            # The parent lineage is legacy/unbudgeted; so is this attempt.
            return _result(BudgetOutcome.WORKFLOW_NOT_FOUND), False
        workflow = _load_workflow(connection, parent.workflow_uuid)
        if workflow.status != "OPEN":
            return _result(BudgetOutcome.WORKFLOW_CLOSED, parent), False
        if _utc(workflow.deadline_at) <= observed_at:
            return (
                _result(BudgetOutcome.WORKFLOW_INFERENCE_DEADLINE_EXCEEDED, parent),
                False,
            )
        child = connection.execute(
            select(
                _TE.c.session_id,
                _TE.c.task_id,
                _TE.c.attempt_number,
                _SESSION.c.instance_id,
            )
            .select_from(_TE.join(_SESSION, _SESSION.c.id == _TE.c.session_id))
            .where(_TE.c.id == task_execution_id)
        ).first()
        if (
            child is None
            or child.session_id != parent.session_id
            or child.task_id != parent.task_id
            or child.instance_id != parent.session_instance_id
            or (
                expected_session_instance_id is not None
                and (
                    child.instance_id != expected_session_instance_id
                    or parent.session_instance_id != expected_session_instance_id
                )
            )
            or child.attempt_number <= parent.attempt_number
        ):
            return _result(BudgetOutcome.IDENTITY_CONFLICT), False
        try:
            connection.execute(
                insert(_BIND).values(
                    task_execution_id=task_execution_id,
                    workflow_uuid=parent.workflow_uuid,
                    binding_kind=binding_kind.value,
                    parent_task_execution_id=parent_task_execution_id,
                    session_id=child.session_id,
                    session_instance_id=child.instance_id,
                    task_id=child.task_id,
                    attempt_number=child.attempt_number,
                    bound_at=observed_at,
                )
            )
        except IntegrityError:
            return _result(BudgetOutcome.IDENTITY_CONFLICT), False
        return (
            BindingResult(
                BudgetOutcome.BOUND,
                task_execution_id,
                workflow_uuid=parent.workflow_uuid,
                binding_kind=binding_kind.value,
            ),
            True,
        )

    def reconcile() -> BindingResult:
        try:
            existing, parent = _fresh_read(
                engine,
                lambda connection: (
                    _load_binding(connection, task_execution_id),
                    _load_binding(connection, parent_task_execution_id),
                ),
            )
        except (SQLAlchemyError, sqlite3.Error):
            return _result(BudgetOutcome.COMMIT_UNCERTAIN)
        if existing is None:
            return _result(BudgetOutcome.COMMIT_UNCERTAIN, reconciled=True)
        result = _classify_existing(existing, parent)
        outcome = (
            BudgetOutcome.BOUND
            if result.outcome is BudgetOutcome.ALREADY_BOUND
            else result.outcome
        )
        return _result(outcome, existing, reconciled=True)

    return _run_write(
        engine, body, failed=lambda outcome: _result(outcome), reconcile=reconcile
    )


def _read_engine(db: Session) -> Engine:
    engine = _engine(db)
    if _schema_outcome(engine) is not None:
        raise WorkflowBudgetSchemaUnavailable(
            "059 workflow budget schema is unavailable or unsupported"
        )
    return engine


def resolve_task_execution_workflow(
    db: Session, task_execution_id: int
) -> WorkflowBudgetState | None:
    """Read-only reconstruction; it never creates an identity.

    ``None`` means the attempt is legacy/unbudgeted (``LEGACY_UNENFORCED``).
    """

    _require_id(task_execution_id, "task_execution_id")

    def read(connection: Connection):
        return connection.execute(
            select(_WF)
            .select_from(_BIND.join(_WF, _WF.c.workflow_uuid == _BIND.c.workflow_uuid))
            .where(_BIND.c.task_execution_id == task_execution_id)
        ).first()

    return _workflow_state(_fresh_read(_read_engine(db), read))


def resolve_planning_session_workflow(
    db: Session, planning_session_id: int
) -> WorkflowBudgetState | None:
    """Read-only; matches only the PlanningSession's current immutable generation."""

    _require_id(planning_session_id, "planning_session_id")

    def read(connection: Connection):
        return connection.execute(
            select(_WF)
            .select_from(
                _PLANNING.join(
                    _WF, _WF.c.planning_generation_id == _PLANNING.c.generation_id
                )
            )
            .where(
                _PLANNING.c.id == planning_session_id,
                _WF.c.planning_session_id == planning_session_id,
            )
        ).first()

    return _workflow_state(_fresh_read(_read_engine(db), read))


def resolve_task_execution_binding(
    db: Session, task_execution_id: int
) -> BindingState | None:
    """Read-only binding snapshot; ``None`` means the attempt is unbound."""

    _require_id(task_execution_id, "task_execution_id")
    row = _fresh_read(
        _read_engine(db),
        lambda connection: _load_binding(connection, task_execution_id),
    )
    if row is None:
        return None
    return BindingState(
        task_execution_id=row.task_execution_id,
        workflow_uuid=row.workflow_uuid,
        binding_kind=row.binding_kind,
        parent_task_execution_id=row.parent_task_execution_id,
        session_id=row.session_id,
        session_instance_id=row.session_instance_id,
        task_id=row.task_id,
        attempt_number=row.attempt_number,
    )


def open_session_workflow_uuids(db: Session, session_id: int) -> tuple[str, ...]:
    """OPEN TASK_EXECUTION workflows owned by one Session, oldest first."""

    _require_id(session_id, "session_id")

    def read(connection: Connection):
        return connection.execute(
            select(_WF.c.workflow_uuid)
            .where(
                _WF.c.session_id == session_id,
                _WF.c.workflow_kind == WorkflowKind.TASK_EXECUTION.value,
                _WF.c.status == "OPEN",
            )
            .order_by(_WF.c.created_at, _WF.c.workflow_uuid)
        ).all()

    return tuple(row.workflow_uuid for row in _fresh_read(_read_engine(db), read))


def budget_enforcement_status(db: Session, *, task_execution_id: int) -> str:
    """``ENFORCED`` only when a durable ENFORCED budget owns the attempt."""

    if resolve_task_execution_workflow(db, task_execution_id) is None:
        return LEGACY_UNENFORCED
    return ENFORCED


def close_workflow(
    db: Session, workflow_uuid: str, *, require_quiescent: bool = False
) -> WorkflowResult:
    """OPEN→CLOSED: no further reservations or attempt bindings are accepted.

    ``require_quiescent`` refuses (``ATTEMPT_ACTIVE``) while any bound attempt
    is PENDING or RUNNING, checked inside the same write transaction, so a
    pending retry, recovery, or in-attempt Completion Repair keeps it OPEN.
    """

    _require_label(workflow_uuid, "workflow_uuid")
    engine = _engine(db)

    def body(connection: Connection, observed_at: datetime):
        row = _load_workflow(connection, workflow_uuid)
        if row is None:
            return WorkflowResult(BudgetOutcome.WORKFLOW_NOT_FOUND), False
        if row.status == "CLOSED":
            return (
                WorkflowResult(BudgetOutcome.ALREADY_CLOSED, _workflow_state(row)),
                False,
            )
        if require_quiescent:
            active = connection.execute(
                select(func.count())
                .select_from(_BIND.join(_TE, _TE.c.id == _BIND.c.task_execution_id))
                .where(
                    _BIND.c.workflow_uuid == workflow_uuid,
                    _TE.c.status.in_(
                        (TaskStatus.PENDING.name, TaskStatus.RUNNING.name)
                    ),
                )
            ).scalar_one()
            if active:
                return (
                    WorkflowResult(BudgetOutcome.ATTEMPT_ACTIVE, _workflow_state(row)),
                    False,
                )
        connection.execute(
            update(_WF)
            .where(_WF.c.workflow_uuid == workflow_uuid, _WF.c.status == "OPEN")
            .values(status="CLOSED", closed_at=observed_at)
        )
        row = _load_workflow(connection, workflow_uuid)
        return WorkflowResult(BudgetOutcome.CLOSED, _workflow_state(row)), True

    def reconcile() -> WorkflowResult:
        try:
            row = _fresh_read(engine, lambda c: _load_workflow(c, workflow_uuid))
        except (SQLAlchemyError, sqlite3.Error):
            return WorkflowResult(BudgetOutcome.COMMIT_UNCERTAIN)
        if row is not None and row.status == "CLOSED":
            return WorkflowResult(
                BudgetOutcome.CLOSED, _workflow_state(row), reconciled=True
            )
        return WorkflowResult(BudgetOutcome.COMMIT_UNCERTAIN, reconciled=True)

    return _run_write(engine, body, failed=_workflow_failed, reconcile=reconcile)


def reserve_provider_request(
    db: Session,
    *,
    workflow_uuid: str,
    idempotency_key: str,
    stage: str,
    runtime_role: str,
    task_execution_id: int | None = None,
    retry_of_reservation_uuid: str | None = None,
) -> ReservationResult:
    """Atomically consume one allowance and record one reservation.

    ``idempotency_key`` identifies this reservation *operation*: repeating
    the call with the same key and arguments (for example after
    ``COMMIT_UNCERTAIN``) returns the committed reservation without consuming
    again. A genuinely new provider POST, including a provider retry, must use
    a fresh key (see :func:`new_idempotency_key`) and may name the reservation
    it retries in ``retry_of_reservation_uuid``.

    Eligibility (workflow OPEN, trusted time strictly before ``deadline_at``,
    ``reserved_requests < max_requests``) is enforced by one conditional
    UPDATE under the write lock; rejection consumes nothing.
    """

    _require_label(workflow_uuid, "workflow_uuid")
    _require_label(idempotency_key, "idempotency_key", _IDEMPOTENCY_KEY)
    _require_label(stage, "stage")
    _require_label(runtime_role, "runtime_role")
    if task_execution_id is not None:
        _require_id(task_execution_id, "task_execution_id")
    if retry_of_reservation_uuid is not None:
        _require_label(retry_of_reservation_uuid, "retry_of_reservation_uuid")
    engine = _engine(db)
    reservation_uuid = str(uuid.uuid4())

    def _classify_replay(row) -> ReservationResult:
        if (
            row.stage == stage
            and row.runtime_role == runtime_role
            and row.task_execution_id == task_execution_id
            and row.retry_of_reservation_uuid == retry_of_reservation_uuid
        ):
            return ReservationResult(
                BudgetOutcome.ALREADY_RESERVED, _reservation_state(row)
            )
        return ReservationResult(
            BudgetOutcome.IDEMPOTENCY_CONFLICT, _reservation_state(row)
        )

    def body(connection: Connection, observed_at: datetime):
        replay = _load_reservation_by_key(connection, workflow_uuid, idempotency_key)
        if replay is not None:
            return _classify_replay(replay), False
        workflow = _load_workflow(connection, workflow_uuid)
        if workflow is None:
            return ReservationResult(BudgetOutcome.WORKFLOW_NOT_FOUND), False
        if workflow.workflow_kind == WorkflowKind.TASK_EXECUTION.value:
            binding = (
                None
                if task_execution_id is None
                else _load_binding(connection, task_execution_id)
            )
            if binding is None or binding.workflow_uuid != workflow_uuid:
                return ReservationResult(BudgetOutcome.IDENTITY_CONFLICT), False
        elif task_execution_id is not None:
            return ReservationResult(BudgetOutcome.IDENTITY_CONFLICT), False
        if retry_of_reservation_uuid is not None:
            prior = _load_reservation(connection, retry_of_reservation_uuid)
            if prior is None or prior.workflow_uuid != workflow_uuid:
                return ReservationResult(BudgetOutcome.IDENTITY_CONFLICT), False

        consumed = connection.execute(
            update(_WF)
            .where(
                _WF.c.workflow_uuid == workflow_uuid,
                _WF.c.status == "OPEN",
                _WF.c.enforcement_mode == EnforcementMode.ENFORCED.value,
                _WF.c.reserved_requests < _WF.c.max_requests,
                _WF.c.deadline_at > observed_at,
            )
            .values(reserved_requests=_WF.c.reserved_requests + 1)
        ).rowcount
        if consumed != 1:
            if workflow.status != "OPEN":
                outcome = BudgetOutcome.WORKFLOW_CLOSED
            elif _utc(workflow.deadline_at) <= observed_at:
                outcome = BudgetOutcome.WORKFLOW_INFERENCE_DEADLINE_EXCEEDED
            else:
                outcome = BudgetOutcome.PROVIDER_REQUEST_BUDGET_EXHAUSTED
            return ReservationResult(outcome), False
        ordinal = connection.execute(
            select(_WF.c.reserved_requests).where(_WF.c.workflow_uuid == workflow_uuid)
        ).scalar_one()
        try:
            connection.execute(
                insert(_RES).values(
                    reservation_uuid=reservation_uuid,
                    workflow_uuid=workflow_uuid,
                    request_ordinal=ordinal,
                    idempotency_key=idempotency_key,
                    stage=stage,
                    runtime_role=runtime_role,
                    task_execution_id=task_execution_id,
                    retry_of_reservation_uuid=retry_of_reservation_uuid,
                    status=ReservationStatus.RESERVED.value,
                    outcome_code=None,
                    reserved_at=observed_at,
                    dispatch_started_at=None,
                    completed_at=None,
                )
            )
        except IntegrityError:
            return ReservationResult(BudgetOutcome.IDENTITY_CONFLICT), False
        # A second sample bounds lock-held work; it never admits a late reservation.
        if _utc(_trusted_utc_now()) >= _utc(workflow.deadline_at):
            return (
                ReservationResult(BudgetOutcome.WORKFLOW_INFERENCE_DEADLINE_EXCEEDED),
                False,
            )
        row = _load_reservation(connection, reservation_uuid)
        return ReservationResult(BudgetOutcome.RESERVED, _reservation_state(row)), True

    def reconcile() -> ReservationResult:
        try:
            row = _fresh_read(
                engine,
                lambda c: _load_reservation_by_key(c, workflow_uuid, idempotency_key),
            )
        except (SQLAlchemyError, sqlite3.Error):
            return ReservationResult(BudgetOutcome.COMMIT_UNCERTAIN)
        if row is None:
            return ReservationResult(BudgetOutcome.COMMIT_UNCERTAIN, reconciled=True)
        if row.reservation_uuid == reservation_uuid:
            return ReservationResult(
                BudgetOutcome.RESERVED, _reservation_state(row), reconciled=True
            )
        result = _classify_replay(row)
        return ReservationResult(result.outcome, result.reservation, reconciled=True)

    return _run_write(
        engine,
        body,
        failed=lambda outcome: ReservationResult(outcome),
        reconcile=reconcile,
    )


def _load_reservation_with_workflow(connection: Connection, reservation_uuid: str):
    return connection.execute(
        select(
            _RES,
            _WF.c.status.label("workflow_status"),
            _WF.c.deadline_at.label("workflow_deadline_at"),
        )
        .select_from(_RES.join(_WF, _WF.c.workflow_uuid == _RES.c.workflow_uuid))
        .where(_RES.c.reservation_uuid == reservation_uuid)
    ).first()


def _blocked_transition(row, target: ReservationStatus) -> BudgetOutcome | None:
    if row.status == target.value:
        return BudgetOutcome.ALREADY_IN_STATE
    if ReservationStatus(row.status) in TERMINAL_STATUSES:
        return BudgetOutcome.TERMINAL_CONFLICT
    return None


def mark_dispatch_started(db: Session, reservation_uuid: str) -> TransitionResult:
    """RESERVED→DISPATCH_STARTED: the only local authorization to send.

    Commit this before the provider POST and send only when
    ``dispatch_permitted``. ``remaining_seconds`` is measured from the
    persisted deadline under the write lock; the caller clamps its request
    timeout to it. A replay, rival, or uncertain commit never permits a send.
    """

    _require_label(reservation_uuid, "reservation_uuid")
    engine = _engine(db)

    def body(connection: Connection, observed_at: datetime):
        row = _load_reservation_with_workflow(connection, reservation_uuid)
        if row is None:
            return TransitionResult(BudgetOutcome.RESERVATION_NOT_FOUND), False
        blocked = _blocked_transition(row, ReservationStatus.DISPATCH_STARTED)
        if blocked is not None:
            return TransitionResult(blocked, _reservation_state(row)), False
        if row.workflow_status != "OPEN":
            return (
                TransitionResult(
                    BudgetOutcome.WORKFLOW_CLOSED, _reservation_state(row)
                ),
                False,
            )
        deadline_at = _utc(row.workflow_deadline_at)
        if deadline_at <= observed_at:
            return (
                TransitionResult(
                    BudgetOutcome.WORKFLOW_INFERENCE_DEADLINE_EXCEEDED,
                    _reservation_state(row),
                ),
                False,
            )
        connection.execute(
            update(_RES)
            .where(
                _RES.c.reservation_uuid == reservation_uuid,
                _RES.c.status == ReservationStatus.RESERVED.value,
            )
            .values(
                status=ReservationStatus.DISPATCH_STARTED.value,
                dispatch_started_at=observed_at,
            )
        )
        row = _load_reservation(connection, reservation_uuid)
        return (
            TransitionResult(
                BudgetOutcome.TRANSITIONED,
                _reservation_state(row),
                remaining_seconds=(deadline_at - observed_at).total_seconds(),
            ),
            True,
        )

    return _run_write(
        engine,
        body,
        failed=lambda outcome: TransitionResult(outcome),
        # Ownership of an ambiguous dispatch marker cannot be proven, so the
        # caller holds; the stale marker later reconciles to OUTCOME_UNKNOWN.
        reconcile=lambda: TransitionResult(
            BudgetOutcome.COMMIT_UNCERTAIN, reconciled=True
        ),
    )


def record_reservation_outcome(
    db: Session,
    reservation_uuid: str,
    status: ReservationStatus,
    *,
    outcome_code: str | None = None,
) -> TransitionResult:
    """Persist one terminal outcome; the first terminal state is final.

    RESERVED may only become CANCELLED (never dispatched). Repeating the same
    terminal status is an idempotent no-op that keeps the first outcome code.
    """

    _require_label(reservation_uuid, "reservation_uuid")
    if status not in TERMINAL_STATUSES:
        raise ValueError("status must be a terminal reservation status")
    if outcome_code is not None:
        _require_label(outcome_code, "outcome_code")
    engine = _engine(db)

    def body(connection: Connection, observed_at: datetime):
        row = _load_reservation(connection, reservation_uuid)
        if row is None:
            return TransitionResult(BudgetOutcome.RESERVATION_NOT_FOUND), False
        blocked = _blocked_transition(row, status)
        if blocked is not None:
            return TransitionResult(blocked, _reservation_state(row)), False
        if (
            row.status == ReservationStatus.RESERVED.value
            and status is not ReservationStatus.CANCELLED
        ):
            return (
                TransitionResult(
                    BudgetOutcome.INVALID_TRANSITION, _reservation_state(row)
                ),
                False,
            )
        connection.execute(
            update(_RES)
            .where(
                _RES.c.reservation_uuid == reservation_uuid,
                _RES.c.status == row.status,
            )
            .values(
                status=status.value, outcome_code=outcome_code, completed_at=observed_at
            )
        )
        row = _load_reservation(connection, reservation_uuid)
        return (
            TransitionResult(BudgetOutcome.TRANSITIONED, _reservation_state(row)),
            True,
        )

    def reconcile() -> TransitionResult:
        try:
            row = _fresh_read(engine, lambda c: _load_reservation(c, reservation_uuid))
        except (SQLAlchemyError, sqlite3.Error):
            return TransitionResult(BudgetOutcome.COMMIT_UNCERTAIN)
        if row is not None and row.status == status.value:
            return TransitionResult(
                BudgetOutcome.TRANSITIONED, _reservation_state(row), reconciled=True
            )
        return TransitionResult(
            BudgetOutcome.COMMIT_UNCERTAIN, _reservation_state(row), reconciled=True
        )

    return _run_write(
        engine,
        body,
        failed=lambda outcome: TransitionResult(outcome),
        reconcile=reconcile,
    )


def reconcile_stale_reservations(
    db: Session,
    *,
    stale_after_seconds: int,
    workflow_uuid: str | None = None,
) -> ReconciliationReport:
    """Conservatively terminate reservations abandoned by a lost process.

    A RESERVED row older than the threshold was never authorized to send, so
    it becomes CANCELLED (``STALE_NOT_DISPATCHED``). A DISPATCH_STARTED row
    older than the threshold may have reached the provider, so it becomes
    OUTCOME_UNKNOWN (``STALE_DISPATCH_AMBIGUOUS``). Neither is refunded,
    nothing is re-sent, and no reservation is created. Terminal rows are
    untouched, so repeated runs are no-ops. The threshold must exceed the
    longest permitted request timeout.
    """

    if type(stale_after_seconds) is not int or stale_after_seconds < 1:
        raise ValueError("stale_after_seconds must be a positive integer")
    if workflow_uuid is not None:
        _require_label(workflow_uuid, "workflow_uuid")
    engine = _engine(db)

    def _scope(*conditions):
        if workflow_uuid is not None:
            conditions = (*conditions, _RES.c.workflow_uuid == workflow_uuid)
        return conditions

    def body(connection: Connection, observed_at: datetime):
        cutoff = observed_at - timedelta(seconds=stale_after_seconds)
        groups = []
        for source, age_column, target, code in (
            (
                ReservationStatus.RESERVED,
                _RES.c.reserved_at,
                ReservationStatus.CANCELLED,
                STALE_NOT_DISPATCHED,
            ),
            (
                ReservationStatus.DISPATCH_STARTED,
                _RES.c.dispatch_started_at,
                ReservationStatus.OUTCOME_UNKNOWN,
                STALE_DISPATCH_AMBIGUOUS,
            ),
        ):
            conditions = _scope(_RES.c.status == source.value, age_column < cutoff)
            uuids = tuple(
                connection.execute(
                    select(_RES.c.reservation_uuid)
                    .where(*conditions)
                    .order_by(_RES.c.reservation_uuid)
                ).scalars()
            )
            if uuids:
                connection.execute(
                    update(_RES)
                    .where(*conditions)
                    .values(
                        status=target.value,
                        outcome_code=code,
                        completed_at=observed_at,
                    )
                )
            groups.append(uuids)
        report = ReconciliationReport(
            BudgetOutcome.RECONCILED, groups[0], groups[1], observed_at
        )
        return report, bool(groups[0] or groups[1])

    return _run_write(
        engine,
        body,
        failed=lambda outcome: ReconciliationReport(outcome),
        # Re-running is safe: a committed pass leaves nothing stale to redo.
        reconcile=lambda: ReconciliationReport(BudgetOutcome.COMMIT_UNCERTAIN),
    )


def inspect_workflow_accounting(
    db: Session, workflow_uuid: str
) -> WorkflowAccounting | None:
    """Read-only audit: the counter must equal the number of reservations."""

    _require_label(workflow_uuid, "workflow_uuid")

    def read(connection: Connection):
        # One statement, so the counter and the ledger come from one snapshot.
        count = (
            select(func.count())
            .select_from(_RES)
            .where(_RES.c.workflow_uuid == _WF.c.workflow_uuid)
            .scalar_subquery()
        )
        row = connection.execute(
            select(_WF, count.label("reservation_count")).where(
                _WF.c.workflow_uuid == workflow_uuid
            )
        ).first()
        statuses = connection.execute(
            select(_RES.c.status, func.count())
            .where(_RES.c.workflow_uuid == workflow_uuid)
            .group_by(_RES.c.status)
        ).all()
        return row, statuses

    row, statuses = _fresh_read(_read_engine(db), read)
    if row is None:
        return None
    return WorkflowAccounting(
        workflow=_workflow_state(row),
        reservation_count=row.reservation_count,
        status_counts={status: count for status, count in statuses},
    )
