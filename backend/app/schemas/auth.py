import re

from pydantic import BaseModel, Field, field_validator

from backend.app.schemas.user import UserResponse

# Pragmatic RFC 5322 subset: local@domain.tld, no spaces, at least one dot in the domain.
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")

PASSWORD_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("an uppercase letter", re.compile(r"[A-Z]")),
    ("a lowercase letter", re.compile(r"[a-z]")),
    ("a number", re.compile(r"\d")),
    ("a special character", re.compile(r"[^A-Za-z0-9]")),
)


def validate_email_format(value: str) -> str:
    value = value.strip()
    if not EMAIL_RE.fullmatch(value):
        raise ValueError("Enter a valid email address.")
    return value.lower()


def validate_password_strength(value: str) -> str:
    missing = [label for label, pattern in PASSWORD_RULES if not pattern.search(value)]
    if missing:
        raise ValueError("Password needs " + ", ".join(missing) + ".")
    return value


class RegisterRequest(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    captcha: str = Field(..., min_length=1, max_length=4096)

    @field_validator("full_name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Full name is required.")
        return value

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return validate_email_format(value)

    @field_validator("password")
    @classmethod
    def _password(cls, value: str) -> str:
        return validate_password_strength(value)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=1, max_length=128)
    # Required after LOGIN_CAPTCHA_AFTER_FAILURES failed attempts (FR-002).
    captcha: str | None = Field(default=None, max_length=4096)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1, max_length=4096)


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(default=None, max_length=4096)


class VerifyEmailRequest(BaseModel):
    token: str = Field(..., min_length=10, max_length=200)


class ResendVerificationRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    captcha: str = Field(..., min_length=1, max_length=4096)


class CaptchaChallenge(BaseModel):
    algorithm: str
    challenge: str
    maxnumber: int
    salt: str
    signature: str


class MessageResponse(BaseModel):
    detail: str
