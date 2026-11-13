from backend.app.services.chunk_context import (
    build_chunk_metadata,
    build_context_summary,
    build_embedding_text,
)


def test_context_summary_includes_parent_heading() -> None:
    assert build_context_summary("Background", "Chapter 1") == (
        "Chapter 1 > Background"
    )


def test_context_summary_without_parent_heading() -> None:
    assert build_context_summary("Background") == "Background"


def test_context_summary_handles_missing_section() -> None:
    assert build_context_summary(None) == ""
    assert build_context_summary("") == ""
    assert build_context_summary(None, "Chapter 1") == "Chapter 1"


def test_chunk_metadata_defaults_missing_section_and_page() -> None:
    metadata = build_chunk_metadata(
        {"text": "content", "position": 3},
        document_id="doc-1",
        document_name="report.pdf",
    )

    assert metadata == {
        "document_id": "doc-1",
        "document_name": "report.pdf",
        "section": "",
        "page": 0,
        "position": 3,
        "context_summary": "",
    }
    assert all(value is not None for value in metadata.values())


def test_chunk_metadata_has_no_none_values() -> None:
    metadata = build_chunk_metadata(
        {"text": "content", "section": None, "page": None, "position": None},
        document_id="doc-2",
        document_name="notes.txt",
        parent_heading=None,
    )

    assert all(value is not None for value in metadata.values())
    assert metadata["section"] == ""
    assert metadata["page"] == 0
    assert metadata["position"] == 0


def test_embedding_text_starts_with_document_and_section() -> None:
    result = build_embedding_text(
        "Important body text.",
        document_name="report.pdf",
        section="Results",
    )

    assert result.startswith("Document: report.pdf | Section: Results\n")


def test_embedding_text_keeps_original_chunk_text_unchanged() -> None:
    original = " First  line.\nSecond line! "
    result = build_embedding_text(original, "report.pdf", "Summary")

    assert result.endswith(original)
