"""Claim verification, fabrication detection and claim-to-passage linking
(FR-049 / AL-76, FR-050 / AL-77, FR-044 / AL-70).

After the model answers, every sentence that states something (a "claim") is checked against the
passages it cites:

* numbers in the claim (dates, amounts, durations) must appear in a cited passage;
* its meaning must be close to a sentence in a cited passage (embedding similarity), or most of
  its content words must appear there.

Unsupported claims are removed from the answer (when something supported remains) and reported.
A claim is linked to the exact chunk that supports it best (FR-044). Citations can only point to
the passages the model was given, which all came from authorized retrieval (FR-050); anything else
is treated as fabricated.

Runs locally (nomic-embed-text through Ollama), one batched embedding call per answer. If the
embedding service fails, the word-level checks still run.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Sequence

from backend.app.core.config import settings
from backend.app.services import embedding
from backend.app.services.context_window import Passage

logger = logging.getLogger(__name__)

_MARKER = re.compile(r"\s*\[(\d{1,3})\]")
_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)(?:\s*\[\d{1,3}\])*", re.MULTILINE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_WORD = re.compile(r"[a-zA-Z][a-zA-Z'-]{2,}")
_STOP = frozenset("""
the and for are but not you all any can her was one our out has have had his how its may new now
see who did does this that with from they them their there then than what when where which while will
would should could must also into onto upon about above below more most such only own same very just
been being were your yours each every some other these those shall within without per via
""".split())


@dataclass
class ClaimCheck:
    text: str
    markers: list[int]
    supported: bool
    score: float
    chunk_id: str | None = None
    reason: str | None = None


@dataclass
class Verification:
    answer: str
    claims: list[ClaimCheck] = field(default_factory=list)
    removed: int = 0
    fabricated_markers: list[int] = field(default_factory=list)

    @property
    def any_supported(self) -> bool:
        return any(c.supported for c in self.claims)


def split_claims(answer: str) -> list[tuple[str, list[int], str]]:
    """(claim text without markers, markers, original sentence incl. markers)."""
    out = []
    for raw in _SENTENCE.findall(answer):
        sentence = raw.strip()
        if not sentence:
            continue
        markers = [int(m) for m in re.findall(r"\[(\d{1,3})\]", sentence)]
        text = _MARKER.sub("", sentence).strip()
        if text:
            out.append((text, markers, sentence))
    return out


def _content_words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text) if w.lower() not in _STOP}


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) > 3]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def _embed_all(texts: list[str]) -> list[list[float]] | None:
    try:
        return await embedding.embed_texts(texts)
    except Exception as exc:  # degrade to word-level checks
        logger.warning("Claim verification without embeddings: %s", exc)
        return None


async def verify(answer: str, passages: Sequence[Passage]) -> Verification:
    claims = split_claims(answer)
    if not claims or not passages:
        return Verification(answer=answer)

    n = len(passages)
    fabricated = sorted({m for _, markers, _ in claims for m in markers if not 1 <= m <= n})
    # Candidate evidence: every sentence of every passage, remembered with its passage index.
    evidence: list[tuple[int, str]] = [(i, s) for i, p in enumerate(passages) for s in _sentences(p.text)]
    vectors = await _embed_all([c[0] for c in claims] + [s for _, s in evidence]) if evidence else None
    claim_vecs = vectors[: len(claims)] if vectors else None
    evid_vecs = vectors[len(claims):] if vectors else None

    checks: list[ClaimCheck] = []
    kept_sentences: list[str] = []
    for ci, (text, markers, sentence) in enumerate(claims):
        valid = [m for m in markers if 1 <= m <= n]
        words, nums = _content_words(text), _numbers(text)
        if not words and not nums:  # "Yes." / "In short:" carry no claim of their own
            checks.append(ClaimCheck(text, valid, True, 1.0))
            kept_sentences.append(sentence)
            continue
        # Cited passages first; an uncited claim may still be linked to any given passage (FR-044).
        scope = [m - 1 for m in valid] or list(range(n))
        best_score, best_idx = 0.0, None
        for ei, (pi, ev_text) in enumerate(evidence):
            if pi not in scope:
                continue
            sim = _cosine(claim_vecs[ci], evid_vecs[ei]) if claim_vecs else 0.0
            overlap = len(words & _content_words(ev_text)) / max(1, len(words))
            score = max(sim, overlap)
            if score > best_score:
                best_score, best_idx = score, pi
        scope_text = " ".join(passages[i].text for i in scope)
        numbers_ok = nums <= _numbers(scope_text)
        supported = numbers_ok and best_score >= settings.QA_CLAIM_MIN_SUPPORT
        reason = None
        if not numbers_ok:
            reason = "A number in this statement does not appear in the cited source."
        elif not supported:
            reason = "This statement is not supported by the cited source."
        elif not valid:
            reason = "Uncited statement linked to its supporting source."
        chunk = passages[best_idx].chunk_id if best_idx is not None else None
        checks.append(ClaimCheck(text, valid or ([best_idx + 1] if supported and best_idx is not None else []),
                                 supported, round(best_score, 3), chunk if supported else None, reason))
        if supported:
            kept_sentences.append(sentence)

    removed = sum(1 for c in checks if not c.supported)
    if removed and kept_sentences:
        cleaned = " ".join(kept_sentences)
    else:
        cleaned = answer
    if removed:
        logger.info("Claim verification removed %d unsupported statement(s)", removed)
    return Verification(answer=cleaned, claims=checks, removed=removed if kept_sentences else 0,
                        fabricated_markers=fabricated)
