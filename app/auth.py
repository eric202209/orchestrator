"""Authentication utilities - JWT and Ed25519"""

from datetime import datetime, timedelta
from typing import Any, Optional
from jose import JWTError, jwt
import bcrypt
import nacl.encoding
import nacl.signing
from nacl.exceptions import BadSignatureError as VerifyError

from app.config import settings

# JWT settings
ALGORITHM = "HS256"

# Password hashing using bcrypt directly (avoids passlib bugs)
def get_password_hash(password: str) -> str:
    """Generate password hash using bcrypt."""
    # bcrypt has 72-byte limit, truncate if needed
    password_bytes = password.encode('utf-8')[:72]
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a hash."""
    password_bytes = plain_password.encode('utf-8')[:72]
    hashed_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(password_bytes, hashed_bytes)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def create_refresh_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT refresh token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(days=7)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def verify_token(token: str, credentials_exception: Any) -> Any:
    """Verify JWT token and return payload."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
        return payload
    except JWTError:
        raise credentials_exception


def verify_ed25519_signature(message: bytes, signature: str, public_key: str) -> bool:
    """
    Verify Ed25519 signature.
    
    Args:
        message: The original message as bytes
        signature: Base64-encoded signature string
        public_key: Base64-encoded public key string
    
    Returns:
        True if signature is valid, False otherwise
    """
    try:
        # Convert base64 strings to bytes
        signature_bytes = nacl.encoding.Base64.decode(signature)
        public_key_bytes = nacl.encoding.Base64.decode(public_key)
        
        # Create verifiable key and verify
        verifiable_key = nacl.signing.VerifiableKey(public_key_bytes)
        verifiable_key.verify(message, signature_bytes)
        
        return True
    except (VerifyError, Exception):
        return False


def sign_ed25519_message(message: bytes, private_key: str) -> str:
    """
    Sign a message with Ed25519.
    
    Args:
        message: The message to sign as bytes
        private_key: Base64-encoded private key string
    
    Returns:
        Base64-encoded signature string
    """
    try:
        private_key_bytes = nacl.encoding.Base64.decode(private_key)
        signing_key = nacl.signing.SigningKey(private_key_bytes)
        signed = signing_key.sign(message)
        return nacl.encoding.Base64.encode(signed.signature).decode('utf-8')
    except Exception as e:
        raise ValueError(f"Failed to sign message: {e}")


def generate_ed25519_keypair() -> tuple[str, str]:
    """
    Generate a new Ed25519 keypair.
    
    Returns:
        Tuple of (public_key, private_key) as base64-encoded strings
    """
    from nacl import signing
    
    key_pair = signing.SigningKey.generate()
    public_key = nacl.encoding.Base64.encode(key_pair.verify_key).decode('utf-8')
    private_key = nacl.encoding.Base64.encode(key_pair).decode('utf-8')
    
    return public_key, private_key
