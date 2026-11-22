import json
import logging
import time

from fastapi import APIRouter, HTTPException, status

from backend.app.api.deps import CurrentUser, DbSession
from backend.app.schemas.retrieval import (
    RetrievalQuery,
    RetrievalResponse,
    RetrievedChunkResponse,
)
from backend.app.services.answer_confidence import similarity
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


def _location_fields(metadata: dict) -> dict:
    """FR-022 / AL-217: document, file name, section and chunk position of a hit."""
    section = metadata.get("section")
    filename = metadata.get("filename") or metadata.get("document_name")
    chunk_index = metadata.get("chunk_index")
    document_id = metadata.get("document_id")
    return {
        "document_id": str(document_id) if document_id else None,
        "filename": str(filename) if filename else None,
        "section": section.strip() if isinstance(section, str) and section.strip() else None,
        "chunk_index": int(chunk_index) if isinstance(chunk_index, (int, float)) else None,
    }


@router.post("/query", response_model=RetrievalResponse)
async def retrieve_for_current_user(
    payload: RetrievalQuery,
    current_user: CurrentUser,
    db: DbSession,
) -> RetrievalResponse:
    started = time.perf_counter()
    try:
        query_embedding = await embed_query(payload.query)
        hits = await query_authorized_chunks(
            db=db,
            user_id=current_user.id,
            query_embedding=query_embedding,
            n_results=payload.top_k,
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

    # Highest similarity first; ties keep the vector store's order.
    ranked = sorted(hits, key=lambda hit: hit.distance)
    took_ms = round((time.perf_counter() - started) * 1000, 2)
    logger.info("Semantic search returned %d results in %.1f ms", len(ranked), took_ms)
    return RetrievalResponse(
        query=payload.query,
        top_k=payload.top_k,
        took_ms=took_ms,
        private_collection_hit=any(hit.visibility == "private" for hit in ranked),
        message=None if ranked else "No matching passages found.",
        results=[
            RetrievedChunkResponse(
                chroma_id=hit.chroma_id,
                document=hit.document,
                distance=hit.distance,
                score=round(similarity(hit.distance), 4),
                rank=rank,
                visibility=hit.visibility,
                metadata=_public_metadata(hit.metadata),
                **_location_fields(hit.metadata),
                **_source_fields(hit.metadata),
            )
            for rank, hit in enumerate(ranked, start=1)
        ],
    )
