"""Section-aware text chunking with token limits and overlap."""
from __future__ import annotations

import re
from typing import TypedDict

import tiktoken

from backend.app.core.config import settings


class ExtractedText(TypedDict):
    page: int
    section: str
    text: str


class TextChunk(TypedDict):
    text: str
    section: str
    page: int
    position: int


_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_WORD_BREAK = re.compile(r"\s+")


def _token_count(text: str, encoding: tiktoken.Encoding) -> int:
    return len(encoding.encode(text, disallowed_special=()))


def _boundaries(
    text: str,
    start: int,
    end: int,
) -> tuple[list[int], list[int], list[int]]:
    segment = text[start:end]
    paragraph = [start + match.end() for match in _PARAGRAPH_BREAK.finditer(segment)]
    sentence = [start + match.end() for match in _SENTENCE_BREAK.finditer(segment)]
    word = [start + match.end() for match in _WORD_BREAK.finditer(segment)]
    return paragraph, sentence, word


def _preferred_boundary(
    boundaries: tuple[list[int], list[int], list[int]],
    *,
    earliest: int,
    latest: int,
    choose_latest: bool,
) -> int | None:
    for candidates in boundaries:
        valid = [point for point in candidates if earliest <= point <= latest]
        if valid:
            return (max if choose_latest else min)(valid)
    return None


def _max_end(
    text: str,
    start: int,
    chunk_size: int,
    encoding: tiktoken.Encoding,
) -> int:
    low = start
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _token_count(text[start:middle], encoding) <= chunk_size:
            low = middle
        else:
            high = middle - 1
    return low


def _overlap_start(
    text: str,
    start: int,
    end: int,
    overlap: int,
    encoding: tiktoken.Encoding,
) -> int:
    if overlap == 0:
        return end

    low = start + 1
    high = end
    while low < high:
        middle = (low + high) // 2
        if _token_count(text[middle:end], encoding) <= overlap:
            high = middle
        else:
            low = middle + 1

    boundaries = _boundaries(text, start, end)
    return _preferred_boundary(
        boundaries,
        earliest=low,
        latest=end - 1,
        choose_latest=False,
    ) or low


def _split_text(
    text: str,
    chunk_size: int,
    overlap: int,
    encoding: tiktoken.Encoding,
) -> list[tuple[int, int, str]]:
    chunks: list[tuple[int, int, str]] = []
    start = 0

    while start < len(text):
        hard_end = _max_end(text, start, chunk_size, encoding)
        if hard_end >= len(text):
            end = len(text)
        else:
            end = _preferred_boundary(
                _boundaries(text, start, hard_end),
                earliest=start + 1,
                latest=hard_end,
                choose_latest=True,
            ) or hard_end
        if end <= start:
            raise ValueError("chunk_size is too small to fit the next character")

        chunk_text = text[start:end].strip()
        if chunk_text:
            content_start = start + len(text[start:end]) - len(text[start:end].lstrip())
            chunks.append((content_start, end, chunk_text))
        if end >= len(text):
            break

        next_start = _overlap_start(text, start, end, overlap, encoding)
        if next_start <= start:
            next_start = end
        start = next_start

    return chunks


def chunk_text(
    documents: list[ExtractedText],
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[TextChunk]:
    """Split extracted sections into token-sized chunks with section/page metadata."""
    size = settings.CHUNK_SIZE if chunk_size is None else chunk_size
    gap = settings.CHUNK_OVERLAP if overlap is None else overlap
    if size <= 0:
        raise ValueError("chunk_size must be positive")
    if gap < 0:
        raise ValueError("overlap must not be negative")
    if gap >= size:
        raise ValueError("overlap must be smaller than chunk_size")
    if not documents:
        return []

    encoding = tiktoken.get_encoding("cl100k_base")
    chunks: list[TextChunk] = []
    position = 0
    section: str | None = None
    section_parts: list[str] = []
    page_offsets: list[tuple[int, int]] = []
    section_length = 0

    def append_section() -> None:
        nonlocal position, section_length
        if section is None or not section_parts:
            return
        section_text = "\n\n".join(section_parts)
        for start, _, piece in _split_text(section_text, size, gap, encoding):
            page = next(
                page_number
                for page_start, page_number in reversed(page_offsets)
                if page_start <= start
            )
            chunks.append(
                {
                    "text": piece,
                    "section": section,
                    "page": page,
                    "position": position,
                }
            )
            position += 1
        section_parts.clear()
        page_offsets.clear()
        section_length = 0

    for document in documents:
        document_section = document["section"]
        if section is not None and document_section != section:
            append_section()
        section = document_section
        text = document["text"]
        if not text.strip():
            continue
        if section_parts:
            section_length += len("\n\n")
        page_offsets.append((section_length, document["page"]))
        section_parts.append(text)
        section_length += len(text)

    append_section()
    return chunks
