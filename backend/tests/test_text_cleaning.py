from backend.app.services.text_cleaning import clean_text


def test_clean_text_collapses_multiple_spaces() -> None:
    assert clean_text("Words    with\t\tspaces") == "Words with spaces"


def test_clean_text_collapses_excess_newlines() -> None:
    assert clean_text("First\n\n\n\nSecond") == "First\n\nSecond"


def test_clean_text_preserves_paragraph_breaks() -> None:
    assert clean_text("First paragraph.\n\nSecond paragraph.") == (
        "First paragraph.\n\nSecond paragraph."
    )


def test_clean_text_strips_trailing_spaces_per_line_and_normalizes_nfc() -> None:
    assert clean_text("Cafe\u0301  \nNext line  ") == "Café\nNext line"


def test_clean_text_removes_broken_and_control_characters() -> None:
    assert clean_text("Before\ufffd\x00middle\x07after") == "Beforemiddleafter"


def test_clean_text_decodes_utf8_bytes() -> None:
    assert clean_text("Résumé — café".encode("utf-8")) == "Résumé — café"


def test_clean_text_detects_fallback_byte_encoding() -> None:
    source = "Résumé déjà vu — café. Quelques mots pour détecter l’encodage."
    assert clean_text(source.encode("cp1252")) == source


def test_clean_text_preserves_accented_and_arabic_text() -> None:
    source = "Crème brûlée — مرحبًا بالعالم"
    assert clean_text(source) == source


def test_clean_text_accepts_empty_string() -> None:
    assert clean_text("") == ""
