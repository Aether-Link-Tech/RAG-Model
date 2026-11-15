"""Context-window management (FR-030 / AL-290, AL-291).

Picks the most relevant passages that fit the LLM's token budget, in relevance order, and
truncates the last one at a sentence boundary instead of mid-sentence.

Token counts use tiktoken (cl100k_base). tiktoken downloads its vocabulary on first use; when
that is impossible (offline machine) we fall back to a conservative estimate so answering
still works: ~3 characters per token over-counts English text, so the budget is never exceeded.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Callable, Sequence

logger = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@lru_cache(maxsize=1)
def _token_counter() -> Callable[[str], int]:
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        return lambda text: len(encoding.encode(text, disallowed_special=()))
    except Exception:  # offline, or tiktoken missing
        logger.warning("tiktoken unavailable; using a conservative token estimate")
        return lambda text: max(1, -(-len(text) // 3))


def count_tokens(text: str) -> int:
    return _token_counter()(text)


@dataclass(frozen=True)
class Passage:
    """One retrieved chunk as the answer pipeline sees it."""

    chunk_id: str
    document_id: str
    document_name: str
    text: str
    distance: float
    page: int | None = None
    section: str | None = None
    source_type: str = "text"


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Longest prefix of whole sentences within max_tokens ('' if not even one sentence fits)."""
    if count_tokens(text) <= max_tokens:
        return text
    kept: list[str] = []
    for sentence in _SENTENCE_END.split(text.strip()):
        candidate = " ".join([*kept, sentence])
        if count_tokens(candidate) > max_tokens:
            break
        kept.append(sentence)
    return " ".join(kept)


def select_passages(
    passages: Sequence[Passage],
    max_tokens: int,
    per_passage_overhead: int = 12,
) -> list[Passage]:
    """Passages in relevance order (smallest distance first) until the budget is used.

    ``per_passage_overhead`` reserves room for the "[n] Document, page" header added by the
    prompt builder. The last passage that does not fit whole is cut at a sentence boundary.
    """
    if max_tokens <= 0:
        return []
    chosen: list[Passage] = []
    used = 0
    for passage in sorted(passages, key=lambda p: p.distance):
        room = max_tokens - used - per_passage_overhead
        if room <= 0:
            break
        size = count_tokens(passage.text)
        if size <= room:
            chosen.append(passage)
            used += size + per_passage_overhead
            continue
        cut = truncate_to_tokens(passage.text, room)
        if cut:
            chosen.append(replace(passage, text=cut))
        break
    logger.info("Context: %d passages, ~%d tokens (budget %d)", len(chosen), used, max_tokens)
    return chosen
