"""Integration tests for JWT authentication, AL-129 `app.user_id` transaction context,
and AL-127 PostgreSQL Row-Level Security (RLS) on `documents`.
"""
from datetime import timedelta
import hashlib
import io
import os
import uuid

import pytest
from sqlalchemy import select, text, update, delete
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.api.v1 import users as users_api
from backend.app.core.config import (
    DEFAULT_DEV_APP_DB_PASSWORD,
    DEFAULT_DEV_SECRET_KEY,
    Settings,
    settings,
)
from backend.app.core.security import create_access_token, decode_access_token, hash_password
from backend.app.db.rls import (
    clear_rls_context,
    configure_rls_context,
    enforce_unauthenticated_rls_context,
)
from backend.app.models.document import Document
from backend.app.models.user import User
from backend.tests.conftest import TestingSessionLocal, purge_test_users

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
        await purge_test_users(user.id)


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
        await purge_test_users(admin_id)


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
        settings.app_database_url,
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

            # Even without calling `enforce_unauthenticated_rls_context`, the reused `rag_app` connection
            # has no `app.user_id` from Session 1 and fails closed automatically!
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


@pytest.mark.asyncio
async def test_admin_self_registration_attempt_forced_to_user_role(
    async_client, test_user: User, db_session: AsyncSession
):
    """Priority 1 / Priority 6 (#1, #2):
    Verify an unauthenticated caller attempting to register with `role='admin'` via
    `POST /api/v1/users` always receives `role='user'` and cannot perform admin actions.
    """
    email = f"attacker-{uuid.uuid4().hex[:8]}@example.com"
    password = "AttackerPassword123!"

    reg_res = await async_client.post(
        "/api/v1/users",
        json={"email": email, "password": password, "role": "admin"},
    )
    assert reg_res.status_code == 201
    created_data = reg_res.json()
    created_uid = uuid.UUID(created_data["id"])
    assert created_data["role"] == "user"

    try:
        # Verify database record has role == 'user'
        db_user = await db_session.get(User, created_uid)
        assert db_user is not None
        assert db_user.role == "user"

        # Log in as the newly registered user and verify JWT claim has role == 'user'
        login_res = await async_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert login_res.status_code == 200
        token = login_res.json()["access_token"]
        decoded = decode_access_token(token)
        assert decoded["role"] == "user"

        # Upload a public document as `test_user` and verify the self-registered caller CANNOT delete it
        pub_res = await async_client.post(
            "/api/v1/documents",
            headers=_bearer_headers(test_user),
            data={"visibility": "public"},
            files={
                "file": (
                    "protected_public.txt",
                    io.BytesIO(f"Protected public content ({uuid.uuid4()})".encode()),
                    "text/plain",
                )
            },
        )
        assert pub_res.status_code == 201
        pub_doc_id = pub_res.json()["id"]

        del_res = await async_client.delete(
            f"/api/v1/documents/{pub_doc_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert del_res.status_code == 403
    finally:
        clear_rls_context(db_session)
        await db_session.rollback()
        await purge_test_users(created_uid)


@pytest.mark.asyncio
async def test_application_connects_as_rag_app_login_role_and_fails_closed_when_unauthenticated(
    async_client, test_user: User
):
    """Priority 2 / Priority 5 / Priority 6 (#3, #4):
    Verify normal application database connections use the `rag_app` LOGIN role
    (`NOSUPERUSER`, `NOBYPASSRLS`), cannot escalate via `RESET ROLE` or `SET ROLE postgres`,
    and fail closed by default when `app.user_id` is not set.
    """
    # Create both a private and a public document
    priv_res = await async_client.post(
        "/api/v1/documents",
        headers=_bearer_headers(test_user),
        data={"visibility": "private"},
        files={
            "file": (
                "role_check_priv.txt",
                io.BytesIO(f"Private under rag_app ({uuid.uuid4()})".encode()),
                "text/plain",
            )
        },
    )
    assert priv_res.status_code == 201
    priv_id = uuid.UUID(priv_res.json()["id"])

    pub_res = await async_client.post(
        "/api/v1/documents",
        headers=_bearer_headers(test_user),
        data={"visibility": "public"},
        files={
            "file": (
                "role_check_pub.txt",
                io.BytesIO(f"Public under rag_app ({uuid.uuid4()})".encode()),
                "text/plain",
            )
        },
    )
    assert pub_res.status_code == 201
    pub_id = uuid.UUID(pub_res.json()["id"])

    # Open a raw session from `TestingSessionLocal` without calling `configure_rls_context`
    async with TestingSessionLocal() as raw_session:
        role_info = (
            await raw_session.execute(
                text(
                    "SELECT session_user, current_user, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                    "FROM pg_roles WHERE rolname = session_user"
                )
            )
        ).one()
        assert role_info.session_user == "rag_app"
        assert role_info.current_user == "rag_app"
        assert role_info.rolsuper is False
        assert role_info.rolbypassrls is False
        assert role_info.rolcreaterole is False
        assert role_info.rolcreatedb is False

        # Executing `RESET ROLE` keeps `current_user == 'rag_app'`
        await raw_session.execute(text("RESET ROLE"))
        after_reset = (await raw_session.execute(text("SELECT current_user"))).scalar_one()
        assert after_reset == "rag_app"

        # Without `app.user_id` set, `SELECT` on `documents` fails closed (returns 0 rows even for public docs!)
        unauth_docs = (
            await raw_session.execute(
                select(Document.id).where(Document.id.in_([priv_id, pub_id]))
            )
        ).scalars().all()
        assert unauth_docs == []

        # Attempt to escalate via `SET ROLE postgres` fails with insufficient privilege
        with pytest.raises(DBAPIError):
            await raw_session.execute(text("SET ROLE postgres"))
        await raw_session.rollback()


@pytest.mark.asyncio
async def test_document_user_id_and_visibility_immutability_enforced_at_database_level(
    async_client, test_user: User, test_user_2: User, db_session: AsyncSession
):
    """Priority 3 / Priority 6 (#6, #7, #8):
    Verify PostgreSQL prevents unauthorized changes to `documents.user_id` and `documents.visibility`:
    - Owners cannot reassign `user_id` or flip `visibility` on their documents.
    - Admins cannot hijack another user's public document (`SET user_id = admin.id`)
      or convert another user's public document into a private document (`SET visibility = 'private'`).
    - Admins CAN update mutable status fields on public documents.
    """
    admin_id = uuid.uuid4()
    admin = User(
        id=admin_id,
        email=f"admin-immut-{admin_id.hex[:8]}@example.com",
        hashed_password="admin_hashed_password",
        role="admin",
        is_deleted=False,
    )
    db_session.add(admin)
    await db_session.commit()

    u1_id = test_user.id
    u2_id = test_user_2.id

    try:
        priv_res = await async_client.post(
            "/api/v1/documents",
            headers=_bearer_headers(test_user),
            data={"visibility": "private"},
            files={
                "file": (
                    "immut_priv.txt",
                    io.BytesIO(f"Immutable owner private ({uuid.uuid4()})".encode()),
                    "text/plain",
                )
            },
        )
        assert priv_res.status_code == 201
        priv_id = uuid.UUID(priv_res.json()["id"])

        pub_res = await async_client.post(
            "/api/v1/documents",
            headers=_bearer_headers(test_user),
            data={"visibility": "public"},
            files={
                "file": (
                    "immut_pub.txt",
                    io.BytesIO(f"Immutable owner public ({uuid.uuid4()})".encode()),
                    "text/plain",
                )
            },
        )
        assert pub_res.status_code == 201
        pub_id = uuid.UUID(pub_res.json()["id"])

        # 1. Owner attempting to reassign `user_id` on their own document -> rejected by DB trigger (42501)
        await configure_rls_context(db_session, u1_id, "user")
        with pytest.raises(DBAPIError, match="Document owner \\(user_id\\) is immutable"):
            await db_session.execute(
                update(Document).where(Document.id == priv_id).values(user_id=u2_id)
            )
        await db_session.rollback()

        # 2. Owner attempting to convert their private document to public -> rejected by DB trigger (42501)
        await configure_rls_context(db_session, u1_id, "user")
        with pytest.raises(DBAPIError, match="Document visibility scope is immutable"):
            await db_session.execute(
                update(Document).where(Document.id == priv_id).values(visibility="public")
            )
        await db_session.rollback()

        # 3. Admin attempting to hijack ownership (`user_id = admin_id`) of User A's public document -> rejected
        await configure_rls_context(db_session, admin_id, "admin")
        with pytest.raises(DBAPIError, match="Document owner \\(user_id\\) is immutable"):
            await db_session.execute(
                update(Document).where(Document.id == pub_id).values(user_id=admin_id)
            )
        await db_session.rollback()

        # 4. Admin attempting to convert User A's public document into a private document -> rejected
        await configure_rls_context(db_session, admin_id, "admin")
        with pytest.raises(DBAPIError, match="Document visibility scope is immutable"):
            await db_session.execute(
                update(Document)
                .where(Document.id == pub_id)
                .values(visibility="private")
            )
        await db_session.rollback()

        await configure_rls_context(db_session, admin_id, "admin")
        with pytest.raises(DBAPIError, match="Document owner \\(user_id\\) is immutable"):
            await db_session.execute(
                update(Document)
                .where(Document.id == pub_id)
                .values(visibility="private", user_id=admin_id)
            )
        await db_session.rollback()

        # 5. Admin updating a mutable field (`status = 'indexed'`) on User A's public document -> allowed
        await configure_rls_context(db_session, admin_id, "admin")
        upd_ok = await db_session.execute(
            update(Document).where(Document.id == pub_id).values(status="indexed")
        )
        assert upd_ok.rowcount == 1
        await db_session.commit()
    finally:
        clear_rls_context(db_session)
        await db_session.rollback()
        await purge_test_users(admin_id)


def test_security_configuration_rejects_insecure_production_settings():
    """Priority 4: Verify `Settings` fails startup when production JWT or DB credentials are weak or default."""
    strong_secret = "prod-jwt-signing-key-with-more-than-32-secure-bytes-2026!"
    secure_app_db_url = "postgresql+asyncpg://rag_app:ProdStrongDbPass_99!@localhost:5432/rag_db"

    # 1. Default development SECRET_KEY in production -> rejected
    with pytest.raises(ValueError, match="Invalid SECRET_KEY"):
        Settings(
            ENV="production",
            SECRET_KEY=DEFAULT_DEV_SECRET_KEY,
            APP_DATABASE_URL=secure_app_db_url,
        )

    # 2. Short SECRET_KEY (< 32 chars) in production -> rejected
    with pytest.raises(ValueError, match="Invalid SECRET_KEY"):
        Settings(
            ENV="production",
            SECRET_KEY="too-short-secret",
            APP_DATABASE_URL=secure_app_db_url,
        )

    # 3. Empty SECRET_KEY even in development -> rejected
    with pytest.raises(ValueError, match="SECRET_KEY must not be empty"):
        Settings(
            ENV="development",
            SECRET_KEY="   ",
        )

    # 4. Superuser `postgres` in production APP_DATABASE_URL -> rejected
    with pytest.raises(ValueError, match="non-superuser role"):
        Settings(
            ENV="production",
            SECRET_KEY=strong_secret,
            APP_DATABASE_URL="postgresql+asyncpg://postgres:ProdStrongDbPass_99!@localhost:5432/rag_db",
        )

    # 5. Default development DB password in production -> rejected
    with pytest.raises(ValueError, match="default development password"):
        Settings(
            ENV="production",
            SECRET_KEY=strong_secret,
            APP_DATABASE_URL=f"postgresql+asyncpg://rag_app:{DEFAULT_DEV_APP_DB_PASSWORD}@localhost:5432/rag_db",
        )

    # 6. Valid production configuration -> accepted
    prod_settings = Settings(
        ENV="production",
        SECRET_KEY=strong_secret,
        APP_DATABASE_URL=secure_app_db_url,
    )
    assert prod_settings.ENV == "production"
    assert prod_settings.app_database_url == secure_app_db_url


@pytest.mark.asyncio
async def test_user_enumeration_and_unauthorized_deletion_blocked(
    async_client, test_user: User, test_user_2: User, db_session: AsyncSession
):
    """Verify unauthenticated callers cannot enumerate users or delete accounts,
    and regular authenticated users cannot list users or delete other users' accounts.
    """
    u1_headers = _bearer_headers(test_user)

    # 1. Unauthenticated user enumeration & profile endpoints -> 401 Unauthorized
    unauth_list = await async_client.get("/api/v1/users")
    assert unauth_list.status_code == 401

    unauth_me = await async_client.get("/api/v1/users/me")
    assert unauth_me.status_code == 401

    # 2. Unauthenticated user deletion endpoints -> 401 Unauthorized
    unauth_del_id = await async_client.delete(f"/api/v1/users/{test_user.id}")
    assert unauth_del_id.status_code == 401

    unauth_del_me = await async_client.delete("/api/v1/users/me")
    assert unauth_del_me.status_code == 401

    unauth_del_priv_col = await async_client.delete("/api/v1/users/me/private-collection")
    assert unauth_del_priv_col.status_code == 401

    # 3. Authenticated regular user attempting user enumeration -> 403 Forbidden
    non_admin_list = await async_client.get("/api/v1/users", headers=u1_headers)
    assert non_admin_list.status_code == 403

    # 4. Authenticated regular user attempting to delete another user -> 403 Forbidden
    non_admin_del_other = await async_client.delete(
        f"/api/v1/users/{test_user_2.id}",
        headers=u1_headers,
    )
    assert non_admin_del_other.status_code == 403

    # Verify target user remains active in PostgreSQL
    db_u2 = await db_session.get(User, test_user_2.id)
    assert db_u2 is not None
    assert db_u2.is_deleted is False

    # 5. Authenticated regular user probing a non-existent UUID -> 403 Forbidden (no UUID existence leak)
    non_admin_probe = await async_client.delete(
        f"/api/v1/users/{uuid.uuid4()}",
        headers=u1_headers,
    )
    assert non_admin_probe.status_code == 403


@pytest.mark.asyncio
async def test_authorized_user_management_and_public_document_preservation(
    async_client, test_user: User, db_session: AsyncSession, monkeypatch
):
    """Verify authorized user-management rules and public-document safety:
    - Admins can list active users (`GET /api/v1/users`) and delete user accounts (`DELETE /api/v1/users/{id}`).
    - Regular users can perform self-service account deletion (`DELETE /api/v1/users/{own_id}` and `/me`).
    - Deleting a user soft-deletes the account (`is_deleted = True`), immediately revokes their JWT,
      cleans up only their private vector collection, and NEVER deletes or corrupts public documents
      uploaded by that user.
    """
    cleaned_private_collections: list[uuid.UUID] = []

    def _spy_delete_private_knowledge(uid: uuid.UUID) -> bool:
        cleaned_private_collections.append(uuid.UUID(str(uid)))
        return True

    monkeypatch.setattr(users_api, "delete_user_private_knowledge", _spy_delete_private_knowledge)

    admin_id = uuid.uuid4()
    user_a_id = uuid.uuid4()
    user_b_id = uuid.uuid4()
    pw_hash = hash_password("TestAccountPass123!")

    admin = User(
        id=admin_id,
        email=f"admin-mgmt-{admin_id.hex[:8]}@example.com",
        hashed_password=pw_hash,
        role="admin",
        is_deleted=False,
    )
    user_a = User(
        id=user_a_id,
        email=f"user-selfdel-{user_a_id.hex[:8]}@example.com",
        hashed_password=pw_hash,
        role="user",
        is_deleted=False,
    )
    user_b = User(
        id=user_b_id,
        email=f"user-admindel-{user_b_id.hex[:8]}@example.com",
        hashed_password=pw_hash,
        role="user",
        is_deleted=False,
    )
    db_session.add_all([admin, user_a, user_b])
    await db_session.commit()

    admin_headers = _bearer_headers(admin)
    user_a_headers = _bearer_headers(user_a)
    user_b_headers = _bearer_headers(user_b)
    observer_headers = _bearer_headers(test_user)

    try:
        # 1. Admin can list active users
        list_res = await async_client.get("/api/v1/users", headers=admin_headers)
        assert list_res.status_code == 200
        listed_ids = {u["id"] for u in list_res.json()}
        assert {str(admin_id), str(user_a_id), str(user_b_id), str(test_user.id)}.issubset(listed_ids)

        # 2. User A and User B each upload a public document (and User A also uploads a private document)
        pub_a_bytes = f"Shared public handbook by User A ({uuid.uuid4()})".encode()
        priv_a_bytes = f"Private notes by User A ({uuid.uuid4()})".encode()
        pub_b_bytes = f"Shared public architecture spec by User B ({uuid.uuid4()})".encode()

        res_pub_a = await async_client.post(
            "/api/v1/documents",
            headers=user_a_headers,
            data={"visibility": "public"},
            files={"file": ("handbook_a.txt", io.BytesIO(pub_a_bytes), "text/plain")},
        )
        assert res_pub_a.status_code == 201
        pub_a_id = res_pub_a.json()["id"]

        res_priv_a = await async_client.post(
            "/api/v1/documents",
            headers=user_a_headers,
            data={"visibility": "private"},
            files={"file": ("notes_a.txt", io.BytesIO(priv_a_bytes), "text/plain")},
        )
        assert res_priv_a.status_code == 201
        priv_a_id = res_priv_a.json()["id"]

        res_pub_b = await async_client.post(
            "/api/v1/documents",
            headers=user_b_headers,
            data={"visibility": "public"},
            files={"file": ("spec_b.txt", io.BytesIO(pub_b_bytes), "text/plain")},
        )
        assert res_pub_b.status_code == 201
        pub_b_id = res_pub_b.json()["id"]

        await configure_rls_context(db_session, test_user.id, "user")
        doc_pub_a = await db_session.get(Document, uuid.UUID(pub_a_id))
        doc_pub_b = await db_session.get(Document, uuid.UUID(pub_b_id))
        assert doc_pub_a is not None
        assert doc_pub_b is not None
        pub_a_path = doc_pub_a.file_path
        pub_b_path = doc_pub_b.file_path

        # 3. Self-service deletion: User A deletes their own account via `DELETE /api/v1/users/{user_a_id}`
        self_del_res = await async_client.delete(
            f"/api/v1/users/{user_a_id}",
            headers=user_a_headers,
        )
        assert self_del_res.status_code == 200
        assert self_del_res.json()["id"] == str(user_a_id)
        assert self_del_res.json()["is_deleted"] is True
        assert user_a_id in cleaned_private_collections

        # User A's JWT is immediately rejected (`401`) and re-login is rejected (`401`)
        after_self_del = await async_client.get("/api/v1/users/me", headers=user_a_headers)
        assert after_self_del.status_code == 401
        relogin_a = await async_client.post(
            "/api/v1/auth/login",
            json={"email": user_a.email, "password": "TestAccountPass123!"},
        )
        assert relogin_a.status_code == 401

        # 4. Admin deletion: Admin deletes User B via `DELETE /api/v1/users/{user_b_id}`
        admin_del_res = await async_client.delete(
            f"/api/v1/users/{user_b_id}",
            headers=admin_headers,
        )
        assert admin_del_res.status_code == 200
        assert admin_del_res.json()["id"] == str(user_b_id)
        assert admin_del_res.json()["is_deleted"] is True
        assert user_b_id in cleaned_private_collections

        # Repeat deletion of already-deleted user returns 404 Not Found
        repeat_del_res = await async_client.delete(
            f"/api/v1/users/{user_b_id}",
            headers=admin_headers,
        )
        assert repeat_del_res.status_code == 404

        # Soft-deleted users no longer appear in active user listing
        list_after = await async_client.get("/api/v1/users", headers=admin_headers)
        assert list_after.status_code == 200
        active_ids_after = {u["id"] for u in list_after.json()}
        assert str(user_a_id) not in active_ids_after
        assert str(user_b_id) not in active_ids_after

        # 5. Verify BOTH deleted users' public documents remain intact, readable, on disk, and deduplicated!
        assert os.path.exists(pub_a_path)
        assert os.path.exists(pub_b_path)

        get_pub_a = await async_client.get(
            f"/api/v1/documents/{pub_a_id}",
            headers=observer_headers,
        )
        assert get_pub_a.status_code == 200
        assert get_pub_a.json()["visibility"] == "public"

        get_pub_b = await async_client.get(
            f"/api/v1/documents/{pub_b_id}",
            headers=observer_headers,
        )
        assert get_pub_b.status_code == 200
        assert get_pub_b.json()["visibility"] == "public"

        # Deleted User A's private document remains inaccessible to everyone (including observer & admin)
        obs_priv_a = await async_client.get(
            f"/api/v1/documents/{priv_a_id}",
            headers=observer_headers,
        )
        assert obs_priv_a.status_code == 404
        adm_priv_a = await async_client.get(
            f"/api/v1/documents/{priv_a_id}",
            headers=admin_headers,
        )
        assert adm_priv_a.status_code == 404

        # AL-176 public deduplication still protects the public document uploaded by deleted User A
        dup_pub_a = await async_client.post(
            "/api/v1/documents",
            headers=observer_headers,
            data={"visibility": "public"},
            files={"file": ("dup_handbook_a.txt", io.BytesIO(pub_a_bytes), "text/plain")},
        )
        assert dup_pub_a.status_code == 409
    finally:
        clear_rls_context(db_session)
        await db_session.rollback()
        await purge_test_users(admin_id, user_a_id, user_b_id)


@pytest.mark.asyncio
async def test_rag_app_postgres_privileges_and_escalation_attempts_blocked(test_user: User):
    """Validate PostgreSQL `rag_app` role attributes, grants, zero inherited memberships,
    and resistance to privilege escalation, RLS tampering, and hard user deletion.
    """
    target_uid = test_user.id

    async with TestingSessionLocal() as raw_session:
        # 1. Inspect `pg_roles` attributes for `rag_app`
        role_row = (
            await raw_session.execute(
                text(
                    "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, "
                    "rolcanlogin, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
        assert role_row.rolname == "rag_app"
        assert role_row.rolcanlogin is True
        assert role_row.rolsuper is False
        assert role_row.rolbypassrls is False
        assert role_row.rolcreaterole is False
        assert role_row.rolcreatedb is False
        assert role_row.rolreplication is False
        assert role_row.rolinherit is False

        # 2. Verify `rag_app` is NOT a member of any parent role (`pg_auth_members`)
        inherited_roles = (
            await raw_session.execute(
                text(
                    "SELECT r.rolname "
                    "FROM pg_auth_members am "
                    "JOIN pg_roles r ON r.oid = am.roleid "
                    "JOIN pg_roles m ON m.oid = am.member "
                    "WHERE m.rolname = current_user"
                )
            )
        ).scalars().all()
        assert inherited_roles == []

        # 3. Verify least-privilege table grants on `public` schema
        grant_rows = (
            await raw_session.execute(
                text(
                    "SELECT table_name, privilege_type "
                    "FROM information_schema.role_table_grants "
                    "WHERE grantee = current_user AND table_schema = 'public'"
                )
            )
        ).all()
        grants_by_table: dict[str, set[str]] = {}
        for tbl, priv in grant_rows:
            grants_by_table.setdefault(tbl, set()).add(priv)

        assert grants_by_table.get("documents") == {"SELECT", "INSERT", "UPDATE", "DELETE"}
        assert grants_by_table.get("users") == {"SELECT", "INSERT", "UPDATE"}
        assert "alembic_version" not in grants_by_table

        # 4. Verify `documents` has both ENABLE ROW LEVEL SECURITY and FORCE ROW LEVEL SECURITY
        rls_flags = (
            await raw_session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname = 'documents'"
                )
            )
        ).one()
        assert rls_flags.relrowsecurity is True
        assert rls_flags.relforcerowsecurity is True
        await raw_session.rollback()

        # 5. Verify all privilege escalation, RLS-disabling, policy/trigger tampering, and hard user DELETE fail
        forbidden_statements = [
            ("SET ROLE postgres", {}),
            ("SET SESSION AUTHORIZATION postgres", {}),
            ("ALTER TABLE documents DISABLE ROW LEVEL SECURITY", {}),
            ("ALTER TABLE documents NO FORCE ROW LEVEL SECURITY", {}),
            ("DROP POLICY documents_select_policy ON documents", {}),
            ("CREATE POLICY rag_app_bypass ON documents FOR ALL USING (true)", {}),
            ("DROP TRIGGER trg_documents_immutable_owner_and_visibility ON documents", {}),
            ("ALTER TABLE documents DISABLE TRIGGER trg_documents_immutable_owner_and_visibility", {}),
            ("TRUNCATE TABLE documents", {}),
            ("DELETE FROM users WHERE id = :uid", {"uid": target_uid}),
            ("SELECT version_num FROM alembic_version", {}),
            ("CREATE TABLE public.rogue_escalation_probe (id int)", {}),
        ]
        for sql_stmt, params in forbidden_statements:
            with pytest.raises(DBAPIError):
                await raw_session.execute(text(sql_stmt), params)
            await raw_session.rollback()
