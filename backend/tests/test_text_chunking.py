from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import tiktoken

from backend.app.core.config import settings
from backend.app.services.text_chunking import ExtractedText, chunk_text


def _document(text: str, *, page: int = 1, section: str = "Overview") -> ExtractedText:
    return {"page": page, "section": section, "text": text}


def test_default_chunk_sizes() -> None:
    assert settings.CHUNK_SIZE == 800
    assert settings.CHUNK_OVERLAP == 100


def test_custom_chunk_sizes_are_read_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("CHUNK_SIZE", "12")
    monkeypatch.setenv("CHUNK_OVERLAP", "3")
    text = (
        "First sentence. Second sentence. Third sentence. Fourth sentence. "
        "Fifth sentence. Sixth sentence."
    )
    code = (
        "import json, sys; "
        "from backend.app.services.text_chunking import chunk_text; "
        "print(json.dumps(chunk_text(json.loads(sys.argv[1]))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, json.dumps([_document(text)])],
        capture_output=True,
        check=True,
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ.copy(),
        text=True,
    )
    chunks = json.loads(result.stdout)
    encoding = tiktoken.get_encoding("cl100k_base")

    assert len(chunks) > 1
    assert all(
        len(encoding.encode(chunk["text"])) <= 12
        for chunk in chunks
    )


def test_chunk_metadata_and_positions_are_preserved() -> None:
    chunks = chunk_text(
        [
            _document("Page two content.", page=2, section="Introduction"),
            _document("Page five content.", page=5, section="Conclusion"),
        ],
        chunk_size=20,
        overlap=2,
    )

    assert chunks == [
        {
            "text": "Page two content.",
            "section": "Introduction",
            "page": 2,
            "position": 0,
        },
        {
            "text": "Page five content.",
            "section": "Conclusion",
            "page": 5,
            "position": 1,
        },
    ]


def test_same_section_can_span_pages_and_chunk_page_tracks_start() -> None:
    chunks = chunk_text(
        [
            _document(
                " ".join(f"First page sentence {i}." for i in range(12)),
                page=1,
            ),
            _document(
                " ".join(f"Second page sentence {i}." for i in range(12)),
                page=2,
            ),
        ],
        chunk_size=18,
        overlap=2,
    )

    assert chunks[0]["page"] == 1
    assert any(chunk["page"] == 2 for chunk in chunks)
    assert all(chunk["section"] == "Overview" for chunk in chunks)


def test_overlap_is_present_between_chunks() -> None:
    text = " ".join(
        f"Sentence number {number} has several descriptive words."
        for number in range(12)
    )
    chunks = chunk_text([_document(text)], chunk_size=20, overlap=6)

    assert len(chunks) > 1
    assert any(
        set(previous["text"].split()) & set(following["text"].split())
        for previous, following in zip(chunks, chunks[1:])
    )


def test_normal_text_splits_at_sentence_boundaries() -> None:
    chunks = chunk_text(
        [_document("First sentence ends. Second sentence ends. Third sentence ends.")],
        chunk_size=6,
        overlap=0,
    )

    assert len(chunks) > 1
    assert all(
        chunk["text"].endswith((".", "!", "?"))
        for chunk in chunks[:-1]
    )


def test_sections_are_never_mixed() -> None:
    chunks = chunk_text(
        [
            _document(
                " ".join(f"Alpha sentence {i}." for i in range(10)),
                section="Alpha",
            ),
            _document(
                " ".join(f"Beta sentence {i}." for i in range(10)),
                section="Beta",
            ),
        ],
        chunk_size=10,
        overlap=2,
    )

    assert {chunk["section"] for chunk in chunks if "Alpha" in chunk["text"]} == {
        "Alpha"
    }
    assert {chunk["section"] for chunk in chunks if "Beta" in chunk["text"]} == {
        "Beta"
    }


def test_empty_input_returns_no_chunks() -> None:
    assert chunk_text([]) == []


def test_overlap_must_be_smaller_than_chunk_size() -> None:
    with pytest.raises(ValueError, match="overlap must be smaller than chunk_size"):
        chunk_text([_document("Some text.")], chunk_size=5, overlap=5)
