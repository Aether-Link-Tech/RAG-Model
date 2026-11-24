"""Tokenizer for keyword (BM25) search (FR-023 / AL-220).

The same function tokenizes stored chunks and user queries, so both sides always
agree: Unicode-normalised, lower-cased word tokens with English stopwords removed.
"""
from __future__ import annotations

import re
import unicodedata

# Letters/digits, optionally joined by an inner apostrophe, hyphen or dot
# ("don't", "covid-19", "v1.5"). Underscores count as separators.
_TOKEN_RE = re.compile(r"[^\W_]+(?:['\-.][^\W_]+)*", re.UNICODE)

# Common English function words; they match almost every chunk and only add noise.
STOPWORDS: frozenset[str] = frozenset(
    """
    a about above after again against all am an and any are as at be because been before
    being below between both but by can could did do does doing down during each few for
    from further had has have having he her here hers herself him himself his how i if in
    into is it its itself just me more most my myself no nor not now of off on once only or
    other our ours ourselves out over own same she should so some such than that the their
    theirs them themselves then there these they this those through to too under until up
    very was we were what when where which while who whom why will with would you your
    yours yourself yourselves also may might must shall us via
    """.split()
)


def tokenize(text: str) -> list[str]:
    """Split text into searchable terms, dropping stopwords and single letters."""
    if not text:
        return []
    normalised = unicodedata.normalize("NFKC", text).casefold()
    normalised = normalised.replace("’", "'")
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(normalised):
        token = match.group(0).strip("'-.")
        if token.endswith("'s"):
            token = token[:-2]
        if not token or token in STOPWORDS:
            continue
        if len(token) == 1 and not token.isdigit():
            continue
        tokens.append(token)
    return tokens
