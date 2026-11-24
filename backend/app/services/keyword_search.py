"""Keyword (BM25) search over the user's authorized chunks (FR-023 / AL-219, AL-221).

The corpus is the chunk text already stored in ChromaDB: the shared public collection
and the caller's own private collection (read with `private_owner_filter`). An in-memory
BM25 index is built per user and cached; it is rebuilt when a chunk is added, deleted or
renamed (vector_store bumps a generation counter), when a collection's size changes
(writes from another process), or after KEYWORD_INDEX_TTL_SECONDS.

Ranked candidates then go through `authorize_retrieved_chunks`, the same PostgreSQL
RLS check semantic search uses (AL-128), so stale, deleted or foreign chunks are dropped
before anything is returned.
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
import uuid
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.db.chroma import get_private_collection, get_public_collection
from backend.app.services.keyword_tokenizer import tokenize
from backend.app.services.vector_store import (
    RetrievedChunk,
    Visibility,
    VectorStoreError,
    authorize_retrieved_chunks,
    chunk_generation,
    is_valid_chunk_metadata,
    private_owner_filter,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KeywordHit:
    chunk: RetrievedChunk
    score: float
    matched_terms: tuple[str, ...]


class Bm25Index:
    """Okapi BM25 with the Lucene IDF, log(1 + (N - n + 0.5) / (n + 0.5)), which stays
    positive even for tiny corpora. Only chunks containing a query term are scored."""

    def __init__(self, chunks: Sequence[RetrievedChunk], k1: float = 1.2, b: float = 0.75):
        self.chunks = list(chunks)
        self.k1 = k1
        self.b = b
        self._lengths: list[int] = []
        self._postings: dict[str, list[tuple[int, int]]] = {}
        for position, chunk in enumerate(self.chunks):
            terms = tokenize(_searchable_text(chunk))
            self._lengths.append(len(terms))
            for term, count in Counter(terms).items():
                self._postings.setdefault(term, []).append((position, count))
        total = sum(self._lengths)
        self._average_length = total / len(self._lengths) if self._lengths and total else 1.0

    def __len__(self) -> int:
        return len(self.chunks)

    def idf(self, term: str) -> float:
        containing = len(self._postings.get(term, ()))
        return math.log(1.0 + (len(self.chunks) - containing + 0.5) / (containing + 0.5))

    def search(self, terms: Sequence[str]) -> list[tuple[int, float, tuple[str, ...]]]:
        scores: dict[int, float] = {}
        matched: dict[int, list[str]] = {}
        for term in dict.fromkeys(terms):  # each distinct term once, in query order
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = self.idf(term)
            for position, count in postings:
                norm = self.k1 * (1 - self.b + self.b * self._lengths[position] / self._average_length)
                scores[position] = scores.get(position, 0.0) + idf * count * (self.k1 + 1) / (count + norm)
                matched.setdefault(position, []).append(term)
        ranked = sorted(scores.items(), key=lambda item: (-item[1], self.chunks[item[0]].chroma_id))
        return [(position, score, tuple(matched[position])) for position, score in ranked]


def _searchable_text(chunk: RetrievedChunk) -> str:
    section = chunk.metadata.get("section")
    return f"{section}\n{chunk.document}" if isinstance(section, str) and section else chunk.document


# ----------------------------------------------------------------------------- cache

@dataclass
class _CacheEntry:
    stamp: tuple
    built_at: float
    index: Bm25Index


_cache: "OrderedDict[tuple, _CacheEntry]" = OrderedDict()
_cache_lock = threading.Lock()


def clear_keyword_index_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _load(collection: Any, where: dict[str, Any], visibility: Visibility, owner_hex: str) -> list[RetrievedChunk]:
    stored = collection.get(where=where, include=["documents", "metadatas"])
    chunks: list[RetrievedChunk] = []
    for chroma_id, document, metadata in zip(
        stored.get("ids") or [], stored.get("documents") or [], stored.get("metadatas") or []
    ):
        metadata = dict(metadata or {})
        if not document or not is_valid_chunk_metadata(metadata, visibility, owner_hex):
            continue
        chunks.append(RetrievedChunk(chroma_id, document, 0.0, metadata, visibility))
    return chunks


def _get_index(user_id: uuid.UUID, include_public: bool, include_private: bool) -> Bm25Index:
    owner_hex = user_id.hex
    try:
        public = get_public_collection() if include_public else None
        private = get_private_collection(user_id) if include_private else None
        stamp = (
            chunk_generation(None, "public") if public is not None else None,
            public.count() if public is not None else None,
            chunk_generation(user_id, "private") if include_private else None,
            private.count() if private is not None else None,
        )
        key = (owner_hex, include_public, include_private)
        with _cache_lock:
            entry = _cache.get(key)
            fresh = (
                entry is not None
                and entry.stamp == stamp
                and time.monotonic() - entry.built_at < settings.KEYWORD_INDEX_TTL_SECONDS
            )
            if fresh:
                _cache.move_to_end(key)
                return entry.index

        chunks: list[RetrievedChunk] = []
        if public is not None:
            chunks += _load(public, {"visibility": {"$eq": "public"}}, "public", owner_hex)
        if private is not None:
            chunks += _load(private, private_owner_filter(user_id), "private", owner_hex)
    except VectorStoreError:
        raise
    except Exception as exc:
        logger.exception("Loading chunks for keyword search failed")
        raise VectorStoreError("ChromaDB read failed") from exc

    index = Bm25Index(chunks, k1=settings.BM25_K1, b=settings.BM25_B)
    with _cache_lock:
        _cache[key] = _CacheEntry(stamp, time.monotonic(), index)
        _cache.move_to_end(key)
        while len(_cache) > max(1, settings.KEYWORD_INDEX_CACHE_SIZE):
            _cache.popitem(last=False)
    return index


# ----------------------------------------------------------------------------- search

async def keyword_search(
    *,
    db: AsyncSession,
    user_id: uuid.UUID | str,
    query: str,
    top_k: int = 5,
    include_public: bool = True,
    include_private: bool = True,
) -> tuple[list[str], list[KeywordHit]]:
    """Return (query terms, top_k authorized hits ranked by BM25 score)."""
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    terms = list(dict.fromkeys(tokenize(query)))
    if not terms or not (include_public or include_private):
        return terms, []

    owner = uuid.UUID(str(user_id))
    index = await asyncio.to_thread(_get_index, owner, include_public, include_private)
    ranked = index.search(terms)

    hits: list[KeywordHit] = []
    batch_size = max(top_k * 2, 20)
    for start in range(0, len(ranked), batch_size):
        batch = ranked[start:start + batch_size]
        allowed = await authorize_retrieved_chunks(
            db=db, user_id=owner, chunks=[index.chunks[position] for position, _, _ in batch]
        )
        allowed_ids = {chunk.chroma_id for chunk in allowed}
        for position, score, matched in batch:
            chunk = index.chunks[position]
            if chunk.chroma_id in allowed_ids:
                hits.append(KeywordHit(chunk, score, matched))
        if len(hits) >= top_k:
            break
    return terms, hits[:top_k]
