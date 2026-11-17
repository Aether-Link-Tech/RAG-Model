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
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Sequence

from backend.app.core.config import settings
from backend.app.services import embedding
from backend.app.services.context_window import Passage

logger = logging.getLogger(__name__)

_MARKER = re.compile(r"\s*\[(\d{1,3})\]")
_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)(?:\s*\[\d{1,3}\])*", re.MULTILINE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_NEGATION = re.compile(r"\b(not|no|never|cannot|can't|prohibited|forbidden|banned|disallowed)\b", re.IGNORECASE)
_WORD = re.compile(r"[a-zA-Z][a-zA-Z'-]{2,}")
_STOP = frozenset("""
the and for are but not you all any can her was one our out has have had his how its may new now
see who did does this that with from they them their there then than what when where which while will
would should could must also into onto upon about above below more most such only own same very just
been being were your yours each every some other these those shall within without per via
document documents states stated state says said specifies specify mentions mention according whereas both
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


def split_sentences(text: str) -> list[str]:
    """Split at . ! ? followed by whitespace (keeps '2026.txt' and '2.5' intact), and at newlines.
    A trailing [n] marker after the full stop stays with its sentence."""
    text = re.sub(r"([.!?])\s*((?:\[\d{1,3}\])+)", r" \2\1", text).replace("  ", " ")  # "x. [1]" -> "x [1]."
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def split_claims(answer: str) -> list[tuple[str, list[int], str]]:
    """(claim text without markers, markers, original sentence incl. markers)."""
    out = []
    for raw in split_sentences(answer):
        sentence = raw.strip()
        if not sentence:
            continue
        markers = [int(m) for m in re.findall(r"\[(\d{1,3})\]", sentence)]
        text = _MARKER.sub("", sentence).strip()
        if text:
            out.append((text, markers, sentence))
    return out


# Quantity limits ("no later than 24 hours", "not more than 5") are not negations of the statement.
_LIMIT_PHRASE = re.compile(r"\b(no|not) (later|more|less|fewer|earlier|sooner|longer) than\b|\bnot exceed\b|\bno less than\b", re.IGNORECASE)


def _negated(text: str) -> bool:
    return bool(_NEGATION.search(_LIMIT_PHRASE.sub(" ", text)))


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


_VECTOR_CACHE: "OrderedDict[str, list[float]]" = OrderedDict()
_CACHE_SIZE = 5000


async def _vectors(texts: list[str]) -> list[list[float]] | None:
    """Embeddings with an in-process cache: passage sentences repeat across questions."""
    missing = [t for t in dict.fromkeys(texts) if t not in _VECTOR_CACHE]
    if missing:
        try:
            fresh = await embedding.embed_texts(missing)
        except Exception as exc:  # degrade to word-level checks
            logger.warning("Claim verification without embeddings: %s", exc)
            return None
        for text, vector in zip(missing, fresh):
            _VECTOR_CACHE[text] = vector
            if len(_VECTOR_CACHE) > _CACHE_SIZE:
                _VECTOR_CACHE.popitem(last=False)
    return [_VECTOR_CACHE[t] for t in texts]


# Statements *about* the documents ("they agree", "No, the policies differ") are judged by the
# comparison analysis (multi_doc), not by passage text.
_RELATIONAL = re.compile(r"\b(agree|agrees|disagree|differ|differs|different|same|consistent|"
                         r"inconsistent|contradict|contradicts|identical|conflict)\b", re.IGNORECASE)


@dataclass
class _Support:
    score: float
    index: int | None
    numbers_ok: bool
    polarity_ok: bool


def _support(text: str, words: set[str], nums: set[str], claim_vec, scope: list[int],
             evidence: list[tuple[int, str]], evid_vecs: dict[int, list[float]],
             passage_texts: list[str]) -> _Support:
    best_score, best_idx, topic_polarities = 0.0, None, set()
    for ei, (pi, ev_text) in enumerate(evidence):
        if pi not in scope:
            continue
        vec = evid_vecs.get(ei)
        sim = _cosine(claim_vec, vec) if claim_vec is not None and vec is not None else 0.0
        ev_words = _content_words(ev_text)
        overlap = len(words & ev_words) / max(1, len(words))
        # Same topic: close in meaning, or most of the source sentence's words are in the claim.
        if sim >= settings.QA_SAME_TOPIC_SIMILARITY or len(words & ev_words) / max(1, len(ev_words)) >= 0.6:
            topic_polarities.add(_negated(ev_text))
        if max(sim, overlap) > best_score:
            best_score, best_idx = max(sim, overlap), pi
    # Word overlap ignores "not": reject when every same-topic source sentence has the opposite polarity.
    polarity_ok = not topic_polarities or _negated(text) in topic_polarities
    # A statement combining facts from several sentences of one passage is judged against the
    # whole passage (only when its polarity agrees with the source).
    for pi in scope:
        whole = len(words & _content_words(passage_texts[pi])) / max(1, len(words))
        if polarity_ok and whole > best_score:
            best_score, best_idx = whole, pi
    scope_text = " ".join(passage_texts[i] for i in scope)
    return _Support(best_score, best_idx, nums <= _numbers(scope_text), polarity_ok)


async def verify(answer: str, passages: Sequence[Passage], relational_ok: bool = False) -> Verification:
    claims = split_claims(answer)
    if not claims or not passages:
        return Verification(answer=answer)

    n = len(passages)
    texts = [p.text for p in passages]
    fabricated = sorted({m for _, markers, _ in claims for m in markers if not 1 <= m <= n})
    evidence: list[tuple[int, str]] = [(i, s) for i, p in enumerate(passages) for s in _sentences(p.text)]
    claim_vecs = await _vectors([c[0] for c in claims])
    evid_vecs: dict[int, list[float]] = {}
    loaded: set[int] = set()

    async def ensure(indices: set[int]) -> None:
        """Speed: embed a passage's sentences only when a statement needs that passage (cached)."""
        todo = [ei for ei, (pi, _) in enumerate(evidence) if pi in indices and pi not in loaded]
        loaded.update(indices)
        if todo and claim_vecs is not None:
            vecs = await _vectors([evidence[ei][1] for ei in todo])
            if vecs:
                evid_vecs.update(zip(todo, vecs))

    checks: list[ClaimCheck] = []
    kept_sentences: list[str] = []
    for ci, (text, markers, sentence) in enumerate(claims):
        valid = [m for m in markers if 1 <= m <= n]
        words, nums = _content_words(text), _numbers(text)
        trivial = not words and not nums  # "Yes." / "In short:" carry no claim of their own
        if trivial or (relational_ok and _RELATIONAL.search(text) and not nums):
            checks.append(ClaimCheck(text, valid, True, 1.0, None,
                                     None if trivial else "Statement about the documents; checked by the comparison."))
            kept_sentences.append(sentence)
            continue
        claim_vec = claim_vecs[ci] if claim_vecs else None
        scope = [m - 1 for m in valid] or list(range(n))
        await ensure(set(scope))
        sup = _support(text, words, nums, claim_vec, scope, evidence, evid_vecs, texts)
        supported = sup.numbers_ok and sup.polarity_ok and sup.score >= settings.QA_CLAIM_MIN_SUPPORT
        corrected = False
        if not supported and valid:
            # The model may cite the wrong passage: try the others before removing a statement.
            others = [i for i in range(n) if i not in scope]
            if others:
                await ensure(set(others))
                alt = _support(text, words, nums, claim_vec, others, evidence, evid_vecs, texts)
                if alt.numbers_ok and alt.polarity_ok and alt.score >= settings.QA_CLAIM_MIN_SUPPORT:
                    sup, supported, corrected = alt, True, True
        reason = None
        if not sup.numbers_ok:
            reason = "A number in this statement does not appear in the cited source."
        elif not sup.polarity_ok:
            reason = "This statement contradicts the cited source."
        elif not supported:
            reason = "This statement is not supported by the cited source."
        elif corrected:
            reason = "Citation corrected to the passage that supports it."
        elif not valid:
            reason = "Uncited statement linked to its supporting source."
        linked = sup.index + 1 if sup.index is not None else None
        out_markers = [linked] if supported and linked and (corrected or not valid) else valid
        chunk = passages[sup.index].chunk_id if supported and sup.index is not None else None
        checks.append(ClaimCheck(text, out_markers, supported, round(sup.score, 3), chunk, reason))
        if supported:
            if corrected:
                bare = re.sub(r"\s*\[\d{1,3}\]", "", sentence).rstrip()
                sentence = (f"{bare[:-1]} [{linked}]{bare[-1]}" if bare[-1:] in ".!?" else f"{bare} [{linked}]")
            elif not valid and linked:  # FR-044: show the linked source in the text
                sentence = f"{sentence.rstrip()} [{linked}]"
            kept_sentences.append(sentence)

    removed = sum(1 for c in checks if not c.supported)
    changed = any(c.reason and c.reason.startswith(("Uncited", "Citation corrected")) for c in checks if c.supported)
    cleaned = " ".join(kept_sentences) if (removed or changed) and kept_sentences else answer
    if removed:
        logger.info("Claim verification removed %d unsupported statement(s)", removed)
    return Verification(answer=cleaned, claims=checks, removed=removed if kept_sentences else 0,
                        fabricated_markers=fabricated)
