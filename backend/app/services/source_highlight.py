"""Source highlights and guest excerpts (public landing page, ask-only access).

Every cited source gets `highlight`: the sentence of the passage that best supports the
statements citing it, so the UI can mark it inside the passage. Guests get only an excerpt
around that sentence (PUBLIC_SOURCE_MAX_CHARS), never a whole chunk, so a document cannot be
rebuilt by asking many questions. Plain word overlap is used: cheap, no extra model call.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from backend.app.schemas.chat import ChatAnswer

_WORD = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)?")
_MARKER = re.compile(r"\[(\d{1,3})\]")
_STOP = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the their this to was "
    "were will with within must should may can not no all any each".split()
)


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def best_sentence(passage: str, statements: Iterable[str]) -> Optional[str]:
    """The passage sentence sharing the most content words with the statements (None if no overlap)."""
    target = set().union(*(_words(s) for s in statements)) if statements else set()
    best, best_score = None, 0.0
    for sentence in sentences(passage):
        words = _words(sentence)
        if not words:
            continue
        overlap = len(words & target)
        score = overlap + overlap / len(words)  # ties go to the more focused sentence
        if overlap and score > best_score:
            best, best_score = sentence, score
    return best


def _statements_for(answer: ChatAnswer, marker: int) -> list[str]:
    claims = [c.text for c in answer.claims if marker in c.markers and c.supported]
    if claims:
        return claims
    cited = [s for s in sentences(answer.answer) if f"[{marker}]" in s]
    return [_MARKER.sub("", s) for s in cited] or [_MARKER.sub("", answer.answer)]


def with_highlights(answer: ChatAnswer) -> ChatAnswer:
    """Set `highlight` on every source (unchanged when nothing matches)."""
    if not answer.sources:
        return answer
    data = answer.model_dump()
    for source in data["sources"]:
        source["highlight"] = best_sentence(source["text"], _statements_for(answer, source["marker"]))
    return ChatAnswer.model_validate(data)


def excerpt(text: str, highlight: Optional[str], max_chars: int) -> str:
    """At most `max_chars` of `text`, centred on the highlighted sentence, with ellipses where cut."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    hl = " ".join((highlight or "").split())
    if hl and len(hl) >= max_chars:
        return hl[: max_chars - 1].rstrip() + "…"
    start = text.find(hl) if hl else -1
    if start < 0:
        start = 0
        end = max_chars
    else:
        pad = (max_chars - len(hl)) // 2
        start = max(0, start - pad)
        end = min(len(text), start + max_chars)
        start = max(0, end - max_chars)
    if start > 0:  # begin at a word boundary
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < start + 30 else start
    if end < len(text):
        space = text.rfind(" ", start, end)
        end = space if space > start else end
    return ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def guest_view(answer: ChatAnswer, max_chars: int) -> ChatAnswer:
    """What a guest may see: excerpts instead of whole passages, no internal chunk ids."""
    data = with_highlights(answer).model_dump()
    for source in data["sources"]:
        source["text"] = excerpt(source["text"], source.get("highlight"), max_chars)
    for claim in data["claims"]:
        claim["chunk_id"] = None
    return ChatAnswer.model_validate(data)
