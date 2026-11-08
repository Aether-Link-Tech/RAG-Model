"""User-specific collections (FR-012 / AL-147).

A collection is a private folder for grouping the caller's own documents.
Every query is scoped to the caller (and enforced again by PostgreSQL RLS); another
user's collection is always 404. Deleting a collection moves its documents to
"Uncategorized" via the database foreign key, it never deletes documents.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.app.api.deps import CurrentUser, DbSession
from backend.app.models.collection import Collection
from backend.app.models.document import Document

router = APIRouter(prefix="/collections", tags=["Collections"])

MAX_COLLECTIONS_PER_USER = 200


class CollectionWrite(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def _clean(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Collection name is required.")
        return value


class CollectionResponse(BaseModel):
    id: uuid.UUID
    name: str
    document_count: int = 0

    model_config = ConfigDict(from_attributes=True)


def _duplicate_name() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You already have a collection with this name.")


async def _owned_collection(db: DbSession, user_id: uuid.UUID, collection_id: uuid.UUID) -> Collection:
    result = await db.execute(
        select(Collection).where(Collection.id == collection_id, Collection.user_id == user_id)
    )
    collection = result.scalar_one_or_none()
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    return collection


async def _count(db: DbSession, user_id: uuid.UUID, collection_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count(Document.id)).where(
            Document.user_id == user_id, Document.collection_id == collection_id
        )
    )
    return int(result.scalar_one())


@router.get("", response_model=list[CollectionResponse])
async def list_collections(current_user: CurrentUser, db: DbSession) -> list[CollectionResponse]:
    counts = (
        select(Document.collection_id, func.count(Document.id).label("n"))
        .where(Document.user_id == current_user.id, Document.collection_id.is_not(None))
        .group_by(Document.collection_id)
        .subquery()
    )
    result = await db.execute(
        select(Collection, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.collection_id == Collection.id)
        .where(Collection.user_id == current_user.id)
        .order_by(func.lower(Collection.name))
    )
    return [
        CollectionResponse(id=collection.id, name=collection.name, document_count=int(count))
        for collection, count in result.all()
    ]


@router.post("", response_model=CollectionResponse, status_code=status.HTTP_201_CREATED)
async def create_collection(payload: CollectionWrite, current_user: CurrentUser, db: DbSession) -> CollectionResponse:
    existing = await db.execute(
        select(func.count(Collection.id)).where(Collection.user_id == current_user.id)
    )
    if int(existing.scalar_one()) >= MAX_COLLECTIONS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"You can have at most {MAX_COLLECTIONS_PER_USER} collections.",
        )
    collection = Collection(user_id=current_user.id, name=payload.name)
    db.add(collection)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _duplicate_name() from exc
    await db.refresh(collection)
    return CollectionResponse(id=collection.id, name=collection.name, document_count=0)


@router.patch("/{collection_id}", response_model=CollectionResponse)
async def rename_collection(
    collection_id: uuid.UUID, payload: CollectionWrite, current_user: CurrentUser, db: DbSession
) -> CollectionResponse:
    user_id = current_user.id
    collection = await _owned_collection(db, user_id, collection_id)
    collection.name = payload.name
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _duplicate_name() from exc
    await db.refresh(collection)
    return CollectionResponse(
        id=collection.id, name=collection.name, document_count=await _count(db, user_id, collection.id)
    )


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(collection_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> None:
    """Delete the collection; its documents become Uncategorized (ON DELETE SET NULL)."""
    collection = await _owned_collection(db, current_user.id, collection_id)
    await db.delete(collection)
    await db.commit()
