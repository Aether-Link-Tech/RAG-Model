"""Outgoing email over SMTP (FR-001 / AL-117)."""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from urllib.parse import urlencode

from backend.app.core.config import LOCAL_DEV_ENVIRONMENTS, settings

logger = logging.getLogger(__name__)


class EmailDeliveryError(RuntimeError):
    """The message could not be handed to the SMTP server."""


def build_verification_link(token: str) -> str:
    separator = "&" if "?" in settings.EMAIL_VERIFICATION_URL else "?"
    return f"{settings.EMAIL_VERIFICATION_URL}{separator}{urlencode({'token': token})}"


def _send_smtp(message: EmailMessage) -> None:
    with smtplib.SMTP(
        settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS
    ) as smtp:
        if settings.SMTP_USE_TLS:
            smtp.starttls()
        if settings.SMTP_USERNAME:
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        smtp.send_message(message)


async def send_email(to: str, subject: str, body: str) -> None:
    if not settings.SMTP_HOST.strip():
        if (settings.ENV or "").lower().strip() in LOCAL_DEV_ENVIRONMENTS:
            logger.info("SMTP not configured; email to %s not sent.\nSubject: %s\n%s", to, subject, body)
            return
        raise EmailDeliveryError("SMTP_HOST is not configured.")

    message = EmailMessage()
    message["From"] = settings.SMTP_FROM
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    try:
        await asyncio.to_thread(_send_smtp, message)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailDeliveryError("Failed to send email.") from exc


async def send_verification_email(to: str, token: str) -> None:
    """Send the account verification link. Errors are logged, never raised:
    this runs as a background task after the response has been sent."""
    link = build_verification_link(token)
    body = (
        "Welcome to Aether-Link.\n\n"
        "Please confirm your email address by opening this link:\n"
        f"{link}\n\n"
        f"The link expires in {settings.EMAIL_VERIFICATION_EXPIRE_HOURS} hours. "
        "If you did not create an account, you can ignore this email."
    )
    try:
        await send_email(to, "Verify your Aether-Link account", body)
    except EmailDeliveryError:
        logger.exception("Verification email to %s could not be sent", to)
