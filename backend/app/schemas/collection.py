import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field

class CollectionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)

class CollectionUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)

class CollectionResponse(BaseModel):
    id: uuid.UUID
    name: str
    user_id: uuid.UUID
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class DocumentMoveRequest(BaseModel):
    collection_id: Optional[uuid.UUID] = None  # None means Uncategorized