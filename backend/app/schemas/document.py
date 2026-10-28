from datetime import datetime
from pydantic import BaseModel

class DocumentBase(BaseModel):
    filename: str
    file_type: str
    size: int

class DocumentCreate(DocumentBase):
    user_id: str

class DocumentResponse(BaseModel):
    doc_id: str
    filename: str
    file_type: str
    size: int
    status: str
    upload_date: datetime
    message: str

    class Config:
        from_attributes = True