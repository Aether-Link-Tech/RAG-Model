import re
import uuid
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_current_user
from backend.app.api.v1 import auth as auth_api
from backend.app.api.v1.auth import INVALID_CREDENTIALS
from backend.app.core.config import settings
from backend.app.core.crypto import email_index
from backend.app.core.security import create_access_token, create_refresh_token, hash_password
from backend.app.main import app
from backend.app.models.user import User
from backend.app.services.auth_state import failed_login_count
from backend.tests.conftest import _CleanupSessionLocal, purge_test_users

VALID_PASSWORD = "ValidPassword!123"


async def _set_user_status(user_id: uuid.UUID, status: str) -> None:
    """Direct SQL update as superuser to set user status (quarantine/active)."""
    async with _CleanupSessionLocal() as session:
        await session.execute(
            text("UPDATE users SET status = :status WHERE id = :user_id"),
            {"status": status, "user_id": user_id},
        )
        await session.commit()


@pytest_asyncio.fixture
async def verified_user(db_session: AsyncSession) -> User:
    """Create a verified test user with status='active'."""
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=f"quarantine-test-{user_id.hex[:8]}@example.com",
        hashed_password=hash_password(VALID_PASSWORD),
        role="user",
        status="active",
        is_verified=True,
        is_deleted=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    yield user

    await purge_test_users(user_id)


@pytest.mark.asyncio
async def test_quarantined_user_login_returns_same_response_as_wrong_password(
    async_client: httpx.AsyncClient, verified_user: User
):
    """1. Quarantined user login returns the same status and body as a wrong password,
    and does NOT count toward lockout."""
    # Obtain wrong password response on active user
    wrong_pw_res = await async_client.post(
        "/api/v1/auth/login",
        json={"email": verified_user.email, "password": "WrongPassword!999"},
    )
    assert wrong_pw_res.status_code == 401
    assert wrong_pw_res.json() == {"detail": INVALID_CREDENTIALS}

    failures_before = await failed_login_count(verified_user.email)

    # Quarantine user via direct SQL update
    await _set_user_status(verified_user.id, "quarantined")

    # Login with the correct password while quarantined
    quarantined_res = await async_client.post(
        "/api/v1/auth/login",
        json={"email": verified_user.email, "password": VALID_PASSWORD},
    )

    # Status code and body must match wrong password response exactly
    assert quarantined_res.status_code == wrong_pw_res.status_code == 401
    assert quarantined_res.json() == wrong_pw_res.json()

    # Quarantined login attempt with valid password must NOT increment lockout counter
    failures_after = await failed_login_count(verified_user.email)
    assert failures_after == failures_before


@pytest.mark.asyncio
async def test_access_token_issued_before_quarantine_is_rejected(
    async_client: httpx.AsyncClient, verified_user: User
):
    """2. An access token issued before quarantine is rejected on a protected route."""
    token = create_access_token(verified_user.id, role=verified_user.role)

    # Token works initially
    res_before = await async_client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert res_before.status_code == 200

    # Quarantine user
    await _set_user_status(verified_user.id, "quarantined")

    # Token must now be rejected immediately on protected route
    res_after = await async_client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert res_after.status_code == 401


@pytest.mark.asyncio
async def test_refresh_token_of_quarantined_user_is_rejected(
    async_client: httpx.AsyncClient, verified_user: User
):
    """3. A refresh token of a quarantined user is rejected."""
    refresh_tok = create_refresh_token(verified_user.id)

    # Quarantine user
    await _set_user_status(verified_user.id, "quarantined")

    res = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_tok},
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_quarantined_user_rejected_on_all_protected_routes(
    async_client: httpx.AsyncClient, verified_user: User
):
    """4. A quarantined user gets 401/403 on each protected route in the app.
    Routes are dynamically enumerated from the router, not hardcoded."""
    paths = app.openapi()["paths"]
    protected_routes: list[tuple[str, str]] = []
    for path, methods in paths.items():
        for method, details in methods.items():
            if method.lower() in ("get", "post", "put", "patch", "delete"):
                sec = details.get("security", [])
                if any("HTTPBearer" in s for s in sec):
                    protected_routes.append((method.upper(), path))

    assert len(protected_routes) > 0, "No protected routes discovered"

    token = create_access_token(verified_user.id, role=verified_user.role)
    await _set_user_status(verified_user.id, "quarantined")

    for method, path in protected_routes:
        # Replace path parameters (e.g. {user_id}, {document_id}, {collection_id}) with dummy UUID
        url = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
        res = await async_client.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        assert res.status_code in (401, 403), (
            f"Route {method} {url} returned {res.status_code}, expected 401 or 403"
        )


@pytest.mark.asyncio
async def test_active_user_is_still_allowed(
    async_client: httpx.AsyncClient, verified_user: User
):
    """5. An active user is still allowed (regression)."""
    # Login works
    login_res = await async_client.post(
        "/api/v1/auth/login",
        json={"email": verified_user.email, "password": VALID_PASSWORD},
    )
    assert login_res.status_code == 200
    tokens = login_res.json()
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    # Access protected route works
    me_res = await async_client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert me_res.status_code == 200
    assert me_res.json()["email"] == verified_user.email

    # Refresh works
    refresh_res = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert refresh_res.status_code == 200
    assert "access_token" in refresh_res.json()

    # Logout works
    logout_res = await async_client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"refresh_token": refresh_token},
    )
    assert logout_res.status_code == 200


@pytest.mark.asyncio
async def test_status_read_failure_fails_closed(
    async_client: httpx.AsyncClient, verified_user: User, monkeypatch
):
    """6. A status read failure fails closed (rejects login, get_current_user, and refresh)."""
    token = create_access_token(verified_user.id, role=verified_user.role)
    refresh_tok = create_refresh_token(verified_user.id)

    # Simulate status read failure by raising an exception when status property/attribute is accessed
    def _exploding_status(self):
        raise RuntimeError("Database read failure for user status")

    monkeypatch.setattr(User, "status", property(_exploding_status))

    # Protected route with access token fails closed (401)
    res_route = await async_client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert res_route.status_code == 401

    # Login fails closed (401)
    res_login = await async_client.post(
        "/api/v1/auth/login",
        json={"email": verified_user.email, "password": VALID_PASSWORD},
    )
    assert res_login.status_code == 401

    # Refresh fails closed (401)
    res_refresh = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_tok},
    )
    assert res_refresh.status_code == 401


@pytest.mark.asyncio
async def test_quarantined_login_matches_wrong_password_headers_and_redis_writes(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
    verified_user: User,
    fake_redis,
    monkeypatch,
):
    """7. Compare quarantined login vs wrong-password login on status code, body,
    all response headers (including X-Captcha-Required), password-hashing work,
    and Redis write count (0 writes for quarantined vs >0 for wrong password)."""
    q_user_id = uuid.uuid4()
    quarantined_user = User(
        id=q_user_id,
        email=f"quarantine-cmp-{q_user_id.hex[:8]}@example.com",
        hashed_password=hash_password(VALID_PASSWORD),
        role="user",
        status="active",
        is_verified=True,
        is_deleted=False,
    )
    db_session.add(quarantined_user)
    await db_session.commit()
    await _set_user_status(q_user_id, "quarantined")

    # Track Redis write commands (direct and pipelined)
    read_cmds = {"GET", "TTL", "PTTL", "EXISTS", "SCAN", "MGET"}
    redis_writes: list[str] = []
    orig_execute = fake_redis.execute_command
    orig_pipeline = fake_redis.pipeline

    async def tracked_execute(*args, **kwargs):
        cmd = str(args[0]).upper()
        if cmd not in read_cmds:
            redis_writes.append(cmd)
        return await orig_execute(*args, **kwargs)

    def tracked_pipeline(*args, **kwargs):
        pipe = orig_pipeline(*args, **kwargs)
        orig_pipe_execute = pipe.execute

        async def tracked_pipe_execute(*pargs, **pkwargs):
            for cmd_args, _ in pipe.command_stack:
                cmd = str(cmd_args[0]).upper()
                if cmd not in read_cmds:
                    redis_writes.append(cmd)
            return await orig_pipe_execute(*pargs, **pkwargs)

        pipe.execute = tracked_pipe_execute
        return pipe

    monkeypatch.setattr(fake_redis, "execute_command", tracked_execute)
    monkeypatch.setattr(fake_redis, "pipeline", tracked_pipeline)

    # Track verify_password invocations to confirm same work up to rejection
    verify_calls = 0
    real_verify_password = auth_api.verify_password

    def tracked_verify_password(plain: str, hashed: str) -> bool:
        nonlocal verify_calls
        verify_calls += 1
        return real_verify_password(plain, hashed)

    monkeypatch.setattr(auth_api, "verify_password", tracked_verify_password)

    try:
        # Case A: Below CAPTCHA threshold (failures == 0)
        redis_writes.clear()
        wrong_res = await async_client.post(
            "/api/v1/auth/login",
            json={"email": verified_user.email, "password": "WrongPassword!999"},
        )
        wrong_writes = list(redis_writes)

        redis_writes.clear()
        quar_res = await async_client.post(
            "/api/v1/auth/login",
            json={"email": quarantined_user.email, "password": VALID_PASSWORD},
        )
        quar_writes = list(redis_writes)

        assert quar_res.status_code == wrong_res.status_code == 401
        assert quar_res.json() == wrong_res.json() == {"detail": INVALID_CREDENTIALS}
        assert dict(quar_res.headers) == dict(wrong_res.headers)
        assert "x-captcha-required" not in quar_res.headers
        assert len(wrong_writes) > 0
        assert len(quar_writes) == 0

        # Case B: At CAPTCHA threshold (failures == LOGIN_CAPTCHA_AFTER_FAILURES)
        threshold = settings.LOGIN_CAPTCHA_AFTER_FAILURES
        await fake_redis.set(f"login_fail:{email_index(verified_user.email)}", str(threshold), ex=900)
        await fake_redis.set(f"login_fail:{email_index(quarantined_user.email)}", str(threshold), ex=900)

        redis_writes.clear()
        wrong_cap_res = await async_client.post(
            "/api/v1/auth/login",
            json={
                "email": verified_user.email,
                "password": "WrongPassword!999",
                "captcha_token": "test-captcha",
            },
        )
        wrong_cap_writes = list(redis_writes)

        redis_writes.clear()
        quar_cap_res = await async_client.post(
            "/api/v1/auth/login",
            json={
                "email": quarantined_user.email,
                "password": VALID_PASSWORD,
                "captcha_token": "test-captcha",
            },
        )
        quar_cap_writes = list(redis_writes)

        assert quar_cap_res.status_code == wrong_cap_res.status_code == 401
        assert quar_cap_res.json() == wrong_cap_res.json() == {"detail": INVALID_CREDENTIALS}
        assert dict(quar_cap_res.headers) == dict(wrong_cap_res.headers)
        assert (
            quar_cap_res.headers.get("x-captcha-required")
            == wrong_cap_res.headers.get("x-captcha-required")
            == "true"
        )
        assert len(wrong_cap_writes) > 0
        assert len(quar_cap_writes) == 0

        assert verify_calls == 4
    finally:
        await purge_test_users(q_user_id)


