"""Per-user document collections (FR-012 / AL-147).

Collections are private to their owner (PostgreSQL RLS + explicit user_id
filters). Another user's collection is always 404. Deleting a collection moves
its documents to "Uncategorized" (collection_id = NULL); documents are never deleted.
"""
import uuid

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from backend.app.api.deps import CurrentUser, DbSession
from backend.app.models.collection import UNCATEGORIZED, Collection
from backend.app.models.document import Document
from backend.app.schemas.collection import CollectionResponse, CollectionWrite

router = APIRouter()

NOT_FOUND = "Collection not found"
DUPLICATE = "You already have a collection with this name."


async def get_owned_collection(db: DbSession, user_id: uuid.UUID, collection_id: uuid.UUID) -> Collection:
    result = await db.execute(
        select(Collection).where(Collection.id == collection_id, Collection.user_id == user_id)
    )
    collection = result.scalar_one_or_none()
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    return collection


async def _document_counts(db: DbSession, user_id: uuid.UUID) -> dict[uuid.UUID | None, int]:
    result = await db.execute(
        select(Document.collection_id, func.count(Document.id))
        .where(Document.user_id == user_id)
        .group_by(Document.collection_id)
    )
    return {collection_id: count for collection_id, count in result.all()}


def _response(collection: Collection, count: int) -> CollectionResponse:
    return CollectionResponse(
        id=collection.id,
        name=collection.name,
        document_count=count,
        created_at=collection.created_at,
        updated_at=collection.updated_at,
    )


def _is_duplicate_name(exc: IntegrityError) -> bool:
    return "uq_collections_user_name" in str(getattr(exc, "orig", exc))


@router.get("", response_model=list[CollectionResponse])
@router.get("/", response_model=list[CollectionResponse], include_in_schema=False)
async def list_collections(current_user: CurrentUser, db: DbSession) -> list[CollectionResponse]:
    """The caller's collections, A-Z, followed by the virtual "Uncategorized" entry."""
    result = await db.execute(
        select(Collection)
        .where(Collection.user_id == current_user.id)
        .order_by(func.lower(Collection.name))
    )
    counts = await _document_counts(db, current_user.id)
    items = [_response(c, counts.get(c.id, 0)) for c in result.scalars().all()]
    items.append(CollectionResponse(id=None, name=UNCATEGORIZED, document_count=counts.get(None, 0)))
    return items


@router.post("", response_model=CollectionResponse, status_code=status.HTTP_201_CREATED)
@router.post(
    "/",
    response_model=CollectionResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
async def create_collection(
    payload: CollectionWrite, current_user: CurrentUser, db: DbSession
) -> CollectionResponse:
    collection = Collection(user_id=current_user.id, name=payload.name)
    db.add(collection)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if _is_duplicate_name(exc):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=DUPLICATE) from exc
        raise
    await db.refresh(collection)
    return _response(collection, 0)


@router.get("/{collection_id}", response_model=CollectionResponse)
async def get_collection(
    collection_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> CollectionResponse:
    collection = await get_owned_collection(db, current_user.id, collection_id)
    counts = await _document_counts(db, current_user.id)
    return _response(collection, counts.get(collection.id, 0))


@router.put("/{collection_id}", response_model=CollectionResponse)
async def rename_collection(
    collection_id: uuid.UUID,
    payload: CollectionWrite,
    current_user: CurrentUser,
    db: DbSession,
) -> CollectionResponse:
    collection = await get_owned_collection(db, current_user.id, collection_id)
    collection.name = payload.name
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if _is_duplicate_name(exc):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=DUPLICATE) from exc
        raise
    await db.refresh(collection)
    counts = await _document_counts(db, current_user.id)
    return _response(collection, counts.get(collection.id, 0))


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(
    collection_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> Response:
    collection = await get_owned_collection(db, current_user.id, collection_id)
    # Move documents to Uncategorized explicitly (the FK's ON DELETE SET NULL is
    # the database-level backstop), then delete the collection in one transaction.
    await db.execute(
        update(Document)
        .where(Document.collection_id == collection.id, Document.user_id == current_user.id)
        .values(collection_id=None)
        .execution_options(synchronize_session=False)
    )
    await db.delete(collection)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
