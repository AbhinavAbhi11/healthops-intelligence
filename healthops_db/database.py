"""Database connection and tenant-scoped SQLAlchemy session helpers."""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass


def database_url(role: str = "app") -> URL:
    """Build a safe SQLAlchemy URL without manual password URL-escaping."""
    if role == "app":
        user_key = "HEALTHOPS_DB_USER"
        password_key = "HEALTHOPS_DB_PASSWORD"
    elif role == "migrator":
        user_key = "HEALTHOPS_MIGRATOR_USER"
        password_key = "HEALTHOPS_MIGRATOR_PASSWORD"
    else:
        raise ValueError("Database role must be 'app' or 'migrator'.")

    user = os.getenv(user_key, "").strip()
    password = os.getenv(password_key, "")
    host = os.getenv("HEALTHOPS_DB_HOST", "127.0.0.1").strip()
    database = os.getenv("HEALTHOPS_DB_NAME", "healthops_db").strip()

    try:
        port = int(os.getenv("HEALTHOPS_DB_PORT", "5432"))
    except ValueError as exc:
        raise RuntimeError("HEALTHOPS_DB_PORT must be an integer.") from exc

    if not user or not password:
        raise RuntimeError(
            f"Database credentials are missing. Configure {user_key} "
            f"and {password_key} in your local .env file."
        )

    if not 1 <= port <= 65535:
        raise RuntimeError("HEALTHOPS_DB_PORT must be between 1 and 65535.")

    if not host or not database:
        raise RuntimeError("Database host and database name are required.")

    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=host,
        port=port,
        database=database,
        query={"application_name": f"healthops-{role}"},
    )


@lru_cache(maxsize=2)
def get_engine(role: str = "app") -> Engine:
    """Return a cached engine for the restricted app or migration role."""
    return create_engine(
        database_url(role),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=30,
        pool_recycle=1800,
    )


@lru_cache(maxsize=2)
def _session_factory(role: str = "app"):
    return sessionmaker(
        bind=get_engine(role),
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )


def check_database(role: str = "app") -> dict[str, str]:
    """Verify a database login without exposing credentials."""
    with get_engine(role).connect() as connection:
        row = connection.execute(
            text(
                "SELECT current_database() AS database_name, "
                "current_user AS login_role, current_schema() AS schema_name"
            )
        ).mappings().one()
        return dict(row)



def _valid_uuid(value: Any, name: str) -> UUID:
    """Validate and normalize a UUID."""
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{name} must be a valid UUID.") from exc


def _valid_token_hash(token_hash: str) -> str:
    """Validate a lowercase SHA-256 digest of an opaque session token."""
    if (
        not isinstance(token_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", token_hash) is None
    ):
        raise ValueError(
            "token_hash must be a lowercase SHA-256 hexadecimal digest."
        )
    return token_hash


def session_lookup(token_hash: str) -> list[dict[str, Any]]:
    """
    Resolve a valid session and return its active organization memberships.

    A user without active memberships may receive a row with null
    organization fields and should be directed through onboarding.
    """
    validated_hash = _valid_token_hash(token_hash)
    factory = _session_factory("app")

    with factory() as session:
        with session.begin():
            rows = session.execute(
                text(
                    """
                    SELECT
                        user_id,
                        email,
                        display_name,
                        organization_id,
                        organization_name,
                        organization_slug,
                        membership_role
                    FROM healthops.resolve_session(:token_hash)
                    """
                ),
                {"token_hash": validated_hash},
            ).mappings().all()

            return [dict(row) for row in rows]


@contextmanager
def tenant_session(
    token_hash: str,
    organization_id: UUID | str,
) -> Iterator[Session]:
    """
    Open a transaction authenticated by a valid session and membership.

    The authenticated user and role are derived by PostgreSQL.
    No caller-supplied user ID is accepted.
    """
    validated_hash = _valid_token_hash(token_hash)
    validated_org = _valid_uuid(organization_id, "organization_id")

    factory = _session_factory("app")

    with factory() as session:
        with session.begin():
            identity = session.execute(
                text(
                    """
                    SELECT
                        authenticated_user_id,
                        active_membership_role
                    FROM healthops.open_tenant_context(
                        :token_hash,
                        :organization_id
                    )
                    """
                ),
                {
                    "token_hash": validated_hash,
                    "organization_id": validated_org,
                },
            ).mappings().one()

            session.info["healthops_user_id"] = (
                identity["authenticated_user_id"]
            )
            session.info["healthops_organization_id"] = validated_org
            session.info["healthops_membership_role"] = (
                identity["active_membership_role"]
            )

            yield session


def dispose_engines() -> None:
    """Dispose cached connection pools during controlled shutdown."""
    for role in ("app", "migrator"):
        try:
            get_engine(role).dispose()
        except Exception:
            pass

    _session_factory.cache_clear()
    get_engine.cache_clear()
