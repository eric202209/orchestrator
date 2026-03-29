"""User management endpoints"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User as UserModel
from app.schemas import UserResponse, UserCreate

router = APIRouter(
    prefix="/users",
    tags=["users"],
)


@router.get(
    "",
    response_model=List[UserResponse],
    status_code=status.HTTP_200_OK,
    summary="List all users",
    description="Retrieve a list of all users in the system",
)
async def list_users(
    skip: int = Query(0, ge=0, description="Number of users to skip"),
    limit: int = Query(
        100, ge=1, le=1000, description="Maximum number of users to return"
    ),
    db: Session = Depends(get_db),
):
    """
    List all users with pagination support.

    - **skip**: Number of users to skip (for pagination)
    - **limit**: Maximum number of users to return (default: 100, max: 1000)
    """
    users = db.query(UserModel).offset(skip).limit(limit).all()
    return users


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Get user by ID",
    description="Retrieve a specific user by their ID",
)
async def get_user(
    user_id: int,
    db: Session = Depends(get_db),
):
    """
    Get a specific user by their ID.

    - **user_id**: The ID of the user to retrieve
    """
    user = db.query(UserModel).filter(UserModel.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user
