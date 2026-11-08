import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.app.core.roles import UserRole


class UserCreate(BaseModel):
    """Admin-only account creation (POST /users). Self-service sign-up uses /auth/register."""

    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    full_name: Optional[str] = Field(default=None, max_length=100)
    role: UserRole = UserRole.USER


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: Optional[str] = None
    role: UserRole
    status: Literal["pending", "active"] = "active"
    created_at: datetime
    is_deleted: bool

    model_config = ConfigDict(from_attributes=True)


class UserRoleUpdate(BaseModel):
    role: UserRole
