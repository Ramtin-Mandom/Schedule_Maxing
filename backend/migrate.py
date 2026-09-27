"""
backend/migrate.py

Versioned schema migrations (Alembic; scripts in backend/migrations/versions).

    python -m backend.migrate upgrade     # apply every pending migration (DATABASE_URL)
    python -m backend.migrate current     # print the applied revision
    python -m backend.migrate check       # exit 1 unless the database is at head
    python -m backend.migrate --env-file .env upgrade   # the same, with DATABASE_URL from that file

Only DATABASE_URL is read (not the JWT settings), so migrations can run as a
separate release step before the server starts. The URL is never printed.
With --env-file (the direct desktop workflow, app/persistence/config.py) the
named file is read -- the process environment still wins -- and a remote
server must be reached over TLS (sslmode=require unless verify-ca/
verify-full is given). Without it, behavior is unchanged: environment only.
An expected failure (configuration, unreachable database, credentials,
data that cannot be migrated) prints one safe message and exits with code
2 -- never a traceback, the URL, host credentials or SQL parameters.
One upgrade (every pending revision) runs in one transaction: a failed
migration -- e.g. existing data that cannot be converted exactly -- rolls
the whole upgrade back, leaving the previous revision in place. On SQLite
(tests) the transaction is explicit, and foreign-key enforcement is
suspended while tables are rebuilt and checked (PRAGMA foreign_key_check)
before the commit.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine

from backend.settings import BackendConfigError, normalize_database_url

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class MigrationIntegrityError(RuntimeError):
    """A migration left rows that violate a foreign key (SQLite check); the upgrade was rolled back."""


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


@contextmanager
def _migration_transaction(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as connection:
        if connection.dialect.name != "sqlite":
            with connection.begin():
                yield connection
            return
        # pysqlite only begins a transaction before DML, so DDL would commit piecemeal: begin explicitly.
        # PRAGMA foreign_keys cannot change inside a transaction, so it is switched before and after.
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        try:
            connection.exec_driver_sql("BEGIN")
            try:
                yield connection
                violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if violations:
                    tables = ", ".join(sorted({str(row[0]) for row in violations}))
                    raise MigrationIntegrityError(f"{len(violations)} rows violate foreign keys (tables: {tables}).")
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            connection.commit()


def upgrade(engine: Engine, revision: str = "head") -> None:
    config = alembic_config()
    with _migration_transaction(engine) as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def downgrade(engine: Engine, revision: str) -> None:
    """For tests and disposable databases only; a real database is only ever upgraded (docs/backend.md)."""
    config = alembic_config()
    with _migration_transaction(engine) as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def _database_url(env_file: str | None) -> tuple[str | object, dict]:
    """The URL to migrate and the engine options: environment only, or --env-file with the direct TLS policy."""
    if env_file is None:
        url = normalize_database_url(os.environ.get("DATABASE_URL", "").strip())
        if not url:
            raise BackendConfigError("DATABASE_URL is required to run migrations.")
        return url, {}
    from app.persistence.config import load_direct_settings

    settings = load_direct_settings(env_file=env_file)
    return settings.effective_url(), {"hide_parameters": True,
                                      "connect_args": {"connect_timeout": settings.connect_timeout_seconds}}


def _safe_failure(error: BaseException) -> str | None:
    """A one-line, secret-free description of an expected failure (None: not an expected failure)."""
    from sqlalchemy import exc as sa_exc

    try:
        from app.persistence.errors import DirectPersistenceError
    except ImportError:  # pragma: no cover - the desktop package is part of this repository
        DirectPersistenceError = ()  # noqa: N806
    if isinstance(error, (BackendConfigError, MigrationIntegrityError)) or (
            DirectPersistenceError and isinstance(error, DirectPersistenceError)):
        return str(error)
    if isinstance(error, sa_exc.SQLAlchemyError):
        from app.persistence.direct import safe_database_error

        return str(safe_database_error(error))
    from backend.migrations.normalized_storage import MigrationDataError

    if isinstance(error, MigrationDataError):  # a migration refused existing data (names rows, never values)
        return str(error)
    return None


def main(argv: list[str] | None = None) -> int:
    from backend.database import create_backend_engine

    parser = argparse.ArgumentParser(description="Schedule Maxing backend schema migrations.")
    parser.add_argument("--env-file", metavar="PATH",
                        help="read DATABASE_URL from this file (the process environment still wins); "
                             "enforces TLS for a remote server")
    parser.add_argument("action", choices=["upgrade", "current", "check"])
    args = parser.parse_args(argv)

    engine = None
    try:
        url, options = _database_url(args.env_file)
        engine = create_backend_engine(url, **options) if options else create_backend_engine(url)
        if args.action == "upgrade":
            upgrade(engine)
            print(f"Database is at revision {head_revision()}.")
            return 0
        with engine.connect() as connection:
            current = current_revision(connection)
        print(f"Current revision: {current}; head: {head_revision()}.")
        return 0 if args.action == "current" or current == head_revision() else 1
    except Exception as error:  # noqa: BLE001 - expected failures become one safe line; others propagate
        message = _safe_failure(error)
        if message is None:
            raise
        print(f"error: {message}", file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
