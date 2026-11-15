"""Answer confidence (FR-046 / AL-296).

Similarity = 1 - cosine distance of a passage to the question. An answer is flagged uncertain
when its best supporting passage is only a weak match, or when it rests on a single passage that
is not a strong match. The reason is shown to the user next to the "Uncertain" badge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from backend.app.core.config import settings

LOW_RELEVANCE = "Low relevance: the closest passages only partly match the question."
LIMITED_SOURCES = "Limited source support: the answer rests on a single passage."


@dataclass(frozen=True)
class Confidence:
    score: float
    uncertain: bool
    reason: str | None


def similarity(distance: float) -> float:
    return max(0.0, min(1.0, 1.0 - distance))


def assess(supporting_distances: Sequence[float]) -> Confidence:
    if not supporting_distances:
        return Confidence(0.0, True, LOW_RELEVANCE)
    sims = sorted((similarity(d) for d in supporting_distances), reverse=True)
    best = sims[0]
    # Best match dominates; extra supporting passages add a little confidence.
    score = round(min(1.0, best + 0.05 * (len(sims) - 1)), 3)
    weak = 1.0 - settings.QA_UNCERTAIN_MAX_DISTANCE
    if best < weak:
        return Confidence(score, True, LOW_RELEVANCE)
    if len(sims) < settings.QA_UNCERTAIN_MIN_SOURCES and best < weak + 0.1:
        return Confidence(score, True, LIMITED_SOURCES)
    return Confidence(score, False, None)
