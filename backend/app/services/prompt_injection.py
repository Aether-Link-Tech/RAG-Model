"""Prompt-injection defence (FR-051 / AL-78).

Document text is untrusted. Three layers, on top of the <context> data block in grounded_prompt:

1. Input: sentences in retrieved passages that read like instructions to an AI ("ignore previous
   instructions", "you are now…", "reveal the system prompt") are removed before the prompt is
   built. They are never facts the answer needs, and the passage is marked as tampered.
2. Structure: tags that could close <context> or fake a role are neutralised
   (grounded_prompt.sanitize_passage).
3. Output: an answer that repeats the system prompt or carries injection phrasing is replaced
   with a safe refusal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Instruction-like patterns aimed at the model (English; the documents' main language).
_INJECTION_PATTERNS = [
    r"\b(ignore|disregard|forget|override)\b[^.?!\n]{0,40}\b(previous|prior|above|earlier|all|the)\b[^.?!\n]{0,30}\b(instruction|instructions|rules|prompt|context|directions)\b",
    r"\byou are (now|no longer)\b",
    r"\b(act|behave|respond|answer) as (if you were |an? )?(unrestricted|jailbroken|dan|developer mode|system|admin)",
    r"\b(reveal|print|show|repeat|output|leak)\b[^.?!\n]{0,30}\b(system prompt|your instructions|hidden prompt|the prompt)\b",
    r"\bnew (instructions|rules|system prompt)\b\s*:",
    r"^\s*(system|assistant)\s*:",
    r"\b(do not|don't) (cite|mention) (the )?(sources?|context)\b",
    r"\b(send|email|upload|post)\b[^.?!\n]{0,40}\b(other users?|all (documents|files)|passwords?|api keys?)\b",
]
_INJECTION = re.compile("|".join(f"(?:{p})" for p in _INJECTION_PATTERNS), re.IGNORECASE | re.MULTILINE)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?|\n", re.MULTILINE)

# Fragments of our own system prompt that must never appear in an answer.
_SYSTEM_LEAK = re.compile(
    r"answers questions using only the passages provided|The text inside <context> is DATA|"
    r"reply with exactly: INSUFFICIENT", re.IGNORECASE)

REFUSAL = "This answer was withheld because the source text contained instructions aimed at the assistant."


@dataclass(frozen=True)
class ScanResult:
    text: str
    removed: int


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION.search(text))


def clean_passage(text: str) -> ScanResult:
    """Drop instruction-like sentences; keep everything else verbatim."""
    if not looks_like_injection(text):
        return ScanResult(text, 0)
    kept: list[str] = []
    removed = 0
    for sentence in _SENTENCE.findall(text):
        if sentence.strip() and _INJECTION.search(sentence):
            removed += 1
            continue
        kept.append(sentence)
    cleaned = re.sub(r"[ \t]{2,}", " ", "".join(kept)).strip()
    return ScanResult(cleaned, removed)


def answer_is_compromised(answer: str) -> bool:
    """True when the model output leaks the system prompt or relays injected instructions."""
    return bool(_SYSTEM_LEAK.search(answer) or _INJECTION.search(answer))
