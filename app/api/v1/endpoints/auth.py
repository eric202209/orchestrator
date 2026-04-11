"""Authentication endpoints"""

from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks, Header
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, APIKey, Device
from app.schemas import (
    Token,
    TokenRefresh,
    UserCreate,
    UserLogin,
    UserResponse,
    APIKeyCreate,
    APIKeyResponse,
    DevicePairRequest,
    DeviceResponse,
    DeviceUnpairResponse,
    VerifySignatureRequest,
    VerifySignatureResponse,
)
from app.auth import (
    verify_password,
    get_password_hash,
    create_access_token,
    create_refresh_token,
    verify_token,
    verify_ed25519_signature,
    generate_ed25519_keypair,
)
from app.dependencies import get_current_user, get_current_active_user

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/auth/login")


# Helper dependency to get user by API key
def get_user_by_api_key(api_key: str, db: Session) -> Optional[User]:
    """Find user by API key hash."""
    api_key_record = db.query(APIKey).filter(APIKey.key_hash == api_key).first()

    if not api_key_record:
        return None

    # Update last_used timestamp
    api_key_record.last_used = datetime.utcnow()
    db.commit()

    return db.query(User).filter(User.id == api_key_record.user_id).first()


# Header parameter for API key
def get_x_api_key(x_api_key: str = Header("X-API-Key", alias="X-API-Key")):
    """Header parameter decorator for API key."""
    return x_api_key


def get_api_key_dependency():
    """Create a dependency that checks for API key authentication."""

    async def api_key_auth(
        x_api_key: str = Depends(get_x_api_key),
        db: Session = Depends(get_db),
    ):
        if not x_api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key required",
                headers={"WWW-Authenticate": "ApiKey"},
            )

        user = get_user_by_api_key(x_api_key, db)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "ApiKey"},
            )

        return user

    return api_key_auth


@router.post(
    "/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED
)
def register(
    user: UserCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    """
    Register a new user.

    - **email**: User's email address
    - **password**: User's password (min 8 characters)

    Returns the user object (without password).
    """
    # Check if user already exists
    existing_user = db.query(User).filter(User.email == user.email).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This email is already registered. Please use a different email or sign in instead.",
        )

    # Create new user
    hashed_password = get_password_hash(user.password)
    new_user = User(
        email=user.email,
        hashed_password=hashed_password,
        is_active=True,
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return new_user


@router.post("/login", response_model=UserResponse, deprecated=True)
def login_login_form(
    form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)
):
    """
    Legacy login endpoint (deprecated). Use /tokens endpoint instead.

    - **username**: User's email
    - **password**: User's password
    """
    user = db.query(User).filter(User.email == form_data.username).first()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )

    access_token = create_access_token(data={"sub": user.email})
    refresh_token = create_refresh_token(data={"sub": user.email})

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": user,
    }


@router.post("/tokens", response_model=Token)
def get_tokens(credentials: UserLogin, db: Session = Depends(get_db)):
    """
    Login and get access/refresh tokens.

    - **email**: User's email
    - **password**: User's password
    """
    user = db.query(User).filter(User.email == credentials.email).first()

    if not user or not verify_password(credentials.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )

    access_token = create_access_token(data={"sub": user.email})
    refresh_token = create_refresh_token(data={"sub": user.email})

    return Token(
        access_token=access_token, refresh_token=refresh_token, token_type="bearer"
    )


@router.post("/refresh", response_model=Token)
def refresh_token(token_data: TokenRefresh, db: Session = Depends(get_db)):
    """
    Refresh access token using refresh token.

    - **refresh_token**: Valid refresh token
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    payload = verify_token(token_data.refresh_token, credentials_exception)
    email: str = payload.get("sub")

    if email is None:
        raise credentials_exception

    # Get user
    user = db.query(User).filter(User.email == email).first()
    if not user or not user.is_active:
        raise credentials_exception

    # Generate new access token
    access_token = create_access_token(data={"sub": email})

    return Token(
        access_token=access_token,
        refresh_token=token_data.refresh_token,
        token_type="bearer",
    )


@router.get("/me", response_model=UserResponse)
def get_current_user_info(
    current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
):
    """Get current authenticated user info."""
    return current_user


@router.post(
    "/api-keys", response_model=APIKeyResponse, status_code=status.HTTP_201_CREATED
)
def create_api_key(
    key_data: APIKeyCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Create a new API key for the authenticated user.

    ⚠️ **Important**: The raw API key is only returned once during creation.
    Store it securely - it cannot be retrieved later.
    """
    import secrets
    import hashlib

    # Generate random API key
    raw_key = f"orch_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    # Create API key record
    api_key = APIKey(
        user_id=current_user.id,
        name=key_data.name,
        key_hash=key_hash,
    )

    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    # Return with raw key (only time it's shown)
    response = APIKeyResponse(
        id=api_key.id,
        user_id=api_key.user_id,
        name=api_key.name,
        key=raw_key,  # Only returned once
        key_hash=api_key.key_hash,
        last_used=api_key.last_used,
        created_at=api_key.created_at,
    )

    return response


@router.get("/api-keys", response_model=list[APIKeyResponse])
def list_api_keys(
    current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
):
    """List all API keys for the authenticated user."""
    api_keys = db.query(APIKey).filter(APIKey.user_id == current_user.id).all()
    return api_keys


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_api_key(
    key_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Revoke (delete) an API key."""
    api_key = (
        db.query(APIKey)
        .filter(APIKey.id == key_id, APIKey.user_id == current_user.id)
        .first()
    )

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        )

    db.delete(api_key)
    db.commit()

    return None


@router.post(
    "/devices/pair", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED
)
def pair_device(
    device_data: DevicePairRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Pair a new Ed25519 device for authentication.

    - **device_name**: Human-readable name for the device
    - **public_key**: Ed25519 public key (base64-encoded)

    💡 **Human-in-the-loop**: This endpoint could send email confirmation
    before activating the device. For now, devices are activated immediately.
    """
    # Check if public key already registered
    existing_device = (
        db.query(Device).filter(Device.public_key == device_data.public_key).first()
    )

    if existing_device:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Device already paired",
        )

    # Create device record
    device = Device(
        user_id=current_user.id,
        name=device_data.device_name,
        public_key=device_data.public_key,
        is_active=True,
    )

    db.add(device)
    db.commit()
    db.refresh(device)

    # TODO: Send email confirmation in background (human-in-the-loop)
    # background_tasks.add(send_device_confirmation_email, device.id, current_user.email)

    return device


@router.get("/devices", response_model=list[DeviceResponse])
def list_devices(
    current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
):
    """List all paired devices for the authenticated user."""
    devices = db.query(Device).filter(Device.user_id == current_user.id).all()
    return devices


@router.delete("/devices/{device_id}", response_model=DeviceUnpairResponse)
def unpair_device(
    device_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Unpair (deactivate) a device."""
    device = (
        db.query(Device)
        .filter(Device.id == device_id, Device.user_id == current_user.id)
        .first()
    )

    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )

    db.delete(device)
    db.commit()

    return DeviceUnpairResponse(
        message="Device unpaired successfully",
        device_id=device_id,
    )


@router.post("/verify", response_model=VerifySignatureResponse)
def verify_signature(request: VerifySignatureRequest, db: Session = Depends(get_db)):
    """
    Verify an Ed25519 signature for testing/debugging.

    This endpoint doesn't require authentication and is useful for
    testing device authentication flows.
    """
    try:
        message_bytes = request.message.encode("utf-8")
        is_valid = verify_ed25519_signature(
            message=message_bytes,
            signature=request.signature,
            public_key=request.public_key,
        )

        return VerifySignatureResponse(
            valid=is_valid,
            message=(
                "Signature verified successfully" if is_valid else "Invalid signature"
            ),
        )
    except Exception as e:
        return VerifySignatureResponse(
            valid=False,
            message=f"Verification error: {str(e)}",
        )


@router.get("/generate-keypair")
def generate_keypair():
    """
    Generate a new Ed25519 keypair for testing.

    ⚠️ **Production use**: Clients should generate their own keypairs
    locally and never transmit private keys over the network.
    """
    public_key, private_key = generate_ed25519_keypair()

    return {
        "public_key": public_key,
        "private_key": private_key,
        "warning": "Store private key securely! It's only shown once.",
    }
