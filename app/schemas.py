"""Pydantic schemas for API validation"""

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from typing import Optional, Any, List
from datetime import datetime
from enum import Enum


class TaskStatusEnum(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    DONE = "done"
    CANCELLED = "cancelled"


# Project Schemas
class ProjectBase(BaseModel):
    name: str
    description: Optional[str] = None
    project_rules: Optional[str] = None
    github_url: Optional[str] = None
    branch: Optional[str] = "main"
    workspace_path: Optional[str] = None


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    project_rules: Optional[str] = None
    github_url: Optional[str] = None
    branch: Optional[str] = None
    workspace_path: Optional[str] = None


class ProjectResponse(ProjectBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: Optional[datetime] = None


# Task Schemas
class TaskBase(BaseModel):
    title: str
    description: Optional[str] = None
    execution_profile: Optional[str] = "full_lifecycle"
    priority: Optional[int] = 0
    plan_position: Optional[int] = None


class TaskCreate(TaskBase):
    project_id: int
    steps: Optional[str] = None  # JSON string of step-by-step plan


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[TaskStatusEnum] = None
    execution_profile: Optional[str] = None
    priority: Optional[int] = None
    plan_position: Optional[int] = None
    steps: Optional[str] = None
    current_step: Optional[int] = None
    error_message: Optional[str] = None
    workspace_status: Optional[str] = None
    promotion_note: Optional[str] = None


class TaskPromotionRequest(BaseModel):
    note: Optional[str] = None


class TaskResponse(TaskBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    plan_id: Optional[int] = None
    status: TaskStatusEnum
    execution_profile: str = "full_lifecycle"
    estimated_effort: Optional[str] = None
    plan_position: Optional[int] = None
    steps: Optional[str] = None
    current_step: Optional[int] = 0
    error_message: Optional[str] = None
    workspace_status: Optional[str] = "isolated"
    promotion_note: Optional[str] = None
    promoted_at: Optional[datetime] = None
    task_subfolder: Optional[str] = None
    session_id: Optional[int] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class PlannerTaskCandidate(BaseModel):
    title: str
    description: Optional[str] = None
    execution_profile: str = "full_lifecycle"
    priority: int = 0
    plan_position: Optional[int] = None
    estimated_effort: Optional[str] = None
    include: bool = True


class PlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    title: str
    source_brain: str
    requirement: str
    markdown: str
    status: str
    created_at: datetime
    updated_at: Optional[datetime] = None


class PlanningMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    prompt_id: Optional[str] = None
    content: str
    metadata_json: Optional[dict[str, Any]] = None
    created_at: datetime


class PlanningArtifactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    artifact_type: str
    filename: str
    content: str
    created_at: datetime


class PlanningSessionSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    title: str
    prompt: str
    status: str
    source_brain: str
    current_prompt_id: Optional[str] = None
    finalized_plan_id: Optional[int] = None
    committed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


class PlanningSessionResponse(PlanningSessionSummaryResponse):
    last_error: Optional[str] = None
    messages: List[PlanningMessageResponse] = []
    artifacts: List[PlanningArtifactResponse] = []
    tasks_preview: List[PlannerTaskCandidate] = []
    committed_task_ids: List[int] = []


class PlanningSessionCreateRequest(BaseModel):
    project_id: int
    prompt: str = Field(min_length=3)
    source_brain: str = "local"


class PlanningSessionRespondRequest(BaseModel):
    response: str = Field(min_length=1)


class PlanningSessionCommitRequest(BaseModel):
    selected_tasks: Optional[List[PlannerTaskCandidate]] = None
    planner_markdown: Optional[str] = None


# Session Schemas
class SessionBase(BaseModel):
    name: str
    description: Optional[str] = None


class SessionCreate(SessionBase):
    project_id: int
    execution_mode: Optional[str] = "automatic"
    default_execution_profile: Optional[str] = "full_lifecycle"


class SessionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    status: Optional[str] = None
    execution_mode: Optional[str] = None
    default_execution_profile: Optional[str] = None
    last_alert_level: Optional[str] = None
    last_alert_message: Optional[str] = None


class SessionResponse(SessionBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    status: str
    execution_mode: str
    default_execution_profile: str = "full_lifecycle"
    is_active: bool
    last_alert_level: Optional[str] = None
    last_alert_message: Optional[str] = None
    last_alert_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    resumed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    instance_id: Optional[str] = None
    deleted_at: Optional[datetime] = None


# Log Entry Schemas
class LogEntryBase(BaseModel):
    level: str
    message: str
    metadata: Optional[str] = None


class LogEntryCreate(LogEntryBase):
    session_id: Optional[int] = None
    task_id: Optional[int] = None


class LogEntryResponse(LogEntryBase):
    id: int


# Task Execute Schema
class TaskExecuteRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task: str
    timeout_seconds: int = (
        600  # Increased from 300 to 600 (10 minutes) for complex tasks
    )
    session_id: Optional[int] = None
    task_id: Optional[int] = None
    created_at: Optional[datetime] = None


# Auth Schemas (moved from auth.py to avoid circular imports)
class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str


class TokenData(BaseModel):
    user_id: Optional[int] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    name: Optional[str] = None

    @field_validator("password")
    @classmethod
    def validate_password_length(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("Password must be at least 8 characters")
        return value


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    name: Optional[str] = None
    is_active: bool = True
    created_at: datetime
    updated_at: Optional[datetime] = None


class TokenRefresh(BaseModel):
    refresh_token: str


class APIKeyCreate(BaseModel):
    name: str
    description: Optional[str] = None


class APIKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    key: Optional[str] = None
    key_hash: str
    last_used: Optional[datetime] = None
    created_at: datetime


class DevicePairRequest(BaseModel):
    device_name: str
    public_key: str


class DeviceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    public_key: str
    is_active: bool
    created_at: datetime


class DeviceUnpairResponse(BaseModel):
    success: bool
    message: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_new_password_length(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("New password must be at least 8 characters")
        return value


class ProfileUpdateRequest(BaseModel):
    name: Optional[str] = None


class SystemSettingsUpdateRequest(BaseModel):
    workspace_root: Optional[str] = None
    mobile_api_key: Optional[str] = None
    rotate_mobile_api_key: bool = False
    agent_backend: Optional[str] = None
    agent_model_family: Optional[str] = None
    agent_adaptation_profile: Optional[str] = None
    orchestration_policy_profile: Optional[str] = None


class AccountSettingsResponse(BaseModel):
    email: EmailStr
    name: Optional[str] = None


class SystemSettingsResponse(BaseModel):
    workspace_root: str
    mobile_base_url: str
    mobile_api_key_configured: bool
    mobile_api_key_preview: Optional[str] = None
    mobile_api_key_source: Optional[str] = None
    openclaw_gateway_url: str
    agent_backend: str
    agent_model_family: str
    agent_adaptation_profile: str
    backend_capabilities: dict[str, Any]
    backend_health: dict[str, Any]
    supported_backends: list[dict[str, Any]] = []
    orchestration_policy_profile: str
    available_policy_profiles: list[dict[str, Any]] = []
    available_adaptation_profiles: list[dict[str, Any]] = []


class AppSettingsResponse(BaseModel):
    account: AccountSettingsResponse
    system: SystemSettingsResponse


class VerifySignatureRequest(BaseModel):
    message: str
    signature: str
    public_key: str


class VerifySignatureResponse(BaseModel):
    valid: bool
    message: str
