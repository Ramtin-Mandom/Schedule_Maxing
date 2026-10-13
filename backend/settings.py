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
    CLIENT_IP_HEADER                a header in which the trusted proxy itself writes the one client
                                    address, overwriting anything the client sent (e.g.
                                    CF-Connecting-IP). Read only from a TRUSTED_PROXIES peer; empty =
                                    the X-Forwarded-For chain is used (backend/protection.py).
    PASSWORD_HASH_CONCURRENCY       2: password hashes/verifications running at once in one process
    PASSWORD_HASH_QUEUE             8: more of them waiting; beyond that: 503 at once
    PASSWORD_HASH_WAIT_SECONDS      5: the longest one waits before 503
    WEB_CONCURRENCY                 1: the number of worker processes (the variable uvicorn reads)
    PASSWORD_HASH_MEMORY_BUDGET_MB  0 (unchecked): when set, startup fails unless
                                    WEB_CONCURRENCY x PASSWORD_HASH_CONCURRENCY x the Argon2 memory cost
                                    (64 MiB) fits in it
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

Email verification and transactional email (docs/email.md). Without APP_PUBLIC_URL, a sender
(SMTP_SENDER) and a provider, registration answers 503 email_unavailable:
    APP_PUBLIC_URL                  the server's own public origin, https (http only for localhost), no
                                    path -- e.g. https://api.example.com. Emailed links are built from
                                    it, never from a request's Host or forwarded headers
    EMAIL_PROVIDER                  "smtp" (default; the SMTP_* settings above), "resend", "brevo" or
                                    "mailjet" (these send over HTTPS: for hosts that block outgoing SMTP)
    MAILJET_API_KEY, MAILJET_SECRET_KEY   both, for EMAIL_PROVIDER=mailjet (never logged or echoed)
    RESEND_API_KEY                  the provider API key for EMAIL_PROVIDER=resend (never logged or echoed)
    BREVO_API_KEY                   the provider API key for EMAIL_PROVIDER=brevo (never logged or echoed)
    EMAIL_WEBHOOK_SECRET            the provider's webhook signing secret ("whsec_..."); without it
                                    POST /webhooks/email answers 404 and no delivery evidence is recorded
    EMAIL_DIAGNOSTIC_RECIPIENT      the only address `python -m backend.email_diagnostic` ever mails
    VERIFICATION_TOKEN_TTL_MINUTES  1440 (15..10080)
    PENDING_REGISTRATION_TTL_HOURS  48 (1..720): an unconfirmed registration is forgotten after this
    EMAIL_DNS_CHECK                 "false"; "true" refuses, at registration, a domain that DNS says
                                    cannot receive mail. A DNS timeout refuses nothing, and passing
                                    proves nothing about the mailbox or who owns it
    REQUIRE_VERIFIED_EMAIL          "false"; "true" refuses record access (403 email_unverified) to
                                    accounts whose address was never verified (docs/email.md)
    OUTBOX_WORKER_ENABLED           "true": a background thread sends queued email and retries

Account security (docs/account-security.md):
    MFA_ENCRYPTION_KEY              a Fernet key used only to encrypt TOTP secrets at rest, e.g. from
                                    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
                                    Without it two-step sign-in cannot be set up (503 mfa_unavailable)
    MFA_ENCRYPTION_KEY_PREVIOUS     the key being rotated away from: still decrypts, never encrypts
                                    (docs/api-security.md, "Keys and rotation")
    MFA_ISSUER                      "Schedule Maxing": the name authenticator apps show
    API_DOCS_ENABLED                /docs, /redoc and /openapi.json: on by default, off by default when
                                    ENVIRONMENT=production. Publishing them is a choice, not a flaw:
                                    every route they describe still authenticates and authorizes
    MIGRATION_DATABASE_URL          read only by `python -m backend.migrate`: a role that may change the
                                    schema, so the server's DATABASE_URL role needs no such right
    PASSWORD_BREACH_CHECK           "true": a new password is looked up in Pwned Passwords by k-anonymity
                                    (5 hex digits of its SHA-1 leave the server; never the password).
                                    Skipped, and logged, when that service cannot be reached
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
    client_ip_header: str = ""
    password_hash_concurrency: int = 2
    password_hash_queue: int = 8
    password_hash_wait_seconds: float = 5.0
    web_concurrency: int = 1
    password_hash_memory_budget_mb: int = 0
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
    # -- email verification and the outbox --
    app_public_url: str = ""
    email_provider: str = "smtp"
    resend_api_key: str = field(default="", repr=False)
    brevo_api_key: str = field(default="", repr=False)
    mailjet_api_key: str = field(default="", repr=False)
    mailjet_secret_key: str = field(default="", repr=False)
    email_webhook_secret: str = field(default="", repr=False)
    email_diagnostic_recipient: str = ""
    verification_token_ttl_minutes: int = 1440
    pending_registration_ttl_hours: int = 48
    email_dns_check: bool = False
    require_verified_email: bool = False
    outbox_worker_enabled: bool = False
    # -- account security --
    mfa_encryption_key: str = field(default="", repr=False)
    mfa_encryption_key_previous: str = field(default="", repr=False)
    mfa_issuer: str = "Schedule Maxing"
    #: The interactive API documentation (/docs, /redoc, /openapi.json). None: on, except in production.
    api_docs_enabled: bool | None = None
    password_breach_check: bool = False

    @property
    def mail_configured(self) -> bool:
        """Whether transactional email can be composed and sent (else registration answers 503)."""
        provider = bool({"resend": self.resend_api_key, "brevo": self.brevo_api_key,
                         "mailjet": self.mailjet_api_key and self.mailjet_secret_key}.get(
            self.email_provider, self.smtp_host))
        return bool((self.app_public_url or self.recovery_public_url) and self.smtp_sender and provider)

    @property
    def docs_enabled(self) -> bool:
        return self.environment != "production" if self.api_docs_enabled is None else self.api_docs_enabled

    @property
    def recovery_configured(self) -> bool:
        """Whether recovery links can be built and delivered (else recovery answers 503)."""
        return self.mail_configured

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
                network = ipaddress.ip_network(proxy, strict=False)
            except ValueError:
                problems.append("TRUSTED_PROXIES must list IP addresses or networks")
                break
            if network.prefixlen == 0:
                # "Everything is a proxy" lets any client name its own address.
                problems.append("TRUSTED_PROXIES must not trust every address (a /0 network)")
                break
        if self.client_ip_header and not all(c.isalnum() or c == "-" for c in self.client_ip_header):
            problems.append("CLIENT_IP_HEADER must be a header name")
        if self.client_ip_header and not self.trusted_proxies:
            problems.append("CLIENT_IP_HEADER needs TRUSTED_PROXIES (the header is believed only from those peers)")
        if not 1 <= self.password_hash_concurrency <= 64:
            problems.append("PASSWORD_HASH_CONCURRENCY must be between 1 and 64")
        if not 0 <= self.password_hash_queue <= 1000:
            problems.append("PASSWORD_HASH_QUEUE must be between 0 and 1000")
        if not 0 < self.password_hash_wait_seconds <= 60:
            problems.append("PASSWORD_HASH_WAIT_SECONDS must be between 0 and 60")
        if not 1 <= self.web_concurrency <= 256:
            problems.append("WEB_CONCURRENCY must be between 1 and 256")
        if self.password_hash_memory_budget_mb < 0:
            problems.append("PASSWORD_HASH_MEMORY_BUDGET_MB must not be negative")
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
        if self.app_public_url and (not _public_url_ok(self.app_public_url)
                                    or urlsplit(self.app_public_url).path not in ("", "/")):
            problems.append("APP_PUBLIC_URL must be an https origin (http only for localhost) without a path, "
                            "query or fragment")
        for name, key in (("MFA_ENCRYPTION_KEY", self.mfa_encryption_key),
                          ("MFA_ENCRYPTION_KEY_PREVIOUS", self.mfa_encryption_key_previous)):
            if key and not _fernet_key_ok(key):
                problems.append(f"{name} must be a Fernet key (32 random bytes, URL-safe base64)")
        if self.mfa_encryption_key_previous and not self.mfa_encryption_key:
            problems.append("MFA_ENCRYPTION_KEY_PREVIOUS needs MFA_ENCRYPTION_KEY (the new key)")
        if self.email_provider not in ("smtp", "resend", "brevo", "mailjet"):
            problems.append("EMAIL_PROVIDER must be smtp, resend, brevo or mailjet")
        if not 15 <= self.verification_token_ttl_minutes <= 10080:
            problems.append("VERIFICATION_TOKEN_TTL_MINUTES must be between 15 and 10080")
        if not 1 <= self.pending_registration_ttl_hours <= 720:
            problems.append("PENDING_REGISTRATION_TTL_HOURS must be between 1 and 720")
        if problems:
            raise BackendConfigError("Invalid backend configuration: " + "; ".join(problems) + ".")


def _fernet_key_ok(key: str) -> bool:
    import base64
    import binascii

    try:
        return len(base64.urlsafe_b64decode(key.encode("ascii"))) == 32
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return False


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
        client_ip_header=environ.get("CLIENT_IP_HEADER", "").strip().lower(),
        password_hash_concurrency=integer("PASSWORD_HASH_CONCURRENCY", 2),
        password_hash_queue=integer("PASSWORD_HASH_QUEUE", 8),
        password_hash_wait_seconds=number("PASSWORD_HASH_WAIT_SECONDS", 5.0),
        web_concurrency=integer("WEB_CONCURRENCY", 1),
        password_hash_memory_budget_mb=integer("PASSWORD_HASH_MEMORY_BUDGET_MB", 0),
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
        app_public_url=environ.get("APP_PUBLIC_URL", "").strip().rstrip("/"),
        email_provider=environ.get("EMAIL_PROVIDER", "").strip().lower() or "smtp",
        resend_api_key=environ.get("RESEND_API_KEY", "").strip(),
        brevo_api_key=environ.get("BREVO_API_KEY", "").strip(),
        mailjet_api_key=environ.get("MAILJET_API_KEY", "").strip(),
        mailjet_secret_key=environ.get("MAILJET_SECRET_KEY", "").strip(),
        email_webhook_secret=environ.get("EMAIL_WEBHOOK_SECRET", "").strip(),
        email_diagnostic_recipient=environ.get("EMAIL_DIAGNOSTIC_RECIPIENT", "").strip(),
        verification_token_ttl_minutes=integer("VERIFICATION_TOKEN_TTL_MINUTES", 1440),
        pending_registration_ttl_hours=integer("PENDING_REGISTRATION_TTL_HOURS", 48),
        email_dns_check=flag("EMAIL_DNS_CHECK", False),
        require_verified_email=flag("REQUIRE_VERIFIED_EMAIL", False),
        outbox_worker_enabled=flag("OUTBOX_WORKER_ENABLED", True),
        mfa_encryption_key=environ.get("MFA_ENCRYPTION_KEY", "").strip(),
        mfa_encryption_key_previous=environ.get("MFA_ENCRYPTION_KEY_PREVIOUS", "").strip(),
        mfa_issuer=environ.get("MFA_ISSUER", "").strip() or "Schedule Maxing",
        api_docs_enabled=flag("API_DOCS_ENABLED", True) if environ.get("API_DOCS_ENABLED", "").strip() else None,
        password_breach_check=flag("PASSWORD_BREACH_CHECK", True),
    )
