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
    # Only populated for the caller's own documents; None for other users' public
    # documents so the uploader's identity is never revealed (FR-006).
    user_id: Optional[uuid.UUID] = None
    is_owner: bool = False
    file_hash: str
    status: str
    visibility: Visibility
    # FR-012: None means "Uncategorized". Only shown for the caller's own documents.
    collection_id: Optional[uuid.UUID] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class PublishResponse(DocumentResponse):
    """Response of POST /documents/{id}/publish (AL-185): the new public document."""

    # Set when the publish succeeded but a non-critical cleanup step did not.
    warning: Optional[str] = None
