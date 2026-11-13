"""Isolated unit tests for centralized per-user storage path hardening (AL-253)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from backend.app.core.config import settings
from backend.app.services import document_ingest
from backend.app.services.storage_paths import (
    UnsafeStoragePathError,
    build_staging_path,
    build_storage_path,
    create_exclusive_staging_file,
    open_user_file_no_follow,
    resolve_user_file_path,
    user_storage_dir,
    validate_safe_filename,
)


def test_document_ingest_reexports_storage_helpers():
    assert document_ingest.UnsafeStoragePathError is UnsafeStoragePathError
    assert document_ingest.user_storage_dir is user_storage_dir
    assert document_ingest.build_storage_path is build_storage_path


def test_user_storage_dir_layout_and_create_flag(isolate_storage):
    user_id = uuid.uuid4()
    directory = user_storage_dir(user_id, create=False)
    assert directory == Path(settings.UPLOAD_DIR) / "users" / str(user_id) / "documents"
    assert not directory.exists()

    created = user_storage_dir(user_id, create=True)
    assert created == directory
    assert created.is_dir()


@pytest.mark.parametrize("bad_id", ["../other", "..", "not-a-uuid", "x/../../y"])
def test_user_storage_dir_rejects_non_uuid(isolate_storage, bad_id):
    with pytest.raises(ValueError):
        user_storage_dir(bad_id)


@pytest.mark.parametrize(
    "bad_filename",
    [
        None,
        "",
        "   ",
        ".",
        "..",
        "../etc/passwd",
        "..\\..\\evil.txt",
        "a.pdf/../../x",
        "/abs/path.txt",
        "C:\\temp\\x.txt",
        "\\\\server\\share\\x.txt",
        "evil\x00.pdf",
        "bad\nname.txt",
        "bad\x7fname.txt",
        "%00evil.pdf",
        "evil.pdf%00.txt",
        "%2e%2e%2fx.txt",
        "..%2f..%2fetc%2fpasswd.txt",
        "%2e%2e%5cx.txt",
        "%252e%252e%252fsecret.txt",
        "%2e%2e",
        "a" * 256 + ".txt",
    ],
)
def test_validate_safe_filename_rejects_unsafe_inputs(bad_filename):
    with pytest.raises(UnsafeStoragePathError):
        validate_safe_filename(bad_filename)


@pytest.mark.parametrize(
    "good_filename",
    [
        "report.pdf",
        "100% summary.pdf",
        "Q1 50%_growth.txt",
        "Annual.Report.2026.pdf",
        "report%20final.pdf",
        "50%off.csv",
    ],
)
def test_validate_safe_filename_allows_legitimate_percent_names(good_filename):
    assert validate_safe_filename(good_filename) == good_filename


def test_build_storage_path_canonical_and_variant(isolate_storage):
    user_id, doc_id = uuid.uuid4(), uuid.uuid4()
    path = Path(build_storage_path(user_id, doc_id, "100% Report.PDF"))
    assert path == Path(settings.UPLOAD_DIR) / "users" / str(user_id) / "documents" / f"{doc_id}.pdf"
    assert path.parent.is_dir()

    variant_hex = "a1b2c3d4e5f60718"
    variant_path = Path(
        build_storage_path(user_id, doc_id, "scan.png", variant=variant_hex, create_dir=False)
    )
    assert variant_path.name == f"{doc_id}_{variant_hex}.png"


@pytest.mark.parametrize("bad_variant", ["../x", "SHORT", "zzzzzzzzzzzzzzzz", "a" * 65])
def test_build_storage_path_rejects_invalid_variant(isolate_storage, bad_variant):
    with pytest.raises(UnsafeStoragePathError):
        build_storage_path(uuid.uuid4(), uuid.uuid4(), "doc.pdf", variant=bad_variant)


@pytest.mark.parametrize("bad_ext_name", ["file.pdf!", "file.toolongext123", "file.a-b"])
def test_build_storage_path_rejects_invalid_extension(isolate_storage, bad_ext_name):
    with pytest.raises(UnsafeStoragePathError):
        build_storage_path(uuid.uuid4(), uuid.uuid4(), bad_ext_name)


@pytest.mark.parametrize("purpose", ["upload", "replace", "publish", "delete"])
def test_build_staging_path_stays_inside_owner_namespace(isolate_storage, purpose):
    user_id, doc_id = uuid.uuid4(), uuid.uuid4()
    staging = build_staging_path(user_id, doc_id, "100% summary.pdf", purpose=purpose)
    owner_dir = user_storage_dir(user_id).resolve()
    assert staging.resolve().parent == owner_dir
    if purpose == "delete":
        assert staging.name.endswith(".deleting")
    else:
        assert staging.name.startswith(f".{purpose}.{doc_id}.")
        assert staging.name.endswith(".pdf.tmp")


def test_build_staging_path_delete_rejects_foreign_existing_path(isolate_storage):
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    doc_b = uuid.uuid4()
    foreign_path = build_storage_path(user_b, doc_b, "secret.pdf")
    with pytest.raises(UnsafeStoragePathError):
        build_staging_path(user_a, doc_b, purpose="delete", existing_path=foreign_path)


def test_build_staging_path_rejects_invalid_purpose(isolate_storage):
    with pytest.raises(UnsafeStoragePathError):
        build_staging_path(uuid.uuid4(), uuid.uuid4(), "a.pdf", purpose="invalid")  # type: ignore[arg-type]


def test_resolve_user_file_path_accepts_owner_file_and_fails_closed(isolate_storage):
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()

    path_a = Path(build_storage_path(user_a, doc_a, "notes.txt"))
    path_a.write_text("alpha", encoding="utf-8")

    path_b = Path(build_storage_path(user_b, doc_b, "secret.txt"))
    path_b.write_text("beta", encoding="utf-8")

    resolved_a = resolve_user_file_path(user_a, path_a, must_exist=True)
    assert resolved_a == path_a.resolve()

    # Cross-user access must fail closed.
    with pytest.raises(UnsafeStoragePathError):
        resolve_user_file_path(user_a, path_b, must_exist=True)

    # Outside UPLOAD_DIR must fail closed.
    with pytest.raises(UnsafeStoragePathError):
        resolve_user_file_path(user_a, "/etc/passwd", must_exist=False)

    # Directory itself must fail closed.
    with pytest.raises(UnsafeStoragePathError):
        resolve_user_file_path(user_a, user_storage_dir(user_a), must_exist=False)

    # Missing/empty/NUL must fail closed.
    for bad in (None, "", "   ", f"{path_a}\x00.txt"):
        with pytest.raises(UnsafeStoragePathError):
            resolve_user_file_path(user_a, bad, must_exist=False)

    # Non-existent file inside owner namespace raises FileNotFoundError when must_exist=True.
    missing = user_storage_dir(user_a) / f"{uuid.uuid4()}.txt"
    with pytest.raises(FileNotFoundError):
        resolve_user_file_path(user_a, missing, must_exist=True)


def test_symlink_escapes_are_rejected(isolate_storage):
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    doc_b = uuid.uuid4()

    dir_a = user_storage_dir(user_a, create=True)
    path_b = Path(build_storage_path(user_b, doc_b, "secret.txt"))
    path_b.write_text("secret", encoding="utf-8")

    # Symlink file inside User A's folder pointing to User B's file.
    symlink_file = dir_a / f"{uuid.uuid4()}.txt"
    os.symlink(path_b, symlink_file)

    with pytest.raises(UnsafeStoragePathError):
        resolve_user_file_path(user_a, symlink_file, must_exist=True)

    with pytest.raises(UnsafeStoragePathError):
        open_user_file_no_follow(user_a, symlink_file)

    # Symlinked documents directory pointing to User B's directory.
    user_c = uuid.uuid4()
    c_parent = Path(settings.UPLOAD_DIR) / "users" / str(user_c)
    c_parent.mkdir(parents=True, exist_ok=True)
    os.symlink(user_storage_dir(user_b, create=True), c_parent / "documents")

    with pytest.raises(UnsafeStoragePathError):
        user_storage_dir(user_c)


def test_race_resistant_helpers_open_and_create(isolate_storage):
    user_id, doc_id = uuid.uuid4(), uuid.uuid4()
    staging_path, handle = create_exclusive_staging_file(user_id, doc_id, "data.txt", purpose="upload")
    try:
        handle.write(b"hello world")
    finally:
        handle.close()

    assert staging_path.is_file()
    with open_user_file_no_follow(user_id, staging_path) as reader:
        assert reader.read() == b"hello world"
