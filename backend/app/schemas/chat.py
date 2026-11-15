"""Q&A API contract (FR-028 / AL-285). Shared with the frontend (mock data) and conversation memory.

POST /api/v1/chat/ask
  JSON response (Accept: application/json) or Server-Sent Events (Accept: text/event-stream):
  "token" {"text": "..."} repeated, then "done" {ChatAnswer}, or "error" {"detail": "..."}.
"""
from __future__ import annotations

import uuid
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


class ChatAnswer(BaseModel):
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    answer: str
    sources: list[ChatSource]
    insufficient: bool = False
    uncertain: bool = False
    uncertain_reason: Optional[str] = None
