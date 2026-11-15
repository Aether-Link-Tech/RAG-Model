"""Question answering pipeline (FR-028 / AL-286).

question -> embed -> authorized retrieval (AL-128, current semantic search) -> relevance cut
-> context window (FR-030) -> grounded prompt (FR-031) -> local LLM -> citation check
-> confidence (FR-046) -> ChatAnswer.

Retrieval goes through query_authorized_chunks, so only the caller's private chunks and public
chunks can ever reach the prompt. Hybrid search (FR-024) can replace `retrieve()` later without
changing the endpoint or the contract.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from typing import AsyncIterator, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.schemas.chat import ChatAnswer, ChatSource
from backend.app.services import answer_confidence, answer_verification, grounded_prompt, llm, prompt_injection
from backend.app.services.context_window import Passage, select_passages
from backend.app.services.embedding import embed_query
from backend.app.services.vector_store import RetrievedChunk, query_authorized_chunks

logger = logging.getLogger(__name__)


@dataclass
class PreparedQuestion:
    question: str
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    passages: list[Passage]          # what the model sees, numbered [1]..[n]
    messages: list[dict[str, str]]   # empty when there is nothing relevant to answer from
    injection_detected: bool = False  # FR-051: instruction-like text was removed from a passage


def _to_passage(hit: RetrievedChunk) -> Passage:
    meta = hit.metadata or {}
    page = meta.get("page")
    section = meta.get("section")
    return Passage(
        chunk_id=hit.chroma_id,
        document_id=str(meta.get("document_id", "")),
        document_name=str(meta.get("filename") or meta.get("document_name") or "Document"),
        text=hit.document or "",
        distance=float(hit.distance),
        page=int(page) if isinstance(page, (int, float)) and page else None,
        section=section if isinstance(section, str) and section else None,
        source_type="ocr" if meta.get("source_type") == "ocr" else "text",
    )


async def retrieve(
    db: AsyncSession,
    user_id: uuid.UUID,
    question: str,
    include_public: bool = True,
    include_private: bool = True,
) -> list[Passage]:
    vector = await embed_query(question)
    hits = await query_authorized_chunks(
        db=db,
        user_id=user_id,
        query_embedding=vector,
        n_results=settings.QA_RETRIEVAL_K,
        include_public=include_public,
        include_private=include_private,
    )
    relevant = [h for h in hits if h.distance < settings.QA_MAX_DISTANCE and (h.document or "").strip()]
    return [_to_passage(h) for h in relevant]


async def prepare(
    db: AsyncSession,
    user_id: uuid.UUID,
    question: str,
    conversation_id: uuid.UUID | None = None,
    include_public: bool = True,
    include_private: bool = True,
) -> PreparedQuestion:
    found = await retrieve(db, user_id, question, include_public, include_private)
    found, injected = _remove_injected_instructions(found)
    passages = select_passages(found, settings.QA_MAX_CONTEXT_TOKENS)
    enough = evidence_is_sufficient(passages)
    messages = grounded_prompt.build_messages(question, passages, settings.LLM_MODEL) if enough else []
    return PreparedQuestion(
        question=question,
        conversation_id=conversation_id or uuid.uuid4(),
        message_id=uuid.uuid4(),
        passages=passages,
        messages=messages,
        injection_detected=injected,
    )


def _remove_injected_instructions(passages: list[Passage]) -> tuple[list[Passage], bool]:
    """FR-051: strip instruction-like sentences from document text before it reaches the model."""
    cleaned, injected = [], False
    for passage in passages:
        result = prompt_injection.clean_passage(passage.text)
        if result.removed:
            injected = True
            logger.warning("Removed %d instruction-like sentence(s) from chunk %s", result.removed, passage.chunk_id)
        if result.text.strip():
            cleaned.append(replace(passage, text=result.text))
    return cleaned, injected


def evidence_is_sufficient(passages: Sequence[Passage]) -> bool:
    """FR-048: answer only with enough relevant passages, or a single strong one."""
    if not passages:
        return False
    if len(passages) >= settings.QA_MIN_EVIDENCE_CHUNKS:
        return True
    return min(p.distance for p in passages) <= settings.QA_STRONG_EVIDENCE_DISTANCE


def _source(marker: int, passage: Passage) -> ChatSource:
    return ChatSource(
        marker=marker,
        document_id=passage.document_id,
        document_name=passage.document_name,
        page=passage.page,
        section=passage.section,
        source_type="ocr" if passage.source_type == "ocr" else "text",
        text=passage.text,
        score=round(answer_confidence.similarity(passage.distance), 4),
    )


def insufficient_answer(prepared: PreparedQuestion) -> ChatAnswer:
    return ChatAnswer(
        conversation_id=prepared.conversation_id,
        message_id=prepared.message_id,
        answer=grounded_prompt.INSUFFICIENT_MESSAGE,
        sources=[],
        insufficient=True,
    )


def finalize(prepared: PreparedQuestion, raw_answer: str) -> ChatAnswer:
    """Turn the model's raw text into the API answer: markers checked, sources, confidence."""
    text = grounded_prompt.strip_reasoning(raw_answer)
    if not prepared.passages or not text or grounded_prompt.is_insufficient(text):
        return insufficient_answer(prepared)

    n = len(prepared.passages)
    text = grounded_prompt.drop_invalid_markers(text, n).strip()
    markers = grounded_prompt.cited_markers(text, n)
    # An answer without valid citations is still shown, with every passage it was given.
    supporting = markers or list(range(1, n + 1))
    sources = [_source(m, prepared.passages[m - 1]) for m in supporting]
    confidence = answer_confidence.assess([prepared.passages[m - 1].distance for m in supporting])
    if not markers:
        logger.info("Answer %s has no valid citations", prepared.message_id)
    return ChatAnswer(
        conversation_id=prepared.conversation_id,
        message_id=prepared.message_id,
        answer=text,
        sources=sources,
        uncertain=confidence.uncertain,
        uncertain_reason=confidence.reason,
    )


async def complete_answer(prepared: PreparedQuestion, raw_answer: str) -> ChatAnswer:
    """finalize() plus the EP-09 checks: compromised output (FR-051) and claim verification
    with claim-to-passage links (FR-049 / FR-050 / FR-044)."""
    result = finalize(prepared, raw_answer)
    result.injection_detected = prepared.injection_detected
    if result.insufficient:
        return result
    if prompt_injection.answer_is_compromised(result.answer):
        logger.warning("Answer %s withheld: looks like injected output", prepared.message_id)
        return _updated(result, answer=prompt_injection.REFUSAL, sources=[], claims=[], uncertain=True,
                        uncertain_reason=prompt_injection.REFUSAL, injection_detected=True)
    if not settings.QA_VERIFY_CLAIMS:
        return result

    check = await answer_verification.verify(result.answer, prepared.passages)
    substantive = [c for c in check.claims if c.score < 1.0 or c.markers]
    if substantive and not any(c.supported for c in substantive):
        logger.info("Answer %s: no statement is supported by its sources", prepared.message_id)
        return insufficient_answer(prepared)

    n = len(prepared.passages)
    markers = grounded_prompt.cited_markers(check.answer, n)
    for claim in check.claims:  # FR-044: uncited statements linked to their source
        for m in claim.markers:
            if claim.supported and m not in markers:
                markers.append(m)
    supporting = markers or [s.marker for s in result.sources]
    confidence = answer_confidence.assess([prepared.passages[m - 1].distance for m in supporting])
    uncertain, reason = confidence.uncertain, confidence.reason
    if check.removed:
        uncertain = True
        reason = f"{check.removed} statement(s) were removed because the sources do not support them."
    return _updated(
        result,
        answer=check.answer,
        sources=[_source(m, prepared.passages[m - 1]).model_dump() for m in supporting],
        claims=[vars(c) for c in check.claims],
        removed_claims=check.removed,
        uncertain=uncertain,
        uncertain_reason=reason,
    )


def _updated(result: ChatAnswer, **changes) -> ChatAnswer:
    """A validated copy (model_copy would skip validation of nested sources/claims)."""
    return ChatAnswer.model_validate({**result.model_dump(), **changes})


async def answer(prepared: PreparedQuestion) -> ChatAnswer:
    if not prepared.messages:
        return insufficient_answer(prepared)
    return await complete_answer(prepared, await llm.complete(prepared.messages))


async def stream_answer(prepared: PreparedQuestion) -> AsyncIterator[tuple[str, object]]:
    """Yield ("token", text) pieces, then ("done", ChatAnswer).

    Text that could be the start of the INSUFFICIENT marker is held back, so the marker itself
    is never streamed to the user.
    """
    if not prepared.messages:
        yield "done", insufficient_answer(prepared)
        return
    marker = grounded_prompt.INSUFFICIENT_MARKER
    pieces: list[str] = []
    pending = ""
    deciding = True
    async for piece in llm.stream(prepared.messages):
        pieces.append(piece)
        if not deciding:
            yield "token", piece
            continue
        pending += piece
        head = pending.lstrip()
        # A reasoning block is never shown: hold it until it closes, then drop it.
        if "<think>".startswith(head[:7]) and head:
            if "</think>" not in head.lower():
                continue
            pending = grounded_prompt.strip_reasoning(pending)
            head = pending.lstrip()
            if not head:
                continue
        if marker.startswith(head) or head.startswith(marker):
            continue  # could still be (or is) the marker: keep holding
        deciding = False
        yield "token", pending
    # The final answer is authoritative (clients replace the streamed text with it).
    yield "done", await complete_answer(prepared, "".join(pieces))


def sources_preview(passages: Sequence[Passage]) -> list[ChatSource]:
    """All passages the model was given, numbered as in the prompt (debug / UI preview)."""
    return [_source(i, p) for i, p in enumerate(passages, start=1)]
