"""Authentication Pydantic schemas"""

from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List
from datetime import datetime


class Token(BaseModel):
    access_token: str
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
    def validate_password_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be 72 bytes or fewer")
        return value


class UserResponse(BaseModel):
    id: int
    email: EmailStr
    name: Optional[str] = None
    is_active: bool = True
    created_at: datetime

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
    key_hash: str  # Only the hash, not the full key
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class DevicePairRequest(BaseModel):
    device_name: str
    public_key: str  # Ed25519 public key in base64


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


class VerifySignatureRequest(BaseModel):
    message: str
    signature: str  # Ed25519 signature in base64


class VerifySignatureResponse(BaseModel):
    valid: bool
    message: str
