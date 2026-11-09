"""Near-duplicate detection (FR-081 / AL-279, AL-280, AL-281).

After text extraction every document gets a MinHash signature (128 permutations over word
3-gram shingles). A new document whose estimated Jaccard similarity with an existing one is
>= 0.5 is rejected with a message naming the existing document.

Speed (AL-281): the signature is cut into 42 LSH bands of 3 values. Only documents sharing
at least one (band, hash) pair are compared exactly, found with one indexed SQL lookup.
With b=42, r=3 a pair at similarity 0.5 becomes a candidate with probability 0.996.

Privacy (AL-280): fingerprints are read through the caller's database session, where RLS
exposes only the caller's own fingerprints and those of public documents. Another user's
private document is never a candidate, so its name can never be revealed.

Accuracy: a 128-permutation MinHash estimates Jaccard similarity to about ±0.04 (one
standard deviation), so documents within a few percent of the 50 % threshold may fall on
either side of it.
"""
from __future__ import annotations

import hashlib
import re
import struct
import uuid
from dataclasses import dataclass
from typing import Optional

import numpy as np
from datasketch import MinHash
from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.document import Document
from backend.app.models.fingerprint import DocumentFingerprint, DocumentFingerprintBand

NUM_PERM = 128
SHINGLE_SIZE = 3
BANDS, ROWS = 42, 3  # 126 of the 128 values are used for LSH
THRESHOLD = 0.5
SEED = 1  # fixed so signatures are comparable across processes and releases

_WORD = re.compile(r"\w+", re.UNICODE)


class NearDuplicateError(ValueError):
    """Raised when a new document is >= THRESHOLD similar to an existing one."""

    def __init__(self, match: "NearDuplicateMatch"):
        super().__init__(match.message)
        self.match = match


@dataclass(frozen=True)
class NearDuplicateMatch:
    document_id: uuid.UUID
    filename: str
    kind: str  # "own" (caller's document) or "public" (another user's public document)
    similarity: float

    @property
    def message(self) -> str:
        percent = int(round(self.similarity * 100))
        if self.kind == "own":
            return (
                f"You already uploaded a very similar document ({percent}% similar): “{self.filename}”. "
                "Use that document, or replace it with this version."
            )
        return (
            f"A public document with very similar content already exists ({percent}% similar): "
            f"“{self.filename}”. You can use that document."
        )


def shingles(content: str, k: int = SHINGLE_SIZE) -> set[str]:
    words = [w.lower() for w in _WORD.findall(content)]
    if len(words) < k:
        return set(words)
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def signature(content: str) -> np.ndarray:
    m = MinHash(num_perm=NUM_PERM, seed=SEED)
    for s in shingles(content):
        m.update(s.encode("utf-8"))
    return np.asarray(m.hashvalues, dtype=np.uint64)


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Estimated Jaccard similarity of the two shingle sets."""
    return float(np.count_nonzero(a == b)) / float(len(a))


def is_near_duplicate(score: float) -> bool:
    return score >= THRESHOLD


def band_hashes(sig: np.ndarray) -> list[tuple[int, int]]:
    """(band, signed 64-bit hash) pairs for the LSH index."""
    out = []
    for band in range(BANDS):
        chunk = sig[band * ROWS:(band + 1) * ROWS].astype("<u8").tobytes()
        digest = hashlib.blake2b(chunk, digest_size=8).digest()
        out.append((band, struct.unpack("<q", digest)[0]))
    return out


def to_bytes(sig: np.ndarray) -> bytes:
    return sig.astype("<u8").tobytes()


def from_bytes(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype="<u8").astype(np.uint64)


async def find_near_duplicate(db: AsyncSession, document: Document, sig: np.ndarray) -> Optional[NearDuplicateMatch]:
    """Best match >= THRESHOLD among documents the caller may see (own + public), or None."""
    pairs = band_hashes(sig)
    candidates_sql = text(
        "SELECT DISTINCT document_id FROM document_fingerprint_bands "
        "WHERE (band, hash) IN :pairs AND document_id <> :self"
    ).bindparams(bindparam("pairs", expanding=True))
    candidate_ids = (await db.execute(candidates_sql, {"pairs": pairs, "self": document.id})).scalars().all()
    if not candidate_ids:
        return None

    rows = await db.execute(
        select(DocumentFingerprint.document_id, DocumentFingerprint.signature, Document.filename,
               Document.user_id, Document.visibility)
        .join(Document, Document.id == DocumentFingerprint.document_id)
        .where(DocumentFingerprint.document_id.in_(candidate_ids))
    )
    best: Optional[NearDuplicateMatch] = None
    for doc_id, raw, filename, owner, visibility in rows:
        score = similarity(sig, from_bytes(raw))
        if not is_near_duplicate(score):
            continue
        if owner == document.user_id:
            kind = "own"
        elif visibility == "public":
            kind = "public"
        else:  # unreachable under RLS; never reveal another user's private document
            continue
        if best is None or score > best.similarity:
            best = NearDuplicateMatch(doc_id, filename, kind, score)
    return best


async def store_fingerprint(db: AsyncSession, document: Document, sig: np.ndarray) -> None:
    """Replace the document's fingerprint and LSH bands (call inside the indexing transaction)."""
    await db.execute(delete(DocumentFingerprintBand).where(DocumentFingerprintBand.document_id == document.id))
    await db.execute(delete(DocumentFingerprint).where(DocumentFingerprint.document_id == document.id))
    db.add(DocumentFingerprint(document_id=document.id, user_id=document.user_id, visibility=document.visibility,
                               num_perm=NUM_PERM, signature=to_bytes(sig)))
    db.add_all(
        DocumentFingerprintBand(document_id=document.id, band=band, hash=h, user_id=document.user_id,
                                visibility=document.visibility)
        for band, h in band_hashes(sig)
    )
