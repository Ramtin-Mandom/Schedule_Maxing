"""
backend/settings.py

Server configuration, read only from environment variables (or an injected
mapping in tests). Nothing here reads a .env file, and no secret value is
ever included in an error message or repr.

Required:
    DATABASE_URL        SQLAlchemy URL of the server database. PostgreSQL in
                        production ("postgresql://..." or
                        "postgresql+psycopg://..."; "postgres://" is accepted
                        and normalized to the psycopg 3 driver).
    JWT_SECRET          HMAC key for access tokens, at least 32 characters.

Optional (with defaults):
    JWT_ISSUER                      "schedule-maxing"
    JWT_AUDIENCE                    "schedule-maxing-api"
    ACCESS_TOKEN_TTL_MINUTES        60 (1..1440)
    API_MAX_PAGE_SIZE               500 (the default page size is 100)
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

#: The only accepted JWT algorithm. Tokens naming any other algorithm
#: (including "none") are rejected at verification.
JWT_ALGORITHM = "HS256"
MIN_SECRET_LENGTH = 32


class BackendConfigError(RuntimeError):
    """The backend cannot start with this configuration. Messages name settings, never their values."""


@dataclass(frozen=True)
class BackendSettings:
    database_url: str = field(repr=False)
    jwt_secret: str = field(repr=False)
    jwt_issuer: str = "schedule-maxing"
    jwt_audience: str = "schedule-maxing-api"
    access_token_ttl_minutes: int = 60
    max_page_size: int = 500
    default_page_size: int = 100

    def __post_init__(self) -> None:
        problems = []
        if not self.database_url:
            problems.append("DATABASE_URL is required")
        if len(self.jwt_secret or "") < MIN_SECRET_LENGTH:
            problems.append(f"JWT_SECRET is required and must be at least {MIN_SECRET_LENGTH} characters")
        if not 1 <= self.access_token_ttl_minutes <= 1440:
            problems.append("ACCESS_TOKEN_TTL_MINUTES must be between 1 and 1440")
        if not 1 <= self.default_page_size <= self.max_page_size:
            problems.append("API_MAX_PAGE_SIZE must be at least the default page size (100)")
        if problems:
            raise BackendConfigError("Invalid backend configuration: " + "; ".join(problems) + ".")


def normalize_database_url(url: str) -> str:
    """Use the psycopg 3 driver for PostgreSQL URLs given without one (Render and others issue postgres://)."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def load_settings(environ: Mapping[str, str] | None = None) -> BackendSettings:
    environ = os.environ if environ is None else environ

    def integer(name: str, default: int) -> int:
        raw = environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            raise BackendConfigError(f"Invalid backend configuration: {name} must be a whole number.") from None

    return BackendSettings(
        database_url=normalize_database_url(environ.get("DATABASE_URL", "").strip()),
        jwt_secret=environ.get("JWT_SECRET", ""),
        jwt_issuer=environ.get("JWT_ISSUER", "").strip() or "schedule-maxing",
        jwt_audience=environ.get("JWT_AUDIENCE", "").strip() or "schedule-maxing-api",
        access_token_ttl_minutes=integer("ACCESS_TOKEN_TTL_MINUTES", 60),
        max_page_size=integer("API_MAX_PAGE_SIZE", 500),
    )
