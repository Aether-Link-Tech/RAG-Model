"""Field-level encryption for personal data at rest (FR-007 / AL-136).

- `encrypt_text` / `decrypt_text`: AES-256-GCM with a random 96-bit nonce per value.
  Stored as ``v1:<base64(nonce || ciphertext || tag)>`` so the format can be rotated.
- `blind_index`: keyed HMAC-SHA256 of a normalised value. It allows exact-match
  lookups (e.g. login by email) and unique constraints without storing plaintext.
- `EncryptedText`: SQLAlchemy column type that encrypts on write and decrypts on read.

Keys come from PII_ENCRYPTION_KEY and PII_INDEX_KEY (base64, 32 bytes each). In local
development they are derived from SECRET_KEY with HKDF when unset; outside development
`Settings.validate_security_settings` requires them, so no key is ever hardcoded.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from backend.app.core.config import settings

_PREFIX = "v1:"
_NONCE_BYTES = 12


class DecryptionError(ValueError):
    """Raised when a stored value cannot be authenticated or decoded."""


def _decode_key(raw: str, name: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{name} must be base64-encoded.") from exc
    if len(key) != 32:
        raise ValueError(f"{name} must decode to exactly 32 bytes (AES-256).")
    return key


def _derive_dev_key(purpose: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"aether-link-dev-pii",
        info=purpose,
    ).derive(settings.SECRET_KEY.encode("utf-8"))


@lru_cache(maxsize=1)
def _keys() -> tuple[bytes, bytes]:
    enc = settings.PII_ENCRYPTION_KEY
    idx = settings.PII_INDEX_KEY
    enc_key = _decode_key(enc, "PII_ENCRYPTION_KEY") if enc.strip() else _derive_dev_key(b"encryption")
    idx_key = _decode_key(idx, "PII_INDEX_KEY") if idx.strip() else _derive_dev_key(b"blind-index")
    return enc_key, idx_key


def reset_key_cache() -> None:
    """Forget cached keys (tests that change key settings call this)."""
    _keys.cache_clear()


def encrypt_text(plaintext: str) -> str:
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(_keys()[0]).encrypt(nonce, plaintext.encode("utf-8"), _PREFIX.encode())
    return _PREFIX + base64.b64encode(nonce + sealed).decode("ascii")


def decrypt_text(stored: str) -> str:
    if not stored.startswith(_PREFIX):
        raise DecryptionError("Unknown ciphertext format.")
    try:
        blob = base64.b64decode(stored[len(_PREFIX):], validate=True)
        nonce, sealed = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        return AESGCM(_keys()[0]).decrypt(nonce, sealed, _PREFIX.encode()).decode("utf-8")
    except Exception as exc:  # InvalidTag, binascii.Error, UnicodeDecodeError
        raise DecryptionError("Stored value could not be decrypted.") from exc


def normalize_email(email: str) -> str:
    return email.strip().lower()


def blind_index(value: str) -> str:
    """Deterministic keyed hash for equality lookups; never reversible without the key."""
    return hmac.new(_keys()[1], value.encode("utf-8"), hashlib.sha256).hexdigest()


def email_index(email: str) -> str:
    return blind_index(normalize_email(email))


class EncryptedText(TypeDecorator):
    """Text column transparently encrypted with AES-256-GCM."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_text(str(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return decrypt_text(value)
