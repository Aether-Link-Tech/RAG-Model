from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class KeywordSearchQuery(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=50)
    include_public: bool = True
    include_private: bool = True

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Query must not be empty or whitespace only.")
        return stripped


class KeywordHitResponse(BaseModel):
    chroma_id: str
    document: str
    # BM25 score: higher is better, not bounded to [0, 1].
    score: float
    rank: int
    matched_terms: list[str]
    visibility: Literal["private", "public"]
    metadata: dict[str, Any]
    document_id: Optional[str] = None
    filename: Optional[str] = None
    section: Optional[str] = None
    page: Optional[int] = None
    chunk_index: Optional[int] = None


class KeywordSearchResponse(BaseModel):
    query: str
    # Terms actually searched for, after tokenizing and removing stopwords.
    query_terms: list[str]
    top_k: int
    results: list[KeywordHitResponse]
    took_ms: float
    message: Optional[str] = None
