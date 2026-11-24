"""POST /api/v1/retrieval/keyword: BM25 keyword search (FR-023 / AL-219)."""
import logging
import time

from fastapi import APIRouter, HTTPException, status

from backend.app.api.deps import CurrentUser, DbSession
from backend.app.schemas.keyword_search import (
    KeywordHitResponse,
    KeywordSearchQuery,
    KeywordSearchResponse,
)
from backend.app.services.keyword_search import keyword_search
from backend.app.services.vector_store import DatabaseAuthorizationError, VectorStoreError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/retrieval", tags=["Retrieval"])

# Internal ownership metadata never leaves the API.
_INTERNAL_METADATA_KEYS = frozenset({"user_id"})


def _hit_response(rank: int, hit) -> KeywordHitResponse:
    metadata = hit.chunk.metadata
    page = metadata.get("page")
    section = metadata.get("section")
    chunk_index = metadata.get("chunk_index")
    filename = metadata.get("filename") or metadata.get("document_name")
    return KeywordHitResponse(
        chroma_id=hit.chunk.chroma_id,
        document=hit.chunk.document,
        score=round(hit.score, 4),
        rank=rank,
        matched_terms=list(hit.matched_terms),
        visibility=hit.chunk.visibility,
        metadata={k: v for k, v in metadata.items() if k not in _INTERNAL_METADATA_KEYS},
        document_id=str(metadata["document_id"]) if metadata.get("document_id") else None,
        filename=str(filename) if filename else None,
        section=section.strip() if isinstance(section, str) and section.strip() else None,
        page=int(page) if isinstance(page, (int, float)) else None,
        chunk_index=int(chunk_index) if isinstance(chunk_index, (int, float)) else None,
    )


@router.post("/keyword", response_model=KeywordSearchResponse)
async def keyword_search_for_current_user(
    payload: KeywordSearchQuery,
    current_user: CurrentUser,
    db: DbSession,
) -> KeywordSearchResponse:
    started = time.perf_counter()
    try:
        terms, hits = await keyword_search(
            db=db,
            user_id=current_user.id,
            query=payload.query,
            top_k=payload.top_k,
            include_public=payload.include_public,
            include_private=payload.include_private,
        )
    except (VectorStoreError, DatabaseAuthorizationError) as exc:
        logger.exception("Keyword search: vector store or authorization failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search index unavailable",
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected error during keyword search")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Retrieval service unavailable",
        ) from exc

    took_ms = round((time.perf_counter() - started) * 1000, 2)
    if not terms:
        message = "The query has no searchable keywords (only common words)."
    elif not hits:
        message = "No matching passages found."
    else:
        message = None
    return KeywordSearchResponse(
        query=payload.query,
        query_terms=terms,
        top_k=payload.top_k,
        took_ms=took_ms,
        message=message,
        results=[_hit_response(rank, hit) for rank, hit in enumerate(hits, start=1)],
    )
