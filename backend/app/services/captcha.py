"""Self-hosted proof-of-work captcha (FR-001 / FR-002), compatible with the ALTCHA scheme.

The server issues a signed challenge: ``challenge = SHA-256(salt + secret_number)``.
The browser finds ``secret_number`` by brute force (about a second of CPU) and sends
the solution back. Verification is server-side and stateless apart from a Redis
"used" marker that stops a solution from being replayed.

No third-party service, keys or tracking are involved, so it also works offline.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time

from backend.app.core.config import settings
from backend.app.core.redis import RedisError, RedisUnavailableError, get_redis

ALGORITHM = "SHA-256"
_USED_PREFIX = "captcha:used:"


class CaptchaError(ValueError):
    """Raised when a captcha solution is missing, invalid, expired or replayed."""


def _signing_key() -> bytes:
    return hashlib.sha256(b"captcha:" + settings.SECRET_KEY.encode("utf-8")).digest()


def _sign(challenge: str) -> str:
    return hmac.new(_signing_key(), challenge.encode("ascii"), hashlib.sha256).hexdigest()


def create_challenge() -> dict:
    expires = int(time.time()) + settings.CAPTCHA_TTL_SECONDS
    salt = f"{secrets.token_hex(12)}?expires={expires}"
    number = secrets.randbelow(settings.CAPTCHA_MAX_NUMBER + 1)
    challenge = hashlib.sha256(f"{salt}{number}".encode("ascii")).hexdigest()
    return {
        "algorithm": ALGORITHM,
        "challenge": challenge,
        "maxnumber": settings.CAPTCHA_MAX_NUMBER,
        "salt": salt,
        "signature": _sign(challenge),
    }


def solve_challenge(challenge: dict) -> str:
    """Reference solver (used by tests and documentation); returns the encoded payload."""
    for number in range(challenge["maxnumber"] + 1):
        if hashlib.sha256(f"{challenge['salt']}{number}".encode("ascii")).hexdigest() == challenge["challenge"]:
            payload = {key: challenge[key] for key in ("algorithm", "challenge", "salt", "signature")}
            payload["number"] = number
            return base64.b64encode(json.dumps(payload).encode()).decode()
    raise CaptchaError("No solution found")


def _expiry_of(salt: str) -> int:
    _, _, query = salt.partition("?")
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key == "expires" and value.isdigit():
            return int(value)
    raise CaptchaError("Captcha has no expiry")


async def verify_solution(encoded: str | None) -> None:
    """Raise CaptchaError unless `encoded` is a fresh, valid, unused solution."""
    if not encoded:
        raise CaptchaError("Captcha is required.")
    try:
        payload = json.loads(base64.b64decode(encoded, validate=True))
        algorithm = payload["algorithm"]
        challenge = str(payload["challenge"])
        salt = str(payload["salt"])
        signature = str(payload["signature"])
        number = int(payload["number"])
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise CaptchaError("Captcha solution is malformed.") from exc

    if algorithm != ALGORITHM or not hmac.compare_digest(signature, _sign(challenge)):
        raise CaptchaError("Captcha solution is invalid.")
    expires = _expiry_of(salt)
    if expires < time.time():
        raise CaptchaError("Captcha expired. Please try again.")
    computed = hashlib.sha256(f"{salt}{number}".encode("ascii")).hexdigest()
    if not hmac.compare_digest(computed, challenge):
        raise CaptchaError("Captcha solution is invalid.")

    try:
        first_use = await get_redis().set(
            _USED_PREFIX + challenge, "1", ex=max(1, int(expires - time.time())), nx=True
        )
    except RedisError as exc:
        raise RedisUnavailableError("Captcha replay store unavailable") from exc
    if not first_use:
        raise CaptchaError("Captcha was already used. Please try again.")
