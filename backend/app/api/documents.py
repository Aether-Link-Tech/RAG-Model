import os
import uuid
from datetime import datetime
import mimetypes
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from app.schemas.document import DocumentResponse

router = APIRouter(prefix="/documents", tags=["Documents"])

# Constants
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB in Bytes
ALLOWED_MIME_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
}


@router.post("/upload", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    user_id: str = Form(...),
    file: UploadFile = File(...)
):
    # 1. Read full file content and get size
    contents = await file.read()
    file_size = len(contents)

    # Reject files > 25MB
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File size exceeds maximum limit of 25MB."
        )

    # 2. Verify MIME type safely using python's built-in mimetypes & extension check
    mime_type, _ = mimetypes.guess_type(file.filename)
    
    # Fallback check if mimetypes fails to guess from filename
    if not mime_type or mime_type not in ALLOWED_MIME_TYPES:
        # Fallback based on extension
        ext = os.path.splitext(file.filename)[1].lower()
        inverse_map = {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".txt": "text/plain"}
        if ext in inverse_map:
            mime_type = inverse_map[ext]
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid file type. Only PDF, DOCX, and TXT files are allowed."
            )

    # 3. Generate unique document ID
    doc_id = str(uuid.uuid4())

    # 4. Storage directory structure: /users/{user_id}/documents/
    user_dir = os.path.join("users", user_id, "documents")
    os.makedirs(user_dir, exist_ok=True)

    file_extension = ALLOWED_MIME_TYPES[mime_type]
    file_path = os.path.join(user_dir, f"{doc_id}{file_extension}")

    # Write file to disk
    with open(file_path, "wb") as f:
        f.write(contents)

    # 5. Return success response
    return DocumentResponse(
        doc_id=doc_id,
        filename=file.filename,
        file_type=mime_type,
        size=file_size,
        status="processing",
        upload_date=datetime.utcnow(),
        message="Document uploaded successfully and queued for processing."
    )