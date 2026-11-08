"""Server-side Google reCAPTCHA v2 verification (FR-001 / AL-118, FR-002)."""
from __future__ import annotations

import logging

import httpx

from backend.app.core.config import LOCAL_DEV_ENVIRONMENTS, settings

logger = logging.getLogger(__name__)


async def verify_captcha(token: str | None, remote_ip: str | None = None) -> bool:
    """Return True only if Google confirms the reCAPTCHA response token.

    With RECAPTCHA_ENABLED=false every call passes. In a local/dev environment
    with no secret key configured, the check is skipped with a warning, so the
    app runs without Google keys; outside development a missing key is refused
    at startup (see Settings.validate_security_settings).
    """
    if not settings.RECAPTCHA_ENABLED:
        return True

    secret = settings.RECAPTCHA_SECRET_KEY.strip()
    if not secret:
        if (settings.ENV or "").lower().strip() in LOCAL_DEV_ENVIRONMENTS:
            logger.warning("RECAPTCHA_SECRET_KEY is not set; skipping captcha check (dev only).")
            return True
        return False

    if not token:
        return False

    data = {"secret": secret, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(settings.RECAPTCHA_VERIFY_URL, data=data)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("reCAPTCHA verification request failed", exc_info=True)
        return False

    return body.get("success") is True
