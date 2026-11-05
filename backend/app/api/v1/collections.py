import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.app.api.deps import get_current_active_user, get_db
from backend.app.models.user import User
from backend.app.models.collection import Collection
from backend.app.models.document import Document
from backend.app.schemas.collection import (
    CollectionCreate,
    CollectionUpdate,
    CollectionResponse,
    DocumentMoveRequest,
)

router = APIRouter(prefix="/collections", tags=["collections"])

@router.post("/", response_model=CollectionResponse, status_code=status.HTTP_201_CREATED)
async def create_collection(
    data: CollectionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Create a new per-user collection."""
    new_col = Collection(name=data.name, user_id=current_user.id)
    db.add(new_col)
    await db.commit()
    await db.refresh(new_col)
    return new_col

@router.get("/", response_model=List[CollectionResponse])
async def list_collections(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List all collections belonging to the current user."""
    result = await db.execute(
        select(Collection).where(Collection.user_id == current_user.id)
    )
    return result.scalars().all()

@router.patch("/{collection_id}", response_model=CollectionResponse)
async def rename_collection(
    collection_id: uuid.UUID,
    data: CollectionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Rename an existing collection."""
    col = await db.get(Collection, collection_id)
    if not col or col.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Collection not found.")

    col.name = data.name
    await db.commit()
    await db.refresh(col)
    return col

@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(
    collection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Delete a collection and move its documents to Uncategorized (collection_id -> NULL)."""
    col = await db.get(Collection, collection_id)
    if not col or col.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Collection not found.")

    # Move related documents to Uncategorized
    docs_result = await db.execute(
        select(Document).where(Document.collection_id == collection_id)
    )
    for doc in docs_result.scalars().all():
        doc.collection_id = None

    await db.delete(col)
    await db.commit()
    return None

@router.post("/documents/{document_id}/move", status_code=status.HTTP_200_OK)
async def move_document_to_collection(
    document_id: uuid.UUID,
    data: DocumentMoveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Move a document to a specific collection or to Uncategorized (collection_id = null)."""
    doc = await db.get(Document, document_id)
    if not doc or doc.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Document not found.")

    if data.collection_id:
        col = await db.get(Collection, data.collection_id)
        if not col or col.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Target collection not found.")
        doc.collection_id = col.id
    else:
        doc.collection_id = None

    await db.commit()
    return {"message": "Document moved successfully."}