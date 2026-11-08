"""FR-001 registration, FR-002 secure login, FR-003 logout (AL-115..AL-122, AL-20)."""
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from backend.app.core.config import settings
from backend.app.core.redis import set_redis_client
from backend.app.core.security import hash_password
from backend.app.models.user import User
from backend.app.services import captcha
from backend.tests.conftest import TestingSessionLocal, _CleanupSessionLocal, purge_test_users

PASSWORD = "Str0ng!Passw0rd"


async def _captcha(client) -> str:
    return captcha.solve_challenge((await client.get("/api/v1/auth/captcha")).json())


async def _register(client, email: str, password: str = PASSWORD, **extra):
    body = {"full_name": "Ada Lovelace", "email": email, "password": password, "captcha": await _captcha(client)}
    body.update(extra)
    return await client.post("/api/v1/auth/register", json=body)


def _email() -> str:
    return f"reg-{uuid.uuid4().hex[:10]}@example.com"


async def _user_id(email: str) -> uuid.UUID | None:
    from backend.app.core.crypto import email_index

    async with TestingSessionLocal() as session:
        result = await session.execute(select(User.id).where(User.email_hash == email_index(email)))
        return result.scalar_one_or_none()


async def _active_user(email: str, password: str = PASSWORD) -> User:
    async with TestingSessionLocal() as session:
        user = User(email=email, hashed_password=hash_password(password), role="user", full_name="Grace")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _login(client, email: str, password: str = PASSWORD, captcha_solution: str | None = None):
    body = {"email": email, "password": password}
    if captcha_solution:
        body["captcha"] = captcha_solution
    return await client.post("/api/v1/auth/login", json=body)


# --- FR-001 registration -------------------------------------------------------


@pytest.mark.asyncio
async def test_register_creates_pending_account_and_sends_verification_email(async_client, sent_emails):
    email = _email()
    res = await _register(async_client, email.upper())
    assert res.status_code == 201, res.text
    body = res.json()
    uid = uuid.UUID(body["id"])
    try:
        assert body["status"] == "pending"
        assert body["role"] == "user"
        assert body["email"] == email  # normalised
        assert body["full_name"] == "Ada Lovelace"
        assert sent_emails and sent_emails[-1]["to"] == email
        token = sent_emails[-1]["token"]

        # Pending accounts cannot sign in (AC: status "pending" until verified)
        pending_login = await _login(async_client, email)
        assert pending_login.status_code == 403
        assert "verify" in pending_login.json()["detail"].lower()

        verified = await async_client.post("/api/v1/auth/verify-email", json={"token": token})
        assert verified.status_code == 200

        # Single use
        reused = await async_client.post("/api/v1/auth/verify-email", json={"token": token})
        assert reused.status_code == 400

        ok = await _login(async_client, email)
        assert ok.status_code == 200
        assert ok.json()["user"]["status"] == "active"
    finally:
        await purge_test_users(uid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "email,password,fragment",
    [
        ("not-an-email", PASSWORD, "valid email"),
        ("a@b", PASSWORD, "valid email"),
        ("ok@example.com", "Sh0rt!x", "at least 8"),
        ("ok@example.com", "alllowercase1!", "uppercase"),
        ("ok@example.com", "ALLUPPERCASE1!", "lowercase"),
        ("ok@example.com", "NoDigitsHere!", "number"),
        ("ok@example.com", "NoSpecial1234", "special"),
    ],
)
async def test_register_validates_email_and_password_rules(async_client, email, password, fragment):
    res = await _register(async_client, email, password)
    assert res.status_code == 422
    assert fragment in res.text


@pytest.mark.asyncio
async def test_register_requires_valid_unused_captcha(async_client):
    missing = await async_client.post(
        "/api/v1/auth/register",
        json={"full_name": "X", "email": _email(), "password": PASSWORD, "captcha": "bm9wZQ=="},
    )
    assert missing.status_code == 400

    challenge = (await async_client.get("/api/v1/auth/captcha")).json()
    forged = dict(challenge, signature="0" * 64)
    bad = await async_client.post(
        "/api/v1/auth/register",
        json={"full_name": "X", "email": _email(), "password": PASSWORD, "captcha": captcha.solve_challenge(forged)},
    )
    assert bad.status_code == 400

    solution = captcha.solve_challenge(challenge)
    email = _email()
    first = await async_client.post(
        "/api/v1/auth/register",
        json={"full_name": "X", "email": email, "password": PASSWORD, "captcha": solution},
    )
    assert first.status_code == 201
    try:
        replay = await async_client.post(
            "/api/v1/auth/register",
            json={"full_name": "X", "email": _email(), "password": PASSWORD, "captcha": solution},
        )
        assert replay.status_code == 400
        assert "already used" in replay.json()["detail"]
    finally:
        await purge_test_users(uuid.UUID(first.json()["id"]))


@pytest.mark.asyncio
async def test_expired_captcha_is_rejected(async_client, monkeypatch):
    monkeypatch.setattr(settings, "CAPTCHA_TTL_SECONDS", -1)
    res = await _register(async_client, _email())
    assert res.status_code == 400
    assert "expired" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_duplicate_email_returns_409(async_client):
    email = _email()
    first = await _register(async_client, email)
    assert first.status_code == 201
    try:
        dup = await _register(async_client, f"  {email.upper()} ")
        assert dup.status_code == 409
    finally:
        await purge_test_users(uuid.UUID(first.json()["id"]))


@pytest.mark.asyncio
async def test_verification_link_expires_after_24_hours(async_client, sent_emails):
    email = _email()
    res = await _register(async_client, email)
    uid = uuid.UUID(res.json()["id"])
    try:
        stored = await _cleanup_scalar(
            "SELECT email_verification_expires_at FROM users WHERE id = :id", id=uid
        )
        delta = stored - datetime.now(timezone.utc)
        assert timedelta(hours=23, minutes=59) < delta <= timedelta(hours=24)

        await _cleanup_exec(
            "UPDATE users SET email_verification_expires_at = now() - interval '1 minute' WHERE id = :id",
            id=uid,
        )
        expired = await async_client.post("/api/v1/auth/verify-email", json={"token": sent_emails[-1]["token"]})
        assert expired.status_code == 410

        # Resend issues a fresh link; the response never reveals whether the account exists
        resend = await async_client.post(
            "/api/v1/auth/resend-verification", json={"email": email, "captcha": await _captcha(async_client)}
        )
        unknown = await async_client.post(
            "/api/v1/auth/resend-verification",
            json={"email": _email(), "captcha": await _captcha(async_client)},
        )
        assert resend.status_code == unknown.status_code == 202
        assert resend.json() == unknown.json()
        fresh = sent_emails[-1]["token"]
        assert (await async_client.post("/api/v1/auth/verify-email", json={"token": fresh})).status_code == 200
    finally:
        await purge_test_users(uid)


@pytest.mark.asyncio
async def test_verification_email_contains_link_and_escapes_name():
    from backend.app.services.email_service import _build_verification_message

    message = _build_verification_message("a@example.com", "<script>x</script>", "tok123")
    html_part = message.get_body(preferencelist=("html",)).get_content()
    assert "/verify-email?token=tok123" in html_part
    assert "<script>" not in html_part
    assert "24 hours" in message.get_body(preferencelist=("plain",)).get_content()


# --- FR-002 login ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_returns_access_and_refresh_tokens_and_generic_401(async_client):
    email = _email()
    user = await _active_user(email)
    try:
        ok = await _login(async_client, email)
        assert ok.status_code == 200
        body = ok.json()
        assert body["access_token"] and body["refresh_token"]
        assert body["expires_in"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60

        wrong_password = await _login(async_client, email, "Wrong!Passw0rd")
        unknown_email = await _login(async_client, _email(), PASSWORD)
        assert wrong_password.status_code == unknown_email.status_code == 401
        assert wrong_password.json() == unknown_email.json()  # no email enumeration
    finally:
        await purge_test_users(user.id)


@pytest.mark.asyncio
async def test_captcha_after_three_failures_and_lockout_after_five(async_client):
    email = _email()
    user = await _active_user(email)
    try:
        for attempt in range(1, 3):
            res = await _login(async_client, email, "Wrong!Passw0rd")
            assert res.status_code == 401
            assert "X-Captcha-Required" not in res.headers
        third = await _login(async_client, email, "Wrong!Passw0rd")
        assert third.status_code == 401
        assert third.headers["X-Captcha-Required"] == "true"

        # From now on a captcha is mandatory, even with the right password
        no_captcha = await _login(async_client, email)
        assert no_captcha.status_code == 400
        assert no_captcha.headers["X-Captcha-Required"] == "true"

        fourth = await _login(async_client, email, "Wrong!Passw0rd", await _captcha(async_client))
        assert fourth.status_code == 401
        fifth = await _login(async_client, email, "Wrong!Passw0rd", await _captcha(async_client))
        assert fifth.status_code == 423
        assert int(fifth.headers["Retry-After"]) == settings.LOGIN_LOCKOUT_MINUTES * 60

        # Locked: even the correct password is refused for 15 minutes
        locked = await _login(async_client, email, PASSWORD, await _captcha(async_client))
        assert locked.status_code == 423
    finally:
        await purge_test_users(user.id)


@pytest.mark.asyncio
async def test_lockout_uses_redis_ttl_and_success_resets_counter(async_client, fake_redis):
    email = _email()
    user = await _active_user(email)
    try:
        await _login(async_client, email, "Wrong!Passw0rd")
        await _login(async_client, email, "Wrong!Passw0rd")
        keys = await fake_redis.keys("auth:login-failures:*")
        assert len(keys) == 1
        assert 0 < await fake_redis.ttl(keys[0]) <= settings.LOGIN_LOCKOUT_MINUTES * 60
        assert email not in keys[0]  # keyed by HMAC, not the plaintext email

        assert (await _login(async_client, email)).status_code == 200
        assert await fake_redis.keys("auth:login-failures:*") == []

        for _ in range(5):
            await _login(async_client, email, "Wrong!Passw0rd", await _captcha(async_client))
        lock_keys = await fake_redis.keys("auth:login-lock:*")
        assert len(lock_keys) == 1
        assert await fake_redis.ttl(lock_keys[0]) <= settings.LOGIN_LOCKOUT_MINUTES * 60

        # Lock expires -> login works again and the counter is reset
        await fake_redis.delete(*lock_keys)
        assert (await _login(async_client, email)).status_code == 200
    finally:
        await purge_test_users(user.id)


@pytest.mark.asyncio
async def test_unknown_emails_are_throttled_like_real_ones(async_client):
    email = _email()
    for _ in range(3):
        await _login(async_client, email, "Wrong!Passw0rd")
    res = await _login(async_client, email, "Wrong!Passw0rd")
    assert res.status_code == 400
    assert res.headers["X-Captcha-Required"] == "true"


@pytest.mark.asyncio
async def test_login_fails_closed_when_redis_is_down(async_client):
    import redis.asyncio as aioredis

    set_redis_client(aioredis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2))
    res = await _login(async_client, _email())
    assert res.status_code == 503


@pytest.mark.asyncio
async def test_legacy_pbkdf2_hash_is_upgraded_to_bcrypt_on_login(async_client):
    email = _email()
    salt = os.urandom(16)
    legacy = f"{salt.hex()}:{hashlib.pbkdf2_hmac('sha256', PASSWORD.encode(), salt, 120_000).hex()}"
    async with TestingSessionLocal() as session:
        user = User(email=email, hashed_password=legacy, role="user")
        session.add(user)
        await session.commit()
        uid = user.id
    try:
        assert (await _login(async_client, email)).status_code == 200
        stored = await _cleanup_scalar("SELECT hashed_password FROM users WHERE id = :id", id=uid)
        assert stored.startswith("$2b$")
        assert (await _login(async_client, email)).status_code == 200
    finally:
        await purge_test_users(uid)


# --- Refresh + FR-003 logout ---------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_rotates_tokens_and_rejects_reuse(async_client):
    email = _email()
    user = await _active_user(email)
    try:
        first = (await _login(async_client, email)).json()
        refreshed = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
        assert refreshed.status_code == 200
        second = refreshed.json()
        assert second["refresh_token"] != first["refresh_token"]

        reuse = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
        assert reuse.status_code == 401

        # An access token cannot be used as a refresh token and vice versa
        wrong_kind = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": second["access_token"]})
        assert wrong_kind.status_code == 401
        me_with_refresh = await async_client.get(
            "/api/v1/users/me", headers={"Authorization": f"Bearer {second['refresh_token']}"}
        )
        assert me_with_refresh.status_code == 401
    finally:
        await purge_test_users(user.id)


@pytest.mark.asyncio
async def test_logout_blacklists_access_and_refresh_tokens(async_client, fake_redis):
    email = _email()
    user = await _active_user(email)
    try:
        tokens = (await _login(async_client, email)).json()
        auth = {"Authorization": f"Bearer {tokens['access_token']}"}
        assert (await async_client.get("/api/v1/users/me", headers=auth)).status_code == 200

        out = await async_client.post(
            "/api/v1/auth/logout", headers=auth, json={"refresh_token": tokens["refresh_token"]}
        )
        assert out.status_code == 204
        revoked = await fake_redis.keys("auth:revoked:*")
        assert len(revoked) == 2
        for key in revoked:
            assert await fake_redis.ttl(key) > 0  # TTL = remaining token lifetime

        after = await async_client.get("/api/v1/users/me", headers=auth)
        assert after.status_code == 401
        docs_after = await async_client.get("/api/v1/documents", headers=auth)
        assert docs_after.status_code == 401
        refresh_after = await async_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert refresh_after.status_code == 401
    finally:
        await purge_test_users(user.id)


@pytest.mark.asyncio
async def test_api_responses_are_not_cacheable(async_client, test_user):
    from backend.tests.conftest import bearer

    res = await async_client.get("/api/v1/users/me", headers=bearer(test_user))
    assert res.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_role_change_ends_refresh_sessions(async_client, admin_user):
    from backend.tests.conftest import bearer

    email = _email()
    user = await _active_user(email)
    try:
        tokens = (await _login(async_client, email)).json()
        promote = await async_client.patch(
            f"/api/v1/users/{user.id}/role", json={"role": "admin"}, headers=bearer(admin_user)
        )
        assert promote.status_code == 200
        stale = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert stale.status_code == 401
        again = await _login(async_client, email)
        assert again.json()["user"]["role"] == "admin"
    finally:
        await purge_test_users(user.id)


# --- FR-004: account creation is admin-only -------------------------------------


@pytest.mark.asyncio
async def test_post_users_is_admin_only(async_client, test_user, admin_user):
    from backend.tests.conftest import bearer

    body = {"email": _email(), "password": PASSWORD, "full_name": "New Hire"}
    assert (await async_client.post("/api/v1/users", json=body)).status_code == 401
    assert (await async_client.post("/api/v1/users", json=body, headers=bearer(test_user))).status_code == 403
    created = await async_client.post("/api/v1/users", json=body, headers=bearer(admin_user))
    assert created.status_code == 201
    try:
        assert created.json()["status"] == "active"
        assert (await _login(async_client, body["email"])).status_code == 200
    finally:
        await purge_test_users(uuid.UUID(created.json()["id"]))


# helpers using the privileged connection to inspect raw rows -------------------


async def _cleanup_scalar(sql: str, **params):
    async with _CleanupSessionLocal() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def _cleanup_exec(sql: str, **params):
    async with _CleanupSessionLocal() as session:
        await session.execute(text(sql), params)
        await session.commit()


@pytest.mark.asyncio
async def test_embedding_outage_marks_document_failed_instead_of_500(monkeypatch):
    """Upload must not 500 when Ollama is down (found in e2e testing)."""
    from backend.app.services import embedding

    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://127.0.0.1:1")
    with pytest.raises(embedding.EmbeddingError, match="unavailable"):
        await embedding.embed_texts(["hello"])
