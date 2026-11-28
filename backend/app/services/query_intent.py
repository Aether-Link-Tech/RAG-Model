"""Query intent classification for chat turns.

Distinguishes standalone conversational turns (greetings, social check-ins, brief
acknowledgements/thanks, and farewells) from information-seeking or document queries.

A message is classified as conversational ONLY when every clause in the message is a
complete conversational act (whole-clause matching, never substring matching). Any mixed-intent
message such as "Hi, what does the uploaded document say about PostgreSQL?" contains a
non-conversational clause and is routed through the full document retrieval and grounding pipeline.
"""
from __future__ import annotations

import re
from enum import Enum


class QueryIntent(str, Enum):
    CONVERSATIONAL = "conversational"
    DOCUMENT_QUERY = "document_query"


_MAX_CONVERSATIONAL_CHARS = 100
_MAX_CONVERSATIONAL_WORDS = 14

_APOSTROPHES = re.compile(r"[’‘`]")
_CLAUSE_SPLIT = re.compile(r"[.!?;,\n\r—–]+|\s+-\s+")
_OUTER_NON_WORD = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_HAS_WORD = re.compile(r"[a-zA-Z]")

# Collapse stretched conversational spellings like "hiii", "hellooo", "heyyy", "thanksss", "byeee".
_STRETCHED_STEMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bh+i{2,}\b", re.IGNORECASE), "hi"),
    (re.compile(r"\bh+e+y{2,}\b", re.IGNORECASE), "hey"),
    (re.compile(r"\bhello{2,}\b", re.IGNORECASE), "hello"),
    (re.compile(r"\bbye{2,}\b", re.IGNORECASE), "bye"),
    (re.compile(r"\bthanks{2,}\b", re.IGNORECASE), "thanks"),
)

_VOCATIVE = r"(?:there|again|everyone|everybody|all|team|folks|friend|friends|aether|assistant|bot|today)"

_GREETING = (
    rf"(?:(?:hi|hello|hey|heya|hiya|howdy|yo|greetings|salutations|"
    rf"good\s+(?:morning|afternoon|evening|day)|morning|afternoon|evening)"
    rf"(?:\s+{_VOCATIVE}){{0,2}})"
)

_CHECKIN = (
    r"(?:how\s+are\s+you(?:\s+doing)?(?:\s+today)?|"
    r"how(?:'s|\s+is|\s+s)\s+(?:it\s+going|everything(?:\s+going)?)(?:\s+today)?|"
    r"how\s+are\s+things(?:\s+going)?|"
    r"how\s+do\s+you\s+do|"
    r"how\s+have\s+you\s+been|"
    r"what(?:'s|\s+is|\s+s)\s+up|"
    r"whats\s+up|"
    r"sup)"
)

_THANKS = (
    rf"(?:thanks(?:\s+(?:a\s+lot|so\s+much|very\s+much|again|a\s+bunch|a\s+million|"
    rf"for\s+(?:your\s+|the\s+)?help|for\s+that))?(?:\s+{_VOCATIVE})?|"
    rf"thank\s+you(?:\s+(?:so\s+much|very\s+much|again|a\s+lot|"
    rf"for\s+(?:your\s+|the\s+)?help|for\s+that))?(?:\s+{_VOCATIVE})?|"
    rf"many\s+thanks|much\s+appreciated|appreciate\s+it|thx|ty)"
)

_FAREWELL = (
    rf"(?:(?:bye(?:[-\s]bye|\s+for\s+now|\s+now)?|good[-\s]?bye|good\s*night|"
    rf"see\s+(?:you|ya)(?:\s+(?:later|soon|tomorrow))?|"
    rf"talk\s+to\s+you\s+later|ttyl|catch\s+you\s+later|take\s+care|"
    rf"have\s+a\s+(?:good|great|nice|wonderful)\s+(?:day|one|evening|night|weekend)|"
    rf"farewell|ciao)(?:\s+{_VOCATIVE})?)"
)

_ACK = r"(?:ok|okay|alright|got\s+it|great|awesome|perfect|cool|nice|understood|makes\s+sense)"

_PRIMARY_CLAUSE = re.compile(
    rf"^(?:{_GREETING}|{_CHECKIN}|{_THANKS}|{_FAREWELL}|"
    rf"{_GREETING}\s+{_CHECKIN}|{_GREETING}\s+{_THANKS}|{_GREETING}\s+{_FAREWELL}|"
    rf"{_THANKS}\s+{_FAREWELL}|{_ACK}\s+(?:{_THANKS}|{_FAREWELL}))$",
    re.IGNORECASE,
)

_SECONDARY_CLAUSE = re.compile(rf"^(?:{_ACK}|{_VOCATIVE})$", re.IGNORECASE)


def _normalize_clause(raw_clause: str) -> str:
    clause = _OUTER_NON_WORD.sub("", raw_clause)
    clause = _WHITESPACE.sub(" ", clause).strip().lower()
    for pattern, replacement in _STRETCHED_STEMS:
        clause = pattern.sub(replacement, clause)
    return clause


def classify_intent(text: str) -> QueryIntent:
    """Return CONVERSATIONAL only for standalone greetings, check-ins, thanks, or farewells."""
    stripped = (text or "").strip()
    if not stripped or len(stripped) > _MAX_CONVERSATIONAL_CHARS or not _HAS_WORD.search(stripped):
        return QueryIntent.DOCUMENT_QUERY

    normalized = _APOSTROPHES.sub("'", stripped)
    if len(normalized.split()) > _MAX_CONVERSATIONAL_WORDS:
        return QueryIntent.DOCUMENT_QUERY

    raw_clauses = _CLAUSE_SPLIT.split(normalized)
    clauses = [_normalize_clause(c) for c in raw_clauses]
    clauses = [c for c in clauses if c]
    if not clauses:
        return QueryIntent.DOCUMENT_QUERY

    has_primary = False
    for clause in clauses:
        if _PRIMARY_CLAUSE.fullmatch(clause):
            has_primary = True
        elif _SECONDARY_CLAUSE.fullmatch(clause):
            continue
        else:
            return QueryIntent.DOCUMENT_QUERY

    return QueryIntent.CONVERSATIONAL if has_primary else QueryIntent.DOCUMENT_QUERY


def is_conversational(text: str) -> bool:
    """True when `text` is a simple conversational message that needs no document retrieval."""
    return classify_intent(text) == QueryIntent.CONVERSATIONAL
