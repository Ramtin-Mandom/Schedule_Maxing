"""
backend/migrate.py

Versioned schema migrations (Alembic; scripts in backend/migrations/versions).

    python -m backend.migrate upgrade     # apply every pending migration (DATABASE_URL)
    python -m backend.migrate current     # print the applied revision
    python -m backend.migrate check       # exit 1 unless the database is at head

Only DATABASE_URL is read (not the JWT settings), so migrations can run as a
separate release step before the server starts. The URL is never printed.
Each migration runs in a transaction; PostgreSQL rolls a failed migration
back completely, leaving the previous revision in place.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine

from backend.settings import BackendConfigError, normalize_database_url

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def upgrade(engine: Engine, revision: str = "head") -> None:
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def downgrade(engine: Engine, revision: str) -> None:
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def main(argv: list[str] | None = None) -> int:
    from backend.database import create_backend_engine

    parser = argparse.ArgumentParser(description="Schedule Maxing backend schema migrations.")
    parser.add_argument("action", choices=["upgrade", "current", "check"])
    args = parser.parse_args(argv)

    url = normalize_database_url(os.environ.get("DATABASE_URL", "").strip())
    if not url:
        raise BackendConfigError("DATABASE_URL is required to run migrations.")
    engine = create_backend_engine(url)
    try:
        if args.action == "upgrade":
            upgrade(engine)
            print(f"Database is at revision {head_revision()}.")
            return 0
        with engine.connect() as connection:
            current = current_revision(connection)
        print(f"Current revision: {current}; head: {head_revision()}.")
        return 0 if args.action == "current" or current == head_revision() else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
