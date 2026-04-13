"""Database models"""

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
    Enum,
    Boolean,
    Float,
    JSON,
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
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    github_url = Column(String(512), nullable=True)
    branch = Column(String(255), default="main")
    workspace_path = Column(String(512), nullable=True)  # Project isolation workspace
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True)  # Soft delete tracking

    tasks = relationship("Task", back_populates="project", cascade="all, delete-orphan")
    sessions = relationship(
        "Session", back_populates="project", cascade="all, delete-orphan"
    )
    permission_requests = relationship(
        "PermissionRequest", back_populates="project", cascade="all, delete-orphan"
    )


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(Enum(TaskStatus), default=TaskStatus.PENDING)
    priority = Column(Integer, default=0)  # Higher = more important
    steps = Column(Text, nullable=True)  # JSON string of step-by-step plan
    current_step = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    # Task workspace subfolder within project
    task_subfolder = Column(String(255), nullable=True)

    project = relationship("Project", back_populates="tasks")
    sessions = relationship(
        "SessionTask", back_populates="task", cascade="all, delete-orphan"
    )
    permission_requests = relationship(
        "PermissionRequest", back_populates="task", cascade="all, delete-orphan"
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
    is_active = Column(Boolean, default=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    stopped_at = Column(DateTime(timezone=True), nullable=True)
    paused_at = Column(DateTime(timezone=True), nullable=True)
    resumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    # Soft delete tracking to prevent ID reuse issues
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    # Unique session instance identifier (changes on recreation)
    instance_id = Column(
        String(36), nullable=True, index=True
    )  # UUID for session versioning

    project = relationship("Project", back_populates="sessions")
    tasks = relationship(
        "SessionTask", back_populates="session", cascade="all, delete-orphan"
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


class TaskCheckpoint(Base):
    """Task checkpoint for resumption"""

    __tablename__ = "task_checkpoints"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=True, index=True)

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
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
