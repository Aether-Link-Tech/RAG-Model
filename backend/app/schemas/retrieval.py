from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.core.config import settings

# Hard ceiling for the schema; RETRIEVAL_MAX_TOP_K can only lower it.
_TOP_K_CEILING = 50


class RetrievalQuery(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    # FR-022 / AL-216: number of results. `n_results` is the older name and still accepted.
    top_k: Optional[int] = Field(default=None, ge=1, le=_TOP_K_CEILING)
    n_results: Optional[int] = Field(default=None, ge=1, le=_TOP_K_CEILING)
    include_public: bool = True
    include_private: bool = True

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Query must not be empty or whitespace only.")
        return stripped

    @model_validator(mode="after")
    def _resolve_top_k(self) -> "RetrievalQuery":
        if self.top_k is not None and self.n_results is not None and self.top_k != self.n_results:
            raise ValueError("Give either top_k or n_results, not two different values.")
        requested = self.top_k or self.n_results or settings.RETRIEVAL_DEFAULT_TOP_K
        limit = min(settings.RETRIEVAL_MAX_TOP_K, _TOP_K_CEILING)
        if requested > limit:
            raise ValueError(f"top_k must be at most {limit}.")
        self.top_k = requested
        self.n_results = requested
        return self


class RetrievedChunkResponse(BaseModel):
    chroma_id: str
    document: str
    distance: float
    # FR-022 / AL-216: cosine similarity in [0, 1] (1 = identical); results are sorted by it.
    score: float
    rank: int
    visibility: Literal["private", "public"]
    metadata: dict[str, Any]
    # FR-022 / AL-217: where the chunk came from, without digging through metadata.
    document_id: Optional[str] = None
    filename: Optional[str] = None
    section: Optional[str] = None
    chunk_index: Optional[int] = None
    # FR-061 / AL-246: where the text came from, so the UI can show "source: image on page X".
    source_type: Literal["text", "ocr"] = "text"
    page: Optional[int] = None
    bbox: Optional[list[float]] = None
    image_id: Optional[str] = None
    ocr_confidence: Optional[float] = None


class RetrievalResponse(BaseModel):
    query: str
    top_k: int
    results: list[RetrievedChunkResponse]
    private_collection_hit: bool
    # FR-022 / AL-218: server-side search time (embedding + vector search + authorization).
    took_ms: float
    message: Optional[str] = None
