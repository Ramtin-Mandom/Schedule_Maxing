"""Fixtures of the direct desktop-to-PostgreSQL tests (app/persistence).

Every test gets its own migrated database: a file-based SQLite database by
default (a real connection pool, so session closure and threads behave as on
a server), or -- with BACKEND_TESTS_ON_POSTGRES=1 and a disposable
TEST_DATABASE_URL whose database name contains "test" -- a private schema of
that PostgreSQL database, dropped afterwards. Nothing here reads a .env file
or DATABASE_URL, and no test contacts any other server.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import make_url, text

from app.persistence.direct import DirectBackend
from backend.database import create_backend_engine
from backend.migrate import upgrade
from tests.db_template import clone_migrated
from backend.settings import normalize_database_url

PASSWORD = "correct horse battery"


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def postgres_url() -> str | None:
    if os.environ.get("BACKEND_TESTS_ON_POSTGRES") != "1":
        return None
    url = normalize_database_url(os.environ.get("TEST_DATABASE_URL", "").strip())
    parsed = make_url(url) if url else None
    if parsed is None or parsed.get_backend_name() != "postgresql" or "test" not in (parsed.database or "").lower():
        pytest.exit("BACKEND_TESTS_ON_POSTGRES=1 needs a PostgreSQL TEST_DATABASE_URL whose database name contains 'test'.")
    return url


@pytest.fixture
def blank_engine(tmp_path):
    url = postgres_url()
    if url is None:
        engine = create_backend_engine(f"sqlite:///{(tmp_path / 'direct.db').as_posix()}")
        yield engine
        engine.dispose()
        return
    schema = f"sm_test_{uuid.uuid4().hex[:12]}"
    admin = create_backend_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_backend_engine(url, connect_args={"options": f"-csearch_path={schema}"}, pool_size=10,
                                   hide_parameters=True)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def engine(blank_engine):
    if postgres_url() is None:
        clone_migrated(blank_engine)  # a private copy of a migrated database (tests/db_template.py)
    else:
        upgrade(blank_engine)
    return blank_engine


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def backend(engine, clock):
    direct = DirectBackend(engine, clock=clock)
    direct.check_schema()
    yield direct
    direct.close()


def account(backend: DirectBackend, email: str, **extra):
    backend.register(email=email, password=PASSWORD, **extra)
    return backend.sign_in(email=email, password=PASSWORD)


@pytest.fixture
def alice(backend):
    return account(backend, "alice@example.com", display_name="Alice")


@pytest.fixture
def bob(backend):
    return account(backend, "bob@example.com")
