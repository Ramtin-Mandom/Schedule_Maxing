"""Alembic environment for the backend schema (see backend/migrate.py)."""

from __future__ import annotations

import os

from alembic import context

from backend.database import create_backend_engine
from backend.models import Base
from backend.settings import normalize_database_url

config = context.config
target_metadata = Base.metadata


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # backend.migrate / tests pass an open connection
        _run(connection)
        return
    url = normalize_database_url(os.environ.get("DATABASE_URL", "").strip())
    if not url:
        raise RuntimeError("DATABASE_URL is required to run migrations.")
    engine = create_backend_engine(url)
    try:
        with engine.begin() as connection:
            _run(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline (SQL script) migrations are not supported; run them against a database.")
run_migrations_online()
