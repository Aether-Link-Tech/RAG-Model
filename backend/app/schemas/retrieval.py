from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class RetrievalQuery(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    n_results: int = Field(default=5, ge=1, le=50)
    include_public: bool = True
    include_private: bool = True


class RetrievedChunkResponse(BaseModel):
    chroma_id: str
    document: str
    distance: float
    visibility: Literal["private", "public"]
    metadata: dict[str, Any]
    # FR-061 / AL-246: where the text came from, so the UI can show "source: image on page X".
    source_type: Literal["text", "ocr"] = "text"
    page: Optional[int] = None
    bbox: Optional[list[float]] = None
    image_id: Optional[str] = None
    ocr_confidence: Optional[float] = None


class RetrievalResponse(BaseModel):
    query: str
    results: list[RetrievedChunkResponse]
    private_collection_hit: bool
    message: Optional[str] = None
