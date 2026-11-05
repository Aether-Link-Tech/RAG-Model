import hashlib
import uuid
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.rls import clear_rls_context, configure_rls_context
from backend.app.models.document import Document
from backend.app.models.user import User
from backend.tests.conftest import TestingSessionLocal, purge_test_users


def _extract_sqlstate(exc: DBAPIError) -> str | None:
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


async def _create_doc(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    user_role: str = "user",
    *,
    visibility: str = "private",
    status: str = "indexed",
    file_hash: str | None = None,
    filename: str = "sample.txt",
) -> Document:
    await configure_rls_context(db_session, user_id, user_role)
    doc_id = uuid.uuid4()
    doc = Document(
        id=doc_id,
        user_id=user_id,
        filename=filename,
        file_path=f"/tmp/private/{user_id}/{doc_id}_{filename}",
        mime_type="text/plain",
        file_size=128,
        file_hash=file_hash or hashlib.sha256(f"content-{doc_id}".encode()).hexdigest(),
        status=status,
        visibility=visibility,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)
    return doc


@pytest.mark.asyncio
async def test_owner_publishes_indexed_private_document(
    test_user: User, db_session: AsyncSession
):
    """1. Owner publishes an indexed private document -> returns a row with
    visibility='public', new id, new file_path, same user_id, and the original
    private row is gone.
    """
    orig_doc = await _create_doc(
        db_session, test_user.id, visibility="private", status="indexed"
    )
    old_id = orig_doc.id
    new_id = uuid.uuid4()
    new_path = f"/tmp/public/{new_id}_sample.txt"

    await configure_rls_context(db_session, test_user.id, "user")
    row = (
        await db_session.execute(
            text(
                "SELECT * FROM publish_document(:doc_id, :new_id, :new_path)"
            ),
            {"doc_id": old_id, "new_id": new_id, "new_path": new_path},
        )
    ).mappings().one()
    await db_session.commit()

    assert row["id"] == new_id
    assert row["user_id"] == test_user.id
    assert row["visibility"] == "public"
    assert row["file_path"] == new_path
    assert row["file_hash"] == orig_doc.file_hash
    assert row["status"] == "indexed"

    await configure_rls_context(db_session, test_user.id, "user")
    old_exists = (
        await db_session.execute(select(Document).where(Document.id == old_id))
    ).scalar_one_or_none()
    assert old_exists is None

    new_doc = (
        await db_session.execute(select(Document).where(Document.id == new_id))
    ).scalar_one_or_none()
    assert new_doc is not None
    assert new_doc.visibility == "public"
    assert new_doc.user_id == test_user.id
    assert new_doc.file_path == new_path


@pytest.mark.asyncio
async def test_admin_publishes_another_users_private_document(
    test_user: User, db_session: AsyncSession
):
    """2. Admin (app.user_role='admin') publishes another user's private document ->
    succeeds, owner user_id is preserved.
    """
    admin_id = uuid.uuid4()
    admin = User(
        id=admin_id,
        email=f"admin-pub-{admin_id.hex[:8]}@example.com",
        hashed_password="admin_hashed_password",
        role="admin",
        is_deleted=False,
    )
    db_session.add(admin)
    await db_session.commit()

    try:
        orig_doc = await _create_doc(
            db_session, test_user.id, visibility="private", status="indexed"
        )
        old_id = orig_doc.id
        new_id = uuid.uuid4()
        new_path = f"/tmp/public/{new_id}_admin_published.txt"

        await configure_rls_context(db_session, admin_id, "admin")
        row = (
            await db_session.execute(
                text(
                    "SELECT * FROM publish_document(:doc_id, :new_id, :new_path)"
                ),
                {"doc_id": old_id, "new_id": new_id, "new_path": new_path},
            )
        ).mappings().one()
        await db_session.commit()

        assert row["id"] == new_id
        assert row["user_id"] == test_user.id
        assert row["visibility"] == "public"
        assert row["file_path"] == new_path

        # Confirm original private row is gone for owner as well
        await configure_rls_context(db_session, test_user.id, "user")
        old_exists = (
            await db_session.execute(select(Document).where(Document.id == old_id))
        ).scalar_one_or_none()
        assert old_exists is None
    finally:
        clear_rls_context(db_session)
        await db_session.rollback()
        await purge_test_users(admin_id)


@pytest.mark.asyncio
async def test_non_admin_other_user_cannot_publish_document(
    test_user: User, test_user_2: User, db_session: AsyncSession
):
    """3. Different non-admin user tries to publish someone else's document ->
    raises SQLSTATE 42501, private row remains unchanged.
    """
    u1_id = test_user.id
    u2_id = test_user_2.id
    orig_doc = await _create_doc(
        db_session, u1_id, visibility="private", status="indexed"
    )
    old_id = orig_doc.id
    new_id = uuid.uuid4()

    await configure_rls_context(db_session, u2_id, "user")
    with pytest.raises(DBAPIError, match="not permitted") as exc_info:
        await db_session.execute(
            text(
                "SELECT * FROM publish_document(:doc_id, :new_id, :new_path)"
            ),
            {
                "doc_id": old_id,
                "new_id": new_id,
                "new_path": f"/tmp/public/{new_id}.txt",
            },
        )
    assert _extract_sqlstate(exc_info.value) == "42501"
    await db_session.rollback()

    # Original private row remains intact for owner
    await configure_rls_context(db_session, u1_id, "user")
    still_there = (
        await db_session.execute(select(Document).where(Document.id == old_id))
    ).scalar_one_or_none()
    assert still_there is not None
    assert still_there.visibility == "private"


@pytest.mark.asyncio
async def test_unauthenticated_call_rejected(
    test_user: User, db_session: AsyncSession
):
    """4. Unauthenticated call (app.user_id empty) -> raises 42501."""
    u1_id = test_user.id
    orig_doc = await _create_doc(
        db_session, u1_id, visibility="private", status="indexed"
    )
    old_id = orig_doc.id
    new_id = uuid.uuid4()

    async with TestingSessionLocal() as raw_session:
        with pytest.raises(DBAPIError, match="not authenticated") as exc_info:
            await raw_session.execute(
                text(
                    "SELECT * FROM publish_document(:doc_id, :new_id, :new_path)"
                ),
                {
                    "doc_id": old_id,
                    "new_id": new_id,
                    "new_path": f"/tmp/public/{new_id}.txt",
                },
            )
        assert _extract_sqlstate(exc_info.value) == "42501"
        await raw_session.rollback()


@pytest.mark.asyncio
async def test_non_indexed_document_rejected(
    test_user: User, db_session: AsyncSession
):
    """5. Non-indexed document (status='pending') -> raises P0001, row unchanged."""
    u1_id = test_user.id
    pending_doc = await _create_doc(
        db_session, u1_id, visibility="private", status="pending"
    )
    old_id = pending_doc.id
    new_id = uuid.uuid4()

    await configure_rls_context(db_session, u1_id, "user")
    with pytest.raises(DBAPIError, match="document not indexed") as exc_info:
        await db_session.execute(
            text(
                "SELECT * FROM publish_document(:doc_id, :new_id, :new_path)"
            ),
            {
                "doc_id": old_id,
                "new_id": new_id,
                "new_path": f"/tmp/public/{new_id}.txt",
            },
        )
    assert _extract_sqlstate(exc_info.value) == "P0001"
    await db_session.rollback()

    await configure_rls_context(db_session, u1_id, "user")
    unchanged = (
        await db_session.execute(select(Document).where(Document.id == old_id))
    ).scalar_one_or_none()
    assert unchanged is not None
    assert unchanged.visibility == "private"
    assert unchanged.status == "pending"


@pytest.mark.asyncio
async def test_missing_or_already_public_document_raises_p0002(
    test_user: User, db_session: AsyncSession
):
    """6. Missing or already-public document id -> raises P0002."""
    u1_id = test_user.id
    pub_doc = await _create_doc(
        db_session, u1_id, visibility="public", status="indexed"
    )
    pub_id = pub_doc.id
    missing_id = uuid.uuid4()

    await configure_rls_context(db_session, u1_id, "user")
    with pytest.raises(DBAPIError, match="document not found") as exc_missing:
        await db_session.execute(
            text(
                "SELECT * FROM publish_document(:doc_id, :new_id, :new_path)"
            ),
            {
                "doc_id": missing_id,
                "new_id": uuid.uuid4(),
                "new_path": "/tmp/public/missing.txt",
            },
        )
    assert _extract_sqlstate(exc_missing.value) == "P0002"
    await db_session.rollback()

    await configure_rls_context(db_session, u1_id, "user")
    with pytest.raises(DBAPIError, match="document not found") as exc_public:
        await db_session.execute(
            text(
                "SELECT * FROM publish_document(:doc_id, :new_id, :new_path)"
            ),
            {
                "doc_id": pub_id,
                "new_id": uuid.uuid4(),
                "new_path": "/tmp/public/already_pub.txt",
            },
        )
    assert _extract_sqlstate(exc_public.value) == "P0002"
    await db_session.rollback()


@pytest.mark.asyncio
async def test_duplicate_public_hash_conflict_preserves_private_row(
    test_user: User, test_user_2: User, db_session: AsyncSession
):
    """7. Duplicate hash conflict -> if a public row with the same file_hash
    already exists, the insert fails with a unique violation (23505) and the
    original private row survives.
    """
    u1_id = test_user.id
    u2_id = test_user_2.id
    shared_hash = hashlib.sha256(f"shared-doc-{uuid.uuid4()}".encode()).hexdigest()

    # User 2 already has a public document with `shared_hash`
    await _create_doc(
        db_session,
        u2_id,
        visibility="public",
        status="indexed",
        file_hash=shared_hash,
    )

    # User 1 has a private indexed document with the same `shared_hash`
    priv_doc = await _create_doc(
        db_session,
        u1_id,
        visibility="private",
        status="indexed",
        file_hash=shared_hash,
    )
    priv_id = priv_doc.id

    await configure_rls_context(db_session, u1_id, "user")
    with pytest.raises(DBAPIError) as exc_info:
        await db_session.execute(
            text(
                "SELECT * FROM publish_document(:doc_id, :new_id, :new_path)"
            ),
            {
                "doc_id": priv_id,
                "new_id": uuid.uuid4(),
                "new_path": "/tmp/public/conflict.txt",
            },
        )
    assert _extract_sqlstate(exc_info.value) == "23505"
    await db_session.rollback()

    # Confirm original private row survived the rolled-back transaction
    await configure_rls_context(db_session, u1_id, "user")
    survived = (
        await db_session.execute(select(Document).where(Document.id == priv_id))
    ).scalar_one_or_none()
    assert survived is not None
    assert survived.visibility == "private"


@pytest.mark.asyncio
async def test_al109_immutability_trigger_still_blocks_direct_visibility_update(
    test_user: User, db_session: AsyncSession
):
    """8. AL-109 immutability trigger still blocks direct UPDATE of visibility
    on an existing row (raises 42501).
    """
    priv_doc = await _create_doc(
        db_session, test_user.id, visibility="private", status="indexed"
    )

    await configure_rls_context(db_session, test_user.id, "user")
    with pytest.raises(
        DBAPIError, match="Document visibility scope is immutable"
    ) as exc_info:
        await db_session.execute(
            update(Document)
            .where(Document.id == priv_doc.id)
            .values(visibility="public")
        )
    assert _extract_sqlstate(exc_info.value) == "42501"
    await db_session.rollback()
