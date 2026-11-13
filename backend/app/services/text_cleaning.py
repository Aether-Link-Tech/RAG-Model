"""Normalize and clean extracted document text."""
from __future__ import annotations

import re
import unicodedata


_REPEATED_SPACES = re.compile(r" {2,}")
_EXCESS_NEWLINES = re.compile(r"\n{3,}")


def clean_text(text: str | bytes) -> str:
    """Clean extracted text while preserving words, punctuation, and paragraphs."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            import chardet

            encoding = chardet.detect(text).get("encoding")
            if not encoding:
                raise ValueError("Unable to detect text byte encoding.")
            try:
                text = text.decode(encoding)
            except (LookupError, UnicodeDecodeError) as exc:
                raise ValueError("Unable to decode text bytes.") from exc
    elif not isinstance(text, str):
        raise TypeError("text must be str or bytes")

    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = "".join(
        character
        for character in text
        if character != "\ufffd"
        and (
            character == "\n"
            or unicodedata.category(character) not in {"Cc", "Cs"}
        )
    )
    text = _REPEATED_SPACES.sub(" ", text)
    text = "\n".join(line.rstrip(" ") for line in text.split("\n"))
    text = _EXCESS_NEWLINES.sub("\n\n", text)
    text = text.strip()
    text.encode("utf-8")
    return text
