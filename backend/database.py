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

from sqlalchemy import JSON, DateTime, Engine, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

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


def create_backend_engine(url: str, **kwargs) -> Engine:
    """
    An engine for `url`. For SQLite (tests, local experiments) foreign keys
    are enabled on every connection, and an in-memory database uses one
    shared connection so every session sees the same data.
    """
    if url.startswith("sqlite"):
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
