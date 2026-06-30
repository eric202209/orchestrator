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
    current_prompt_id = Column(String(64), nullable=True)
    processing_token = Column(String(64), nullable=True, index=True)
    processing_started_at = Column(DateTime(timezone=True), nullable=True)
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
    queued_at = Column(DateTime(timezone=True), nullable=True)
    queue_latency_seconds = Column(Float, nullable=True)
    tokens_in = Column(Integer, nullable=True)
    tokens_out = Column(Integer, nullable=True)
    token_source = Column(String(64), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
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
