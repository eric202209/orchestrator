"""Pydantic schemas for API validation"""

from pydantic import BaseModel, EmailStr, HttpUrl
from typing import Optional, List
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
    github_url: Optional[str] = None
    branch: Optional[str] = "main"
    workspace_path: Optional[str] = None


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    github_url: Optional[str] = None
    branch: Optional[str] = None
    workspace_path: Optional[str] = None


class ProjectResponse(ProjectBase):
    id: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# Task Schemas
class TaskBase(BaseModel):
    title: str
    description: Optional[str] = None
    priority: Optional[int] = 0


class TaskCreate(TaskBase):
    project_id: int
    steps: Optional[str] = None  # JSON string of step-by-step plan


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[TaskStatusEnum] = None
    priority: Optional[int] = None
    steps: Optional[str] = None
    current_step: Optional[int] = None
    error_message: Optional[str] = None


class TaskResponse(TaskBase):
    id: int
    project_id: int
    status: TaskStatusEnum
    steps: Optional[str] = None
    current_step: Optional[int] = 0
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# Session Schemas
class SessionBase(BaseModel):
    name: str
    description: Optional[str] = None


class SessionCreate(SessionBase):
    project_id: int


class SessionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    status: Optional[str] = None


class SessionResponse(SessionBase):
    id: int
    project_id: int
    status: str
    is_active: bool
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    resumed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


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
    task: str
    timeout_seconds: int = (
        600  # Increased from 300 to 600 (10 minutes) for complex tasks
    )
    session_id: Optional[int] = None
    task_id: Optional[int] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


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


class UserResponse(BaseModel):
    id: int
    email: EmailStr
    name: Optional[str] = None
    is_active: bool = True
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TokenRefresh(BaseModel):
    refresh_token: str


class APIKeyCreate(BaseModel):
    name: str
    description: Optional[str] = None


class APIKeyResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    key_hash: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class DevicePairRequest(BaseModel):
    device_name: str
    public_key: str


class DeviceResponse(BaseModel):
    id: int
    device_name: str
    public_key: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class DeviceUnpairResponse(BaseModel):
    success: bool
    message: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class ProfileUpdateRequest(BaseModel):
    name: Optional[str] = None


class SystemSettingsUpdateRequest(BaseModel):
    workspace_root: Optional[str] = None
    mobile_api_key: Optional[str] = None
    rotate_mobile_api_key: bool = False


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


class AppSettingsResponse(BaseModel):
    account: AccountSettingsResponse
    system: SystemSettingsResponse


class VerifySignatureRequest(BaseModel):
    message: str
    signature: str


class VerifySignatureResponse(BaseModel):
    valid: bool
    message: str
