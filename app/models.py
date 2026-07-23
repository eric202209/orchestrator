"""Database models"""

import uuid

from sqlalchemy import (
    Column,
    Float,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
    Enum,
    Boolean,
    JSON,
    UniqueConstraint,
    Index,
    CheckConstraint,
    text,
)
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.sql import func
import enum

Base = declarative_base()


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    DONE = "done"
    CANCELLED = "cancelled"


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    project_rules = Column(Text, nullable=True)
    github_url = Column(String(512), nullable=True)
    branch = Column(String(255), default="main")
    workspace_path = Column(String(512), nullable=True)  # Project isolation workspace
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)  # Soft delete tracking

    tasks = relationship("Task", back_populates="project", cascade="all, delete-orphan")
    plans = relationship("Plan", back_populates="project", cascade="all, delete-orphan")
    sessions = relationship(
        "Session", back_populates="project", cascade="all, delete-orphan"
    )
    planning_sessions = relationship(
        "PlanningSession", back_populates="project", cascade="all, delete-orphan"
    )
    permission_requests = relationship(
        "PermissionRequest", back_populates="project", cascade="all, delete-orphan"
    )


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=True, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(Enum(TaskStatus), default=TaskStatus.PENDING)
    execution_profile = Column(String(30), default="full_lifecycle")
    workflow_stage = Column(String(30), nullable=True)
    priority = Column(Integer, default=0)  # Higher = more important
    plan_position = Column(Integer, nullable=True, index=True)
    estimated_effort = Column(String(50), nullable=True)
    steps = Column(Text, nullable=True)  # JSON string of step-by-step plan
    current_step = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    workspace_status = Column(String(30), default="isolated")
    promotion_note = Column(Text, nullable=True)
    promoted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    # Task workspace subfolder within project
    task_subfolder = Column(String(255), nullable=True)
    # Workflow template applied at task creation (optional)
    template_id = Column(String(50), nullable=True)

    # Add unique constraint on (project_id, task_subfolder) to prevent race conditions
    __table_args__ = (
        UniqueConstraint(
            "project_id", "task_subfolder", name="uq_tasks_project_subfolder"
        ),
    )

    project = relationship("Project", back_populates="tasks")
    plan = relationship("Plan", back_populates="tasks")
    sessions = relationship(
        "SessionTask", back_populates="task", cascade="all, delete-orphan"
    )
    executions = relationship(
        "TaskExecution", back_populates="task", cascade="all, delete-orphan"
    )
    permission_requests = relationship(
        "PermissionRequest", back_populates="task", cascade="all, delete-orphan"
    )
    checkpoints = relationship(
        "TaskCheckpoint", back_populates="task", cascade="all, delete-orphan"
    )


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(
        String(50), default="pending"
    )  # pending, running, paused, stopped, completed
    execution_mode = Column(String(20), default="automatic")
    default_execution_profile = Column(String(30), default="full_lifecycle")
    is_active = Column(Boolean, default=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    stopped_at = Column(DateTime(timezone=True), nullable=True)
    paused_at = Column(DateTime(timezone=True), nullable=True)
    resumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    # Soft delete tracking to prevent ID reuse issues
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    last_alert_level = Column(String(20), nullable=True)
    last_alert_message = Column(Text, nullable=True)
    last_alert_at = Column(DateTime(timezone=True), nullable=True)
    # Unique session instance identifier (changes on recreation)
    instance_id = Column(
        String(36), nullable=True, index=True
    )  # UUID for session versioning
    escalation_backend_id = Column(String(64), nullable=True)
    model_lane_label = Column(String(64), nullable=True)
    model_lane_metadata = Column(JSON, nullable=True)
    repair_churn_stopped = Column(Boolean, nullable=True, default=False)
    repair_churn_trigger = Column(String(64), nullable=True)

    __table_args__ = (
        Index(
            "ix_sessions_project_name_active",
            "project_id",
            "name",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_sessions_deleted_instance", "deleted_at", "instance_id"),
    )

    project = relationship("Project", back_populates="sessions")
    tasks = relationship(
        "SessionTask", back_populates="session", cascade="all, delete-orphan"
    )
    task_executions = relationship(
        "TaskExecution", back_populates="session", cascade="all, delete-orphan"
    )
    permission_requests = relationship(
        "PermissionRequest", back_populates="session", cascade="all, delete-orphan"
    )
    # Context preservation relationships
    session_state = relationship(
        "SessionState",
        back_populates="session",
        uselist=False,
        cascade="all, delete-orphan",
    )
    conversation_history = relationship(
        "ConversationHistory", back_populates="session", cascade="all, delete-orphan"
    )
    checkpoints = relationship(
        "TaskCheckpoint", back_populates="session", cascade="all, delete-orphan"
    )


class Plan(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    source_brain = Column(String(50), nullable=False, default="local")
    requirement = Column(Text, nullable=False)
    markdown = Column(Text, nullable=False)
    status = Column(String(50), nullable=False, default="draft")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    project = relationship("Project", back_populates="plans")
    tasks = relationship("Task", back_populates="plan")
    planning_sessions = relationship("PlanningSession", back_populates="finalized_plan")


class PlanningSession(Base):
    __tablename__ = "planning_sessions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    prompt = Column(Text, nullable=False)
    status = Column(String(50), nullable=False, default="active", index=True)
    source_brain = Column(String(50), nullable=False, default="local")
    # Protocol identity is explicit so legacy rows can remain v1 while future
    # sessions opt into a newer protocol without changing the legacy fields.
    protocol_version = Column(
        String(16), nullable=False, default="v1", server_default="v1"
    )
    # Immutable logical identity.  Integer IDs are deliberately reusable on
    # SQLite, so asynchronous planning work must never use ``id`` alone.
    generation_id = Column(
        String(36), nullable=True, unique=True, default=lambda: str(uuid.uuid4())
    )
    planning_backend = Column(String(64), nullable=True)
    planner_model = Column(String(255), nullable=True)
    reasoning_profile = Column(String(128), nullable=True)
    configuration_fingerprint = Column(String(64), nullable=True)
    current_prompt_id = Column(String(64), nullable=True)
    processing_token = Column(String(64), nullable=True, index=True)
    processing_started_at = Column(DateTime(timezone=True), nullable=True)
    # Observational only: the owner fence is generation + processing_token.
    processing_task_id = Column(String(255), nullable=True, index=True)
    finalized_plan_id = Column(
        Integer, ForeignKey("plans.id"), nullable=True, index=True
    )
    committed_at = Column(DateTime(timezone=True), nullable=True)
    committed_task_ids = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        Index(
            "ux_planning_sessions_one_active",
            "project_id",
            unique=True,
            sqlite_where=text("status IN ('active', 'waiting_for_input')"),
            postgresql_where=text("status IN ('active', 'waiting_for_input')"),
        ),
    )

    project = relationship("Project", back_populates="planning_sessions")
    finalized_plan = relationship("Plan", back_populates="planning_sessions")
    messages = relationship(
        "PlanningMessage",
        back_populates="planning_session",
        cascade="all, delete-orphan",
    )
    artifacts = relationship(
        "PlanningArtifact",
        back_populates="planning_session",
        cascade="all, delete-orphan",
    )
    protocol_input = relationship(
        "PlanningProtocolInput",
        back_populates="planning_session",
        uselist=False,
        cascade="all, delete-orphan",
    )
    protocol_checkpoints = relationship(
        "PlanningCheckpoint",
        back_populates="planning_session",
        cascade="all, delete-orphan",
    )
    review_events = relationship(
        "PlanningReviewEvent",
        back_populates="planning_session",
        cascade="all, delete-orphan",
    )
    completion_manifest = relationship(
        "PlanningCompletionManifest",
        back_populates="planning_session",
        uselist=False,
        cascade="all, delete-orphan",
    )
    commit_manifests = relationship(
        "PlanningCommitManifest",
        back_populates="planning_session",
        cascade="all, delete-orphan",
    )


class PlanningMessage(Base):
    __tablename__ = "planning_messages"

    id = Column(Integer, primary_key=True, index=True)
    planning_session_id = Column(
        Integer, ForeignKey("planning_sessions.id"), nullable=False, index=True
    )
    role = Column(String(20), nullable=False)
    prompt_id = Column(String(64), nullable=True, index=True)
    content = Column(Text, nullable=False)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    planning_session = relationship("PlanningSession", back_populates="messages")


class PlanningArtifact(Base):
    __tablename__ = "planning_artifacts"

    id = Column(Integer, primary_key=True, index=True)
    planning_session_id = Column(
        Integer, ForeignKey("planning_sessions.id"), nullable=False, index=True
    )
    artifact_type = Column(String(50), nullable=False)
    filename = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    is_latest = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    planning_session = relationship("PlanningSession", back_populates="artifacts")


class PlanningProtocolInput(Base):
    """Immutable Protocol v2 manifest envelope plus Phase 28B projections.

    ``manifest_json`` and ``manifest_hash`` are the authority.  The older
    identity columns remain as compatibility metadata for existing readers.
    """

    __tablename__ = "planning_protocol_inputs"

    id = Column(Integer, primary_key=True, index=True)
    planning_session_id = Column(
        Integer,
        ForeignKey("planning_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    protocol_version = Column(String(16), nullable=False)
    session_generation_id = Column(String(36), nullable=False)
    input_hash = Column(String(64), nullable=False, index=True)
    engineering_context_identity = Column(String(512), nullable=False)
    provider_identity = Column(String(255), nullable=False)
    model_configuration = Column(JSON, nullable=False)
    repository_identity = Column(String(512), nullable=False)
    manifest_id = Column(String(128), nullable=True, index=True)
    manifest_schema_version = Column(String(64), nullable=True)
    manifest_hash = Column(String(64), nullable=True, index=True)
    manifest_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    planning_session = relationship("PlanningSession", back_populates="protocol_input")


class PlanningCheckpoint(Base):
    """Append-only stage checkpoint carrying its dependency and owner identity."""

    __tablename__ = "planning_checkpoints"

    id = Column(Integer, primary_key=True, index=True)
    planning_session_id = Column(
        Integer,
        ForeignKey("planning_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage_name = Column(String(100), nullable=False)
    checkpoint_version = Column(Integer, nullable=False, default=1)
    protocol_version = Column(String(16), nullable=False)
    session_generation_id = Column(String(36), nullable=False)
    stage_generation_id = Column(String(36), nullable=False)
    attempt_id = Column(String(36), nullable=False)
    fencing_token = Column(String(128), nullable=False)
    status = Column(String(20), nullable=False)
    content_hash = Column(String(64), nullable=False)
    # Planning Brief checkpoints additionally carry canonical-domain metadata.
    # These fields are nullable so generic Protocol v2 and historical rows keep
    # their Phase 28B/28C shape.
    schema_version = Column(String(64), nullable=True)
    brief_hash = Column(String(64), nullable=True, index=True)
    renderer_version = Column(String(64), nullable=True)
    validator_version = Column(String(64), nullable=True)
    validation_json = Column(JSON, nullable=True)
    content = Column(Text, nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    failure_reason = Column(Text, nullable=True)
    invalidated_at = Column(DateTime(timezone=True), nullable=True)
    # A promotion is a new accepted checkpoint linked to an immutable review
    # event.  Automatic accepted checkpoints leave this metadata null.
    promotion_review_event_id = Column(String(128), nullable=True, index=True)
    promotion_reason_code = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "planning_session_id",
            "stage_name",
            "checkpoint_version",
            "attempt_id",
            name="uq_planning_checkpoint_attempt",
        ),
        CheckConstraint(
            "status IN ('accepted', 'failed', 'invalidated')",
            name="ck_planning_checkpoint_status",
        ),
    )

    planning_session = relationship(
        "PlanningSession", back_populates="protocol_checkpoints"
    )
    dependencies = relationship(
        "PlanningCheckpointDependency",
        foreign_keys="PlanningCheckpointDependency.checkpoint_id",
        back_populates="checkpoint",
        cascade="all, delete-orphan",
    )
    review_events = relationship(
        "PlanningReviewEvent",
        foreign_keys="PlanningReviewEvent.candidate_checkpoint_id",
        back_populates="candidate_checkpoint",
    )


class PlanningCheckpointDependency(Base):
    """Many-to-many parent edges for immutable checkpoint dependencies."""

    __tablename__ = "planning_checkpoint_dependencies"

    checkpoint_id = Column(
        Integer,
        ForeignKey("planning_checkpoints.id", ondelete="CASCADE"),
        primary_key=True,
    )
    parent_checkpoint_id = Column(
        Integer,
        ForeignKey("planning_checkpoints.id"),
        primary_key=True,
    )

    __table_args__ = (
        CheckConstraint(
            "checkpoint_id <> parent_checkpoint_id",
            name="ck_planning_checkpoint_dependency_not_self",
        ),
    )

    checkpoint = relationship(
        "PlanningCheckpoint",
        foreign_keys=[checkpoint_id],
        back_populates="dependencies",
    )
    parent_checkpoint = relationship(
        "PlanningCheckpoint", foreign_keys=[parent_checkpoint_id]
    )


class PlanningReviewEvent(Base):
    """Append-only Protocol v2 operator-review event stream."""

    __tablename__ = "planning_review_events"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String(128), nullable=False, unique=True, index=True)
    review_id = Column(String(128), nullable=False, index=True)
    event_sequence = Column(Integer, nullable=False)
    event_type = Column(String(40), nullable=False, index=True)
    schema_version = Column(String(64), nullable=False)

    planning_session_id = Column(
        Integer,
        ForeignKey("planning_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    protocol_version = Column(String(16), nullable=False, index=True)
    stage_name = Column(String(100), nullable=False, index=True)
    stage_version = Column(Integer, nullable=False)
    stage_generation_id = Column(String(128), nullable=False)
    candidate_checkpoint_id = Column(
        Integer,
        ForeignKey("planning_checkpoints.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    candidate_checkpoint_version = Column(Integer, nullable=False)
    candidate_content_hash = Column(String(64), nullable=False, index=True)

    session_generation_id = Column(String(128), nullable=False, index=True)
    input_manifest_id = Column(String(128), nullable=False, index=True)
    input_manifest_hash = Column(String(64), nullable=False)
    brief_checkpoint_id = Column(Integer, nullable=True, index=True)
    brief_hash = Column(String(64), nullable=True)
    predecessor_json = Column(JSON, nullable=False)
    configuration_fingerprint = Column(String(64), nullable=False)
    candidate_attempt_id = Column(String(128), nullable=True)

    validator_version = Column(String(128), nullable=False)
    validation_hash = Column(String(64), nullable=False, index=True)
    validation_json = Column(JSON, nullable=False)
    review_reason_codes = Column(JSON, nullable=False)
    candidate_binding_json = Column(JSON, nullable=False)

    operator_subject = Column(String(255), nullable=False, index=True)
    operator_role = Column(String(128), nullable=False)
    authority_basis = Column(String(128), nullable=False)
    actor_kind = Column(String(32), nullable=False)

    decision_type = Column(String(40), nullable=False)
    decision_text = Column(Text, nullable=True)
    command_identity = Column(String(128), nullable=True)
    amendment_id = Column(String(128), nullable=True)
    amendment_hash = Column(String(64), nullable=True)

    prior_review_head_sequence = Column(Integer, nullable=False)
    resulting_sequence = Column(Integer, nullable=False)
    review_concurrency_token = Column(String(128), nullable=False)
    owner_fence_fingerprint = Column(String(128), nullable=True)

    idempotency_key = Column(String(128), nullable=False)
    canonical_request_hash = Column(String(64), nullable=False)
    previous_event_hash = Column(String(64), nullable=True)
    event_hash = Column(String(64), nullable=False, index=True)
    promotion_checkpoint_id = Column(Integer, nullable=True, index=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "review_id", "event_sequence", name="uq_planning_review_event_sequence"
        ),
        UniqueConstraint(
            "operator_subject",
            "idempotency_key",
            name="uq_planning_review_event_idempotency",
        ),
        CheckConstraint(
            "protocol_version = 'v2'",
            name="ck_planning_review_protocol_v2",
        ),
        CheckConstraint(
            "event_sequence >= 1 AND resulting_sequence = event_sequence",
            name="ck_planning_review_event_sequence_positive",
        ),
        CheckConstraint(
            "event_type IN ('review_opened','acknowledge_only','approve_unchanged',"
            "'reject','request_regeneration','request_amendment','cancel_review')",
            name="ck_planning_review_event_type",
        ),
        Index(
            "ix_planning_review_session_stage_candidate",
            "planning_session_id",
            "stage_name",
            "candidate_checkpoint_id",
        ),
        Index(
            "ix_planning_review_created_event_type",
            "created_at",
            "event_type",
        ),
        Index(
            "ux_planning_review_one_terminal",
            "review_id",
            unique=True,
            sqlite_where=text(
                "event_type IN ('approve_unchanged','reject','request_regeneration',"
                "'request_amendment','cancel_review')"
            ),
            postgresql_where=text(
                "event_type IN ('approve_unchanged','reject','request_regeneration',"
                "'request_amendment','cancel_review')"
            ),
        ),
        Index(
            "ux_planning_review_candidate_open",
            "candidate_checkpoint_id",
            unique=True,
            sqlite_where=text("event_type = 'review_opened'"),
            postgresql_where=text("event_type = 'review_opened'"),
        ),
    )

    planning_session = relationship("PlanningSession", back_populates="review_events")
    candidate_checkpoint = relationship(
        "PlanningCheckpoint", foreign_keys=[candidate_checkpoint_id]
    )


class PlanningCompletionManifest(Base):
    """Immutable final attestation of accepted checkpoints and dependencies."""

    __tablename__ = "planning_completion_manifests"

    id = Column(Integer, primary_key=True, index=True)
    planning_session_id = Column(
        Integer,
        ForeignKey("planning_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    protocol_version = Column(String(16), nullable=False)
    session_generation_id = Column(String(36), nullable=False)
    accepted_checkpoint_versions = Column(JSON, nullable=False)
    dependency_hashes = Column(JSON, nullable=False)
    manifest_hash = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    planning_session = relationship(
        "PlanningSession", back_populates="completion_manifest"
    )


class PlanningCommitManifest(Base):
    """Immutable future-facing commit identity and Task provenance record."""

    __tablename__ = "planning_commit_manifests"

    id = Column(Integer, primary_key=True, index=True)
    planning_session_id = Column(
        Integer,
        ForeignKey("planning_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    completion_manifest_id = Column(
        Integer,
        ForeignKey("planning_completion_manifests.id"),
        nullable=True,
        index=True,
    )
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=True, index=True)
    protocol_version = Column(String(16), nullable=False)
    session_generation_id = Column(String(36), nullable=False)
    commit_identity = Column(String(128), nullable=False, unique=True)
    task_provenance = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    planning_session = relationship(
        "PlanningSession", back_populates="commit_manifests"
    )
    completion_manifest = relationship("PlanningCompletionManifest")
    plan = relationship("Plan")


class ExecutionPlan(Base):
    """Immutable Execution Plan graph materialized from one accepted
    Structured Task Plan.  Structural fields are write-once; only
    ``status`` and ``superseded_by_execution_plan_id`` may ever change,
    and only a future dedicated lifecycle/transition service may do so
    (Phase 29B-1 only sets the initial ``status`` at creation)."""

    __tablename__ = "execution_plans"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    planning_session_id = Column(
        Integer, ForeignKey("planning_sessions.id"), nullable=False, index=True
    )
    planning_commit_manifest_id = Column(
        Integer,
        ForeignKey("planning_commit_manifests.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    generation = Column(Integer, nullable=False, default=1)
    protocol_version = Column(String(16), nullable=False)
    source_commit_identity = Column(String(128), nullable=False)
    source_plan_checkpoint_id = Column(
        Integer, ForeignKey("planning_checkpoints.id"), nullable=False, index=True
    )
    source_plan_hash = Column(String(64), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="active")
    superseded_by_execution_plan_id = Column(
        Integer, ForeignKey("execution_plans.id"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "generation > 0", name="ck_execution_plans_generation_positive"
        ),
        CheckConstraint(
            "protocol_version = 'v2'", name="ck_execution_plans_protocol_v2"
        ),
        Index("ix_execution_plans_project_status", "project_id", "status"),
    )

    project = relationship("Project")
    planning_session = relationship("PlanningSession")
    planning_commit_manifest = relationship("PlanningCommitManifest")
    source_plan_checkpoint = relationship("PlanningCheckpoint")
    tasks = relationship(
        "ExecutionTask", back_populates="execution_plan", cascade="all, delete-orphan"
    )
    dependency_edges = relationship(
        "ExecutionDependencyEdge",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    groups = relationship(
        "ExecutionGroup", back_populates="execution_plan", cascade="all, delete-orphan"
    )
    scheduler_claims = relationship(
        "ExecutionTaskSchedulerClaim",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    dispatch_intents = relationship(
        "ExecutionTaskDispatchIntent",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    runtime_attempts = relationship(
        "ExecutionTaskAttempt",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    runtime_leases = relationship(
        "ExecutionTaskRuntimeLease",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    runtime_starts = relationship(
        "ExecutionTaskRuntimeStart",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    runtime_outcomes = relationship(
        "ExecutionTaskAttemptOutcome",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    validation_contracts = relationship(
        "ExecutionTaskValidationSpecification",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    resolved_validation_evidence = relationship(
        "ExecutionTaskResolvedValidationEvidence",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    validation_predicate_results = relationship(
        "ExecutionTaskValidationPredicateResult",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    validation_runs = relationship(
        "ExecutionTaskValidationRun",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    acceptance_decisions = relationship(
        "ExecutionTaskAcceptanceDecision",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    candidate_contents = relationship(
        "ExecutionTaskCandidateContent",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    recovery_inputs = relationship(
        "ExecutionTaskRecoveryInput",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    recovery_authorizations = relationship(
        "ExecutionTaskRecoveryAuthorization",
        back_populates="execution_plan",
        cascade="all, delete-orphan",
    )
    recovery_policy_id = Column(String(64), nullable=True, index=True)
    recovery_policy_version = Column(Integer, nullable=True)
    validation_contract_set_hash = Column(String(64), nullable=True, index=True)


class ExecutionTask(Base):
    """Immutable per-task specification within one Execution Plan.

    ``task_spec`` is the full canonical ``StructuredTaskPlan.Task`` dict
    (Phase 28I) and ``done_when`` is the ordered list of that task's
    ``work_items[*].done_when`` strings, both persisted exactly as the
    accepted plan authored them.  ``status`` is a lifecycle projection, not
    part of the immutable specification; only a dedicated future
    transition service may write to it after creation.
    """

    __tablename__ = "execution_tasks"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_task_id = Column(String(32), nullable=False)
    title = Column(String(255), nullable=False)
    blocking_state = Column(String(32), nullable=False)
    task_spec = Column(JSON, nullable=False)
    done_when = Column(JSON, nullable=False)
    validation_contract_status = Column(
        String(32), nullable=False, default="legacy_unstructured"
    )
    validation_contract_id = Column(
        Integer,
        nullable=True,
        unique=True,
        index=True,
    )
    status = Column(String(20), nullable=False, default="pending")
    state_version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "execution_plan_id", "plan_task_id", name="uq_execution_tasks_plan_task"
        ),
    )

    execution_plan = relationship("ExecutionPlan", back_populates="tasks")
    transitions = relationship(
        "ExecutionTaskTransition",
        back_populates="execution_task",
        cascade="all, delete-orphan",
        order_by="ExecutionTaskTransition.sequence",
    )
    scheduler_claims = relationship(
        "ExecutionTaskSchedulerClaim",
        back_populates="execution_task",
        cascade="all, delete-orphan",
    )
    dispatch_intents = relationship(
        "ExecutionTaskDispatchIntent",
        back_populates="execution_task",
        cascade="all, delete-orphan",
    )
    runtime_attempts = relationship(
        "ExecutionTaskAttempt",
        back_populates="execution_task",
        cascade="all, delete-orphan",
    )
    runtime_leases = relationship(
        "ExecutionTaskRuntimeLease",
        back_populates="execution_task",
        cascade="all, delete-orphan",
    )
    runtime_starts = relationship(
        "ExecutionTaskRuntimeStart",
        back_populates="execution_task",
        cascade="all, delete-orphan",
    )
    runtime_outcomes = relationship(
        "ExecutionTaskAttemptOutcome",
        back_populates="execution_task",
        cascade="all, delete-orphan",
    )
    resolved_validation_evidence = relationship(
        "ExecutionTaskResolvedValidationEvidence",
        back_populates="execution_task",
        cascade="all, delete-orphan",
    )
    validation_predicate_results = relationship(
        "ExecutionTaskValidationPredicateResult",
        back_populates="execution_task",
        cascade="all, delete-orphan",
    )
    validation_runs = relationship(
        "ExecutionTaskValidationRun",
        back_populates="execution_task",
        cascade="all, delete-orphan",
    )
    acceptance_decisions = relationship(
        "ExecutionTaskAcceptanceDecision",
        back_populates="execution_task",
        cascade="all, delete-orphan",
    )
    recovery_inputs = relationship(
        "ExecutionTaskRecoveryInput",
        back_populates="execution_task",
        cascade="all, delete-orphan",
    )
    recovery_authorizations = relationship(
        "ExecutionTaskRecoveryAuthorization",
        back_populates="execution_task",
        cascade="all, delete-orphan",
    )
    candidate_contents = relationship(
        "ExecutionTaskCandidateContent",
        back_populates="execution_task",
        cascade="all, delete-orphan",
    )


class ExecutionValidationSchema(Base):
    """Immutable canonical JSON Schema authority.

    ``schema_id`` and ``schema_sha256`` are both the content-derived
    ``sha256:<64 lowercase hex>`` identity.  Logical names are descriptive
    metadata only and never release authority.
    """

    __tablename__ = "execution_validation_schemas"

    id = Column(Integer, primary_key=True, index=True)
    schema_id = Column(String(71), nullable=False, unique=True, index=True)
    schema_type = Column(String(32), nullable=False)
    schema_version = Column(String(96), nullable=False)
    dialect = Column(String(255), nullable=False)
    canonical_schema_payload = Column(JSON, nullable=False)
    schema_sha256 = Column(String(71), nullable=False, unique=True, index=True)
    schema_size_bytes = Column(Integer, nullable=False)
    schema_depth = Column(Integer, nullable=False)
    schema_object_members = Column(Integer, nullable=False)
    schema_array_length = Column(Integer, nullable=False)
    schema_max_string_length = Column(Integer, nullable=False)
    schema_reference_count = Column(Integer, nullable=False)
    schema_regex_length = Column(Integer, nullable=False)
    storage_backend_id = Column(String(64), nullable=False)
    storage_backend_version = Column(String(32), nullable=False)
    logical_name = Column(String(128), nullable=True)
    logical_version = Column(String(64), nullable=True)
    idempotency_key = Column(String(128), nullable=False, unique=True)
    canonical_command_payload = Column(JSON, nullable=False)
    canonical_command_hash = Column(String(64), nullable=False)
    canonical_metadata_payload = Column(JSON, nullable=False)
    canonical_metadata_hash = Column(String(64), nullable=False)
    creation_actor_type = Column(String(64), nullable=False)
    creation_actor_id = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "schema_type = 'json_schema'",
            name="ck_execution_validation_schema_type",
        ),
        CheckConstraint(
            "schema_size_bytes >= 0 AND schema_depth >= 0 "
            "AND schema_object_members >= 0 AND schema_array_length >= 0 "
            "AND schema_max_string_length >= 0 AND schema_reference_count >= 0 "
            "AND schema_regex_length >= 0",
            name="ck_execution_validation_schema_bounds_nonnegative",
        ),
        Index(
            "ix_execution_validation_schemas_dialect",
            "dialect",
        ),
    )

    validation_specifications = relationship(
        "ExecutionTaskValidationSpecification",
        back_populates="validation_schema",
    )


class ExecutionTaskValidationSpecification(Base):
    """Immutable validation authority bound to one released Execution Task.

    ``legacy_unstructured`` rows are compatibility records created for
    pre-contract releases.  They preserve authored ``done_when`` text but
    contain no executable predicate authority.
    """

    __tablename__ = "execution_task_validation_specifications"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    release_generation = Column(Integer, nullable=False)
    contract_status = Column(String(32), nullable=False)
    schema_version = Column(String(96), nullable=False)
    original_done_when = Column(JSON, nullable=False)
    structured_contract = Column(JSON, nullable=True)
    pass_policy = Column(JSON, nullable=True)
    review_requirement = Column(JSON, nullable=True)
    environment_identity = Column(JSON, nullable=True)
    validator_set_identity = Column(String(128), nullable=True)
    validation_schema_id = Column(
        Integer,
        ForeignKey("execution_validation_schemas.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    validation_schema_reference = Column(String(96), nullable=True)
    validation_schema_hash = Column(String(71), nullable=True, index=True)
    validation_schema_dialect = Column(String(255), nullable=True)
    canonical_payload = Column(JSON, nullable=False)
    canonical_specification_hash = Column(String(64), nullable=False, index=True)
    hash_algorithm = Column(String(16), nullable=False, default="sha256")
    specification_source = Column(String(64), nullable=False)
    release_authority_reference = Column(String(128), nullable=False)
    creation_actor_type = Column(String(64), nullable=False)
    creation_actor_id = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "execution_plan_id",
            "execution_task_id",
            "release_generation",
            name="uq_execution_task_validation_release_generation",
        ),
        CheckConstraint(
            "release_generation > 0",
            name="ck_execution_task_validation_generation_positive",
        ),
        CheckConstraint(
            "contract_status IN ('structured_executable', 'legacy_unstructured', "
            "'validation_not_required', 'unsupported')",
            name="ck_execution_task_validation_status",
        ),
        CheckConstraint(
            "hash_algorithm = 'sha256'",
            name="ck_execution_task_validation_hash_algorithm",
        ),
        Index(
            "ix_execution_task_validation_plan_status",
            "execution_plan_id",
            "contract_status",
        ),
    )

    execution_plan = relationship(
        "ExecutionPlan", back_populates="validation_contracts"
    )
    execution_task = relationship(
        "ExecutionTask",
        foreign_keys=[execution_task_id],
    )
    validation_schema = relationship(
        "ExecutionValidationSchema",
        back_populates="validation_specifications",
    )


class ExecutionTaskResolvedValidationEvidence(Base):
    """Immutable read-only resolution of one released evidence descriptor.

    The row stores bounded metadata and canonical hashes only.  It never
    stores unbounded candidate output content and has no lifecycle authority.
    """

    __tablename__ = "execution_task_resolved_validation_evidence"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_attempt_id = Column(
        Integer,
        ForeignKey("execution_task_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate_outcome_id = Column(
        Integer,
        ForeignKey("execution_task_attempt_outcomes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    validation_specification_id = Column(
        Integer,
        ForeignKey("execution_task_validation_specifications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    validation_specification_hash = Column(String(64), nullable=False, index=True)
    evidence_key = Column(String(64), nullable=False)
    evidence_type = Column(String(64), nullable=False)
    source = Column(String(64), nullable=False)
    normalized_reference = Column(String(255), nullable=False)
    source_authority_id = Column(String(128), nullable=False)
    resolver_id = Column(String(64), nullable=False)
    resolver_version = Column(String(64), nullable=False)
    resolver_contract_version = Column(String(64), nullable=False)
    environment_configuration_hash = Column(String(64), nullable=False)
    expected_hash_algorithm = Column(String(16), nullable=True)
    expected_hash = Column(String(64), nullable=True)
    actual_hash = Column(String(64), nullable=True)
    media_type = Column(String(128), nullable=True)
    byte_size = Column(Integer, nullable=True)
    structured_metadata_summary = Column(JSON, nullable=False)
    content_addressed_reference = Column(String(255), nullable=True)
    content_projection = Column(JSON, nullable=True)
    expected_output_reference = Column(String(512), nullable=True)
    resolution_status = Column(String(32), nullable=False, index=True)
    resolution_idempotency_key = Column(String(128), nullable=False, unique=True)
    deterministic_resolution_command_id = Column(
        String(128), nullable=False, unique=True
    )
    canonical_resolution_command_payload = Column(JSON, nullable=False)
    canonical_resolution_command_hash = Column(String(64), nullable=False)
    canonical_evidence_payload = Column(JSON, nullable=False)
    canonical_evidence_payload_hash = Column(String(64), nullable=False)
    task_state_at_resolution = Column(String(20), nullable=False)
    task_state_version_at_resolution = Column(Integer, nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=False)
    creation_actor_type = Column(String(64), nullable=False)
    creation_actor_id = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "candidate_outcome_id",
            "validation_specification_id",
            "evidence_key",
            name="uq_execution_task_resolved_evidence_candidate_spec_key",
        ),
        CheckConstraint(
            "resolution_status IN ('resolved', 'missing', 'hash_mismatch', "
            "'unsupported', 'unavailable', 'invalid_reference', 'too_large', "
            "'invalid_content')",
            name="ck_execution_task_resolved_evidence_status",
        ),
        CheckConstraint(
            "byte_size IS NULL OR byte_size >= 0",
            name="ck_execution_task_resolved_evidence_byte_size_nonnegative",
        ),
        CheckConstraint(
            "task_state_version_at_resolution >= 0",
            name="ck_execution_task_resolved_evidence_state_version_nonnegative",
        ),
        Index(
            "ix_execution_task_resolved_evidence_task_status",
            "execution_task_id",
            "resolution_status",
        ),
        Index(
            "ix_execution_task_resolved_evidence_spec_key",
            "validation_specification_id",
            "evidence_key",
        ),
    )

    execution_plan = relationship(
        "ExecutionPlan", back_populates="resolved_validation_evidence"
    )
    execution_task = relationship(
        "ExecutionTask", back_populates="resolved_validation_evidence"
    )
    execution_task_attempt = relationship("ExecutionTaskAttempt")
    candidate_outcome = relationship("ExecutionTaskAttemptOutcome")
    validation_specification = relationship("ExecutionTaskValidationSpecification")


class ExecutionTaskValidationPredicateResult(Base):
    """One deterministic predicate result, never an acceptance decision."""

    __tablename__ = "execution_task_validation_predicate_results"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_attempt_id = Column(
        Integer,
        ForeignKey("execution_task_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate_outcome_id = Column(
        Integer,
        ForeignKey("execution_task_attempt_outcomes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    validation_specification_id = Column(
        Integer,
        ForeignKey("execution_task_validation_specifications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    validation_specification_hash = Column(String(64), nullable=False, index=True)
    predicate_id = Column(String(64), nullable=False)
    predicate_version = Column(Integer, nullable=False)
    predicate_order = Column(Integer, nullable=False)
    evidence_snapshot_id = Column(
        Integer,
        ForeignKey(
            "execution_task_resolved_validation_evidence.id", ondelete="CASCADE"
        ),
        nullable=False,
        index=True,
    )
    evidence_key = Column(String(64), nullable=False)
    validator_id = Column(String(64), nullable=False)
    validator_version = Column(Integer, nullable=False)
    validator_set_id = Column(String(128), nullable=False)
    validator_set_version = Column(String(64), nullable=False)
    environment_configuration_hash = Column(String(64), nullable=False)
    result_status = Column(String(32), nullable=False, index=True)
    passed = Column(Boolean, nullable=False)
    result_code = Column(String(64), nullable=False)
    diagnostics = Column(JSON, nullable=False)
    expected_summary = Column(JSON, nullable=True)
    actual_summary = Column(JSON, nullable=True)
    canonical_result_payload = Column(JSON, nullable=False)
    canonical_result_hash = Column(String(64), nullable=False)
    validator_idempotency_key = Column(String(128), nullable=False, unique=True)
    deterministic_validator_command_id = Column(
        String(128), nullable=False, unique=True
    )
    canonical_validator_command_payload = Column(JSON, nullable=False)
    canonical_validator_command_hash = Column(String(64), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=False)
    creation_actor_type = Column(String(64), nullable=False)
    creation_actor_id = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "candidate_outcome_id",
            "validation_specification_id",
            "predicate_id",
            "predicate_version",
            name="uq_execution_task_validation_result_candidate_spec_predicate",
        ),
        CheckConstraint(
            "predicate_version > 0",
            name="ck_execution_task_validation_result_predicate_version_positive",
        ),
        CheckConstraint(
            "validator_version > 0",
            name="ck_execution_task_validation_result_validator_version_positive",
        ),
        CheckConstraint(
            "result_status IN ('passed', 'failed', 'missing_evidence', "
            "'validator_error', 'unsupported', 'invalid_evidence')",
            name="ck_execution_task_validation_result_status",
        ),
        CheckConstraint(
            "((result_status = 'passed' AND passed = 1) OR "
            "(result_status <> 'passed' AND passed = 0))",
            name="ck_execution_task_validation_result_passed_consistent",
        ),
        Index(
            "ix_execution_task_validation_result_task_status",
            "execution_task_id",
            "result_status",
        ),
        Index(
            "ix_execution_task_validation_result_spec_predicate",
            "validation_specification_id",
            "predicate_id",
            "predicate_version",
        ),
    )

    execution_plan = relationship(
        "ExecutionPlan", back_populates="validation_predicate_results"
    )
    execution_task = relationship(
        "ExecutionTask", back_populates="validation_predicate_results"
    )
    execution_task_attempt = relationship("ExecutionTaskAttempt")
    candidate_outcome = relationship("ExecutionTaskAttemptOutcome")
    validation_specification = relationship("ExecutionTaskValidationSpecification")
    evidence_snapshot = relationship("ExecutionTaskResolvedValidationEvidence")


class ExecutionTaskValidationRun(Base):
    """Canonical orchestration record for one frozen candidate validation."""

    __tablename__ = "execution_task_validation_runs"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_attempt_id = Column(
        Integer,
        ForeignKey("execution_task_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate_outcome_id = Column(
        Integer,
        ForeignKey("execution_task_attempt_outcomes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    validation_specification_id = Column(
        Integer,
        ForeignKey("execution_task_validation_specifications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    validation_specification_hash = Column(String(64), nullable=False, index=True)
    validation_contract_set_hash = Column(String(64), nullable=False, index=True)
    task_state_at_start = Column(String(20), nullable=False)
    task_state_version_at_start = Column(Integer, nullable=False)
    validation_run_generation = Column(Integer, nullable=False)
    validation_idempotency_key = Column(String(128), nullable=False, unique=True)
    deterministic_validation_command_id = Column(
        String(128), nullable=False, unique=True
    )
    canonical_validation_command_payload = Column(JSON, nullable=False)
    canonical_validation_command_hash = Column(String(64), nullable=False)
    validator_set_id = Column(String(128), nullable=False)
    validator_set_version = Column(String(64), nullable=False)
    environment_configuration_hash = Column(String(64), nullable=False)
    resolver_contract_version = Column(String(64), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    run_status = Column(String(32), nullable=False, index=True)
    required_evidence_count = Column(Integer, nullable=False, default=0)
    resolved_evidence_count = Column(Integer, nullable=False, default=0)
    required_predicate_count = Column(Integer, nullable=False, default=0)
    evaluated_predicate_count = Column(Integer, nullable=False, default=0)
    passed_predicate_count = Column(Integer, nullable=False, default=0)
    failed_predicate_count = Column(Integer, nullable=False, default=0)
    missing_predicate_count = Column(Integer, nullable=False, default=0)
    unsupported_predicate_count = Column(Integer, nullable=False, default=0)
    validator_error_count = Column(Integer, nullable=False, default=0)
    invalid_evidence_count = Column(Integer, nullable=False, default=0)
    pass_policy_result = Column(String(32), nullable=True)
    review_requirement = Column(String(32), nullable=True)
    review_result = Column(JSON, nullable=True)
    final_validation_classification = Column(String(32), nullable=True, index=True)
    aggregate_evidence_hash = Column(String(64), nullable=True)
    aggregate_predicate_result_hash = Column(String(64), nullable=True)
    canonical_result_payload = Column(JSON, nullable=True)
    canonical_result_hash = Column(String(64), nullable=True)
    acceptance_decision_id = Column(Integer, nullable=True, unique=True, index=True)
    lifecycle_transition_id = Column(Integer, nullable=True, index=True)
    lifecycle_transition_sequence = Column(Integer, nullable=True)
    bounded_reason = Column(String(64), nullable=True)
    bounded_detail = Column(String(1024), nullable=True)
    creation_actor_type = Column(String(64), nullable=False)
    creation_actor_id = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "candidate_outcome_id",
            "validation_specification_id",
            "validation_run_generation",
            name="uq_execution_task_validation_run_candidate_spec_generation",
        ),
        CheckConstraint(
            "validation_run_generation > 0",
            name="ck_execution_task_validation_run_generation_positive",
        ),
        CheckConstraint(
            "task_state_version_at_start >= 0",
            name="ck_execution_task_validation_run_state_version_nonnegative",
        ),
        CheckConstraint(
            "run_status IN ('pending', 'running', 'completed', 'blocked', "
            "'validation_error', 'review_required', 'accepted', 'rejected', 'cancelled')",
            name="ck_execution_task_validation_run_status",
        ),
        Index(
            "ix_execution_task_validation_runs_task_status",
            "execution_task_id",
            "run_status",
        ),
        Index(
            "ix_execution_task_validation_runs_candidate_spec",
            "candidate_outcome_id",
            "validation_specification_id",
        ),
    )

    execution_plan = relationship("ExecutionPlan", back_populates="validation_runs")
    execution_task = relationship("ExecutionTask", back_populates="validation_runs")
    execution_task_attempt = relationship("ExecutionTaskAttempt")
    candidate_outcome = relationship("ExecutionTaskAttemptOutcome")
    validation_specification = relationship("ExecutionTaskValidationSpecification")


class ExecutionTaskAcceptanceDecision(Base):
    """Immutable acceptance classification and, only when authorized, lifecycle link."""

    __tablename__ = "execution_task_acceptance_decisions"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_attempt_id = Column(
        Integer,
        ForeignKey("execution_task_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate_outcome_id = Column(
        Integer,
        ForeignKey("execution_task_attempt_outcomes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    validation_specification_id = Column(
        Integer,
        ForeignKey("execution_task_validation_specifications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    validation_specification_hash = Column(String(64), nullable=False, index=True)
    validation_run_id = Column(
        Integer,
        ForeignKey("execution_task_validation_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    validation_run_result_hash = Column(String(64), nullable=False)
    aggregate_evidence_hash = Column(String(64), nullable=False)
    aggregate_predicate_result_hash = Column(String(64), nullable=False)
    pass_policy_id = Column(String(64), nullable=False)
    pass_policy_version = Column(Integer, nullable=False)
    pass_policy_result = Column(String(32), nullable=False)
    review_requirement = Column(String(32), nullable=False)
    review_result = Column(JSON, nullable=False)
    review_reference = Column(String(128), nullable=True)
    decision_status = Column(String(32), nullable=False, index=True)
    decision_idempotency_key = Column(String(128), nullable=False, unique=True)
    deterministic_decision_command_id = Column(String(128), nullable=False, unique=True)
    canonical_decision_command_payload = Column(JSON, nullable=False)
    canonical_decision_command_hash = Column(String(64), nullable=False)
    canonical_decision_payload = Column(JSON, nullable=False)
    canonical_decision_hash = Column(String(64), nullable=False)
    decision_reason = Column(String(64), nullable=False)
    bounded_detail = Column(String(1024), nullable=True)
    decision_actor_type = Column(String(64), nullable=False)
    decision_actor_id = Column(String(255), nullable=False)
    decided_at = Column(DateTime(timezone=True), nullable=False)
    lifecycle_transition_id = Column(Integer, nullable=True, index=True)
    lifecycle_transition_sequence = Column(Integer, nullable=True)
    resulting_task_state = Column(String(20), nullable=False)
    resulting_task_state_version = Column(Integer, nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "candidate_outcome_id",
            "validation_specification_id",
            name="uq_execution_task_acceptance_candidate_spec",
        ),
        CheckConstraint(
            "pass_policy_version > 0",
            name="ck_execution_task_acceptance_policy_version_positive",
        ),
        CheckConstraint(
            "resulting_task_state_version >= 0",
            name="ck_execution_task_acceptance_state_version_nonnegative",
        ),
        CheckConstraint(
            "decision_status IN ('accepted', 'rejected', 'blocked', "
            "'validation_error', 'review_required')",
            name="ck_execution_task_acceptance_decision_status",
        ),
        Index(
            "ix_execution_task_acceptance_task_status",
            "execution_task_id",
            "decision_status",
        ),
        Index(
            "ix_execution_task_acceptance_plan_status",
            "execution_plan_id",
            "decision_status",
        ),
    )

    execution_plan = relationship(
        "ExecutionPlan", back_populates="acceptance_decisions"
    )
    execution_task = relationship(
        "ExecutionTask", back_populates="acceptance_decisions"
    )
    execution_task_attempt = relationship("ExecutionTaskAttempt")
    candidate_outcome = relationship("ExecutionTaskAttemptOutcome")
    validation_specification = relationship("ExecutionTaskValidationSpecification")
    validation_run = relationship("ExecutionTaskValidationRun")


class ExecutionTaskRecoveryInput(Base):
    """Immutable, source-bound evidence presented to recovery policy."""

    __tablename__ = "execution_task_recovery_inputs"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    failed_attempt_id = Column(
        Integer,
        ForeignKey("execution_task_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_generation = Column(Integer, nullable=False)
    runtime_outcome_id = Column(
        Integer,
        ForeignKey("execution_task_attempt_outcomes.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    validation_run_id = Column(
        Integer,
        ForeignKey("execution_task_validation_runs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    acceptance_decision_id = Column(
        Integer,
        ForeignKey("execution_task_acceptance_decisions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    recovery_source = Column(String(64), nullable=False)
    failure_category = Column(String(64), nullable=False)
    failure_code = Column(String(64), nullable=True)
    exception_type = Column(String(128), nullable=True)
    provider_request_id = Column(String(255), nullable=True)
    failed_predicate_summary = Column(JSON, nullable=True)
    aggregate_evidence_hash = Column(String(64), nullable=True)
    aggregate_predicate_result_hash = Column(String(64), nullable=True)
    lifecycle_transition_id = Column(
        Integer,
        ForeignKey("execution_task_transitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lifecycle_transition_sequence = Column(Integer, nullable=False)
    task_state_at_creation = Column(String(20), nullable=False)
    task_state_version_at_creation = Column(Integer, nullable=False)
    prior_recovery_authorization_id = Column(
        Integer,
        nullable=True,
    )
    retry_count = Column(Integer, nullable=False)
    recovery_generation = Column(Integer, nullable=False)
    canonical_input_payload = Column(JSON, nullable=False)
    canonical_input_hash = Column(String(64), nullable=False, index=True)
    input_idempotency_key = Column(String(128), nullable=False, unique=True)
    creation_actor_type = Column(String(64), nullable=False)
    creation_actor_id = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "execution_task_id",
            "recovery_generation",
            name="uq_execution_task_recovery_input_task_generation",
        ),
        UniqueConstraint(
            "lifecycle_transition_id",
            name="uq_execution_task_recovery_input_transition",
        ),
        CheckConstraint(
            "attempt_generation > 0 AND recovery_generation > 0",
            name="ck_execution_task_recovery_input_generation_positive",
        ),
        CheckConstraint(
            "retry_count >= 0 AND task_state_version_at_creation >= 0",
            name="ck_execution_task_recovery_input_counts_nonnegative",
        ),
        Index(
            "ix_execution_task_recovery_inputs_task_source",
            "execution_task_id",
            "recovery_source",
        ),
    )

    execution_plan = relationship("ExecutionPlan", back_populates="recovery_inputs")
    execution_task = relationship("ExecutionTask", back_populates="recovery_inputs")
    failed_attempt = relationship(
        "ExecutionTaskAttempt", foreign_keys=[failed_attempt_id]
    )
    runtime_outcome = relationship("ExecutionTaskAttemptOutcome")
    validation_run = relationship("ExecutionTaskValidationRun")
    acceptance_decision = relationship("ExecutionTaskAcceptanceDecision")
    lifecycle_transition = relationship("ExecutionTaskTransition")


class ExecutionTaskRecoveryAuthorization(Base):
    """Canonical Phase 29 recovery decision and authorization record.

    Policy classification and final action intentionally share one immutable
    record.  Only ``authorized`` creates a replacement attempt; blocked and
    operator-required records remain lifecycle-neutral.
    """

    __tablename__ = "execution_task_recovery_authorizations"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recovery_input_id = Column(
        Integer,
        ForeignKey("execution_task_recovery_inputs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    failed_attempt_id = Column(
        Integer,
        ForeignKey("execution_task_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recovery_generation = Column(Integer, nullable=False)
    policy_id = Column(String(64), nullable=False)
    policy_version = Column(Integer, nullable=False)
    strategy_id = Column(String(64), nullable=True)
    strategy_version = Column(Integer, nullable=True)
    authorization_status = Column(String(32), nullable=False, index=True)
    decision_classification = Column(String(64), nullable=False)
    decision_reason = Column(String(64), nullable=False)
    retry_budget_before = Column(Integer, nullable=False)
    retry_budget_after = Column(Integer, nullable=False)
    next_attempt_generation = Column(Integer, nullable=True)
    strategy_parameters = Column(JSON, nullable=True)
    strategy_parameter_hash = Column(String(64), nullable=True)
    not_before = Column(DateTime(timezone=True), nullable=True)
    backoff_policy_id = Column(String(64), nullable=True)
    backoff_policy_version = Column(Integer, nullable=True)
    operator_required = Column(Boolean, nullable=False, default=False)
    authorization_idempotency_key = Column(String(128), nullable=False, unique=True)
    deterministic_authorization_command_id = Column(
        String(128), nullable=False, unique=True
    )
    canonical_authorization_command_payload = Column(JSON, nullable=False)
    canonical_authorization_command_hash = Column(String(64), nullable=False)
    canonical_authorization_payload = Column(JSON, nullable=False)
    canonical_authorization_hash = Column(String(64), nullable=False, index=True)
    lifecycle_transition_id = Column(
        Integer,
        ForeignKey("execution_task_transitions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    lifecycle_transition_sequence = Column(Integer, nullable=True)
    replacement_attempt_id = Column(
        Integer,
        ForeignKey("execution_task_attempts.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
        index=True,
    )
    resulting_task_state = Column(String(20), nullable=False)
    resulting_task_state_version = Column(Integer, nullable=False)
    decision_actor_type = Column(String(64), nullable=False)
    decision_actor_id = Column(String(255), nullable=False)
    authorized_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "execution_task_id",
            "recovery_generation",
            name="uq_execution_task_recovery_authorization_task_generation",
        ),
        CheckConstraint(
            "recovery_generation > 0 AND policy_version > 0",
            name="ck_execution_task_recovery_authorization_generation_positive",
        ),
        CheckConstraint(
            "retry_budget_before >= 0 AND retry_budget_after >= 0",
            name="ck_execution_task_recovery_authorization_budget_nonnegative",
        ),
        CheckConstraint(
            "next_attempt_generation IS NULL OR next_attempt_generation > 0",
            name="ck_execution_task_recovery_authorization_next_generation_positive",
        ),
        CheckConstraint(
            "authorization_status IN ('authorized', 'operator_required', 'exhausted', "
            "'non_retryable', 'blocked', 'error', 'cancelled')",
            name="ck_execution_task_recovery_authorization_status",
        ),
        Index(
            "ix_execution_task_recovery_authorizations_task_status",
            "execution_task_id",
            "authorization_status",
        ),
    )

    execution_plan = relationship(
        "ExecutionPlan", back_populates="recovery_authorizations"
    )
    execution_task = relationship(
        "ExecutionTask", back_populates="recovery_authorizations"
    )
    recovery_input = relationship(
        "ExecutionTaskRecoveryInput",
        foreign_keys=[recovery_input_id],
    )
    failed_attempt = relationship(
        "ExecutionTaskAttempt", foreign_keys=[failed_attempt_id]
    )
    replacement_attempt = relationship(
        "ExecutionTaskAttempt",
        foreign_keys=[replacement_attempt_id],
        uselist=False,
    )
    lifecycle_transition = relationship("ExecutionTaskTransition")


class ExecutionTaskSchedulerClaim(Base):
    """Durable scheduler ownership boundary for one ready Execution Task.

    A claim is permission to attempt a future dispatch.  It is deliberately
    not a task lifecycle state, runtime attempt, worker lease, or proof that
    dispatch occurred.
    """

    __tablename__ = "execution_task_scheduler_claims"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    planning_session_id = Column(
        Integer, ForeignKey("planning_sessions.id"), nullable=False, index=True
    )
    scheduler_id = Column(String(255), nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=False, unique=True, index=True)
    command_payload = Column(JSON, nullable=False)
    canonical_command_hash = Column(String(64), nullable=False)
    fencing_token = Column(Integer, nullable=False)
    claimed_task_state = Column(String(20), nullable=False, default="ready")
    claimed_task_state_version = Column(Integer, nullable=False)
    claimed_eligibility_decision_hash = Column(String(64), nullable=False)
    claimed_graph_hash = Column(String(64), nullable=False)
    predecessor_fence_hash = Column(String(64), nullable=False)
    predecessor_fences = Column(JSON, nullable=False)
    claim_status = Column(String(16), nullable=False, default="active", index=True)
    lease_duration_seconds = Column(Integer, nullable=False)
    acquired_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    released_at = Column(DateTime(timezone=True), nullable=True)
    release_reason = Column(String(64), nullable=True)
    released_by_scheduler_id = Column(String(255), nullable=True)
    release_idempotency_key = Column(
        String(128), nullable=True, unique=True, index=True
    )
    canonical_release_hash = Column(String(64), nullable=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    consumed_dispatch_intent_id = Column(
        Integer, nullable=True, unique=True, index=True
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "claim_status IN ('active', 'released', 'expired', 'consumed')",
            name="ck_execution_task_scheduler_claim_status",
        ),
        CheckConstraint(
            "claimed_task_state = 'ready'",
            name="ck_execution_task_scheduler_claim_ready_state",
        ),
        CheckConstraint(
            "fencing_token > 0",
            name="ck_execution_task_scheduler_claim_fence_positive",
        ),
        CheckConstraint(
            "claimed_task_state_version >= 0",
            name="ck_execution_task_scheduler_claim_version_nonnegative",
        ),
        CheckConstraint(
            "lease_duration_seconds >= 5 AND lease_duration_seconds <= 300",
            name="ck_execution_task_scheduler_claim_lease_bounds",
        ),
        CheckConstraint(
            "expires_at > acquired_at",
            name="ck_execution_task_scheduler_claim_expiry_after_acquire",
        ),
        Index(
            "uq_execution_task_scheduler_claim_active",
            "execution_task_id",
            unique=True,
            sqlite_where=text("claim_status = 'active'"),
            postgresql_where=text("claim_status = 'active'"),
        ),
        Index(
            "ix_execution_task_scheduler_claim_plan_status_expiry",
            "execution_plan_id",
            "claim_status",
            "expires_at",
        ),
        Index(
            "ix_execution_task_scheduler_claim_project_status_expiry",
            "project_id",
            "claim_status",
            "expires_at",
        ),
    )

    execution_plan = relationship("ExecutionPlan", back_populates="scheduler_claims")
    execution_task = relationship("ExecutionTask", back_populates="scheduler_claims")
    dispatch_intent = relationship(
        "ExecutionTaskDispatchIntent",
        back_populates="scheduler_claim",
        uselist=False,
        foreign_keys="ExecutionTaskDispatchIntent.scheduler_claim_id",
    )


class ExecutionTaskDispatchIntent(Base):
    """Durable command boundary between a scheduler claim and broker publish.

    This row is the logical dispatch command.  It is deliberately separate
    from both scheduler ownership and worker/runtime lifecycle state.  A
    retry of publication keeps this row, its attempt, and its broker task id.
    """

    __tablename__ = "execution_task_dispatch_intents"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scheduler_claim_id = Column(
        Integer,
        ForeignKey("execution_task_scheduler_claims.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    scheduler_id = Column(String(255), nullable=False, index=True)
    claim_fencing_token = Column(Integer, nullable=False)
    claim_eligibility_decision_hash = Column(String(64), nullable=False)
    claim_graph_hash = Column(String(64), nullable=False)
    claim_predecessor_fence_hash = Column(String(64), nullable=False)
    claim_predecessor_fences = Column(JSON, nullable=False)
    claimed_task_state = Column(String(20), nullable=False)
    claimed_task_state_version = Column(Integer, nullable=False)
    dispatch_idempotency_key = Column(String(128), nullable=False, unique=True)
    dispatch_command_id = Column(String(128), nullable=False, unique=True)
    canonical_command_payload = Column(JSON, nullable=False)
    canonical_command_hash = Column(String(64), nullable=False)
    worker_command_payload = Column(JSON, nullable=False)
    worker_command_hash = Column(String(64), nullable=False)
    runtime_attempt_id = Column(Integer, nullable=True, unique=True, index=True)
    broker_task_id = Column(String(255), nullable=False, unique=True, index=True)
    dispatch_status = Column(
        String(24), nullable=False, default="pending_submission", index=True
    )
    created_at = Column(DateTime(timezone=True), nullable=False)
    submission_started_at = Column(DateTime(timezone=True), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    failed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancellation_reason = Column(String(64), nullable=True)
    last_submission_error_code = Column(String(64), nullable=True)
    last_submission_detail = Column(String(1024), nullable=True)
    submission_count = Column(Integer, nullable=False, default=0)
    submission_attempt_number = Column(Integer, nullable=False, default=0)
    submission_idempotency_key = Column(String(128), nullable=True)
    submitter_id = Column(String(255), nullable=True)
    submission_fencing_token = Column(Integer, nullable=False, default=0)
    submission_lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    broker_returned_task_id = Column(String(255), nullable=True)
    creation_actor_type = Column(String(32), nullable=False)
    creation_actor_id = Column(String(255), nullable=False)
    created_by_idempotency_key = Column(String(128), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "dispatch_status IN ('pending_submission', 'submitting', "
            "'submitted', 'submission_failed', 'cancelled')",
            name="ck_execution_task_dispatch_intent_status",
        ),
        CheckConstraint(
            "claim_fencing_token > 0",
            name="ck_execution_task_dispatch_intent_fence_positive",
        ),
        CheckConstraint(
            "claimed_task_state = 'ready' AND claimed_task_state_version >= 0",
            name="ck_execution_task_dispatch_intent_task_fence",
        ),
        CheckConstraint(
            "submission_count >= 0 AND submission_attempt_number >= 0",
            name="ck_execution_task_dispatch_intent_submission_counts",
        ),
        CheckConstraint(
            "submission_fencing_token >= 0",
            name="ck_execution_task_dispatch_intent_submission_fence",
        ),
        Index(
            "ix_execution_task_dispatch_intents_task_status",
            "execution_task_id",
            "dispatch_status",
        ),
        Index(
            "ix_execution_task_dispatch_intents_recovery",
            "dispatch_status",
            "submission_lease_expires_at",
        ),
        Index(
            "ix_execution_task_dispatch_intents_plan_status",
            "execution_plan_id",
            "dispatch_status",
        ),
    )

    execution_plan = relationship("ExecutionPlan", back_populates="dispatch_intents")
    execution_task = relationship("ExecutionTask", back_populates="dispatch_intents")
    scheduler_claim = relationship(
        "ExecutionTaskSchedulerClaim",
        back_populates="dispatch_intent",
        foreign_keys=[scheduler_claim_id],
    )
    runtime_attempt = relationship(
        "ExecutionTaskAttempt",
        back_populates="dispatch_intent",
        uselist=False,
        foreign_keys="ExecutionTaskAttempt.dispatch_intent_id",
    )


class ExecutionTaskAttempt(Base):
    """Canonical runtime-attempt identity for the Phase 29 execution path.

    It has no relationship to legacy ``TaskExecution``.  The attempt is
    created before publication and remains the same across broker retries.
    """

    __tablename__ = "execution_task_attempts"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dispatch_intent_id = Column(
        Integer,
        ForeignKey("execution_task_dispatch_intents.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
        index=True,
    )
    attempt_number = Column(Integer, nullable=False)
    attempt_identity = Column(String(128), nullable=False, unique=True, index=True)
    broker_task_id = Column(String(255), nullable=True, unique=True, index=True)
    predecessor_attempt_id = Column(
        Integer,
        ForeignKey("execution_task_attempts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    recovery_authorization_id = Column(
        Integer,
        nullable=True,
        unique=True,
        index=True,
    )
    recovery_generation = Column(Integer, nullable=True)
    replacement_reason = Column(String(64), nullable=True)
    strategy_id = Column(String(64), nullable=True)
    strategy_version = Column(Integer, nullable=True)
    strategy_parameter_hash = Column(String(64), nullable=True)
    attempt_status = Column(String(24), nullable=False, default="created", index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "execution_task_id",
            "attempt_number",
            name="uq_execution_task_attempt_task_number",
        ),
        CheckConstraint(
            "attempt_number > 0",
            name="ck_execution_task_attempt_number_positive",
        ),
        CheckConstraint(
            "attempt_status IN ('created', 'submitted', 'running', "
            "'candidate_completed', 'cancelled', 'failed', 'succeeded')",
            name="ck_execution_task_attempt_status",
        ),
        Index(
            "ix_execution_task_attempts_task_status",
            "execution_task_id",
            "attempt_status",
        ),
        Index(
            "ix_execution_task_attempts_predecessor",
            "predecessor_attempt_id",
        ),
    )

    execution_plan = relationship("ExecutionPlan", back_populates="runtime_attempts")
    execution_task = relationship("ExecutionTask", back_populates="runtime_attempts")
    dispatch_intent = relationship(
        "ExecutionTaskDispatchIntent",
        back_populates="runtime_attempt",
        foreign_keys=[dispatch_intent_id],
    )
    predecessor_attempt = relationship(
        "ExecutionTaskAttempt",
        remote_side=[id],
        foreign_keys=[predecessor_attempt_id],
        uselist=False,
    )
    runtime_leases = relationship(
        "ExecutionTaskRuntimeLease",
        back_populates="execution_task_attempt",
        cascade="all, delete-orphan",
    )
    runtime_start = relationship(
        "ExecutionTaskRuntimeStart",
        back_populates="execution_task_attempt",
        uselist=False,
        cascade="all, delete-orphan",
    )
    runtime_outcome = relationship(
        "ExecutionTaskAttemptOutcome",
        back_populates="execution_task_attempt",
        uselist=False,
        cascade="all, delete-orphan",
    )

    @property
    def attempt_generation(self) -> int:
        """Phase 29 terminology for the historical ``attempt_number`` field."""

        return int(self.attempt_number)


class ExecutionTaskRuntimeLease(Base):
    """Durable, fenced runtime ownership for one canonical attempt.

    A lease is worker ownership, not broker delivery.  Historical rows are
    retained so a later recovery phase can inspect every owner without
    overwriting an earlier worker's evidence.
    """

    __tablename__ = "execution_task_runtime_leases"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_attempt_id = Column(
        Integer,
        ForeignKey("execution_task_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dispatch_intent_id = Column(
        Integer,
        ForeignKey("execution_task_dispatch_intents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    broker_task_id = Column(String(255), nullable=False, index=True)
    worker_id = Column(String(255), nullable=False)
    worker_hostname = Column(String(255), nullable=False)
    worker_pid = Column(Integer, nullable=False)
    worker_process_start_identity = Column(String(255), nullable=False)
    worker_instance_id = Column(String(255), nullable=False, index=True)
    ownership_fencing_token = Column(Integer, nullable=False)
    lease_status = Column(String(16), nullable=False, default="active", index=True)
    lease_duration_seconds = Column(Integer, nullable=False)
    acquired_at = Column(DateTime(timezone=True), nullable=False)
    lease_expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=False)
    released_at = Column(DateTime(timezone=True), nullable=True)
    release_reason = Column(String(64), nullable=True)
    ownership_idempotency_key = Column(String(128), nullable=False, unique=True)
    canonical_ownership_command_payload = Column(JSON, nullable=False)
    canonical_ownership_command_hash = Column(String(64), nullable=False)
    lifecycle_transition_id = Column(Integer, nullable=True, index=True)
    lifecycle_transition_sequence = Column(Integer, nullable=True)
    lifecycle_resulting_state_version = Column(Integer, nullable=True)
    runtime_started_at = Column(DateTime(timezone=True), nullable=False)
    runtime_start_evidence = Column(JSON, nullable=False)
    progress_state = Column(String(32), nullable=True)
    progress_sequence = Column(Integer, nullable=False, default=0)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    closure_reason = Column(String(64), nullable=True)
    closed_outcome_id = Column(Integer, nullable=True, index=True)
    closed_worker_instance_id = Column(String(255), nullable=True)
    closed_ownership_fencing_token = Column(Integer, nullable=True)
    canonical_closure_hash = Column(String(64), nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "lease_status IN ('active', 'released', 'expired', 'completed', 'revoked')",
            name="ck_execution_task_runtime_lease_status",
        ),
        CheckConstraint(
            "ownership_fencing_token > 0",
            name="ck_execution_task_runtime_lease_fence_positive",
        ),
        CheckConstraint(
            "lease_duration_seconds >= 10 AND lease_duration_seconds <= 300",
            name="ck_execution_task_runtime_lease_duration_bounds",
        ),
        CheckConstraint(
            "worker_pid > 0",
            name="ck_execution_task_runtime_lease_worker_pid_positive",
        ),
        CheckConstraint(
            "lease_expires_at > acquired_at",
            name="ck_execution_task_runtime_lease_expiry_after_acquire",
        ),
        CheckConstraint(
            "progress_sequence >= 0",
            name="ck_execution_task_runtime_lease_progress_sequence_nonnegative",
        ),
        Index(
            "uq_execution_task_runtime_lease_active",
            "execution_task_attempt_id",
            unique=True,
            sqlite_where=text("lease_status = 'active'"),
            postgresql_where=text("lease_status = 'active'"),
        ),
        Index(
            "ix_execution_task_runtime_leases_attempt_status_expiry",
            "execution_task_attempt_id",
            "lease_status",
            "lease_expires_at",
        ),
        Index(
            "ix_execution_task_runtime_leases_plan_status",
            "execution_plan_id",
            "lease_status",
        ),
    )

    execution_plan = relationship("ExecutionPlan", back_populates="runtime_leases")
    execution_task = relationship("ExecutionTask", back_populates="runtime_leases")
    execution_task_attempt = relationship(
        "ExecutionTaskAttempt", back_populates="runtime_leases"
    )
    dispatch_intent = relationship("ExecutionTaskDispatchIntent")


class ExecutionTaskRuntimeStart(Base):
    """Durable handoff from fenced ownership to one runtime invocation.

    This row is deliberately separate from the C5 ownership-acquisition
    timestamp.  It records the canonical, idempotent start command that is
    committed immediately before an injected runtime adapter is invoked.
    """

    __tablename__ = "execution_task_runtime_starts"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_attempt_id = Column(
        Integer,
        ForeignKey("execution_task_attempts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    dispatch_intent_id = Column(
        Integer,
        ForeignKey("execution_task_dispatch_intents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_lease_id = Column(
        Integer,
        ForeignKey("execution_task_runtime_leases.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    broker_task_id = Column(String(255), nullable=False, index=True)
    worker_instance_id = Column(String(255), nullable=False, index=True)
    ownership_fencing_token = Column(Integer, nullable=False)
    execution_start_idempotency_key = Column(String(128), nullable=False, unique=True)
    deterministic_start_command_id = Column(String(128), nullable=False, unique=True)
    canonical_start_command_payload = Column(JSON, nullable=False)
    canonical_start_command_hash = Column(String(64), nullable=False)
    runtime_adapter_name = Column(String(64), nullable=False)
    adapter_version = Column(String(64), nullable=True)
    execution_mode = Column(String(32), nullable=False)
    configuration_hash = Column(String(64), nullable=False)
    provider_request_id = Column(String(255), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    lifecycle_state_at_start = Column(String(20), nullable=False)
    lifecycle_state_version_at_start = Column(Integer, nullable=False)
    creation_actor_type = Column(String(32), nullable=False)
    creation_actor_id = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "ownership_fencing_token > 0",
            name="ck_execution_task_runtime_start_fence_positive",
        ),
        CheckConstraint(
            "lifecycle_state_version_at_start >= 0",
            name="ck_execution_task_runtime_start_state_version_nonnegative",
        ),
        Index(
            "ix_execution_task_runtime_starts_plan_task",
            "execution_plan_id",
            "execution_task_id",
        ),
        Index(
            "ix_execution_task_runtime_starts_lease_worker",
            "runtime_lease_id",
            "worker_instance_id",
        ),
    )

    execution_plan = relationship("ExecutionPlan", back_populates="runtime_starts")
    execution_task = relationship("ExecutionTask", back_populates="runtime_starts")
    execution_task_attempt = relationship(
        "ExecutionTaskAttempt", back_populates="runtime_start"
    )
    runtime_lease = relationship("ExecutionTaskRuntimeLease")
    dispatch_intent = relationship("ExecutionTaskDispatchIntent")


class ExecutionTaskAttemptOutcome(Base):
    """One bounded, canonical interpretation of one Phase 29 attempt."""

    __tablename__ = "execution_task_attempt_outcomes"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_attempt_id = Column(
        Integer,
        ForeignKey("execution_task_attempts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    dispatch_intent_id = Column(
        Integer,
        ForeignKey("execution_task_dispatch_intents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_lease_id = Column(
        Integer,
        ForeignKey("execution_task_runtime_leases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_start_id = Column(
        Integer,
        ForeignKey("execution_task_runtime_starts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    worker_instance_id = Column(String(255), nullable=False, index=True)
    ownership_fencing_token = Column(Integer, nullable=False)
    outcome_idempotency_key = Column(String(128), nullable=False, unique=True)
    deterministic_outcome_command_id = Column(String(128), nullable=False, unique=True)
    canonical_outcome_command_payload = Column(JSON, nullable=False)
    canonical_outcome_command_hash = Column(String(64), nullable=False)
    outcome_status = Column(String(32), nullable=False, index=True)
    completed_at = Column(DateTime(timezone=True), nullable=False)
    runtime_duration_seconds = Column(Float, nullable=False)
    provider_request_id = Column(String(255), nullable=True)
    output_reference = Column(String(512), nullable=True)
    output_hash = Column(String(64), nullable=True)
    usage_summary = Column(JSON, nullable=True)
    failure_category = Column(String(64), nullable=True)
    failure_code = Column(String(64), nullable=True)
    sanitized_failure_detail = Column(String(1024), nullable=True)
    exception_type = Column(String(128), nullable=True)
    diagnostics = Column(JSON, nullable=True)
    lifecycle_transition_id = Column(Integer, nullable=True, index=True)
    lifecycle_transition_sequence = Column(Integer, nullable=True)
    lifecycle_resulting_state_version = Column(Integer, nullable=True)
    lease_closed_at = Column(DateTime(timezone=True), nullable=True)
    lease_closure_reason = Column(String(64), nullable=True)
    lease_closure_hash = Column(String(64), nullable=True)
    creation_actor_type = Column(String(32), nullable=False)
    creation_actor_id = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "ownership_fencing_token > 0",
            name="ck_execution_task_attempt_outcome_fence_positive",
        ),
        CheckConstraint(
            "outcome_status IN ('candidate_completed', 'attempt_failed', "
            "'attempt_cancelled')",
            name="ck_execution_task_attempt_outcome_status",
        ),
        CheckConstraint(
            "runtime_duration_seconds >= 0",
            name="ck_execution_task_attempt_outcome_duration_nonnegative",
        ),
        Index(
            "ix_execution_task_attempt_outcomes_plan_status",
            "execution_plan_id",
            "outcome_status",
        ),
        Index(
            "ix_execution_task_attempt_outcomes_task_completed",
            "execution_task_id",
            "completed_at",
        ),
    )

    execution_plan = relationship("ExecutionPlan", back_populates="runtime_outcomes")
    execution_task = relationship("ExecutionTask", back_populates="runtime_outcomes")
    execution_task_attempt = relationship(
        "ExecutionTaskAttempt", back_populates="runtime_outcome"
    )
    dispatch_intent = relationship("ExecutionTaskDispatchIntent")
    runtime_lease = relationship("ExecutionTaskRuntimeLease")
    runtime_start = relationship("ExecutionTaskRuntimeStart")
    candidate_content = relationship(
        "ExecutionTaskCandidateContent",
        back_populates="candidate_outcome",
        uselist=False,
    )


class ExecutionTaskCandidateContent(Base):
    """Immutable byte-backed content linked to exactly one candidate outcome.

    The local content store owns the bytes; this row owns the task-scoped
    linkage and the independently recomputed metadata.  Equal hashes may be
    reused by the store across rows, but an outcome can never be relinked.
    """

    __tablename__ = "execution_task_candidate_contents"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_attempt_id = Column(
        Integer,
        ForeignKey("execution_task_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_generation = Column(Integer, nullable=False)
    candidate_outcome_id = Column(
        Integer,
        ForeignKey("execution_task_attempt_outcomes.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    content_sha256 = Column(String(64), nullable=False, index=True)
    declared_sha256 = Column(String(64), nullable=True)
    byte_length = Column(Integer, nullable=False)
    media_type = Column(String(64), nullable=False)
    storage_backend_id = Column(String(64), nullable=False)
    storage_backend_version = Column(String(32), nullable=False)
    storage_key = Column(String(160), nullable=False)
    ingestion_idempotency_key = Column(String(128), nullable=False, unique=True)
    canonical_ingestion_command_payload = Column(JSON, nullable=False)
    canonical_ingestion_command_hash = Column(String(64), nullable=False)
    canonical_metadata_payload = Column(JSON, nullable=False)
    canonical_metadata_hash = Column(String(64), nullable=False)
    content_projection = Column(JSON, nullable=True)
    content_projection_hash = Column(String(64), nullable=True)
    content_projection_version = Column(String(64), nullable=True)
    creation_actor_type = Column(String(64), nullable=False)
    creation_actor_id = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "execution_task_id",
            "candidate_outcome_id",
            name="uq_execution_task_candidate_content_outcome",
        ),
        CheckConstraint(
            "attempt_generation > 0",
            name="ck_execution_task_candidate_content_generation_positive",
        ),
        CheckConstraint(
            "byte_length >= 0",
            name="ck_execution_task_candidate_content_length_nonnegative",
        ),
        Index(
            "ix_execution_task_candidate_contents_task_hash",
            "execution_task_id",
            "content_sha256",
        ),
        Index(
            "ix_execution_task_candidate_contents_plan_created",
            "execution_plan_id",
            "created_at",
        ),
    )

    execution_plan = relationship("ExecutionPlan", back_populates="candidate_contents")
    execution_task = relationship("ExecutionTask", back_populates="candidate_contents")
    execution_task_attempt = relationship("ExecutionTaskAttempt")
    candidate_outcome = relationship(
        "ExecutionTaskAttemptOutcome", back_populates="candidate_content"
    )


class ExecutionTaskTransition(Base):
    """Immutable lifecycle transition event for one Execution Task."""

    __tablename__ = "execution_task_transitions"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer,
        ForeignKey("execution_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_task_id = Column(String(32), nullable=False)
    sequence = Column(Integer, nullable=False)
    from_state = Column(String(20), nullable=False)
    to_state = Column(String(20), nullable=False)
    reason_code = Column(String(64), nullable=False)
    reason_detail = Column(String(1024), nullable=True)
    actor_type = Column(String(32), nullable=False)
    actor_id = Column(String(255), nullable=False)
    command_id = Column(String(128), nullable=False)
    expected_version = Column(Integer, nullable=False)
    resulting_version = Column(Integer, nullable=False)
    canonical_command_hash = Column(String(64), nullable=False)
    canonical_payload_hash = Column(String(64), nullable=False)
    previous_event_hash = Column(String(64), nullable=True)
    event_hash = Column(String(64), nullable=False, index=True)
    runtime_attempt_id = Column(Integer, nullable=True, index=True)
    runtime_lease_id = Column(Integer, nullable=True, index=True)
    runtime_ownership_fence = Column(Integer, nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "execution_task_id",
            "sequence",
            name="uq_execution_task_transition_sequence",
        ),
        UniqueConstraint(
            "execution_task_id",
            "actor_type",
            "actor_id",
            "command_id",
            name="uq_execution_task_transition_idempotency",
        ),
        Index(
            "ix_execution_task_transitions_plan_task",
            "execution_plan_id",
            "execution_task_id",
        ),
        Index(
            "ix_execution_task_transitions_command",
            "actor_type",
            "actor_id",
            "command_id",
        ),
    )

    execution_task = relationship("ExecutionTask", back_populates="transitions")
    execution_plan = relationship("ExecutionPlan")


class ExecutionDependencyEdge(Base):
    """Immutable dependency edge materialized from the accepted plan's
    ``Dependency`` records.  ``source_dependency_type`` preserves the
    plan-side type; ``runtime_class`` is the Phase 29A conservative
    mapping used by a future scheduler."""

    __tablename__ = "execution_dependency_edges"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_dependency_id = Column(String(32), nullable=False)
    prerequisite_execution_task_id = Column(
        Integer, ForeignKey("execution_tasks.id"), nullable=False, index=True
    )
    dependent_execution_task_id = Column(
        Integer, ForeignKey("execution_tasks.id"), nullable=False, index=True
    )
    source_dependency_type = Column(String(32), nullable=False)
    runtime_class = Column(String(32), nullable=False)
    rationale = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "execution_plan_id",
            "plan_dependency_id",
            name="uq_execution_dependency_edges_plan_dep",
        ),
        CheckConstraint(
            "prerequisite_execution_task_id <> dependent_execution_task_id",
            name="ck_execution_dependency_edges_not_self",
        ),
    )

    execution_plan = relationship("ExecutionPlan", back_populates="dependency_edges")
    prerequisite_task = relationship(
        "ExecutionTask", foreign_keys=[prerequisite_execution_task_id]
    )
    dependent_task = relationship(
        "ExecutionTask", foreign_keys=[dependent_execution_task_id]
    )


class ExecutionGroup(Base):
    """Immutable execution-group metadata materialized from the accepted
    plan's ``ExecutionGroup`` records.  Preserved as scheduler metadata
    only; group kind does not itself gate eligibility (Phase 29A §6)."""

    __tablename__ = "execution_groups"

    id = Column(Integer, primary_key=True, index=True)
    execution_plan_id = Column(
        Integer,
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_group_id = Column(String(32), nullable=False)
    kind = Column(String(32), nullable=False)
    order_index = Column(Integer, nullable=False)
    parallel_limit = Column(Integer, nullable=False)
    skip_policy = Column(String(32), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "execution_plan_id",
            "plan_group_id",
            name="uq_execution_groups_plan_group",
        ),
    )

    execution_plan = relationship("ExecutionPlan", back_populates="groups")
    members = relationship(
        "ExecutionGroupMember",
        back_populates="execution_group",
        cascade="all, delete-orphan",
    )


class ExecutionGroupMember(Base):
    """Normalized group membership row.  Every referenced task must
    belong to the same Execution Plan as its group (enforced by the
    commit service, not by a JSON list)."""

    __tablename__ = "execution_group_members"

    id = Column(Integer, primary_key=True, index=True)
    execution_group_id = Column(
        Integer,
        ForeignKey("execution_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_task_id = Column(
        Integer, ForeignKey("execution_tasks.id"), nullable=False, index=True
    )
    member_order = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "execution_group_id",
            "execution_task_id",
            name="uq_execution_group_members_unique_task",
        ),
    )

    execution_group = relationship("ExecutionGroup", back_populates="members")
    execution_task = relationship("ExecutionTask")


class ExecutionCommitCommand(Base):
    """Phase 29B-3 command-replay binding for the public execution-commit
    endpoint.  This is control state only -- it never becomes a second
    authority source.  ``PlanningCommitManifest`` and ``ExecutionPlan``
    remain the sole authority records; this row only lets a caller replay
    or resume a specific idempotency-keyed release command."""

    __tablename__ = "execution_commit_commands"

    id = Column(Integer, primary_key=True, index=True)
    planning_session_id = Column(
        Integer,
        ForeignKey("planning_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operator_subject = Column(String(255), nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=False)
    canonical_request_hash = Column(String(64), nullable=False)
    planning_commit_manifest_id = Column(
        Integer, ForeignKey("planning_commit_manifests.id"), nullable=True, index=True
    )
    execution_plan_id = Column(
        Integer, ForeignKey("execution_plans.id"), nullable=True, index=True
    )
    boundary_state = Column(String(40), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "operator_subject",
            "idempotency_key",
            name="uq_execution_commit_command_idempotency",
        ),
    )

    planning_session = relationship("PlanningSession")
    planning_commit_manifest = relationship("PlanningCommitManifest")
    execution_plan = relationship("ExecutionPlan")


class SessionState(Base):
    """Session state persistence model"""

    __tablename__ = "session_states"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)

    # State data (JSON)
    current_step = Column(Integer, default=0)
    total_steps = Column(Integer, default=0)
    plan = Column(Text, nullable=True)  # JSON string of orchestration plan
    execution_results = Column(Text, nullable=True)  # JSON string of results
    debug_attempts = Column(Text, nullable=True)  # JSON string of debug history
    changed_files = Column(Text, nullable=True)  # JSON string of file changes

    session = relationship("Session", back_populates="session_state")


class ConversationHistory(Base):
    """Conversation history for sessions"""

    __tablename__ = "conversation_history"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False, index=True)

    # Message data
    role = Column(String(20), nullable=False)  # "user", "assistant", "system"
    content = Column(Text, nullable=False)
    metadata_json = Column(JSON, nullable=True)  # Additional context

    session = relationship("Session", back_populates="conversation_history")


class SessionTask(Base):
    __tablename__ = "session_tasks"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    status = Column(Enum(TaskStatus), default=TaskStatus.PENDING)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    session = relationship("Session", back_populates="tasks")
    task = relationship("Task", back_populates="sessions")


class TaskExecution(Base):
    __tablename__ = "task_executions"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    attempt_number = Column(Integer, nullable=False)
    status = Column(Enum(TaskStatus), default=TaskStatus.PENDING, nullable=False)
    failure_category = Column(String(64), nullable=True)
    backend_id = Column(String(64), nullable=True)
    planning_session_id = Column(
        Integer, ForeignKey("planning_sessions.id"), nullable=True, index=True
    )
    planning_backend = Column(String(64), nullable=True)
    execution_backend = Column(String(64), nullable=True)
    planner_model = Column(String(255), nullable=True)
    executor_model = Column(String(255), nullable=True)
    reasoning_profile = Column(String(128), nullable=True)
    configuration_fingerprint = Column(String(64), nullable=True)
    queued_at = Column(DateTime(timezone=True), nullable=True)
    queue_latency_seconds = Column(Float, nullable=True)
    tokens_in = Column(Integer, nullable=True)
    tokens_out = Column(Integer, nullable=True)
    token_source = Column(String(64), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    worker_pid = Column(Integer, nullable=True)
    worker_hostname = Column(String(255), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "task_id",
            "attempt_number",
            name="uq_task_executions_session_task_attempt",
        ),
    )

    session = relationship("Session", back_populates="task_executions")
    task = relationship("Task", back_populates="executions")
    planning_session = relationship("PlanningSession")
    logs = relationship("LogEntry", back_populates="task_execution")
    change_sets = relationship(
        "TaskExecutionChangeSet",
        back_populates="task_execution",
        cascade="all, delete-orphan",
    )


class TaskExecutionChangeSet(Base):
    __tablename__ = "task_execution_change_sets"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=True, index=True)
    task_execution_id = Column(
        Integer, ForeignKey("task_executions.id"), nullable=False, index=True
    )
    base_snapshot_key = Column(String(255), nullable=False)
    head_snapshot_key = Column(String(255), nullable=True)
    snapshot_path = Column(Text, nullable=True)
    target_path = Column(Text, nullable=True)
    snapshot_exists = Column(Boolean, default=False, nullable=False)
    added_files = Column(JSON, nullable=False, default=lambda: [])
    modified_files = Column(JSON, nullable=False, default=lambda: [])
    deleted_files = Column(JSON, nullable=False, default=lambda: [])
    warning_flags = Column(JSON, nullable=False, default=lambda: [])
    review_decision = Column(JSON, nullable=True)
    review_reason = Column(String(255), nullable=True)
    disposition = Column(String(50), default="captured", nullable=False, index=True)
    disposition_reason = Column(Text, nullable=True)
    disposition_at = Column(DateTime(timezone=True), nullable=True)
    disposition_metadata = Column(JSON, nullable=True)
    status = Column(String(50), nullable=True, index=True)
    captured_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "task_execution_id",
            name="uq_task_execution_change_sets_task_execution_id",
        ),
        Index("ix_task_execution_change_sets_task_recorded", "task_id", "created_at"),
    )

    task_execution = relationship("TaskExecution", back_populates="change_sets")


class TaskCheckpoint(Base):
    """Task checkpoint for resumption"""

    __tablename__ = "task_checkpoints"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id = Column(
        Integer,
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Checkpoint data
    checkpoint_type = Column(String(50), nullable=False)  # "before", "after", "error"
    step_number = Column(Integer, nullable=True)
    description = Column(String(512), nullable=True)
    state_snapshot = Column(Text, nullable=True)  # Full state JSON

    # Context
    logs_snapshot = Column(Text, nullable=True)  # JSON string of recent logs
    error_info = Column(Text, nullable=True)  # JSON string of error details

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    task = relationship("Task", back_populates="checkpoints")
    session = relationship("Session", back_populates="checkpoints")


class PermissionRequest(Base):
    """Permission request model for approval workflow"""

    __tablename__ = "permission_requests"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True, index=True)

    # Operation details
    operation_type = Column(String(50), nullable=False, index=True)
    target_path = Column(String(512), nullable=True)
    command = Column(Text, nullable=True)
    description = Column(Text, nullable=True)

    # Approval details
    status = Column(String(20), default="pending", index=True)
    approved_by = Column(String(255), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    denied_reason = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    project = relationship("Project", back_populates="permission_requests")
    session = relationship("Session", back_populates="permission_requests")
    task = relationship("Task", back_populates="permission_requests")


class LogEntry(Base):
    __tablename__ = "log_entries"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True, index=True)
    task_execution_id = Column(
        Integer, ForeignKey("task_executions.id"), nullable=True, index=True
    )
    level = Column(String(50), nullable=False)  # INFO, WARNING, ERROR
    message = Column(Text, nullable=False)
    log_metadata = Column(
        Text, nullable=True
    )  # JSON string (renamed from metadata to avoid SQLAlchemy reserved name)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Session instance tracking to prevent ID reuse issues
    session_instance_id = Column(
        String(36), nullable=True, index=True
    )  # UUID matching session.instance_id

    task_execution = relationship("TaskExecution", back_populates="logs")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=True)  # User's display name
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    api_keys = relationship(
        "APIKey", back_populates="user", cascade="all, delete-orphan"
    )
    devices = relationship(
        "Device", back_populates="user", cascade="all, delete-orphan"
    )


class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    key_hash = Column(
        String(255), nullable=False, unique=True, index=True
    )  # Store hash, not raw key
    last_used = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="api_keys")


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    public_key = Column(
        String(255), nullable=False, unique=True, index=True
    )  # Ed25519 public key
    is_active = Column(Boolean, default=True)
    last_seen = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    user = relationship("User", back_populates="devices")


class SystemSetting(Base):
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(255), nullable=False, unique=True, index=True)
    value = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class InterventionRequest(Base):
    """Human-in-the-loop intervention request.

    Created when a running session needs operator input before it can continue.
    Intervention types:
      guidance   — operator provides free-form steering text
      approval   — operator approves or denies a proposed action
      information — operator supplies a fact the runtime cannot determine itself
    """

    __tablename__ = "intervention_requests"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)

    intervention_type = Column(String(20), nullable=False, index=True)
    initiated_by = Column(String(20), default="ai", nullable=False)
    prompt = Column(Text, nullable=False)
    context_snapshot = Column(Text, nullable=True)  # JSON

    status = Column(String(20), default="pending", nullable=False, index=True)
    operator_reply = Column(Text, nullable=True)
    operator_id = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    replied_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class ExecutionFailureSummary(Base):
    """Agent-generated summary of a failed execution session.

    Created on first GET /sessions/{id}/failure-summary. One per session.
    The summary is bounded to ~500 tokens so it safely seeds a new PlanningSession.
    operator_feedback is operator free-text added before replanning.
    """

    __tablename__ = "execution_failure_summaries"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(
        Integer, ForeignKey("sessions.id"), nullable=False, unique=True, index=True
    )
    summary = Column(Text, nullable=False)
    operator_feedback = Column(Text, nullable=True)
    generated_at = Column(DateTime(timezone=True), server_default=func.now())
    feedback_at = Column(DateTime(timezone=True), nullable=True)
    replan_planning_session_id = Column(
        Integer, ForeignKey("planning_sessions.id"), nullable=True
    )


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    source_path = Column(String(512), nullable=True)
    knowledge_type = Column(String(50), nullable=False)
    tags = Column(JSON, nullable=True)
    project_scope = Column(String(255), nullable=True)
    applies_to = Column(JSON, nullable=True)
    failure_signature = Column(String(255), nullable=True)
    tool_name = Column(String(255), nullable=True)
    priority = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    version = Column(Integer, default=1)
    checksum = Column(String(64), nullable=False)
    sync_status = Column(String(20), nullable=False, default="synced")
    sync_required_at = Column(DateTime(timezone=True), nullable=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    usage_logs = relationship(
        "KnowledgeUsageLog",
        back_populates="knowledge_item",
        cascade="all, delete-orphan",
    )
    revisions = relationship(
        "KnowledgeItemRevision",
        back_populates="knowledge_item",
        cascade="all, delete-orphan",
        order_by="KnowledgeItemRevision.version",
    )
    lifecycle_events = relationship(
        "KnowledgeLifecycleEvent",
        back_populates="knowledge_item",
        cascade="all, delete-orphan",
        order_by="KnowledgeLifecycleEvent.created_at",
    )


class KnowledgeItemRevision(Base):
    __tablename__ = "knowledge_item_revisions"

    id = Column(Integer, primary_key=True, index=True)
    knowledge_item_id = Column(
        String(36), ForeignKey("knowledge_items.id"), nullable=False, index=True
    )
    version = Column(Integer, nullable=False)
    previous_version = Column(Integer, nullable=False)
    changed_fields = Column(JSON, nullable=False)
    before_snapshot = Column(JSON, nullable=False)
    after_snapshot = Column(JSON, nullable=False)
    change_reason = Column(Text, nullable=True)
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    knowledge_item = relationship("KnowledgeItem", back_populates="revisions")


class KnowledgeLifecycleEvent(Base):
    __tablename__ = "knowledge_lifecycle_events"

    id = Column(Integer, primary_key=True, index=True)
    knowledge_item_id = Column(
        String(36), ForeignKey("knowledge_items.id"), nullable=False, index=True
    )
    event_type = Column(String(50), nullable=False)
    payload = Column(JSON, nullable=True)
    actor = Column(String(255), nullable=True)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    knowledge_item = relationship("KnowledgeItem", back_populates="lifecycle_events")


class GuidanceScope(str, enum.Enum):
    GLOBAL = "global"
    PROJECT = "project"
    SESSION = "session"
    TASK = "task"


class GuidanceStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"
    EXPIRED = "expired"


class HumanGuidance(Base):
    __tablename__ = "human_guidance"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True, index=True)
    scope = Column(Enum(GuidanceScope), nullable=False, index=True)
    message = Column(Text, nullable=False)
    status = Column(
        Enum(GuidanceStatus), nullable=False, default=GuidanceStatus.ACTIVE, index=True
    )
    priority = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)
    disabled_at = Column(DateTime(timezone=True), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(String(255), nullable=True)
    revision = Column(Integer, nullable=False, default=1)

    backend_targets = Column(
        JSON, nullable=True
    )  # list[str]; None / missing treated as ["all"]
    model_targets = Column(
        JSON, nullable=True
    )  # list[str]; None / missing treated as ["all"]
    purpose_targets = Column(
        JSON, nullable=True
    )  # list[str]; None / missing treated as ["all"]; values: all/planning/execution/repair/validation

    revisions = relationship(
        "HumanGuidanceRevision",
        back_populates="guidance",
        cascade="all, delete-orphan",
    )


class HumanGuidanceRevision(Base):
    __tablename__ = "human_guidance_revisions"

    id = Column(Integer, primary_key=True, index=True)
    guidance_id = Column(
        Integer, ForeignKey("human_guidance.id"), nullable=False, index=True
    )
    revision = Column(Integer, nullable=False)
    message = Column(Text, nullable=False)
    changed_by = Column(String(255), nullable=True)
    changed_at = Column(DateTime(timezone=True), server_default=func.now())
    change_reason = Column(Text, nullable=True)

    guidance = relationship("HumanGuidance", back_populates="revisions")


class HumanGuidanceUsage(Base):
    __tablename__ = "human_guidance_usage"

    id = Column(Integer, primary_key=True, index=True)
    guidance_id = Column(
        Integer, ForeignKey("human_guidance.id"), nullable=True, index=True
    )
    project_id = Column(Integer, nullable=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True, index=True)
    used_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    rendered = Column(Boolean, nullable=False, default=False)
    selected = Column(Boolean, nullable=False, default=False)
    trimmed = Column(Boolean, nullable=False, default=False)
    selection_score = Column(Integer, nullable=True)
    source = Column(String(50), nullable=False, default="human_guidance_table")
    render_position = Column(Integer, nullable=True)
    rendered_chars = Column(Integer, nullable=True)
    message_hash = Column(String(64), nullable=True)


class HumanGuidanceConflict(Base):
    __tablename__ = "human_guidance_conflicts"

    id = Column(Integer, primary_key=True, index=True)
    guidance_id = Column(
        Integer, ForeignKey("human_guidance.id"), nullable=True, index=True
    )
    project_id = Column(Integer, nullable=True, index=True)
    session_id = Column(Integer, nullable=True, index=True)
    task_id = Column(Integer, nullable=True, index=True)
    task_title = Column(String(512), nullable=True)
    guidance_scope = Column(String(50), nullable=True)
    guidance_message = Column(Text, nullable=False)
    conflict_excerpt = Column(Text, nullable=False, default="")
    conflict_patterns = Column(Text, nullable=True)  # JSON text: ["pattern_name"]
    severity = Column(String(20), nullable=False, default="warning")
    status = Column(String(20), nullable=False, default="open", index=True)
    detected_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by = Column(String(255), nullable=True)
    resolution_note = Column(Text, nullable=True)
    source = Column(String(50), nullable=False, default="heuristic")


class HumanGuidanceActivation(Base):
    __tablename__ = "human_guidance_activations"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, nullable=True, index=True)
    session_id = Column(Integer, nullable=True, index=True)
    scope = Column(String(20), nullable=False, index=True)  # "project" | "session"
    table_enabled = Column(Boolean, nullable=False, default=False)
    persistence_enabled = Column(Boolean, nullable=False, default=False)
    render_enabled = Column(Boolean, nullable=False, default=False)
    injection_enabled = Column(Boolean, nullable=False, default=False)
    conflict_detection_enabled = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    enabled_by = Column(String(255), nullable=True)
    disabled_at = Column(DateTime(timezone=True), nullable=True)
    disabled_by = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False, default="disabled", index=True)


class KnowledgeUsageLog(Base):
    __tablename__ = "knowledge_usage_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True, index=True)
    knowledge_item_id = Column(
        String(36), ForeignKey("knowledge_items.id"), nullable=False, index=True
    )
    trigger_phase = Column(String(50), nullable=False)
    retrieval_reason = Column(String(512), nullable=False)
    retrieval_query = Column(String(512), nullable=True)
    confidence = Column(Float, nullable=False)
    rank = Column(Integer, nullable=False)
    used_in_prompt = Column(Boolean, nullable=False)
    was_effective = Column(Boolean, nullable=True, default=None)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    knowledge_item = relationship("KnowledgeItem", back_populates="usage_logs")
