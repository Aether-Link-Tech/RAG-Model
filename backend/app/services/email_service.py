"""Outgoing email (FR-001 verification links).

Sent over SMTP from a background task right after the registration response, so
delivery starts immediately (AC: within 10 seconds) without slowing the request.
Local development uses the Mailpit container (SMTP :1025, inbox UI :8025).
"""
from __future__ import annotations

import asyncio
import html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import quote

from backend.app.core.config import settings

logger = logging.getLogger(__name__)


def verification_link(token: str) -> str:
    return f"{settings.FRONTEND_BASE_URL.rstrip('/')}/verify-email?token={quote(token)}"


def _build_verification_message(to_email: str, full_name: str | None, token: str) -> EmailMessage:
    link = verification_link(token)
    greeting = f"Hi {full_name}," if full_name else "Hi,"
    hours = settings.EMAIL_VERIFICATION_TTL_HOURS
    message = EmailMessage()
    message["Subject"] = "Verify your Aether Link account"
    message["From"] = settings.SMTP_FROM
    message["To"] = to_email
    message.set_content(
        f"{greeting}\n\n"
        f"Confirm your email address to activate your Aether Link workspace:\n\n{link}\n\n"
        f"This link expires in {hours} hours and can be used once.\n"
        "If you did not create an account, you can ignore this email.\n"
    )
    message.add_alternative(
        f"""<!doctype html><html><body style="font-family:Arial,sans-serif;color:#0f172a">
<p>{html.escape(greeting)}</p>
<p>Confirm your email address to activate your Aether Link workspace.</p>
<p><a href="{html.escape(link, quote=True)}" style="display:inline-block;padding:12px 20px;background:#2563eb;color:#fff;
border-radius:8px;text-decoration:none;font-weight:600">Verify email</a></p>
<p style="color:#64748b;font-size:13px">This link expires in {hours} hours and can be used once.
If you did not create an account, you can ignore this email.</p>
</body></html>""",
        subtype="html",
    )
    return message


def _send(message: EmailMessage) -> None:
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS) as smtp:
        if settings.SMTP_STARTTLS:
            smtp.starttls(context=ssl.create_default_context())
        if settings.SMTP_USERNAME:
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        smtp.send_message(message)


async def send_verification_email(to_email: str, full_name: str | None, token: str) -> None:
    """Send the verification email; failures are logged (the user can request a resend)."""
    try:
        await asyncio.to_thread(_send, _build_verification_message(to_email, full_name, token))
    except Exception:
        logger.exception("Verification email could not be sent")
