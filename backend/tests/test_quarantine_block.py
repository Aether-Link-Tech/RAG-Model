import re
import uuid
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_current_user
from backend.app.api.v1.auth import INVALID_CREDENTIALS
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

