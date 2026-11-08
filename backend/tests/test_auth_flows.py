"""FR-001 register, FR-002 login lockout, FR-003 logout, refresh tokens, bcrypt (AL-115..AL-121, AL-135)."""
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1 import auth as auth_api
from backend.app.core.config import Settings, settings
from backend.app.core.roles import UserRole
from backend.app.core.security import (
    TokenValidationError,
    bcrypt_cost,
    create_access_token,
    create_email_verification_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from backend.app.models.user import User
from backend.app.services import captcha as captcha_service
from backend.tests.conftest import purge_test_users

STRONG_PASSWORD = "Str0ng!Passw0rd"


@pytest.fixture
def sent_emails(monkeypatch) -> list[tuple[str, str]]:
    """Capture verification emails instead of sending them."""
    sent: list[tuple[str, str]] = []

    async def _capture(to: str, token: str) -> None:
        sent.append((to, token))

    monkeypatch.setattr(auth_api, "send_verification_email", _capture)
    return sent


@pytest.fixture
def created_user_ids() -> list[uuid.UUID]:
    return []


@pytest_asyncio.fixture(autouse=True)
async def _purge_created(created_user_ids) -> AsyncGenerator[None, None]:
    yield
    await purge_test_users(*created_user_ids)


def _email() -> str:
    return f"auth-{uuid.uuid4().hex[:10]}@example.com"


async def _make_user(
    db_session: AsyncSession,
    created_user_ids: list[uuid.UUID],
    *,
    password: str = STRONG_PASSWORD,
    verified: bool = True,
    role: str = "user",
    hashed: str | None = None,
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=_email(),
        full_name="Test Person",
        hashed_password=hashed if hashed is not None else hash_password(password),
        role=role,
        is_verified=verified,
    )
    db_session.add(user)
    await db_session.commit()
    created_user_ids.append(user.id)
    return user


async def _login(client: httpx.AsyncClient, email: str, password: str, **extra) -> httpx.Response:
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password, **extra})


def _enable_captcha(monkeypatch, *, valid_token: str = "good-token") -> list[dict]:
    """Turn reCAPTCHA on and answer Google's siteverify with a mock transport."""
    calls: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        calls.append(form)
        return httpx.Response(200, json={"success": form.get("response") == valid_token})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(settings, "RECAPTCHA_ENABLED", True)
    monkeypatch.setattr(settings, "RECAPTCHA_SECRET_KEY", "test-secret")
    monkeypatch.setattr(
        captcha_service.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(_handler), **kw),
    )
    return calls


# --- AL-116 / AL-135: bcrypt ------------------------------------------------------------


def test_bcrypt_hash_is_not_the_password_and_verifies():
    hashed = hash_password(STRONG_PASSWORD)
    assert hashed != STRONG_PASSWORD
    assert STRONG_PASSWORD not in hashed
    assert hashed.startswith("$2b$")
    assert verify_password(STRONG_PASSWORD, hashed)
    assert not verify_password("Wr0ng!Password", hashed)
    # Salted: the same password never hashes the same way twice.
    assert hash_password(STRONG_PASSWORD) != hashed


def test_bcrypt_cost_factor_is_at_least_12():
    assert settings.BCRYPT_ROUNDS >= 12
    hashed = hash_password(STRONG_PASSWORD)
    assert bcrypt_cost(hashed) == settings.BCRYPT_ROUNDS

    start = time.perf_counter()
    hash_password(STRONG_PASSWORD)
    elapsed = time.perf_counter() - start
    # Cost 12 is ~250 ms on typical hardware; a much faster hash means a low cost.
    assert elapsed > 0.05, f"bcrypt hash took only {elapsed:.3f}s"


def test_settings_reject_bcrypt_cost_below_12():
    with pytest.raises(ValueError, match="BCRYPT_ROUNDS"):
        Settings(BCRYPT_ROUNDS=10)


def test_passwords_longer_than_72_bytes_are_fully_checked():
    base = "Aa1!" + "x" * 80
    hashed = hash_password(base + "1")
    assert verify_password(base + "1", hashed)
    assert not verify_password(base + "2", hashed)


def test_legacy_pbkdf2_hashes_still_verify_and_need_rehash():
    import hashlib

    salt = b"0123456789abcdef"
    derived = hashlib.pbkdf2_hmac("sha256", STRONG_PASSWORD.encode(), salt, 120_000)
    legacy = f"{salt.hex()}:{derived.hex()}"
    assert verify_password(STRONG_PASSWORD, legacy)
    assert not verify_password("Wr0ng!Password", legacy)
    assert password_needs_rehash(legacy)
    assert not password_needs_rehash(hash_password(STRONG_PASSWORD))
    assert not verify_password(STRONG_PASSWORD, "not-a-hash")


@pytest.mark.asyncio
async def test_login_upgrades_legacy_hash_to_bcrypt(async_client, db_session, created_user_ids):
    import hashlib

    salt = b"fedcba9876543210"
    derived = hashlib.pbkdf2_hmac("sha256", STRONG_PASSWORD.encode(), salt, 120_000)
    user = await _make_user(db_session, created_user_ids, hashed=f"{salt.hex()}:{derived.hex()}")

    res = await _login(async_client, user.email, STRONG_PASSWORD)
    assert res.status_code == 200

    await db_session.refresh(user)
    assert bcrypt_cost(user.hashed_password) == settings.BCRYPT_ROUNDS
    assert verify_password(STRONG_PASSWORD, user.hashed_password)


# --- AL-115: register ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_creates_pending_account_and_sends_email(
    async_client, db_session, sent_emails, created_user_ids
):
    email = _email()
    res = await async_client.post(
        "/api/v1/auth/register",
        json={"full_name": "  Aya Khan ", "email": email.upper(), "password": STRONG_PASSWORD},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["status"] == "pending_verification"
    user_id = uuid.UUID(body["user_id"])
    created_user_ids.append(user_id)

    user = await db_session.get(User, user_id)
    assert user.email == email  # normalized to lower case
    assert user.full_name == "Aya Khan"
    assert user.role == "user"
    assert user.is_verified is False
    assert user.hashed_password != STRONG_PASSWORD
    assert verify_password(STRONG_PASSWORD, user.hashed_password)

    assert [to for to, _ in sent_emails] == [email]


@pytest.mark.asyncio
async def test_register_duplicate_email_returns_409(async_client, sent_emails, created_user_ids):
    email = _email()
    payload = {"full_name": "First", "email": email, "password": STRONG_PASSWORD}
    first = await async_client.post("/api/v1/auth/register", json=payload)
    assert first.status_code == 201
    created_user_ids.append(uuid.UUID(first.json()["user_id"]))

    again = await async_client.post(
        "/api/v1/auth/register", json={**payload, "email": email.upper()}
    )
    assert again.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "password",
    [
        "Sh0rt!",  # too short
        "alllower1!",  # no uppercase
        "ALLUPPER1!",  # no lowercase
        "NoDigits!!",  # no digit
        "NoSpecial11",  # no special character
    ],
)
async def test_register_rejects_weak_passwords(async_client, password):
    res = await async_client.post(
        "/api/v1/auth/register",
        json={"full_name": "Weak", "email": _email(), "password": password},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("email", ["plainaddress", "no-at.example.com", "a@b", "a b@example.com", "a@@example.com"])
async def test_register_rejects_bad_email(async_client, email):
    res = await async_client.post(
        "/api/v1/auth/register",
        json={"full_name": "Bad Email", "email": email, "password": STRONG_PASSWORD},
    )
    assert res.status_code == 422


# --- AL-118: captcha ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_requires_valid_captcha(async_client, monkeypatch, sent_emails, created_user_ids):
    calls = _enable_captcha(monkeypatch)
    payload = {"full_name": "Captcha", "email": _email(), "password": STRONG_PASSWORD}

    missing = await async_client.post("/api/v1/auth/register", json=payload)
    assert missing.status_code == 400

    wrong = await async_client.post(
        "/api/v1/auth/register", json={**payload, "captcha_token": "bad-token"}
    )
    assert wrong.status_code == 400
    assert sent_emails == []

    ok = await async_client.post(
        "/api/v1/auth/register", json={**payload, "captcha_token": "good-token"}
    )
    assert ok.status_code == 201
    created_user_ids.append(uuid.UUID(ok.json()["user_id"]))

    # The secret is sent server-side only, together with the user's token.
    assert calls[-1]["secret"] == "test-secret"
    assert calls[-1]["response"] == "good-token"


@pytest.mark.asyncio
async def test_captcha_fails_closed_when_google_is_unreachable(monkeypatch):
    monkeypatch.setattr(settings, "RECAPTCHA_ENABLED", True)
    monkeypatch.setattr(settings, "RECAPTCHA_SECRET_KEY", "test-secret")

    def _boom(request):
        raise httpx.ConnectError("down")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        captcha_service.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(_boom), **kw),
    )
    assert await captcha_service.verify_captcha("any") is False


@pytest.mark.asyncio
async def test_captcha_without_secret_is_refused_outside_dev(monkeypatch):
    monkeypatch.setattr(settings, "RECAPTCHA_ENABLED", True)
    monkeypatch.setattr(settings, "RECAPTCHA_SECRET_KEY", "")
    monkeypatch.setattr(settings, "ENV", "production")
    assert await captcha_service.verify_captcha("any") is False


# --- AL-117: email verification ---------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_email_activates_account_and_allows_login(
    async_client, db_session, sent_emails, created_user_ids
):
    email = _email()
    reg = await async_client.post(
        "/api/v1/auth/register",
        json={"full_name": "Verify Me", "email": email, "password": STRONG_PASSWORD},
    )
    user_id = uuid.UUID(reg.json()["user_id"])
    created_user_ids.append(user_id)

    before = await _login(async_client, email, STRONG_PASSWORD)
    assert before.status_code == 403

    (_, token), = sent_emails
    verified = await async_client.get("/api/v1/auth/verify-email", params={"token": token})
    assert verified.status_code == 200
    assert verified.json()["status"] == "verified"

    # Using the link twice is harmless.
    again = await async_client.get("/api/v1/auth/verify-email", params={"token": token})
    assert again.status_code == 200

    after = await _login(async_client, email, STRONG_PASSWORD)
    assert after.status_code == 200
    assert after.json()["user"]["is_verified"] is True


@pytest.mark.asyncio
async def test_verify_email_rejects_bad_tokens(async_client, db_session, created_user_ids):
    user = await _make_user(db_session, created_user_ids, verified=False)

    expired = create_email_verification_token(user.id, user.email, expires_delta=timedelta(seconds=-1))
    wrong_email = create_email_verification_token(user.id, "someone-else@example.com")
    access_token = create_access_token(user.id)
    for token in (expired, wrong_email, access_token, "garbage"):
        res = await async_client.get("/api/v1/auth/verify-email", params={"token": token})
        assert res.status_code == 400, token

    await db_session.refresh(user)
    assert user.is_verified is False


def test_verification_link_expires_in_24_hours():
    from backend.app.core.security import decode_email_verification_token

    claims = decode_email_verification_token(create_email_verification_token(uuid.uuid4(), "a@example.com"))
    assert claims["exp"] - claims["iat"] == 24 * 3600


@pytest.mark.asyncio
async def test_resend_verification_is_generic_and_rate_limited(
    async_client, db_session, sent_emails, created_user_ids
):
    pending = await _make_user(db_session, created_user_ids, verified=False)
    active = await _make_user(db_session, created_user_ids, verified=True)

    responses = []
    for email in (pending.email, pending.email, active.email, _email()):
        res = await async_client.post("/api/v1/auth/resend-verification", json={"email": email})
        assert res.status_code == 202
        responses.append(res.json())

    # Same answer for every case, so it cannot reveal which emails exist.
    assert all(r == responses[0] for r in responses)
    # One email only: the second request falls inside the cooldown.
    assert [to for to, _ in sent_emails] == [pending.email]


@pytest.mark.asyncio
async def test_verification_email_falls_back_to_log_in_dev(caplog):
    import logging

    from backend.app.services.email import send_verification_email

    with caplog.at_level(logging.INFO, logger="backend.app.services.email"):
        await send_verification_email("dev@example.com", "tok123")
    assert "verify-email?token=tok123" in caplog.text


# --- AL-121: failed-attempt counter and lockout -----------------------------------------


@pytest.mark.asyncio
async def test_login_requires_captcha_after_three_failures(
    async_client, db_session, created_user_ids, fake_redis, monkeypatch
):
    user = await _make_user(db_session, created_user_ids)
    for _ in range(3):
        assert (await _login(async_client, user.email, "Wr0ng!Password")).status_code == 401
    assert int(await fake_redis.get(f"login_fail:{user.email}")) == 3

    no_captcha = await _login(async_client, user.email, STRONG_PASSWORD)
    assert no_captcha.status_code == 400
    assert "captcha" in no_captcha.json()["detail"].lower()

    _enable_captcha(monkeypatch)
    bad = await _login(async_client, user.email, STRONG_PASSWORD, captcha_token="bad-token")
    assert bad.status_code == 400

    ok = await _login(async_client, user.email, STRONG_PASSWORD, captcha_token="good-token")
    assert ok.status_code == 200
    # Success resets the counter.
    assert await fake_redis.get(f"login_fail:{user.email}") is None


@pytest.mark.asyncio
async def test_login_locks_for_15_minutes_after_five_failures(
    async_client, db_session, created_user_ids, fake_redis
):
    user = await _make_user(db_session, created_user_ids)
    statuses = []
    for _ in range(5):
        res = await _login(async_client, user.email, "Wr0ng!Password", captcha_token="x")
        statuses.append(res.status_code)
    assert statuses == [401, 401, 401, 401, 423]

    key = f"login_fail:{user.email}"
    ttl = await fake_redis.ttl(key)
    assert 14 * 60 < ttl <= 15 * 60

    # Even the right password is refused while locked.
    locked = await _login(async_client, user.email, STRONG_PASSWORD, captcha_token="x")
    assert locked.status_code == 423
    assert int(locked.headers["Retry-After"]) > 0

    # When the window expires, the account works again.
    await fake_redis.delete(key)
    assert (await _login(async_client, user.email, STRONG_PASSWORD)).status_code == 200


@pytest.mark.asyncio
async def test_unknown_email_gets_same_401_and_is_counted(async_client, fake_redis):
    email = _email()
    res = await _login(async_client, email, STRONG_PASSWORD)
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password"
    assert int(await fake_redis.get(f"login_fail:{email}")) == 1


@pytest.mark.asyncio
async def test_login_fails_closed_when_redis_is_down(async_client, db_session, created_user_ids):
    import fakeredis

    from backend.app.core.redis import set_redis_client

    user = await _make_user(db_session, created_user_ids)
    server = fakeredis.FakeServer()
    server.connected = False
    set_redis_client(fakeredis.FakeAsyncRedis(server=server, decode_responses=True))

    res = await _login(async_client, user.email, STRONG_PASSWORD)
    assert res.status_code == 503


# --- Refresh tokens ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_returns_access_and_refresh_tokens(async_client, db_session, created_user_ids):
    user = await _make_user(db_session, created_user_ids)
    body = (await _login(async_client, user.email, STRONG_PASSWORD)).json()
    assert decode_access_token(body["access_token"])["user_id"] == user.id
    assert decode_refresh_token(body["refresh_token"])["user_id"] == user.id

    # Token types cannot be swapped.
    with pytest.raises(TokenValidationError):
        decode_access_token(body["refresh_token"])
    with pytest.raises(TokenValidationError):
        decode_refresh_token(body["access_token"])
    me = await async_client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {body['refresh_token']}"}
    )
    assert me.status_code == 401


@pytest.mark.asyncio
async def test_refresh_rotates_tokens_and_rereads_role(async_client, db_session, created_user_ids):
    user = await _make_user(db_session, created_user_ids)
    first = (await _login(async_client, user.email, STRONG_PASSWORD)).json()

    # Role change in the DB takes effect at the next refresh.
    user.role = UserRole.ADMIN.value
    await db_session.commit()

    refreshed = await async_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert refreshed.status_code == 200
    new = refreshed.json()
    assert decode_access_token(new["access_token"])["role"] is UserRole.ADMIN
    assert new["refresh_token"] != first["refresh_token"]

    # The used refresh token is revoked (rotation).
    reused = await async_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert reused.status_code == 401


@pytest.mark.asyncio
async def test_refresh_rejects_bad_tokens(async_client, db_session, created_user_ids):
    user = await _make_user(db_session, created_user_ids)
    deleted = await _make_user(db_session, created_user_ids)
    deleted.is_deleted = True
    await db_session.commit()

    for token in (
        create_access_token(user.id),
        create_refresh_token(user.id, expires_delta=timedelta(seconds=-1)),
        create_refresh_token(deleted.id),
        "garbage",
    ):
        res = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token})
        assert res.status_code == 401


# --- FR-003 / AL-20: logout --------------------------------------------------------------


@pytest.mark.asyncio
async def test_logout_revokes_access_and_refresh_tokens(async_client, db_session, created_user_ids):
    user = await _make_user(db_session, created_user_ids)
    tokens = (await _login(async_client, user.email, STRONG_PASSWORD)).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    assert (await async_client.get("/api/v1/users/me", headers=headers)).status_code == 200

    out = await async_client.post(
        "/api/v1/auth/logout", headers=headers, json={"refresh_token": tokens["refresh_token"]}
    )
    assert out.status_code == 200

    after = await async_client.get("/api/v1/users/me", headers=headers)
    assert after.status_code == 401
    refresh = await async_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refresh.status_code == 401

    # A fresh login still works.
    assert (await _login(async_client, user.email, STRONG_PASSWORD)).status_code == 200


@pytest.mark.asyncio
async def test_logout_blacklist_entry_expires_with_the_token(
    async_client, db_session, created_user_ids, fake_redis
):
    user = await _make_user(db_session, created_user_ids)
    token = create_access_token(user.id, expires_delta=timedelta(minutes=10))
    res = await async_client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200

    keys = [k async for k in fake_redis.scan_iter("auth:blacklist:*")]
    assert len(keys) == 1
    assert token not in keys[0]  # only a digest is stored
    assert 9 * 60 < await fake_redis.ttl(keys[0]) <= 10 * 60


@pytest.mark.asyncio
async def test_logout_does_not_revoke_someone_elses_refresh_token(
    async_client, db_session, created_user_ids
):
    alice = await _make_user(db_session, created_user_ids)
    bob = await _make_user(db_session, created_user_ids)
    bob_refresh = create_refresh_token(bob.id)

    res = await async_client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {create_access_token(alice.id)}"},
        json={"refresh_token": bob_refresh},
    )
    assert res.status_code == 200
    still_ok = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": bob_refresh})
    assert still_ok.status_code == 200


@pytest.mark.asyncio
async def test_logout_requires_authentication(async_client):
    assert (await async_client.post("/api/v1/auth/logout")).status_code == 401


@pytest.mark.asyncio
async def test_protected_routes_fail_closed_when_blacklist_is_unavailable(
    async_client, test_user: User
):
    import fakeredis

    from backend.app.core.redis import set_redis_client

    server = fakeredis.FakeServer()
    server.connected = False
    set_redis_client(fakeredis.FakeAsyncRedis(server=server, decode_responses=True))

    res = await async_client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {create_access_token(test_user.id)}"},
    )
    assert res.status_code == 503


# --- Admin-created accounts ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_users_is_admin_only_and_creates_verified_accounts(
    async_client, db_session, created_user_ids
):
    admin = await _make_user(db_session, created_user_ids, role="admin")
    regular = await _make_user(db_session, created_user_ids)
    payload = {"email": _email(), "password": STRONG_PASSWORD, "full_name": "Made By Admin"}

    assert (await async_client.post("/api/v1/users", json=payload)).status_code == 401
    as_user = await async_client.post(
        "/api/v1/users",
        json=payload,
        headers={"Authorization": f"Bearer {create_access_token(regular.id)}"},
    )
    assert as_user.status_code == 403

    as_admin = await async_client.post(
        "/api/v1/users",
        json=payload,
        headers={"Authorization": f"Bearer {create_access_token(admin.id, role='admin')}"},
    )
    assert as_admin.status_code == 201
    created_user_ids.append(uuid.UUID(as_admin.json()["id"]))
    assert as_admin.json()["is_verified"] is True
    assert as_admin.json()["role"] == "user"
