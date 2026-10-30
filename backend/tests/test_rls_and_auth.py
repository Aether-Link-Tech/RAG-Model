"""Integration tests for JWT authentication, AL-129 `app.user_id` transaction context,
and AL-127 PostgreSQL Row-Level Security (RLS) on `documents`.
"""
from datetime import timedelta
import hashlib
import io
import uuid

import pytest
from sqlalchemy import select, text, update, delete
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.core.security import create_access_token, hash_password
from backend.app.db.rls import (
    clear_rls_context,
    configure_rls_context,
    enforce_unauthenticated_rls_context,
)
from backend.app.models.document import Document
from backend.app.models.user import User

VALID_TXT_BYTES = b"Top-secret enterprise knowledge base document."


def _bearer_headers(user: User) -> dict[str, str]:
    token = create_access_token(user.id, role=user.role)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_auth_login_issues_valid_jwt_and_rejects_invalid_credentials(
    async_client, db_session: AsyncSession
):
    """Verify `POST /api/v1/auth/login` verifies password and issues a usable Bearer JWT."""
    password = "CorrectHorseBatteryStaple!99"
    email = f"login-{uuid.uuid4().hex[:8]}@example.com"
    user = User(
        id=uuid.uuid4(),
        email=email,
        hashed_password=hash_password(password),
        role="user",
        is_deleted=False,
    )
    db_session.add(user)
    await db_session.commit()

    try:
        # Wrong password -> 401
        bad_res = await async_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "WrongPassword123!"},
        )
        assert bad_res.status_code == 401

        # Correct password -> 200 with Bearer token
        ok_res = await async_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert ok_res.status_code == 200
        body = ok_res.json()
        assert body["token_type"] == "bearer"
        assert body["user"]["id"] == str(user.id)

        # Use issued token against protected `/api/v1/users/me`
        me_res = await async_client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {body['access_token']}"},
        )
        assert me_res.status_code == 200
        assert me_res.json()["id"] == str(user.id)
    finally:
        await db_session.execute(delete(User).where(User.id == user.id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_missing_expired_and_invalid_jwts_are_rejected(
    async_client, test_user: User
):
    """Phase 5 (#4): Missing, expired, bad-signature, wrong-issuer/audience, or non-UUID sub tokens return 401."""
    # 1. Missing Authorization header
    r_missing = await async_client.get("/api/v1/documents")
    assert r_missing.status_code == 401

    # 2. Expired JWT
    expired_token = create_access_token(
        test_user.id,
        expires_delta=timedelta(minutes=-5),
    )
    r_expired = await async_client.get(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert r_expired.status_code == 401

    # 3. Invalid signature (wrong secret)
    bad_sig_token = create_access_token(
        test_user.id,
        secret_key="wrong-signing-key-that-does-not-match-server-secret",
    )
    r_bad_sig = await async_client.get(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {bad_sig_token}"},
    )
    assert r_bad_sig.status_code == 401

    # 4. Wrong issuer
    bad_iss_token = create_access_token(
        test_user.id,
        issuer="untrusted-issuer",
    )
    r_bad_iss = await async_client.get(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {bad_iss_token}"},
    )
    assert r_bad_iss.status_code == 401

    # 5. Wrong audience
    bad_aud_token = create_access_token(
        test_user.id,
        audience="wrong-audience",
    )
    r_bad_aud = await async_client.get(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {bad_aud_token}"},
    )
    assert r_bad_aud.status_code == 401

    # 6. Non-UUID subject claim
    bad_sub_token = create_access_token("not-a-valid-uuid")
    r_bad_sub = await async_client.get(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {bad_sub_token}"},
    )
    assert r_bad_sub.status_code == 401


@pytest.mark.asyncio
async def test_client_supplied_user_id_cannot_bypass_authorization(
    async_client, test_user: User, test_user_2: User
):
    """Phase 5 (#5): Client-supplied `X-User-Id` headers or parameters cannot authenticate or impersonate."""
    # User A uploads a private document
    up_res = await async_client.post(
        "/api/v1/documents",
        headers=_bearer_headers(test_user),
        data={"visibility": "private"},
        files={"file": ("victim_private.txt", io.BytesIO(VALID_TXT_BYTES), "text/plain")},
    )
    assert up_res.status_code == 201
    victim_doc_id = up_res.json()["id"]

    # 1. Supplying only `X-User-Id` without a valid JWT is rejected with 401
    legacy_header_res = await async_client.get(
        f"/api/v1/documents/{victim_doc_id}",
        headers={"X-User-Id": str(test_user.id)},
    )
    assert legacy_header_res.status_code == 401

    # 2. User B supplying their own valid JWT PLUS `X-User-Id: <User A>` cannot read User A's private doc
    impersonation_get = await async_client.get(
        f"/api/v1/documents/{victim_doc_id}",
        headers={
            **_bearer_headers(test_user_2),
            "X-User-Id": str(test_user.id),
        },
    )
    assert impersonation_get.status_code == 404

    # 3. User B supplying `X-User-Id: <User A>` and form field `user_id` on upload still records User B as owner
    impersonation_upload = await async_client.post(
        "/api/v1/documents",
        headers={
            **_bearer_headers(test_user_2),
            "X-User-Id": str(test_user.id),
        },
        data={"visibility": "private", "user_id": str(test_user.id)},
        files={"file": ("u2_own.txt", io.BytesIO(b"User B upload content"), "text/plain")},
    )
    assert impersonation_upload.status_code == 201
    assert impersonation_upload.json()["user_id"] == str(test_user_2.id)


@pytest.mark.asyncio
async def test_private_and_public_access_via_api_and_postgres_rls(
    async_client, test_user: User, test_user_2: User, db_session: AsyncSession
):
    """Phase 5 (#1, #2, #3):
    - User A can access their private documents.
    - User B cannot access User A's private documents (API & direct PostgreSQL RLS).
    - Public documents are accessible to authenticated users, and hidden when unauthenticated.
    """
    priv_bytes = f"User A strictly private research notes ({uuid.uuid4()}).".encode()
    pub_bytes = f"Organization-wide public knowledge base article ({uuid.uuid4()}).".encode()

    res_priv = await async_client.post(
        "/api/v1/documents",
        headers=_bearer_headers(test_user),
        data={"visibility": "private"},
        files={"file": ("u1_private.txt", io.BytesIO(priv_bytes), "text/plain")},
    )
    assert res_priv.status_code == 201
    priv_id = uuid.UUID(res_priv.json()["id"])

    res_pub = await async_client.post(
        "/api/v1/documents",
        headers=_bearer_headers(test_user),
        data={"visibility": "public"},
        files={"file": ("shared_public.txt", io.BytesIO(pub_bytes), "text/plain")},
    )
    assert res_pub.status_code == 201
    pub_id = uuid.UUID(res_pub.json()["id"])

    # API checks for User A: sees both their private doc and the public doc
    u1_get_priv = await async_client.get(
        f"/api/v1/documents/{priv_id}",
        headers=_bearer_headers(test_user),
    )
    assert u1_get_priv.status_code == 200
    assert u1_get_priv.json()["filename"] == "u1_private.txt"

    u1_list = await async_client.get(
        "/api/v1/documents",
        headers=_bearer_headers(test_user),
    )
    assert u1_list.status_code == 200
    u1_ids = {d["id"] for d in u1_list.json()}
    assert str(priv_id) in u1_ids
    assert str(pub_id) in u1_ids

    # API checks for User B: CANNOT see User A's private doc (404 & omitted from list), CAN see public doc
    u2_get_priv = await async_client.get(
        f"/api/v1/documents/{priv_id}",
        headers=_bearer_headers(test_user_2),
    )
    assert u2_get_priv.status_code == 404

    u2_get_pub = await async_client.get(
        f"/api/v1/documents/{pub_id}",
        headers=_bearer_headers(test_user_2),
    )
    assert u2_get_pub.status_code == 200
    assert u2_get_pub.json()["id"] == str(pub_id)

    u2_list = await async_client.get(
        "/api/v1/documents",
        headers=_bearer_headers(test_user_2),
    )
    assert u2_list.status_code == 200
    u2_ids = {d["id"] for d in u2_list.json()}
    assert str(priv_id) not in u2_ids
    assert str(pub_id) in u2_ids

    u1_id, u1_role = test_user.id, test_user.role
    u2_id, u2_role = test_user_2.id, test_user_2.role

    # Direct PostgreSQL RLS verification (unfiltered `SELECT * FROM documents WHERE id IN (...)`!):
    # 1. Under User A's RLS context -> PostgreSQL returns both `priv_id` and `pub_id`
    await configure_rls_context(db_session, u1_id, u1_role)
    u1_rls_rows = (
        await db_session.execute(
            select(Document.id).where(Document.id.in_([priv_id, pub_id]))
        )
    ).scalars().all()
    assert set(u1_rls_rows) == {priv_id, pub_id}
    await db_session.commit()
    clear_rls_context(db_session)

    # 2. Under User B's RLS context -> PostgreSQL RLS hides `priv_id` and returns only `pub_id`
    await configure_rls_context(db_session, u2_id, u2_role)
    u2_rls_rows = (
        await db_session.execute(
            select(Document.id).where(Document.id.in_([priv_id, pub_id]))
        )
    ).scalars().all()
    assert set(u2_rls_rows) == {pub_id}
    await db_session.commit()
    clear_rls_context(db_session)

    # 3. Under unauthenticated RLS context (`app.user_id = ''`) -> PostgreSQL RLS hides ALL rows
    await enforce_unauthenticated_rls_context(db_session)
    anon_rls_rows = (
        await db_session.execute(
            select(Document.id).where(Document.id.in_([priv_id, pub_id]))
        )
    ).scalars().all()
    assert anon_rls_rows == []
    await db_session.commit()
    clear_rls_context(db_session)


@pytest.mark.asyncio
async def test_unauthorized_writes_and_deletions_rejected_by_rls_and_api(
    async_client, test_user: User, test_user_2: User, db_session: AsyncSession
):
    """Phase 5 (#7): Unauthorized INSERT, UPDATE, and DELETE are rejected by PostgreSQL RLS and API."""
    # Create User A private document and public document
    priv_payload = f"Private doc to protect ({uuid.uuid4()})".encode()
    pub_payload = f"Public doc owned by User A ({uuid.uuid4()})".encode()
    res_priv = await async_client.post(
        "/api/v1/documents",
        headers=_bearer_headers(test_user),
        data={"visibility": "private"},
        files={"file": ("u1_priv_del.txt", io.BytesIO(priv_payload), "text/plain")},
    )
    assert res_priv.status_code == 201
    priv_id = uuid.UUID(res_priv.json()["id"])

    res_pub = await async_client.post(
        "/api/v1/documents",
        headers=_bearer_headers(test_user),
        data={"visibility": "public"},
        files={"file": ("u1_pub_del.txt", io.BytesIO(pub_payload), "text/plain")},
    )
    assert res_pub.status_code == 201
    pub_id = uuid.UUID(res_pub.json()["id"])

    u1_headers = _bearer_headers(test_user)
    u2_headers = _bearer_headers(test_user_2)
    u1_id = test_user.id
    u2_id, u2_role = test_user_2.id, test_user_2.role

    # 1. Direct SQL INSERT under User B attempting to insert a document with `user_id = User A`
    # MUST be rejected by PostgreSQL RLS `WITH CHECK` policy!
    await configure_rls_context(db_session, u2_id, u2_role)
    forged_doc = Document(
        id=uuid.uuid4(),
        user_id=u1_id,  # Forged owner!
        filename="forged.txt",
        file_path="/tmp/forged.txt",
        mime_type="text/plain",
        file_size=12,
        file_hash=hashlib.sha256(b"forged-payload").hexdigest(),
        status="pending",
        visibility="private",
    )
    db_session.add(forged_doc)
    with pytest.raises(DBAPIError, match="row-level security policy"):
        await db_session.flush()
    await db_session.rollback()
    clear_rls_context(db_session)

    # 2. Direct SQL UPDATE & DELETE under User B targeting User A's private or public documents
    await configure_rls_context(db_session, u2_id, u2_role)
    upd_result = await db_session.execute(
        update(Document)
        .where(Document.id.in_([priv_id, pub_id]))
        .values(filename="hacked.txt")
    )
    assert upd_result.rowcount == 0

    del_result = await db_session.execute(
        delete(Document).where(Document.id.in_([priv_id, pub_id]))
    )
    assert del_result.rowcount == 0
    await db_session.commit()
    clear_rls_context(db_session)

    # 3. API DELETE by User B on User A's private document -> 404 Not Found
    api_del_priv_u2 = await async_client.delete(
        f"/api/v1/documents/{priv_id}",
        headers=u2_headers,
    )
    assert api_del_priv_u2.status_code == 404

    # 4. API DELETE by User B (non-owner, non-admin) on User A's public document -> 403 Forbidden
    api_del_pub_u2 = await async_client.delete(
        f"/api/v1/documents/{pub_id}",
        headers=u2_headers,
    )
    assert api_del_pub_u2.status_code == 403

    # 5. API DELETE by User A (owner) succeeds with 204 No Content
    api_del_priv_u1 = await async_client.delete(
        f"/api/v1/documents/{priv_id}",
        headers=u1_headers,
    )
    assert api_del_priv_u1.status_code == 204

    api_del_pub_u1 = await async_client.delete(
        f"/api/v1/documents/{pub_id}",
        headers=u1_headers,
    )
    assert api_del_pub_u1.status_code == 204


@pytest.mark.asyncio
async def test_admin_can_delete_public_document_but_cannot_access_other_users_private_documents(
    async_client, test_user: User, db_session: AsyncSession
):
    """Verify an admin can delete a public document uploaded by another user,
    but still cannot read or delete another user's private document.
    """
    admin_id = uuid.uuid4()
    admin = User(
        id=admin_id,
        email=f"admin-{admin_id.hex[:8]}@example.com",
        hashed_password="admin_hashed_password",
        role="admin",
        is_deleted=False,
    )
    db_session.add(admin)
    await db_session.commit()
    admin_headers = _bearer_headers(admin)
    u1_headers = _bearer_headers(test_user)

    try:
        res_priv = await async_client.post(
            "/api/v1/documents",
            headers=u1_headers,
            data={"visibility": "private"},
            files={
                "file": (
                    "u1_secret.txt",
                    io.BytesIO(f"Secret private content ({uuid.uuid4()})".encode()),
                    "text/plain",
                )
            },
        )
        assert res_priv.status_code == 201
        priv_id = res_priv.json()["id"]

        res_pub = await async_client.post(
            "/api/v1/documents",
            headers=u1_headers,
            data={"visibility": "public"},
            files={
                "file": (
                    "u1_public.txt",
                    io.BytesIO(f"Shared public content ({uuid.uuid4()})".encode()),
                    "text/plain",
                )
            },
        )
        assert res_pub.status_code == 201
        pub_id = res_pub.json()["id"]

        # Admin CANNOT read or delete User A's private document (404 Not Found)
        admin_get_priv = await async_client.get(
            f"/api/v1/documents/{priv_id}",
            headers=admin_headers,
        )
        assert admin_get_priv.status_code == 404

        admin_del_priv = await async_client.delete(
            f"/api/v1/documents/{priv_id}",
            headers=admin_headers,
        )
        assert admin_del_priv.status_code == 404

        # Admin CAN delete User A's public document (204 No Content)
        admin_del_pub = await async_client.delete(
            f"/api/v1/documents/{pub_id}",
            headers=admin_headers,
        )
        assert admin_del_pub.status_code == 204
    finally:
        clear_rls_context(db_session)
        await db_session.rollback()
        await db_session.execute(delete(User).where(User.id == admin_id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_pooled_database_connection_reuse_does_not_leak_rls_context(
    async_client, test_user: User, test_user_2: User
):
    """Phase 5 (#6): Reusing a single pooled database connection across transactions
    must never leak `app.user_id` or `app.user_role` to subsequent callers.
    """
    up_res = await async_client.post(
        "/api/v1/documents",
        headers=_bearer_headers(test_user),
        data={"visibility": "private"},
        files={"file": ("pool_isolation.txt", io.BytesIO(b"Pooled connection isolation test"), "text/plain")},
    )
    assert up_res.status_code == 201
    u1_priv_id = uuid.UUID(up_res.json()["id"])

    # Create a dedicated engine with QueuePool of size 1 (forcing all sessions to reuse the exact same PG connection)
    pooled_engine = create_async_engine(
        settings.DATABASE_URL,
        pool_size=1,
        max_overflow=0,
        future=True,
    )
    PooledSessionLocal = async_sessionmaker(
        bind=pooled_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    try:
        # Session 1: User A authenticates and reads their private document on the single pooled connection
        async with PooledSessionLocal() as session_1:
            await configure_rls_context(session_1, test_user.id, test_user.role)
            backend_pid_1 = (await session_1.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            active_uid_1 = (
                await session_1.execute(text("SELECT current_setting('app.user_id', true)"))
            ).scalar_one()
            assert active_uid_1 == str(test_user.id)

            docs_1 = (
                await session_1.execute(select(Document.id).where(Document.id == u1_priv_id))
            ).scalars().all()
            assert docs_1 == [u1_priv_id]
            await session_1.commit()
            clear_rls_context(session_1)

        # Session 2: Immediately check out the SAME pooled PostgreSQL connection without setting User A's context
        async with PooledSessionLocal() as session_2:
            backend_pid_2 = (await session_2.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            assert backend_pid_2 == backend_pid_1  # Proven same underlying PostgreSQL connection!

            # Enforce unauthenticated RLS role on this reused connection -> `app.user_id` is empty and 0 rows visible
            await enforce_unauthenticated_rls_context(session_2)
            leaked_uid = (
                await session_2.execute(text("SELECT NULLIF(current_setting('app.user_id', true), '')"))
            ).scalar_one_or_none()
            assert leaked_uid is None

            docs_2 = (
                await session_2.execute(select(Document.id).where(Document.id == u1_priv_id))
            ).scalars().all()
            assert docs_2 == []
            await session_2.commit()
            clear_rls_context(session_2)

        # Session 3: User B checks out the SAME pooled connection -> sees User B context, NOT User A's private doc
        async with PooledSessionLocal() as session_3:
            await configure_rls_context(session_3, test_user_2.id, test_user_2.role)
            backend_pid_3 = (await session_3.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            assert backend_pid_3 == backend_pid_1

            docs_3 = (
                await session_3.execute(select(Document.id).where(Document.id == u1_priv_id))
            ).scalars().all()
            assert docs_3 == []
            await session_3.commit()
            clear_rls_context(session_3)
    finally:
        await pooled_engine.dispose()
