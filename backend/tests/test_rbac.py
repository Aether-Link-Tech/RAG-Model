"""FR-004 / AL-21: role-based access control (UserRole enum, JWT role claim, role guard)."""
import uuid
from collections.abc import AsyncGenerator

import jwt
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import require_roles
from backend.app.core.config import settings
from backend.app.core.roles import UserRole
from backend.app.core.security import (
    TokenValidationError,
    create_access_token,
    decode_access_token,
)
from backend.app.models.user import User
from backend.tests.conftest import purge_test_users


def _bearer(user_id: uuid.UUID, role: UserRole | str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id, role=role)}"}


def _raw_token(**overrides) -> str:
    """Sign a token directly, bypassing create_access_token's role validation."""
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "user",
        "iat": 1_700_000_000,
        "exp": 4_000_000_000,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
    }
    payload.update(overrides)
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> AsyncGenerator[User, None]:
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=f"admin-{user_id.hex[:8]}@example.com",
        hashed_password="admin_hashed_password",
        role=UserRole.ADMIN.value,
        is_deleted=False,
    )
    db_session.add(user)
    await db_session.commit()
    yield user
    await purge_test_users(user_id)


# --- Unit tests: enum and JWT claim (no database) -------------------------------


def test_user_role_enum_matches_db_constraint_values():
    assert {r.value for r in UserRole} == {"admin", "user"}
    assert UserRole.ADMIN == "admin"


def test_access_token_carries_role_claim():
    uid = uuid.uuid4()
    payload = decode_access_token(create_access_token(uid, role=UserRole.ADMIN))
    assert payload["user_id"] == uid
    assert payload["role"] is UserRole.ADMIN

    default_payload = decode_access_token(create_access_token(uid))
    assert default_payload["role"] is UserRole.USER


def test_create_access_token_rejects_unknown_role():
    with pytest.raises(ValueError):
        create_access_token(uuid.uuid4(), role="superuser")


@pytest.mark.parametrize("role", [None, "superuser", "", 1])
def test_decode_rejects_missing_or_invalid_role_claim(role):
    with pytest.raises(TokenValidationError):
        decode_access_token(_raw_token(role=role))


@pytest.mark.asyncio
async def test_require_roles_needs_at_least_one_role():
    with pytest.raises(ValueError):
        require_roles()


@pytest.mark.asyncio
async def test_require_roles_guard_allows_and_denies_by_role():
    """Exercise the guard on a throwaway app, with get_current_user stubbed out."""
    from backend.app.api.deps import get_current_user

    app = FastAPI()
    caller = {"role": UserRole.USER}

    async def _fake_current_user() -> User:
        return User(id=uuid.uuid4(), email="x@example.com", hashed_password="x", role=caller["role"])

    app.dependency_overrides[get_current_user] = _fake_current_user

    @app.get("/admin-only")
    async def _admin_only(user: User = Depends(require_roles(UserRole.ADMIN))):
        return {"ok": True}

    @app.get("/any-role")
    async def _any(user: User = Depends(require_roles(UserRole.ADMIN, UserRole.USER))):
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/admin-only")).status_code == 403
        assert (await client.get("/any-role")).status_code == 200
        caller["role"] = UserRole.ADMIN
        assert (await client.get("/admin-only")).status_code == 200


# --- Integration tests: protected endpoints (PostgreSQL) -------------------------


@pytest.mark.asyncio
async def test_list_users_requires_admin(async_client, test_user: User, admin_user: User):
    unauth = await async_client.get("/api/v1/users")
    assert unauth.status_code == 401

    as_user = await async_client.get("/api/v1/users", headers=_bearer(test_user.id, test_user.role))
    assert as_user.status_code == 403

    as_admin = await async_client.get("/api/v1/users", headers=_bearer(admin_user.id, admin_user.role))
    assert as_admin.status_code == 200
    ids = {u["id"] for u in as_admin.json()}
    assert str(test_user.id) in ids and str(admin_user.id) in ids


@pytest.mark.asyncio
async def test_forged_admin_claim_is_rejected(async_client, test_user: User):
    """A regular user's token re-signed with role=admin must not grant admin access."""
    forged = _bearer(test_user.id, UserRole.ADMIN)
    res = await async_client.get("/api/v1/users", headers=forged)
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_admin_can_change_user_role_and_old_token_stops_working(
    async_client, test_user: User, admin_user: User
):
    admin_headers = _bearer(admin_user.id, admin_user.role)
    old_user_headers = _bearer(test_user.id, UserRole.USER)

    # Regular users cannot change roles.
    denied = await async_client.patch(
        f"/api/v1/users/{admin_user.id}/role", json={"role": "user"}, headers=old_user_headers
    )
    assert denied.status_code == 403

    promoted = await async_client.patch(
        f"/api/v1/users/{test_user.id}/role", json={"role": "admin"}, headers=admin_headers
    )
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin"

    # The token minted with the old role is now stale.
    stale = await async_client.get("/api/v1/users/me", headers=old_user_headers)
    assert stale.status_code == 401

    # A fresh token with the new role grants admin access.
    fresh = await async_client.get("/api/v1/users", headers=_bearer(test_user.id, UserRole.ADMIN))
    assert fresh.status_code == 200


@pytest.mark.asyncio
async def test_role_update_validation(async_client, test_user: User, admin_user: User):
    admin_headers = _bearer(admin_user.id, admin_user.role)

    self_change = await async_client.patch(
        f"/api/v1/users/{admin_user.id}/role", json={"role": "user"}, headers=admin_headers
    )
    assert self_change.status_code == 400

    bad_role = await async_client.patch(
        f"/api/v1/users/{test_user.id}/role", json={"role": "superuser"}, headers=admin_headers
    )
    assert bad_role.status_code == 422

    missing = await async_client.patch(
        f"/api/v1/users/{uuid.uuid4()}/role", json={"role": "admin"}, headers=admin_headers
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_collections_are_open_to_every_signed_in_user(async_client, test_user: User):
    """FR-012: collections are per-user, so any authenticated user may create one."""
    assert (await async_client.get("/api/v1/collections")).status_code == 401
    res = await async_client.post(
        "/api/v1/collections", json={"name": "Mine"}, headers=_bearer(test_user.id, test_user.role)
    )
    assert res.status_code == 201


# --- AL-123: roles + user_roles tables -------------------------------------------


@pytest.mark.asyncio
async def test_roles_table_is_seeded(async_client, admin_user: User):
    res = await async_client.get("/api/v1/admin/roles", headers=_bearer(admin_user.id, admin_user.role))
    assert res.status_code == 200
    assert {r["name"] for r in res.json()} == {"admin", "user", "guest"}


@pytest.mark.asyncio
async def test_user_roles_follow_users_role(db_session: AsyncSession, test_user: User):
    """The DB trigger keeps exactly one user_roles row per user, matching users.role."""
    from sqlalchemy import text

    async def _role_names() -> list[str]:
        rows = await db_session.execute(
            text(
                "SELECT r.name FROM user_roles ur JOIN roles r ON r.id = ur.role_id "
                "WHERE ur.user_id = :uid"
            ),
            {"uid": test_user.id},
        )
        return [r[0] for r in rows]

    assert await _role_names() == ["user"]

    test_user.role = UserRole.ADMIN.value
    await db_session.commit()
    assert await _role_names() == ["admin"]


@pytest.mark.asyncio
async def test_user_roles_rows_are_private_to_owner(
    db_session: AsyncSession, test_user: User, test_user_2: User
):
    """RLS: a regular user only sees their own user_roles row."""
    from sqlalchemy import select

    from backend.app.db.rls import configure_rls_context
    from backend.app.models.role import UserRoleAssignment

    own_id, other_id = test_user.id, test_user_2.id
    await db_session.rollback()
    await configure_rls_context(db_session, own_id, "user")
    rows = (await db_session.execute(select(UserRoleAssignment.user_id))).scalars().all()
    assert own_id in rows
    assert other_id not in rows


# --- AL-124 / AL-126: /admin/* routes are admin-only ------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v1/admin/users"),
        ("get", "/api/v1/admin/roles"),
        ("patch", "/api/v1/admin/users/{other}/role"),
    ],
)
async def test_admin_routes_reject_regular_users(
    async_client, test_user: User, admin_user: User, method: str, path: str
):
    url = path.format(other=admin_user.id)
    kwargs = {"json": {"role": "user"}} if method == "patch" else {}

    unauth = await getattr(async_client, method)(url, **kwargs)
    assert unauth.status_code == 401

    as_user = await getattr(async_client, method)(
        url, headers=_bearer(test_user.id, test_user.role), **kwargs
    )
    assert as_user.status_code == 403


@pytest.mark.asyncio
async def test_admin_routes_allow_admins(async_client, test_user: User, admin_user: User):
    headers = _bearer(admin_user.id, admin_user.role)

    listed = await async_client.get("/api/v1/admin/users", headers=headers)
    assert listed.status_code == 200
    assert str(test_user.id) in {u["id"] for u in listed.json()}

    promoted = await async_client.patch(
        f"/api/v1/admin/users/{test_user.id}/role", json={"role": "admin"}, headers=headers
    )
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin"


def test_every_admin_route_is_guarded():
    """The admin guard sits on the /admin router itself, so every route under it is covered."""
    from backend.app.api.v1 import admin

    assert admin.router.prefix == "/admin"
    assert admin.router.routes
    guards = [
        d.dependency
        for d in admin.router.dependencies
        if getattr(d.dependency, "__name__", "") == "_role_guard"
    ]
    assert len(guards) == 1
