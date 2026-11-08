import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.models.collection import UNCATEGORIZED


class CollectionWrite(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Collection name is required.")
        if value.casefold() == UNCATEGORIZED.casefold():
            raise ValueError(f'"{UNCATEGORIZED}" is reserved.')
        return value


class CollectionResponse(BaseModel):
    # `id` is None only for the virtual "Uncategorized" entry.
    id: Optional[uuid.UUID]
    name: str
    document_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
