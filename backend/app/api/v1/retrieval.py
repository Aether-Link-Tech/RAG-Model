from fastapi import APIRouter, HTTPException, status

from backend.app.api.deps import CurrentUser
from backend.app.schemas.retrieval import (
    RetrievalQuery,
    RetrievalResponse,
    RetrievedChunkResponse,
)
from backend.app.services.embedding import EmbeddingError, embed_query
from backend.app.services.vector_store import query_for_user

router = APIRouter(prefix="/retrieval", tags=["Retrieval"])


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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Vector store unavailable: {exc}",
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
                metadata=hit.metadata,
            )
            for hit in hits
        ],
    )
