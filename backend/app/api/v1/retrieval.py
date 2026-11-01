import logging

from fastapi import APIRouter, HTTPException, status

from backend.app.api.deps import CurrentUser
from backend.app.schemas.retrieval import (
    RetrievalQuery,
    RetrievalResponse,
    RetrievedChunkResponse,
)
from backend.app.services.embedding import EmbeddingError, embed_query
from backend.app.services.vector_store import query_for_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/retrieval", tags=["Retrieval"])

# Internal ownership metadata never leaves the API, so public hits cannot reveal
# which user uploaded a document.
_INTERNAL_METADATA_KEYS = frozenset({"user_id"})


def _public_metadata(metadata: dict) -> dict:
    return {k: v for k, v in metadata.items() if k not in _INTERNAL_METADATA_KEYS}


@router.post("/query", response_model=RetrievalResponse)
async def retrieve_for_current_user(
    payload: RetrievalQuery,
    current_user: CurrentUser,
) -> RetrievalResponse:
    try:
        query_embedding = await embed_query(payload.query)
        hits = query_for_user(
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
    except Exception as exc:
        # Internal error text can name collections/tenants; log it, don't return it.
        logger.exception("Vector store query failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vector store unavailable",
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
            )
            for hit in hits
        ],
    )
