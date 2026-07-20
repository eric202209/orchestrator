"""Content-agnostic stage orchestration for Planning Protocol v2.

The engine owns stage lifecycle policy.  A stage supplies only its execution,
validation, and acceptance behavior; persistence, fencing, dependency loading,
retry, invalidation, recovery, and completion remain here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import json
import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    PlanningCheckpoint,
    PlanningCompletionManifest,
    PlanningProtocolInput,
    PlanningSession,
)
from app.services.planning.protocol_persistence import (
    PROTOCOL_V2,
    PlanningProtocolPersistenceService,
    ProtocolOwnershipError,
)
from app.services.planning.input_manifest import InputManifest
from app.services.planning.planning_brief import PlanningBrief

logger = logging.getLogger(__name__)


class StageEngineError(RuntimeError):
    """The stage graph or lifecycle cannot be advanced safely."""


class StageOwnershipError(StageEngineError):
    """The session does not have a usable current owner fence."""


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    VALIDATED = "validated"
    ACCEPTED = "accepted"
    FAILED = "failed"
    INVALIDATED = "invalidated"
    BLOCKED = "blocked"
    COMPLETED = "completed"


@dataclass(frozen=True)
class StageExecutionPolicy:
    """Execution controls that do not contain provider-specific behavior."""

    retryable: bool = True
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")


@dataclass(frozen=True)
class StageValidationPolicy:
    """Validation persistence policy shared by all stage types."""

    persist_rejected_output: bool = True


@dataclass(frozen=True)
class StageAcceptancePolicy:
    """Acceptance controls shared by all stage types."""

    require_explicit_acceptance: bool = True


@dataclass(frozen=True)
class StageValidation:
    valid: bool
    reason: str | None = None


@dataclass(frozen=True)
class StageAcceptance:
    accepted: bool
    reason: str | None = None


@dataclass(frozen=True)
class StageInstance:
    session_id: int | None
    stage_identifier: str
    stage_version: int
    stage_generation_id: str
    attempt_id: str


@dataclass(frozen=True)
class StageFailure:
    stage_identifier: str
    attempt_id: str
    reason: str
    checkpoint: PlanningCheckpoint | None = None


@dataclass(frozen=True)
class StageRetry:
    stage_identifier: str
    previous_attempt_id: str | None
    attempt_id: str


@dataclass(frozen=True)
class StageInvalidation:
    stage_identifier: str
    checkpoint_id: int
    reason: str


StageExecute = Callable[["StageContext"], Any]
StageValidate = Callable[[Any, "StageContext"], Any]
StageAccept = Callable[[Any, "StageContext"], Any]


class StageDefinition:
    """Definition and behavior contract for one reusable stage.

    Subclasses may override ``execute``, ``validate``, and ``accept``.  Small
    stages can instead pass the three callbacks to the constructor.  Neither
    form knows anything about Planning artifacts or providers.
    """

    def __init__(
        self,
        identifier: str,
        *,
        version: int = 1,
        prerequisites: Iterable[str] = (),
        execution_policy: StageExecutionPolicy | None = None,
        validation_policy: StageValidationPolicy | None = None,
        acceptance_policy: StageAcceptancePolicy | None = None,
        execute: StageExecute | None = None,
        validate: StageValidate | None = None,
        accept: StageAccept | None = None,
    ) -> None:
        normalized_identifier = str(identifier or "").strip()
        if not normalized_identifier:
            raise ValueError("stage identifier is required")
        if version < 1:
            raise ValueError("stage version must be positive")
        self.identifier = normalized_identifier
        self.version = int(version)
        self.prerequisites = tuple(
            dict.fromkeys(
                str(prerequisite).strip()
                for prerequisite in prerequisites
                if str(prerequisite).strip()
            )
        )
        self.execution_policy = execution_policy or StageExecutionPolicy()
        self.validation_policy = validation_policy or StageValidationPolicy()
        self.acceptance_policy = acceptance_policy or StageAcceptancePolicy()
        self._execute = execute
        self._validate = validate
        self._accept = accept

    def execute(self, context: "StageContext") -> Any:
        if self._execute is None:
            raise NotImplementedError(
                f"stage {self.identifier!r} must implement execute()"
            )
        return self._execute(context)

    def validate(self, output: Any, context: "StageContext") -> Any:
        if self._validate is None:
            return StageValidation(valid=True)
        return self._validate(output, context)

    def accept(self, output: Any, context: "StageContext") -> Any:
        if self._accept is None:
            return StageAcceptance(accepted=True)
        return self._accept(output, context)


@dataclass(frozen=True)
class StageOwnership:
    session_id: int
    session_generation_id: str
    fencing_token: str


class StageDependencyGraph:
    """Validated, deterministic DAG of stage definitions."""

    def __init__(self, definitions: Iterable[StageDefinition] = ()) -> None:
        by_identifier: dict[str, StageDefinition] = {}
        for definition in definitions:
            if definition.identifier in by_identifier:
                raise StageEngineError(
                    f"duplicate stage identifier: {definition.identifier}"
                )
            by_identifier[definition.identifier] = definition
        for definition in by_identifier.values():
            missing = sorted(set(definition.prerequisites) - set(by_identifier))
            if missing:
                raise StageEngineError(
                    f"stage {definition.identifier!r} has unknown prerequisites: {missing}"
                )
        self._definitions = by_identifier
        self._topological_order = self._sort_topologically()

    @property
    def definitions(self) -> tuple[StageDefinition, ...]:
        return tuple(self._definitions[name] for name in self._topological_order)

    @property
    def identifiers(self) -> tuple[str, ...]:
        return self._topological_order

    def get(self, identifier: str) -> StageDefinition:
        try:
            return self._definitions[identifier]
        except KeyError as exc:
            raise StageEngineError(f"unknown stage: {identifier}") from exc

    def descendants(self, identifier: str) -> tuple[str, ...]:
        self.get(identifier)
        children = {
            name: set(definition.prerequisites)
            for name, definition in self._definitions.items()
        }
        found: set[str] = set()
        frontier = [identifier]
        while frontier:
            parent = frontier.pop(0)
            for name in sorted(children):
                if parent in children[name] and name not in found:
                    found.add(name)
                    frontier.append(name)
        return tuple(name for name in self._topological_order if name in found)

    def _sort_topologically(self) -> tuple[str, ...]:
        remaining = {
            name: set(definition.prerequisites)
            for name, definition in self._definitions.items()
        }
        ordered: list[str] = []
        while remaining:
            ready = sorted(name for name, deps in remaining.items() if not deps)
            if not ready:
                cycle = ", ".join(sorted(remaining))
                raise StageEngineError(f"stage dependency cycle: {cycle}")
            ordered.extend(ready)
            for name in ready:
                remaining.pop(name)
            for deps in remaining.values():
                deps.difference_update(ready)
        return tuple(ordered)


class StageCheckpointAccess:
    """Read-only stage-facing checkpoint access backed by the persistence API."""

    def __init__(
        self,
        persistence: PlanningProtocolPersistenceService,
        ownership: StageOwnership,
        protocol_version: str,
    ) -> None:
        self._persistence = persistence
        self.ownership = ownership
        self.protocol_version = protocol_version

    def accepted_predecessors(
        self, stage_versions: Mapping[str, int]
    ) -> dict[str, PlanningCheckpoint]:
        return self._persistence.accepted_predecessors(
            self.ownership.session_id,
            stage_versions=stage_versions,
        )

    def all_checkpoints(self) -> list[PlanningCheckpoint]:
        return self._persistence.list_checkpoints(self.ownership.session_id)

    def assert_owner(self) -> PlanningSession:
        return self._persistence.assert_owner(
            self.ownership.session_id,
            protocol_version=self.protocol_version,
            session_generation_id=self.ownership.session_generation_id,
            fencing_token=self.ownership.fencing_token,
        )


@dataclass
class StageContext:
    session: PlanningSession
    protocol_version: str
    protocol_input: PlanningProtocolInput | None
    input_manifest: InputManifest
    checkpoint_access: StageCheckpointAccess
    dependency_graph: StageDependencyGraph
    ownership: StageOwnership
    configuration: Mapping[str, Any] = field(default_factory=dict)
    logger: logging.Logger = field(default_factory=lambda: logger)
    predecessor_checkpoints: Mapping[str, PlanningCheckpoint] = field(
        default_factory=dict
    )
    planning_brief: PlanningBrief | None = None

    @property
    def accepted_brief(self) -> PlanningBrief | None:
        """The accepted Planning Brief predecessor, or ``None``."""

        return self.planning_brief


@dataclass
class StageExecution:
    stage: StageDefinition
    attempt_id: str
    stage_generation_id: str
    status: StageStatus
    predecessor_checkpoints: Mapping[str, PlanningCheckpoint] = field(
        default_factory=dict
    )
    output: Any = None
    validation: StageValidation | None = None
    acceptance: StageAcceptance | None = None
    checkpoint: PlanningCheckpoint | None = None
    error: str | None = None
    retry_event: StageRetry | None = None

    @property
    def instance(self) -> StageInstance:
        return StageInstance(
            session_id=(
                self.checkpoint.planning_session_id
                if self.checkpoint is not None
                else None
            ),
            stage_identifier=self.stage.identifier,
            stage_version=self.stage.version,
            stage_generation_id=self.stage_generation_id,
            attempt_id=self.attempt_id,
        )

    @property
    def failure(self) -> StageFailure | None:
        if self.status != StageStatus.FAILED:
            return None
        return StageFailure(
            stage_identifier=self.stage.identifier,
            attempt_id=self.attempt_id,
            reason=self.error or "stage failed",
            checkpoint=self.checkpoint,
        )


@dataclass(frozen=True)
class StageRecovery:
    session_id: int
    resumable: bool
    next_stage: str | None
    reason: str
    effective_checkpoints: Mapping[tuple[str, int], PlanningCheckpoint]


@dataclass(frozen=True)
class StageCompletion:
    complete: bool
    reason: str
    manifest: PlanningCompletionManifest | None = None


@dataclass
class StageRunResult:
    status: StageStatus
    execution: StageExecution | None = None
    completion: StageCompletion | None = None
    reason: str | None = None


class StageExecutor:
    """Execute, persist, recover, and advance a Protocol v2 stage graph."""

    def __init__(
        self,
        db: Session,
        stage_definitions: Iterable[StageDefinition] = (),
        *,
        configuration: Mapping[str, Any] | None = None,
        stage_logger: logging.Logger | None = None,
    ) -> None:
        self.db = db
        self.persistence = PlanningProtocolPersistenceService(db)
        self.graph = StageDependencyGraph(stage_definitions)
        self.configuration = dict(configuration or {})
        self.logger = stage_logger or logger
        self._running: set[tuple[int, str]] = set()

    def acquire_ownership(
        self,
        session_id: int,
        *,
        session_generation_id: str,
        fencing_token: str,
    ) -> StageOwnership:
        """Validate session-level ownership before stage execution.

        The PlanningSessionService remains the authority that acquires and
        replaces leases.  This method is the stage-engine boundary that makes
        the same fence explicit and reusable.
        """

        session = self.persistence.assert_owner(
            session_id,
            protocol_version=PROTOCOL_V2,
            session_generation_id=session_generation_id,
            fencing_token=fencing_token,
        )
        return StageOwnership(
            session_id=session.id,
            session_generation_id=session.generation_id,
            fencing_token=fencing_token,
        )

    def recover(self, session_id: int) -> StageRecovery:
        state = self.persistence.recovery_state(session_id)
        effective = self.persistence.effective_checkpoints(
            session_id,
            stage_versions={
                definition.identifier: definition.version
                for definition in self.graph.definitions
            },
        )
        for identifier in self.graph.identifiers:
            definition = self.graph.get(identifier)
            current = effective.get((identifier, definition.version))
            if current is not None and current.status == "accepted":
                continue
            if current is not None and current.status == "invalidated":
                return StageRecovery(
                    session_id,
                    True,
                    identifier,
                    "invalidated stage is resumable",
                    effective,
                )
            if current is not None and current.status == "failed":
                return StageRecovery(
                    session_id,
                    True,
                    identifier,
                    "failed stage is retryable",
                    effective,
                )
            return StageRecovery(
                session_id,
                True,
                identifier,
                "stage has not completed",
                effective,
            )
        if not self.graph.identifiers:
            return StageRecovery(
                session_id,
                True,
                None,
                "no stages registered; orchestration can complete",
                effective,
            )
        return StageRecovery(
            session_id,
            False,
            None,
            "all registered stages accepted",
            effective,
        )

    def load_accepted_predecessors(
        self, session_id: int, stage_identifier: str
    ) -> dict[str, PlanningCheckpoint]:
        definition = self.graph.get(stage_identifier)
        return self.persistence.accepted_predecessors(
            session_id,
            stage_versions={
                prerequisite: self.graph.get(prerequisite).version
                for prerequisite in sorted(definition.prerequisites)
            },
        )

    def execute_stage(
        self,
        session_id: int,
        stage_identifier: str,
        *,
        session_generation_id: str,
        fencing_token: str,
        retry: bool = False,
        force: bool = False,
    ) -> StageExecution:
        definition = self.graph.get(stage_identifier)
        ownership = self.acquire_ownership(
            session_id,
            session_generation_id=session_generation_id,
            fencing_token=fencing_token,
        )
        current = self.persistence.effective_checkpoints(
            session_id,
            stage_versions={stage_identifier: definition.version},
        ).get((stage_identifier, definition.version))
        if current is not None and current.status == "accepted" and not retry:
            return StageExecution(
                definition,
                current.attempt_id,
                current.stage_generation_id,
                StageStatus.ACCEPTED,
                checkpoint=current,
            )
        if current is not None and current.status == "accepted" and retry and not force:
            raise StageEngineError(
                f"accepted stage {stage_identifier!r} cannot be retried directly"
            )
        if (
            current is not None
            and not retry
            and current.status
            in {
                "failed",
                "invalidated",
            }
        ):
            return StageExecution(
                definition,
                current.attempt_id,
                current.stage_generation_id,
                StageStatus(current.status),
                checkpoint=current,
                error=current.failure_reason,
            )

        predecessors = self.load_accepted_predecessors(session_id, stage_identifier)
        missing = sorted(set(definition.prerequisites) - set(predecessors))
        attempt_id = str(uuid.uuid4())
        stage_generation_id = str(uuid.uuid4())
        if missing:
            return StageExecution(
                definition,
                attempt_id,
                stage_generation_id,
                StageStatus.BLOCKED,
                predecessor_checkpoints=predecessors,
                error=f"accepted prerequisites missing: {', '.join(missing)}",
            )

        session = self._get_session(session_id)
        checkpoint_access = StageCheckpointAccess(
            self.persistence, ownership, session.protocol_version
        )
        context = StageContext(
            session=session,
            protocol_version=session.protocol_version,
            protocol_input=self.persistence.recovery_state(session_id)["input"],
            input_manifest=self._require_input_manifest(session_id),
            checkpoint_access=checkpoint_access,
            dependency_graph=self.graph,
            ownership=ownership,
            configuration=self.configuration,
            logger=self.logger,
            predecessor_checkpoints=predecessors,
            planning_brief=self._load_accepted_planning_brief(session_id),
        )
        key = (session_id, stage_identifier)
        self._running.add(key)
        try:
            self.persistence.assert_owner(
                session_id,
                protocol_version=PROTOCOL_V2,
                session_generation_id=ownership.session_generation_id,
                fencing_token=ownership.fencing_token,
            )
            output = definition.execute(context)
            validation = self._normalize_validation(
                definition.validate(output, context)
            )
            if not validation.valid:
                checkpoint = self._record_failure(
                    session_id,
                    definition,
                    output,
                    attempt_id,
                    stage_generation_id,
                    ownership,
                    validation.reason or "stage validation failed",
                )
                return StageExecution(
                    definition,
                    attempt_id,
                    stage_generation_id,
                    StageStatus.FAILED,
                    predecessors,
                    output,
                    validation,
                    error=validation.reason or "stage validation failed",
                    checkpoint=checkpoint,
                )
            acceptance = self._normalize_acceptance(definition.accept(output, context))
            if not acceptance.accepted:
                checkpoint = self._record_failure(
                    session_id,
                    definition,
                    output,
                    attempt_id,
                    stage_generation_id,
                    ownership,
                    acceptance.reason or "stage acceptance rejected",
                )
                return StageExecution(
                    definition,
                    attempt_id,
                    stage_generation_id,
                    StageStatus.FAILED,
                    predecessors,
                    output,
                    validation,
                    acceptance,
                    checkpoint,
                    acceptance.reason or "stage acceptance rejected",
                )
            self.persistence.assert_owner(
                session_id,
                protocol_version=PROTOCOL_V2,
                session_generation_id=ownership.session_generation_id,
                fencing_token=ownership.fencing_token,
            )
            checkpoint = self.persistence.record_checkpoint(
                session_id,
                stage_name=definition.identifier,
                checkpoint_version=definition.version,
                content=self._serialize_output(output),
                stage_generation_id=stage_generation_id,
                attempt_id=attempt_id,
                fencing_token=ownership.fencing_token,
                session_generation_id=ownership.session_generation_id,
                protocol_version=PROTOCOL_V2,
                status="accepted",
                parent_checkpoint_ids=[
                    predecessors[name].id for name in sorted(predecessors)
                ],
            )
            return StageExecution(
                definition,
                attempt_id,
                stage_generation_id,
                StageStatus.ACCEPTED,
                predecessors,
                output,
                validation,
                acceptance,
                checkpoint,
            )
        except ProtocolOwnershipError:
            self.db.rollback()
            raise
        except Exception as exc:
            self.db.rollback()
            try:
                checkpoint = self._record_failure(
                    session_id,
                    definition,
                    None,
                    attempt_id,
                    stage_generation_id,
                    ownership,
                    str(exc),
                )
            except ProtocolOwnershipError:
                self.db.rollback()
                raise
            return StageExecution(
                definition,
                attempt_id,
                stage_generation_id,
                StageStatus.FAILED,
                predecessors,
                error=str(exc),
                checkpoint=checkpoint,
            )
        finally:
            self._running.discard(key)

    def retry_stage(
        self,
        session_id: int,
        stage_identifier: str,
        *,
        session_generation_id: str,
        fencing_token: str,
    ) -> StageExecution:
        definition = self.graph.get(stage_identifier)
        ownership = self.acquire_ownership(
            session_id,
            session_generation_id=session_generation_id,
            fencing_token=fencing_token,
        )
        current = self.persistence.effective_checkpoints(
            session_id,
            stage_versions={stage_identifier: definition.version},
        ).get((stage_identifier, definition.version))
        self.invalidate_downstream(
            session_id,
            stage_identifier,
            session_generation_id=ownership.session_generation_id,
            fencing_token=ownership.fencing_token,
            reason=f"stage {stage_identifier} retry",
        )
        execution = self.execute_stage(
            session_id,
            stage_identifier,
            session_generation_id=ownership.session_generation_id,
            fencing_token=ownership.fencing_token,
            retry=True,
            force=True,
        )
        execution.retry_event = StageRetry(
            stage_identifier=stage_identifier,
            previous_attempt_id=current.attempt_id if current is not None else None,
            attempt_id=execution.attempt_id,
        )
        return execution

    def invalidate_downstream(
        self,
        session_id: int,
        stage_identifier: str,
        *,
        session_generation_id: str,
        fencing_token: str,
        reason: str = "predecessor changed",
    ) -> list[PlanningCheckpoint]:
        ownership = self.acquire_ownership(
            session_id,
            session_generation_id=session_generation_id,
            fencing_token=fencing_token,
        )
        return self.persistence.invalidate_checkpoints(
            session_id,
            stage_names=self.graph.descendants(stage_identifier),
            reason=reason,
            fencing_token=ownership.fencing_token,
            session_generation_id=ownership.session_generation_id,
            protocol_version=PROTOCOL_V2,
        )

    def evaluate_completion(
        self,
        session_id: int,
        *,
        session_generation_id: str,
        fencing_token: str,
        required_stage_identifiers: Sequence[str] | None = None,
    ) -> StageCompletion:
        ownership = self.acquire_ownership(
            session_id,
            session_generation_id=session_generation_id,
            fencing_token=fencing_token,
        )
        # Completion is also a recovery boundary: it must attest to the
        # persisted manifest, never to a live reconstruction of inputs.
        self._require_input_manifest(session_id)
        required = tuple(
            required_stage_identifiers
            if required_stage_identifiers is not None
            else self.graph.identifiers
        )
        for identifier in required:
            self.graph.get(identifier)
        if any((session_id, identifier) in self._running for identifier in required):
            return StageCompletion(False, "a required stage is still running")
        effective = self.persistence.effective_checkpoints(
            session_id,
            stage_versions={
                identifier: self.graph.get(identifier).version
                for identifier in required
            },
        )
        accepted = []
        dependency_hashes: set[str] = set()
        for identifier in required:
            definition = self.graph.get(identifier)
            checkpoint = effective.get((identifier, definition.version))
            if checkpoint is None:
                return StageCompletion(False, f"stage {identifier} is not accepted")
            if checkpoint.status == "invalidated":
                return StageCompletion(False, f"stage {identifier} is invalidated")
            if checkpoint.status != "accepted":
                return StageCompletion(False, f"stage {identifier} is not accepted")
            accepted.append(
                {
                    "checkpoint_id": checkpoint.id,
                    "stage_name": checkpoint.stage_name,
                    "checkpoint_version": checkpoint.checkpoint_version,
                    "content_hash": checkpoint.content_hash,
                }
            )
            for dependency in checkpoint.dependencies:
                parent = self.db.get(
                    PlanningCheckpoint, dependency.parent_checkpoint_id
                )
                if parent is not None:
                    dependency_hashes.add(parent.content_hash)
        manifest = self.persistence.record_completion_manifest(
            session_id,
            accepted_checkpoint_versions=accepted,
            dependency_hashes=sorted(dependency_hashes),
            fencing_token=ownership.fencing_token,
            session_generation_id=ownership.session_generation_id,
            protocol_version=PROTOCOL_V2,
        )
        return StageCompletion(True, "all required stages accepted", manifest)

    def advance(
        self,
        session_id: int,
        *,
        session_generation_id: str,
        fencing_token: str,
    ) -> StageRunResult:
        """Run the next resumable stage until failure or completion."""

        ownership = self.acquire_ownership(
            session_id,
            session_generation_id=session_generation_id,
            fencing_token=fencing_token,
        )
        if not self.graph.identifiers:
            completion = self.evaluate_completion(
                session_id,
                session_generation_id=ownership.session_generation_id,
                fencing_token=ownership.fencing_token,
            )
            return StageRunResult(
                StageStatus.COMPLETED if completion.complete else StageStatus.BLOCKED,
                completion=completion,
                reason=completion.reason,
            )

        for identifier in self.graph.identifiers:
            definition = self.graph.get(identifier)
            current = self.persistence.effective_checkpoints(
                session_id,
                stage_versions={identifier: definition.version},
            ).get((identifier, definition.version))
            if current is not None and current.status == "accepted":
                continue
            if current is not None and current.status in {"failed", "invalidated"}:
                execution = self.retry_stage(
                    session_id,
                    identifier,
                    session_generation_id=ownership.session_generation_id,
                    fencing_token=ownership.fencing_token,
                )
            else:
                execution = self.execute_stage(
                    session_id,
                    identifier,
                    session_generation_id=ownership.session_generation_id,
                    fencing_token=ownership.fencing_token,
                )
            if execution.status != StageStatus.ACCEPTED:
                return StageRunResult(
                    execution.status,
                    execution=execution,
                    reason=execution.error,
                )
        completion = self.evaluate_completion(
            session_id,
            session_generation_id=ownership.session_generation_id,
            fencing_token=ownership.fencing_token,
        )
        return StageRunResult(
            StageStatus.COMPLETED if completion.complete else StageStatus.BLOCKED,
            completion=completion,
            reason=completion.reason,
        )

    run = advance

    @staticmethod
    def _normalize_validation(value: Any) -> StageValidation:
        if isinstance(value, StageValidation):
            return value
        if isinstance(value, bool):
            return StageValidation(value)
        if value is None:
            return StageValidation(True)
        return StageValidation(bool(value))

    @staticmethod
    def _normalize_acceptance(value: Any) -> StageAcceptance:
        if isinstance(value, StageAcceptance):
            return value
        if isinstance(value, bool):
            return StageAcceptance(value)
        if value is None:
            return StageAcceptance(True)
        return StageAcceptance(bool(value))

    def _record_failure(
        self,
        session_id: int,
        definition: StageDefinition,
        output: Any,
        attempt_id: str,
        stage_generation_id: str,
        ownership: StageOwnership,
        reason: str,
    ) -> PlanningCheckpoint:
        content = self._serialize_output(output) if output is not None else ""
        return self.persistence.record_checkpoint(
            session_id,
            stage_name=definition.identifier,
            checkpoint_version=definition.version,
            content=content,
            stage_generation_id=stage_generation_id,
            attempt_id=attempt_id,
            fencing_token=ownership.fencing_token,
            session_generation_id=ownership.session_generation_id,
            protocol_version=PROTOCOL_V2,
            status="failed",
            failure_reason=str(reason or "stage failed"),
        )

    @staticmethod
    def _serialize_output(output: Any) -> str:
        if isinstance(output, str):
            return output
        return json.dumps(output, sort_keys=True, separators=(",", ":"))

    def _get_session(self, session_id: int) -> PlanningSession:
        session = (
            self.db.query(PlanningSession)
            .filter(PlanningSession.id == session_id)
            .populate_existing()
            .one_or_none()
        )
        if session is None:
            raise StageEngineError(f"planning session {session_id} not found")
        return session

    def _require_input_manifest(self, session_id: int) -> InputManifest:
        try:
            manifest = self.persistence.load_input_manifest(session_id)
        except Exception as exc:
            raise StageEngineError(str(exc)) from exc
        if manifest is None:
            raise StageEngineError(
                "Protocol v2 stage execution requires a persisted input manifest"
            )
        return manifest

    def _load_accepted_planning_brief(self, session_id: int) -> PlanningBrief | None:
        try:
            return self.persistence.load_accepted_planning_brief(session_id)
        except Exception as exc:
            raise StageEngineError(str(exc)) from exc


# Names used by callers that describe the component as a service or engine.
StageOrchestrationService = StageExecutor
StageEngine = StageExecutor
