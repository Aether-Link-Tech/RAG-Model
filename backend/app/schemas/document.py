import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Visibility = Literal["private", "public"]


class DocumentBase(BaseModel):
    filename: str = Field(..., max_length=255)
    mime_type: str = Field(..., max_length=100)
    file_size: int = Field(..., gt=0)


class DocumentCreate(DocumentBase):
    user_id: uuid.UUID
    file_path: str
    file_hash: str = Field(..., min_length=64, max_length=64, description="64-char SHA-256 hexadecimal hash")
    visibility: Visibility = "private"


class DocumentResponse(DocumentBase):
    id: uuid.UUID
    user_id: uuid.UUID
    file_hash: str
    status: str
    visibility: Visibility
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
