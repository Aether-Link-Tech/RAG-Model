"""Multi-document reasoning (EP-06): FR-033 retrieval, FR-034 synthesis prompt, FR-035 contradictions.

* FR-033: a question that compares ("compare", "difference", "versus", "do they agree"...) gets
  the top passages *per document* (QA_MULTIDOC_PER_DOC, default 3) from the most relevant
  documents, so at least two documents are represented. Authorization is unchanged: candidates
  still come from query_authorized_chunks.
* FR-034: the prompt groups passages by document (Document A, B...) and asks the model to
  attribute every point to its document (see grounded_prompt.COMPARISON_RULES).
* FR-035: statements on the same topic in different documents are paired by meaning
  (nomic-embed-text) and classified: contradict (conflicting numbers or opposite polarity),
  agree (same content), or differ. No extra LLM call, so it stays fast on CPU.
"""
from __future__ import annotations

import logging
import math
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal, Sequence

from backend.app.core.config import settings
from backend.app.services import embedding
from backend.app.services.context_window import Passage

logger = logging.getLogger(__name__)

_COMPARE = re.compile(
    r"\b(compare|comparison|contrast|differ(?:s|ence|ences|ent)?|versus|vs\.?|both|between|"
    r"agree|disagree|contradict(?:s|ion)?|consistent|inconsistent|same as|similar(?:ities)?|"
    r"which (?:one|document|policy|version))\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_NEGATION = re.compile(r"\b(not|no|never|cannot|can't|prohibited|forbidden|banned|disallowed|without)\b",
                       re.IGNORECASE)

Relation = Literal["agree", "differ", "contradict"]


@dataclass
class Pair:
    relation: Relation
    document_a: str
    quote_a: str
    document_b: str
    quote_b: str
    similarity: float


@dataclass
class Comparison:
    relation: Literal["agree", "differ", "contradict", "unrelated"]
    documents: list[str]
    pairs: list[Pair] = field(default_factory=list)


FALSE_AGREEMENT = "The documents contradict each other on this point."
_AGREEMENT = re.compile(r"\b(agree|agrees|both (?:state|say|allow|require)|same|consistent|identical)\b", re.IGNORECASE)
_CONTRAST = re.compile(r"\b(but|while|whereas|however|although|unlike|differ|contradict|instead)\b", re.IGNORECASE)


def drop_false_agreement(answer: str, comparison: "Comparison") -> tuple[str, list[str]]:
    """FR-035: when the documents contradict each other, a sentence that claims they agree
    (with no contrast in it) is wrong even if each half is true of one document. Remove it."""
    if comparison.relation != "contradict":
        return answer, []
    kept, dropped = [], []
    from backend.app.services.answer_verification import split_sentences

    for sentence in split_sentences(answer):
        if _AGREEMENT.search(sentence) and not _CONTRAST.search(sentence):
            dropped.append(sentence.strip())
        else:
            kept.append(sentence.strip())
    return (" ".join(s for s in kept if s), dropped) if dropped and kept else (answer, [])


def is_comparison(question: str) -> bool:
    return bool(_COMPARE.search(question))


def per_document(passages: Sequence[Passage], per_doc: int | None = None,
                 max_docs: int | None = None) -> list[Passage]:
    """Top `per_doc` passages from each of the `max_docs` most relevant documents (FR-033)."""
    per_doc = per_doc or settings.QA_MULTIDOC_PER_DOC
    max_docs = max_docs or settings.QA_MULTIDOC_MAX_DOCS
    groups: "OrderedDict[str, list[Passage]]" = OrderedDict()
    for p in sorted(passages, key=lambda p: p.distance):
        groups.setdefault(p.document_id, [])
        if len(groups[p.document_id]) < per_doc:
            groups[p.document_id].append(p)
    chosen = list(groups.values())[:max_docs]
    return [p for group in chosen for p in group]


def document_count(passages: Sequence[Passage]) -> int:
    return len({p.document_id for p in passages})


def document_letters(passages: Sequence[Passage]) -> dict[str, str]:
    """document_id -> "A", "B", ... in first-appearance order (used by the prompt labels)."""
    letters: dict[str, str] = {}
    for p in passages:
        if p.document_id not in letters:
            letters[p.document_id] = chr(ord("A") + len(letters))
    return letters


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip().split()) >= 4]


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def classify(sentence_a: str, sentence_b: str, similarity: float) -> Relation:
    nums_a = {n.replace(",", "") for n in _NUMBER.findall(sentence_a)}
    nums_b = {n.replace(",", "") for n in _NUMBER.findall(sentence_b)}
    if nums_a and nums_b and nums_a != nums_b:
        return "contradict"
    if bool(_NEGATION.search(sentence_a)) != bool(_NEGATION.search(sentence_b)):
        return "contradict"
    if similarity >= settings.QA_AGREE_SIMILARITY:
        return "agree"
    return "differ"


async def compare(passages: Sequence[Passage], max_pairs: int = 5) -> Comparison:
    """FR-035: pair same-topic statements across documents and classify them."""
    names: "OrderedDict[str, str]" = OrderedDict()
    for p in passages:
        names.setdefault(p.document_id, p.document_name)
    if len(names) < 2:
        return Comparison("unrelated", list(names.values()))

    items = [(p.document_id, s) for p in passages for s in _sentences(p.text)]
    if not items:
        return Comparison("unrelated", list(names.values()))
    try:
        vectors = await embedding.embed_texts([s for _, s in items])
    except Exception as exc:
        logger.warning("Comparison without embeddings: %s", exc)
        return Comparison("unrelated", list(names.values()))

    candidates = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i][0] == items[j][0]:
                continue
            sim = _cosine(vectors[i], vectors[j])
            if sim >= settings.QA_SAME_TOPIC_SIMILARITY:
                candidates.append((sim, i, j))
    candidates.sort(reverse=True)

    pairs: list[Pair] = []
    used: set[int] = set()
    for sim, i, j in candidates:
        if i in used or j in used:
            continue
        used.update((i, j))
        (doc_a, s_a), (doc_b, s_b) = items[i], items[j]
        pairs.append(Pair(classify(s_a, s_b, sim), names[doc_a], s_a, names[doc_b], s_b, round(sim, 3)))
        if len(pairs) == max_pairs:
            break

    if not pairs:
        relation = "unrelated"
    elif any(p.relation == "contradict" for p in pairs):
        relation = "contradict"
    elif any(p.relation == "differ" for p in pairs):
        relation = "differ"
    else:
        relation = "agree"
    return Comparison(relation, list(names.values()), pairs)
