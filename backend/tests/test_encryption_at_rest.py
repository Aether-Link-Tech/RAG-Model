"""FR-007 encryption at rest: bcrypt cost, AES-256-GCM PII fields, blind-index lookup (AL-135, AL-136)."""
import base64
import uuid

import bcrypt
import pytest
from sqlalchemy import select, text

from backend.app.core import crypto
from backend.app.core.config import Settings, settings
from backend.app.core.security import hash_password, verify_password
from backend.app.models.user import User
from backend.tests.conftest import TestingSessionLocal, _CleanupSessionLocal, purge_test_users


def test_default_bcrypt_cost_is_at_least_12():
    assert Settings().BCRYPT_ROUNDS >= 12


def test_hash_password_uses_configured_bcrypt_cost(monkeypatch):
    monkeypatch.setattr(settings, "BCRYPT_ROUNDS", 12)
    hashed = hash_password("Str0ng!Passw0rd")
    assert hashed.startswith("$2b$12$")
    assert verify_password("Str0ng!Passw0rd", hashed)
    assert not verify_password("wrong", hashed)


def test_long_passphrases_are_not_truncated():
    base = "A1!" + "x" * 80
    hashed = hash_password(base + "tail-one")
    assert verify_password(base + "tail-one", hashed)
    assert not verify_password(base + "tail-two", hashed)


def test_garbage_hashes_never_raise():
    assert verify_password("anything", "not-a-hash") is False
    assert verify_password("anything", "$2b$bad") is False


def test_aes_gcm_roundtrip_is_randomised_and_tamper_evident():
    first = crypto.encrypt_text("alice@example.com")
    second = crypto.encrypt_text("alice@example.com")
    assert first != second  # random nonce per value
    assert first.startswith("v1:")
    assert crypto.decrypt_text(first) == "alice@example.com"

    blob = bytearray(base64.b64decode(first[3:]))
    blob[-1] ^= 0x01
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_text("v1:" + base64.b64encode(bytes(blob)).decode())


def test_explicit_keys_are_validated(monkeypatch):
    monkeypatch.setattr(settings, "PII_ENCRYPTION_KEY", base64.b64encode(b"short").decode())
    crypto.reset_key_cache()
    try:
        with pytest.raises(ValueError, match="32 bytes"):
            crypto.encrypt_text("x")
    finally:
        monkeypatch.setattr(settings, "PII_ENCRYPTION_KEY", "")
        crypto.reset_key_cache()


def test_blind_index_is_keyed_and_normalised():
    assert crypto.email_index("Alice@Example.com ") == crypto.email_index("alice@example.com")
    import hashlib

    assert crypto.email_index("alice@example.com") != hashlib.sha256(b"alice@example.com").hexdigest()


@pytest.mark.asyncio
async def test_email_and_full_name_are_ciphertext_in_postgres():
    email = f"pii-{uuid.uuid4().hex[:8]}@example.com"
    async with TestingSessionLocal() as session:
        user = User(email=email, full_name="Marie Curie", hashed_password=hash_password("Str0ng!Passw0rd"))
        session.add(user)
        await session.commit()
        uid = user.id
    try:
        async with _CleanupSessionLocal() as raw:
            row = (
                await raw.execute(
                    text("SELECT email, full_name, email_hash, hashed_password FROM users WHERE id = :id"),
                    {"id": uid},
                )
            ).one()
        assert email not in row.email and row.email.startswith("v1:")
        assert "Marie" not in row.full_name and row.full_name.startswith("v1:")
        assert row.email_hash == crypto.email_index(email)
        assert bcrypt.checkpw(b"Str0ng!Passw0rd", row.hashed_password.encode())

        # The ORM decrypts transparently and looks users up through the blind index
        async with TestingSessionLocal() as session:
            found = (
                await session.execute(select(User).where(User.email_hash == crypto.email_index(email.upper())))
            ).scalar_one()
            assert found.email == email and found.full_name == "Marie Curie"
    finally:
        await purge_test_users(uid)


@pytest.mark.asyncio
async def test_api_never_returns_ciphertext(async_client, test_user):
    from backend.tests.conftest import bearer

    me = (await async_client.get("/api/v1/users/me", headers=bearer(test_user))).json()
    assert me["email"] == test_user.email
    assert not me["email"].startswith("v1:")
