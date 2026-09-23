"""Versioned backend migrations: they create exactly the ORM schema (no drift),
upgrade/downgrade cleanly, and the release-step CLI works from DATABASE_URL."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from backend.database import create_backend_engine
from backend.migrate import current_revision, downgrade, head_revision, upgrade
from backend.models import Base

ROOT = Path(__file__).resolve().parents[2]


def test_migrations_create_exactly_the_model_schema() -> None:
    engine = create_backend_engine("sqlite://")
    upgrade(engine)
    with engine.connect() as connection:
        assert current_revision(connection) == head_revision()
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
        tables = set(inspect(connection).get_table_names())
    assert tables == set(Base.metadata.tables) | {"alembic_version"}


def test_downgrade_and_upgrade_again() -> None:
    engine = create_backend_engine("sqlite://")
    upgrade(engine)
    downgrade(engine, "base")
    with engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) == {"alembic_version"}
    upgrade(engine)
    upgrade(engine)  # repeated: a no-op
    with engine.connect() as connection:
        assert current_revision(connection) == head_revision()


def _cli(action: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "backend.migrate", action], cwd=ROOT, capture_output=True,
                          text=True, timeout=120, env={**os.environ, **env})


def test_the_migration_cli_uses_database_url_only(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'server.db').as_posix()}"
    env = {"DATABASE_URL": url, "JWT_SECRET": ""}  # migrations do not need the auth settings

    assert _cli("check", env).returncode == 1  # not migrated yet
    upgraded = _cli("upgrade", env)
    assert upgraded.returncode == 0, upgraded.stderr
    assert _cli("check", env).returncode == 0
    assert url not in upgraded.stdout + upgraded.stderr

    missing = _cli("upgrade", {"DATABASE_URL": ""})
    assert missing.returncode != 0 and "DATABASE_URL is required" in missing.stderr
    engine = create_backend_engine(url)
    with engine.connect() as connection:
        assert current_revision(connection) == head_revision()
    engine.dispose()
