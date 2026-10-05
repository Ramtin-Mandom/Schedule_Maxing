"""
tests/db_template.py

A fully migrated server database, built ONCE per test process with the real
Alembic chain (backend.migrate.upgrade) and then copied into each test's own
fresh SQLite database with SQLite's online backup API.

Every test still gets its own private database -- an exact page-for-page copy
of a just-migrated empty one, alembic_version included -- so no state is shared
between tests; only the ~0.4 s it takes to replay every migration is saved.
The migration path itself stays covered by the tests that call upgrade()
directly (tests/backend/test_migrations.py, test_normalized_migration.py, the
readiness and direct-mode migration tests).

PostgreSQL runs (BACKEND_TESTS_ON_POSTGRES=1) do not use this: their fixtures
keep migrating each private schema for real.
"""

from __future__ import annotations

import sqlite3
import threading

_lock = threading.Lock()
_template: sqlite3.Connection | None = None


def _template_connection() -> sqlite3.Connection:
    global _template
    with _lock:
        if _template is None:
            from backend.database import create_backend_engine
            from backend.migrate import upgrade

            engine = create_backend_engine("sqlite://")
            upgrade(engine)
            raw = engine.raw_connection()
            try:
                template = sqlite3.connect(":memory:", check_same_thread=False)
                raw.driver_connection.backup(template)
            finally:
                raw.close()
                engine.dispose()
            _template = template
    return _template


def clone_migrated(engine) -> None:
    """Fill `engine`'s fresh, empty SQLite database with an exact copy of a fully migrated one."""
    template = _template_connection()
    raw = engine.raw_connection()
    try:
        with _lock:
            template.backup(raw.driver_connection)
    finally:
        raw.close()
