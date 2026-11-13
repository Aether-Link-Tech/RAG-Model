import json
import logging

from fastapi import APIRouter, HTTPException, status

from backend.app.api.deps import CurrentUser, DbSession
from backend.app.schemas.retrieval import (
    RetrievalQuery,
    RetrievalResponse,
    RetrievedChunkResponse,
)
from backend.app.services.embedding import EmbeddingError, embed_query
from backend.app.services.vector_store import (
    DatabaseAuthorizationError,
    VectorStoreError,
    query_authorized_chunks,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/retrieval", tags=["Retrieval"])

# Internal ownership metadata never leaves the API, so public hits cannot reveal
# which user uploaded a document.
_INTERNAL_METADATA_KEYS = frozenset({"user_id"})


def _public_metadata(metadata: dict) -> dict:
    return {k: v for k, v in metadata.items() if k not in _INTERNAL_METADATA_KEYS}


def _source_fields(metadata: dict) -> dict:
    """FR-061 / AL-246: expose where a chunk came from (text or an image on page X)."""
    bbox = metadata.get("bbox")
    if isinstance(bbox, str):
        try:
            bbox = json.loads(bbox)
        except ValueError:
            bbox = None
    page = metadata.get("page")
    confidence = metadata.get("ocr_confidence")
    return {
        "source_type": "ocr" if metadata.get("source_type") == "ocr" else "text",
        "page": int(page) if isinstance(page, (int, float)) else None,
        "bbox": [float(v) for v in bbox] if isinstance(bbox, list) and len(bbox) == 4 else None,
        "image_id": metadata.get("image_id") if isinstance(metadata.get("image_id"), str) else None,
        "ocr_confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
    }


@router.post("/query", response_model=RetrievalResponse)
async def retrieve_for_current_user(
    payload: RetrievalQuery,
    current_user: CurrentUser,
    db: DbSession,
) -> RetrievalResponse:
    try:
        query_embedding = await embed_query(payload.query)
        hits = await query_authorized_chunks(
            db=db,
            user_id=current_user.id,
            query_embedding=query_embedding,
            n_results=payload.n_results,
            include_public=payload.include_public,
            include_private=payload.include_private,
        )
    except EmbeddingError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except (VectorStoreError, DatabaseAuthorizationError) as exc:
        logger.exception("Retrieval vector store or database authorization failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vector store unavailable",
        ) from exc
    except Exception as exc:
        # Internal error text can name collections/tenants; log it, don't return it.
        logger.exception("Unexpected error during retrieval")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Retrieval service unavailable",
        ) from exc

    return RetrievalResponse(
        query=payload.query,
        private_collection_hit=any(hit.visibility == "private" for hit in hits),
        results=[
            RetrievedChunkResponse(
                chroma_id=hit.chroma_id,
                document=hit.document,
                distance=hit.distance,
                visibility=hit.visibility,
                metadata=_public_metadata(hit.metadata),
                **_source_fields(hit.metadata),
            )
            for hit in hits
        ],
    )
