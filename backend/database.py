"""
backend/database.py

Engine/session construction and portable column types.

PostgreSQL (psycopg 3) is the production database. SQLite is supported only
so the ordinary test suite runs without a server; PostgreSQL-specific
behavior is verified by the optional `postgres`-marked tests
(tests/backend/test_postgres.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
import ipaddress

from sqlalchemy import JSON, URL, DateTime, Engine, create_engine, event, make_url
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from backend.settings import BackendConfigError, normalize_database_url

#: JSON documents: JSONB on PostgreSQL, JSON text elsewhere.
JSONDocument = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator):
    """
    An aware UTC instant. Stored as TIMESTAMP WITH TIME ZONE on PostgreSQL;
    SQLite has no time zones, so values are stored as UTC and re-marked UTC
    when read. A naive datetime is rejected rather than guessed at.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not accepted; use an aware UTC instant")
        value = value.astimezone(timezone.utc)
        return value if dialect.name == "postgresql" else value.replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def create_backend_engine(url: str | URL, **kwargs) -> Engine:
    """
    An engine for `url` (a string, or a sqlalchemy URL -- passed through
    unrendered, so its escaping is kept). For SQLite (tests, local
    experiments) foreign keys are enabled on every connection, and an
    in-memory database uses one shared connection so every session sees the
    same data.
    """
    kwargs.setdefault("hide_parameters", True)
    parsed = make_url(normalize_database_url(url) if isinstance(url, str) else url)
    if parsed.get_backend_name() == "postgresql":
        host = parsed.host or parsed.query.get("host", "")
        # An omitted host may be supplied by libpq's PGHOST environment;
        # require TLS unless the URL explicitly names a local destination.
        local = str(host).lower() == "localhost" or str(host).startswith("/")
        try:
            local = local or ipaddress.ip_address(str(host).strip("[]")).is_loopback
        except ValueError:
            pass
        if not local:
            sslmode = parsed.query.get("sslmode")
            if sslmode is not None and sslmode not in ("require", "verify-ca", "verify-full"):
                raise BackendConfigError("DATABASE_URL must require TLS for remote PostgreSQL.")
            if sslmode is None:
                parsed = parsed.update_query_dict({"sslmode": "require"})
    url = parsed
    if parsed.get_backend_name() == "sqlite":
        url = str(url)
        options = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url in ("sqlite://", "sqlite+pysqlite://"):
            options["poolclass"] = StaticPool
        options.update(kwargs)
        engine = create_engine(url, **options)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.close()

        return engine
    return create_engine(url, pool_pre_ping=True, **kwargs)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """A request-scoped session: rolled back on error, always closed. Commits are explicit."""
    session = factory()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
