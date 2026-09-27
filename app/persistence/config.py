"""
app/persistence/config.py

Configuration of the direct desktop-to-PostgreSQL connection. Independent of
the HTTP backend's settings: it needs no JWT secret.

    settings = load_direct_settings(env_file=Path(".env"))   # at an entry point only
    url = settings.effective_url()                           # a sqlalchemy URL, TLS enforced

Loading rules:
    - Nothing is read at import time. Only an application/CLI entry point
      calls load_direct_settings(), with an explicitly selected env file (or
      none: environment only). There is no search for .env files elsewhere.
    - The process environment overrides the env file. Values are taken
      verbatim (no variable interpolation), so a password containing '$' or
      '%'-escapes is preserved exactly.
    - DATABASE_URL is the connection. "postgres://" and "postgresql://" use
      the psycopg 3 driver (backend.settings.normalize_database_url); host,
      database, credentials, query options and their escaping are kept.

TLS: a remote server (any host other than localhost/127.0.0.1/::1 or a
Unix socket) must be reached over TLS. Without an sslmode the connection
uses sslmode=require (in memory -- the file is never rewritten);
require/verify-ca/verify-full are kept; an explicit weaker mode (disable,
allow, prefer) is refused. verify-full is the strongest choice when the
server's CA certificate is available (sslrootcert=...).

Pooling: a small pool (default 3 + 2 overflow), pre-ping on checkout,
bounded pool and connect timeouts, SQL parameters hidden from errors and
logs, no SQL echo. Every message here names settings, never their values.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.persistence.errors import DirectConfigError, DirectModeUnavailableError

ENV_DATABASE_URL = "DATABASE_URL"
#: sslmode values that guarantee an encrypted connection; the last two also verify the server.
SECURE_SSLMODES = ("require", "verify-ca", "verify-full")
WEAK_SSLMODES = ("disable", "allow", "prefer")
APPLICATION_NAME = "schedule-maxing-desktop"


@dataclass(frozen=True)
class DirectDatabaseSettings:
    database_url: str = field(repr=False)
    pool_size: int = 3
    max_overflow: int = 2
    pool_timeout_seconds: float = 10.0
    connect_timeout_seconds: int = 10
    #: Where DATABASE_URL came from ("environment" or "env file") -- safe to show; the value never is.
    source: str = "environment"

    def __post_init__(self) -> None:
        if not self.database_url:
            raise DirectConfigError(f"{ENV_DATABASE_URL} is required for direct PostgreSQL mode.")
        if not (1 <= self.pool_size <= 10 and 0 <= self.max_overflow <= 10):
            raise DirectConfigError("The connection pool must hold 1-10 connections with 0-10 overflow.")
        if not (0 < self.pool_timeout_seconds <= 120 and 0 < self.connect_timeout_seconds <= 120):
            raise DirectConfigError("Connection and pool timeouts must be between 1 and 120 seconds.")

    def effective_url(self):
        """The sqlalchemy URL to connect with: psycopg 3, TLS enforced for a remote host."""
        return effective_url(self.database_url)

    def describe(self) -> dict[str, str]:
        """Safe facts about the connection (no credentials, no full host): for status output and logs."""
        url = self.effective_url()
        return {"source": self.source, "tls": _sslmode(url) or "not required (local server)",
                "local_server": str(_is_local(url))}


def read_env_file(path: str | Path) -> dict[str, str]:
    """The variables of one explicitly selected .env file, verbatim (python-dotenv, no interpolation)."""
    env_path = Path(path)
    if not env_path.is_file():
        raise DirectConfigError(f"The env file {env_path} does not exist.")
    try:
        from dotenv import dotenv_values
    except ModuleNotFoundError:
        raise DirectModeUnavailableError("python-dotenv") from None
    values = dotenv_values(env_path, interpolate=False)
    return {key: value for key, value in values.items() if value is not None}


def merged_environment(env_file: str | Path | None, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The env file's variables overridden by the process environment."""
    environ = os.environ if environ is None else environ
    merged = read_env_file(env_file) if env_file is not None else {}
    merged.update(environ)
    return merged


def load_direct_settings(env_file: str | Path | None = None, environ: Mapping[str, str] | None = None,
                         **overrides) -> DirectDatabaseSettings:
    """Direct-mode settings from the process environment and, if given, one explicit env file."""
    environ = os.environ if environ is None else environ
    from_environment = bool(environ.get(ENV_DATABASE_URL, "").strip())
    values = merged_environment(env_file, environ)
    raw = values.get(ENV_DATABASE_URL, "").strip()
    source = "environment" if from_environment else "env file"
    settings = DirectDatabaseSettings(database_url=normalize_url(raw), source=source, **overrides)
    settings.effective_url()  # validate now (driver, host, TLS) so a bad URL fails at startup
    return settings


def normalize_url(raw: str) -> str:
    from backend.settings import normalize_database_url  # a pure function; imports nothing optional

    return normalize_database_url(raw)


def _import_sqlalchemy_url():
    try:
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError
    except ModuleNotFoundError:
        raise DirectModeUnavailableError("SQLAlchemy") from None
    return make_url, ArgumentError


def _query_value(url, name: str) -> str | None:
    value = url.query.get(name)
    if isinstance(value, tuple):
        value = value[-1] if value else None
    return value


def _sslmode(url) -> str | None:
    return _query_value(url, "sslmode")


def _is_local(url) -> bool:
    host = url.host or _query_value(url, "host") or ""
    if not host or host.startswith("/"):
        return True  # a Unix-domain socket
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def effective_url(raw: str):
    """
    Parse `raw` (already psycopg-normalized) and return the URL to connect
    with. Refuses non-PostgreSQL URLs, other drivers and weak TLS modes for a
    remote host, without echoing any part of the URL.
    """
    make_url, argument_error = _import_sqlalchemy_url()
    try:
        url = make_url(raw)
    except (argument_error, ValueError):
        raise DirectConfigError(f"{ENV_DATABASE_URL} is not a valid database URL.") from None
    if url.get_backend_name() != "postgresql":
        raise DirectConfigError(f"{ENV_DATABASE_URL} must be a PostgreSQL URL (postgresql://...).")
    if url.get_driver_name() != "psycopg":
        raise DirectConfigError(f"{ENV_DATABASE_URL} must use the psycopg 3 driver (postgresql://... or "
                                "postgresql+psycopg://...).")
    if not url.database:
        raise DirectConfigError(f"{ENV_DATABASE_URL} must name a database.")
    if _is_local(url):
        return url
    mode = _sslmode(url)
    if mode is None:
        return url.update_query_dict({"sslmode": "require"})
    if mode in SECURE_SSLMODES:
        return url
    if mode in WEAK_SSLMODES:
        raise DirectConfigError(
            f"{ENV_DATABASE_URL} asks for sslmode={mode}, which may send data unencrypted to a remote server. Use "
            "sslmode=require, verify-ca or verify-full (or remove sslmode: require is then used)."
        )
    raise DirectConfigError(f"{ENV_DATABASE_URL} has an unknown sslmode; use require, verify-ca or verify-full.")


def engine_options(settings: DirectDatabaseSettings) -> dict:
    """create_engine() keyword arguments of the direct connection pool."""
    return {
        "pool_pre_ping": True,
        "pool_size": settings.pool_size,
        "max_overflow": settings.max_overflow,
        "pool_timeout": settings.pool_timeout_seconds,
        "pool_recycle": 1800,
        "hide_parameters": True,
        "echo": False,
        "connect_args": {"connect_timeout": settings.connect_timeout_seconds, "application_name": APPLICATION_NAME},
    }
