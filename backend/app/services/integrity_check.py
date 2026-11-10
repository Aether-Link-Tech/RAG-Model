"""Cross-layer integrity check: PostgreSQL vs ChromaDB vs file storage (FR-075 / AL-270).

Isolation is enforced in three places that can drift apart: document rows in PostgreSQL,
vectors in ChromaDB (one public collection + one private collection per user) and files
on disk (one folder per user). This module compares them and reports anything that could
expose data across users or break retrieval:

  critical  private_chunk_in_public   a private document's chunk sits in the public collection
  critical  owner_mismatch            a chunk sits in another user's private collection
  critical  file_outside_owner_folder a document's file is not inside its owner's folder
  warning   orphan_vector             a chunk whose document no longer exists
  warning   invalid_chunk_metadata    a chunk without a valid document_id / owner / visibility
  warning   collection_owner_mismatch a private collection stamped with a different owner
  warning   unknown_collection        a collection that is neither public nor user_<id>_private
  warning   missing_file              a document row whose file is gone
  warning   missing_vectors           an "indexed" document with no chunks in ChromaDB
  info      orphan_file               a file on disk that no document references

The check is read-only. With repair=True it deletes orphan / misplaced vectors and marks
documents whose file is missing as failed; it never deletes files on disk.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from backend.app.core.config import settings
from backend.app.db import chroma

CRITICAL, WARNING, INFO = "critical", "warning", "info"
SEVERITY = {
    "private_chunk_in_public": CRITICAL,
    "owner_mismatch": CRITICAL,
    "file_outside_owner_folder": CRITICAL,
    "orphan_vector": WARNING,
    "invalid_chunk_metadata": WARNING,
    "collection_owner_mismatch": WARNING,
    "unknown_collection": WARNING,
    "missing_file": WARNING,
    "missing_vectors": WARNING,
    "orphan_file": INFO,
}
# Files that belong to in-flight or staged operations, not to documents.
_TRANSIENT_SUFFIXES = (".tmp", ".deleting")
_PAGE = 500


@dataclass(frozen=True)
class DocumentRecord:
    id: uuid.UUID
    user_id: uuid.UUID
    visibility: str
    status: str
    file_path: str


@dataclass
class Issue:
    kind: str
    detail: str
    document_id: Optional[str] = None
    collection: Optional[str] = None
    chroma_ids: list[str] = field(default_factory=list)
    path: Optional[str] = None

    @property
    def severity(self) -> str:
        return SEVERITY[self.kind]


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    documents_checked: int = 0
    chunks_checked: int = 0
    collections_checked: int = 0
    files_checked: int = 0
    repaired_vectors: int = 0
    documents_marked_failed: int = 0

    def by_kind(self, kind: str) -> list[Issue]:
        return [i for i in self.issues if i.kind == kind]

    @property
    def has_critical(self) -> bool:
        return any(i.severity == CRITICAL for i in self.issues)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = defaultdict(int)
        for issue in self.issues:
            counts[issue.kind] += 1
        return {
            "documents_checked": self.documents_checked,
            "chunks_checked": self.chunks_checked,
            "collections_checked": self.collections_checked,
            "files_checked": self.files_checked,
            "issues": dict(sorted(counts.items())),
            "critical": sum(1 for i in self.issues if i.severity == CRITICAL),
            "repaired_vectors": self.repaired_vectors,
            "documents_marked_failed": self.documents_marked_failed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "issues": [dict(asdict(i), severity=i.severity) for i in self.issues],
        }


def _uuid_or_none(value: Any) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _all_chunks(collection: Any) -> Iterable[tuple[str, dict[str, Any]]]:
    offset = 0
    while True:
        page = collection.get(include=["metadatas"], limit=_PAGE, offset=offset)
        ids = page.get("ids") or []
        if not ids:
            return
        for chroma_id, metadata in zip(ids, page.get("metadatas") or [{}] * len(ids)):
            yield chroma_id, dict(metadata or {})
        if len(ids) < _PAGE:
            return
        offset += _PAGE


def _classify_collection(collection: Any) -> tuple[str, Optional[str]]:
    """Return ("public", None), ("private", owner_hex) or ("unknown", None)."""
    name = collection.name
    if name == settings.CHROMA_PUBLIC_COLLECTION:
        return "public", None
    prefix, _, suffix = settings.CHROMA_PRIVATE_COLLECTION_TEMPLATE.partition("{user_id}")
    if name.startswith(prefix) and name.endswith(suffix):
        owner = name[len(prefix):len(name) - len(suffix)]
        if _uuid_or_none(owner) is not None:
            return "private", uuid.UUID(owner).hex
    return "unknown", None


def _check_vectors(report: Report, docs: dict[uuid.UUID, DocumentRecord], client: Any,
                   bad_vectors: dict[str, list[str]]) -> dict[uuid.UUID, int]:
    chunk_counts: dict[uuid.UUID, int] = defaultdict(int)
    for listed in client.list_collections():
        collection = listed if hasattr(listed, "get") else client.get_collection(str(listed))
        report.collections_checked += 1
        kind, owner_hex = _classify_collection(collection)
        name = collection.name
        if kind == "unknown":
            report.issues.append(Issue("unknown_collection", f"Collection {name!r} is not public or user-private",
                                       collection=name))
            continue
        if kind == "private":
            stamped = (collection.metadata or {}).get("user_id")
            if stamped != owner_hex or (collection.metadata or {}).get("visibility") != "private":
                report.issues.append(Issue("collection_owner_mismatch",
                                           f"Collection {name!r} is stamped for owner {stamped!r}", collection=name))

        for chroma_id, meta in _all_chunks(collection):
            report.chunks_checked += 1
            doc_id = _uuid_or_none(meta.get("document_id"))
            meta_owner, meta_vis = meta.get("user_id"), meta.get("visibility")
            if doc_id is None or meta_vis not in ("private", "public") or not meta_owner:
                report.issues.append(Issue("invalid_chunk_metadata", "Chunk has no valid document_id, owner or visibility",
                                           collection=name, chroma_ids=[chroma_id]))
                bad_vectors[name].append(chroma_id)
                continue
            doc = docs.get(doc_id)
            if doc is None:
                report.issues.append(Issue("orphan_vector", "Chunk belongs to a document that no longer exists",
                                           document_id=str(doc_id), collection=name, chroma_ids=[chroma_id]))
                bad_vectors[name].append(chroma_id)
                continue
            if kind == "public":
                if doc.visibility != "public":
                    report.issues.append(Issue("private_chunk_in_public",
                                               "A private document's chunk is in the public collection",
                                               document_id=str(doc_id), collection=name, chroma_ids=[chroma_id]))
                    bad_vectors[name].append(chroma_id)
                    continue
            else:
                if doc.user_id.hex != owner_hex or doc.visibility != "private" or meta_owner != owner_hex:
                    report.issues.append(Issue("owner_mismatch",
                                               "Chunk is in a private collection that does not belong to its document's owner",
                                               document_id=str(doc_id), collection=name, chroma_ids=[chroma_id]))
                    bad_vectors[name].append(chroma_id)
                    continue
            if meta_owner != doc.user_id.hex or meta_vis != doc.visibility:
                report.issues.append(Issue("invalid_chunk_metadata", "Chunk owner/visibility metadata differs from the database",
                                           document_id=str(doc_id), collection=name, chroma_ids=[chroma_id]))
                bad_vectors[name].append(chroma_id)
                continue
            chunk_counts[doc_id] += 1
    return chunk_counts


def _is_document_image(path: Path, root: Path, docs: dict[uuid.UUID, DocumentRecord]) -> bool:
    """Extracted images (FR-058) live in <owner folder>/images/<document_id>/ of an existing document."""
    doc_id = _uuid_or_none(path.parent.name)
    doc = docs.get(doc_id) if doc_id else None
    if doc is None:
        return False
    expected = (owner_folders(root, doc.user_id)[0] / "images" / str(doc.id)).resolve()
    return path.parent == expected


def owner_folders(root: Path, user_id: uuid.UUID) -> tuple[Path, ...]:
    """Folders a user's files may live in: the current layout <root>/users/<id>/documents
    (AL-139) and the legacy <root>/<id> used before it."""
    return (root / "users" / str(user_id) / "documents", root / str(user_id))


def _check_files(report: Report, docs: dict[uuid.UUID, DocumentRecord], upload_dir: Path) -> None:
    root = upload_dir.resolve()
    referenced: set[Path] = set()
    for doc in docs.values():
        path = Path(doc.file_path).resolve()
        referenced.add(path)
        if not any(path.is_relative_to(folder) for folder in owner_folders(root, doc.user_id)):
            report.issues.append(Issue("file_outside_owner_folder", "Document file is not inside its owner's folder",
                                       document_id=str(doc.id), path=str(path)))
        if not path.is_file():
            report.issues.append(Issue("missing_file", "Document file is missing from storage",
                                       document_id=str(doc.id), path=str(path)))
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        report.files_checked += 1
        if path.name.startswith(".") or path.name.endswith(_TRANSIENT_SUFFIXES):
            continue
        if path.resolve() not in referenced and not _is_document_image(path.resolve(), root, docs):
            report.issues.append(Issue("orphan_file", "File on disk is not referenced by any document",
                                       path=str(path.resolve())))


def check_integrity(
    documents: Iterable[DocumentRecord],
    *,
    chroma_client: Any = None,
    upload_dir: str | Path | None = None,
    repair: bool = False,
    mark_failed: Optional[Callable[[uuid.UUID, str], None]] = None,
) -> Report:
    """Compare documents (all users) with ChromaDB and storage. See module docstring."""
    docs = {d.id: d for d in documents}
    report = Report(documents_checked=len(docs))
    client = chroma_client if chroma_client is not None else chroma.get_chroma_client()
    bad_vectors: dict[str, list[str]] = defaultdict(list)

    chunk_counts = _check_vectors(report, docs, client, bad_vectors)
    for doc in docs.values():
        if doc.status == "indexed" and chunk_counts.get(doc.id, 0) == 0:
            report.issues.append(Issue("missing_vectors", "Document is marked indexed but has no chunks",
                                       document_id=str(doc.id)))
    _check_files(report, docs, Path(upload_dir or settings.UPLOAD_DIR))

    if repair:
        for name, ids in bad_vectors.items():
            if ids:
                client.get_collection(name).delete(ids=ids)
                report.repaired_vectors += len(ids)
        if mark_failed is not None:
            for issue in report.by_kind("missing_file"):
                mark_failed(uuid.UUID(issue.document_id), "File missing from storage (integrity check). Upload it again.")
                report.documents_marked_failed += 1
    return report
