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
    BROWSER_SESSION_TTL_MINUTES     720 (5..43200): lifetime of a browser (cookie) session
    BROWSER_COOKIE_SECURE           "true"; "false" only for plain-http local development
    ALLOWED_ORIGINS                 extra origins (comma-separated, e.g. a dev frontend) whose
                                    cookie-authenticated requests are accepted besides the
                                    server's own origin (CSRF/origin checks only -- not CORS)

Server protections (Milestone 6, docs/backend.md "Protections"):
    ALLOWED_HOSTS                   Host header values served (comma-separated); empty = any
                                    (development only). Behind a proxy, list the public host.
    CORS_ORIGINS                    origins allowed to call the API from a browser page on another
                                    origin, with credentials (comma-separated, exact; never "*").
                                    Empty = no CORS headers (same-origin pages need none).
    TRUSTED_PROXIES                 IPs/CIDRs of reverse proxies whose X-Forwarded-For is believed
                                    for the client address (rate limiting). Empty = none.
    RATE_LIMIT_ENABLED              "true" (default); shared, database-backed limits on sign-in,
                                    registration and recovery (backend/rate_limit.py)
    MAX_REQUEST_BYTES               8388608: larger request bodies are refused with 413 before parsing
    DB_CONNECT_TIMEOUT_SECONDS      10;  DB_STATEMENT_TIMEOUT_MS 30000;  DB_POOL_TIMEOUT_SECONDS 10
                                    (PostgreSQL only)
    GENERATION_TIME_LIMIT_SECONDS   20: a hosted generation that has not finished by then stops before
                                    anything is saved (503 generation_limit)

Password recovery (all of these, or recovery answers 503 recovery_unavailable):
    RECOVERY_PUBLIC_URL             the reset page's public URL, https (http only for localhost), e.g.
                                    https://app.example.com/auth/recovery/reset; links are built from
                                    it -- never from request Host or forwarded headers
    SMTP_HOST, SMTP_SENDER          the outgoing mail server and the From address
    SMTP_PORT                       587;  SMTP_STARTTLS "true";  SMTP_SSL "false" (implicit TLS, port 465)
    SMTP_USERNAME, SMTP_PASSWORD    optional credentials (never logged or echoed)
    DELIVERY_TIMEOUT_SECONDS        10
    RECOVERY_TOKEN_TTL_MINUTES      30 (5..1440)
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

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
    refresh_token_expire_days: int = 30
    environment: str = "development"
    max_page_size: int = 500
    default_page_size: int = 100
    browser_session_ttl_minutes: int = 720
    browser_cookie_secure: bool = True
    allowed_origins: tuple[str, ...] = ()
    # -- protections (load_settings enables rate limiting by default; a programmatic instance opts in) --
    allowed_hosts: tuple[str, ...] = ()
    cors_origins: tuple[str, ...] = ()
    trusted_proxies: tuple[str, ...] = ()
    rate_limit_enabled: bool = False
    max_request_bytes: int = 8 * 1024 * 1024
    db_connect_timeout_seconds: int = 10
    db_statement_timeout_ms: int = 30000
    db_pool_timeout_seconds: int = 10
    generation_time_limit_seconds: float = 20.0
    # -- password recovery --
    recovery_public_url: str = ""
    recovery_token_ttl_minutes: int = 30
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = field(default="", repr=False)
    smtp_password: str = field(default="", repr=False)
    smtp_sender: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    delivery_timeout_seconds: float = 10.0

    @property
    def recovery_configured(self) -> bool:
        """Whether recovery links can be built and delivered (else recovery answers 503)."""
        return bool(self.recovery_public_url and self.smtp_host and self.smtp_sender)

    def __post_init__(self) -> None:
        problems = []
        if not self.database_url:
            problems.append("DATABASE_URL is required")
        if len(self.jwt_secret or "") < MIN_SECRET_LENGTH:
            problems.append(f"JWT_SECRET is required and must be at least {MIN_SECRET_LENGTH} characters")
        if not 1 <= self.access_token_ttl_minutes <= 1440:
            problems.append("ACCESS_TOKEN_TTL_MINUTES must be between 1 and 1440")
        if not 1 <= self.refresh_token_expire_days <= 365:
            problems.append("REFRESH_TOKEN_EXPIRE_DAYS must be between 1 and 365")
        if self.environment not in ("development", "test", "production"):
            problems.append("ENVIRONMENT must be development, test or production")
        if self.environment == "production":
            if not normalize_database_url(self.database_url).startswith("postgresql"):
                problems.append("DATABASE_URL must use PostgreSQL in production")
            if not self.allowed_hosts or any("*" in host for host in self.allowed_hosts):
                problems.append("ALLOWED_HOSTS must list explicit production hosts")
            if not self.browser_cookie_secure:
                problems.append("BROWSER_COOKIE_SECURE must be true in production")
        if not 5 <= self.browser_session_ttl_minutes <= 43200:
            problems.append("BROWSER_SESSION_TTL_MINUTES must be between 5 and 43200")
        if any(not _origin_ok(origin) for origin in self.allowed_origins):
            problems.append("ALLOWED_ORIGINS must list origins like https://app.example.com (no path or trailing slash)")
        if not 1 <= self.default_page_size <= self.max_page_size:
            problems.append("API_MAX_PAGE_SIZE must be at least the default page size (100)")
        if any(not _origin_ok(origin) for origin in self.cors_origins):
            problems.append("CORS_ORIGINS must list exact origins like https://app.example.com (never *)")
        for proxy in self.trusted_proxies:
            try:
                ipaddress.ip_network(proxy, strict=False)
            except ValueError:
                problems.append("TRUSTED_PROXIES must list IP addresses or networks")
                break
        if not 1024 <= self.max_request_bytes <= 256 * 1024 * 1024:
            problems.append("MAX_REQUEST_BYTES must be between 1024 and 268435456")
        if not 0 < self.generation_time_limit_seconds <= 600:
            problems.append("GENERATION_TIME_LIMIT_SECONDS must be between 0 and 600")
        for name, value in (("DB_CONNECT_TIMEOUT_SECONDS", self.db_connect_timeout_seconds),
                            ("DB_POOL_TIMEOUT_SECONDS", self.db_pool_timeout_seconds)):
            if not 0 < value <= 120:
                problems.append(f"{name} must be between 1 and 120")
        if not 0 < self.db_statement_timeout_ms <= 600000:
            problems.append("DB_STATEMENT_TIMEOUT_MS must be between 1 and 600000")
        if not 5 <= self.recovery_token_ttl_minutes <= 1440:
            problems.append("RECOVERY_TOKEN_TTL_MINUTES must be between 5 and 1440")
        if not 0 < self.delivery_timeout_seconds <= 120:
            problems.append("DELIVERY_TIMEOUT_SECONDS must be between 0 and 120")
        if self.recovery_public_url and not _public_url_ok(self.recovery_public_url):
            problems.append("RECOVERY_PUBLIC_URL must be an https URL (http only for localhost) without a query "
                            "or fragment")
        if problems:
            raise BackendConfigError("Invalid backend configuration: " + "; ".join(problems) + ".")


def _origin_ok(origin: str) -> bool:
    try:
        parts = urlsplit(origin)
        _ = parts.port  # reject malformed ports
        return (parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.path
                and not parts.query and not parts.fragment and "@" not in parts.netloc
                and "*" not in parts.netloc and not any(character.isspace() for character in origin))
    except ValueError:
        return False


def _public_url_ok(url: str) -> bool:
    parts = urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    return (parts.scheme == "https" or (parts.scheme == "http" and local)) and bool(parts.hostname) \
        and not parts.query and not parts.fragment and "@" not in parts.netloc


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

    def number(name: str, default: float) -> float:
        raw = environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            raise BackendConfigError(f"Invalid backend configuration: {name} must be a number.") from None

    def flag(name: str, default: bool) -> bool:
        raw = environ.get(name, "").strip().lower()
        return default if not raw else raw not in ("false", "0", "no")

    def listing(name: str) -> tuple[str, ...]:
        return tuple(item.strip() for item in environ.get(name, "").split(",") if item.strip())

    return BackendSettings(
        database_url=normalize_database_url(environ.get("DATABASE_URL", "").strip()),
        jwt_secret=environ.get("JWT_SECRET", ""),
        jwt_issuer=environ.get("JWT_ISSUER", "").strip() or "schedule-maxing",
        jwt_audience=environ.get("JWT_AUDIENCE", "").strip() or "schedule-maxing-api",
        access_token_ttl_minutes=integer("ACCESS_TOKEN_TTL_MINUTES", 60),
        refresh_token_expire_days=integer("REFRESH_TOKEN_EXPIRE_DAYS", 30),
        environment=environ.get("ENVIRONMENT", "development").strip(),
        max_page_size=integer("API_MAX_PAGE_SIZE", 500),
        browser_session_ttl_minutes=integer("BROWSER_SESSION_TTL_MINUTES", 720),
        browser_cookie_secure=environ.get("BROWSER_COOKIE_SECURE", "true").strip().lower() not in ("false", "0", "no"),
        allowed_origins=tuple(
            origin.strip() for origin in environ.get("ALLOWED_ORIGINS", "").split(",") if origin.strip()
        ),
        allowed_hosts=listing("ALLOWED_HOSTS"),
        cors_origins=listing("CORS_ORIGINS"),
        trusted_proxies=listing("TRUSTED_PROXIES"),
        rate_limit_enabled=flag("RATE_LIMIT_ENABLED", True),
        max_request_bytes=integer("MAX_REQUEST_BYTES", 8 * 1024 * 1024),
        db_connect_timeout_seconds=integer("DB_CONNECT_TIMEOUT_SECONDS", 10),
        db_statement_timeout_ms=integer("DB_STATEMENT_TIMEOUT_MS", 30000),
        db_pool_timeout_seconds=integer("DB_POOL_TIMEOUT_SECONDS", 10),
        generation_time_limit_seconds=number("GENERATION_TIME_LIMIT_SECONDS", 20.0),
        recovery_public_url=environ.get("RECOVERY_PUBLIC_URL", "").strip(),
        recovery_token_ttl_minutes=integer("RECOVERY_TOKEN_TTL_MINUTES", 30),
        smtp_host=environ.get("SMTP_HOST", "").strip(),
        smtp_port=integer("SMTP_PORT", 587),
        smtp_username=environ.get("SMTP_USERNAME", ""),
        smtp_password=environ.get("SMTP_PASSWORD", ""),
        smtp_sender=environ.get("SMTP_SENDER", "").strip(),
        smtp_starttls=flag("SMTP_STARTTLS", True),
        smtp_ssl=flag("SMTP_SSL", False),
        delivery_timeout_seconds=number("DELIVERY_TIMEOUT_SECONDS", 10.0),
    )
