"""Password and database-backed session services for HealthOps."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from .database import get_engine, session_lookup

PBKDF2_ITERATIONS = 310_000
SESSION_HOURS = 12


class AuthenticationError(Exception):
    """Expected authentication or account-setup failure."""


def hash_session_token(token: str) -> str:
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        raise ValueError("Invalid session token.")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_password(password: str) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError("Password must contain between 12 and 256 characters.")

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    salt_text = base64.urlsafe_b64encode(salt).decode().rstrip("=")
    digest_text = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt_text}${digest_text}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iteration_text, salt_text, expected_text = stored_hash.split("$", 3)
        iterations = int(iteration_text)
        if scheme != "pbkdf2_sha256" or not 100_000 <= iterations <= 1_000_000:
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(
            expected_text + "=" * (-len(expected_text) % 4)
        )
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError):
        return False


def _normalise_email(email: str) -> str:
    if not isinstance(email, str):
        raise ValueError("Enter a valid email address.")
    value = email.strip().lower()
    if len(value) > 320 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise ValueError("Enter a valid email address.")
    return value


def auth_setup_required() -> bool:
    with get_engine("app").connect() as connection:
        return bool(
            connection.execute(
                text("SELECT healthops.auth_setup_required()")
            ).scalar_one()
        )


def _new_session():
    token = secrets.token_urlsafe(32)
    token_hash = hash_session_token(token)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
    return token, token_hash, expires_at


def _active_identity(token_hash: str) -> dict[str, Any]:
    rows = session_lookup(token_hash)
    identity = next(
        (dict(row) for row in rows if row.get("organization_id") is not None),
        None,
    )
    if identity is None:
        raise AuthenticationError(
            "The account has no active organization membership."
        )
    return identity


def bootstrap_first_owner(
    organization_name: str,
    display_name: str,
    email: str,
    password: str,
):
    if not auth_setup_required():
        raise AuthenticationError("Initial setup has already been completed.")

    organization_name = str(organization_name or "").strip()
    display_name = str(display_name or "").strip()
    if not 2 <= len(organization_name) <= 200:
        raise ValueError("Organization name must contain 2–200 characters.")
    if not 2 <= len(display_name) <= 160:
        raise ValueError("Name must contain 2–160 characters.")

    email_normalized = _normalise_email(email)
    password_hash = _hash_password(password)
    slug = re.sub(r"[^a-z0-9]+", "-", organization_name.lower()).strip("-")[:72]
    if not slug:
        slug = "healthcare-organization"

    token, token_hash, expires_at = _new_session()
    with get_engine("app").begin() as connection:
        connection.execute(
            text("""
                SELECT *
                FROM healthops.bootstrap_first_owner(
                    :email, :email_normalized, :display_name, :password_hash,
                    :organization_name, :organization_slug, :token_hash, :expires_at
                )
            """),
            {
                "email": email.strip(),
                "email_normalized": email_normalized,
                "display_name": display_name,
                "password_hash": password_hash,
                "organization_name": organization_name,
                "organization_slug": slug,
                "token_hash": token_hash,
                "expires_at": expires_at,
            },
        ).mappings().one()

    return token, _active_identity(token_hash)


def login_user(email: str, password: str):
    email_normalized = _normalise_email(email)
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        raise AuthenticationError("Email or password is incorrect.")

    with get_engine("app").connect() as connection:
        row = connection.execute(
            text("""
                SELECT user_id, password_hash, is_active
                FROM healthops.lookup_login(:email_normalized)
            """),
            {"email_normalized": email_normalized},
        ).mappings().first()

    if (
        row is None
        or not row["is_active"]
        or not _verify_password(password, row["password_hash"])
    ):
        raise AuthenticationError("Email or password is incorrect.")

    token, token_hash, expires_at = _new_session()
    with get_engine("app").begin() as connection:
        connection.execute(
            text("""
                SELECT healthops.create_session_for_user(
                    :user_id, :password_hash, :token_hash, :expires_at
                )
            """),
            {
                "user_id": row["user_id"],
                "password_hash": row["password_hash"],
                "token_hash": token_hash,
                "expires_at": expires_at,
            },
        )

    return token, _active_identity(token_hash)


def logout_user(token_hash: str) -> None:
    with get_engine("app").begin() as connection:
        connection.execute(
            text("SELECT healthops.revoke_session(:token_hash)"),
            {"token_hash": token_hash},
        )