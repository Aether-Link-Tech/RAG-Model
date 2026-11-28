"""Q&A API contract (FR-028 / AL-285). Shared with the frontend (mock data) and conversation memory.

POST /api/v1/chat/ask
  JSON response (Accept: application/json) or Server-Sent Events (Accept: text/event-stream):
  "token" {"text": "..."} repeated, then "done" {ChatAnswer}, or "error" {"detail": "..."}.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ChatAskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    conversation_id: Optional[uuid.UUID] = None
    include_public: bool = True
    include_private: bool = True


class ChatSource(BaseModel):
    marker: int = Field(..., ge=1, description="The [n] marker used in the answer")
    document_id: str
    document_name: str
    page: Optional[int] = None
    section: Optional[str] = None
    source_type: Literal["text", "ocr"] = "text"
    text: str
    score: float = Field(..., description="Similarity 0..1 (1 - cosine distance)")
    # The sentence of `text` that supports the answer, for highlighting (None if nothing matched).
    highlight: Optional[str] = None


class ClaimCheck(BaseModel):
    """FR-049 / FR-044: one statement of the answer and the source that supports it."""

    text: str
    markers: list[int] = []
    supported: bool
    score: float
    chunk_id: Optional[str] = None
    reason: Optional[str] = None


class ComparisonPair(BaseModel):
    """FR-035: two statements on the same topic from different documents."""

    relation: Literal["agree", "differ", "contradict"]
    document_a: str
    quote_a: str
    document_b: str
    quote_b: str
    similarity: float


class Comparison(BaseModel):
    relation: Literal["agree", "differ", "contradict", "unrelated"]
    documents: list[str]
    pairs: list[ComparisonPair] = []


class ChatAnswer(BaseModel):
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    answer: str
    sources: list[ChatSource]
    insufficient: bool = False
    uncertain: bool = False
    uncertain_reason: Optional[str] = None
    # EP-09 (additive): statement-level verification and what was removed.
    claims: list[ClaimCheck] = []
    removed_claims: int = 0
    injection_detected: bool = False
    # EP-06 (additive): set when the question compares two or more documents.
    comparison: Optional[Comparison] = None


class PublicAskRequest(BaseModel):
    """POST /api/v1/public/ask: a guest question on the landing page (no conversation, nothing stored)."""

    question: str = Field(..., min_length=1, max_length=4000)


class ShowcaseUpdate(BaseModel):
    showcase: bool


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class ConversationMessage(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: Literal["user", "assistant", "system"]
    content: str
    sources: list[ChatSource] = []
    insufficient: bool = False
    uncertain: bool = False
    uncertain_reason: Optional[str] = None
    claims: list[ClaimCheck] = []
    removed_claims: int = 0
    injection_detected: bool = False
    comparison: Optional[Comparison] = None
    created_at: datetime


class ConversationDetail(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    messages: list[ConversationMessage] = []
