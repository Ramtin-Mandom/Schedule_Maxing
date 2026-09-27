"""
app/persistence -- the optional direct desktop-to-PostgreSQL composition.

The desktop's default storage stays the local SQLite database
(app/execution/db.py). This package is the *optional* alternative: the
desktop process talks to the server's PostgreSQL schema directly, through
the backend's framework-free services (accounts, planning repository,
mutation path) -- no FastAPI, no HTTP, no JWT.

Importing this package reads nothing and connects nowhere. Entry points:

    from app.persistence import load_direct_settings, open_direct_backend
    settings = load_direct_settings(env_file=".env")   # explicit file; the process environment wins
    backend = open_direct_backend(settings)             # checks the schema revision; never migrates

It needs the packages of requirements-direct.txt (SQLAlchemy, psycopg,
Alembic, argon2-cffi, python-dotenv); without them open_direct_backend()
raises DirectModeUnavailableError naming the missing package.

Modules: config.py (settings, env file, TLS), errors.py (safe errors),
direct.py (engine, units of work, accounts, AccountSession), planning.py
(DirectPlanningRepository for PlanningService), executions.py
(DirectExecutionService, DirectExecutionReader).
"""

from __future__ import annotations

from app.persistence.config import DirectDatabaseSettings, load_direct_settings
from app.persistence.errors import DirectModeUnavailableError

__all__ = ["DirectDatabaseSettings", "load_direct_settings", "open_direct_backend"]

#: The optional packages direct mode imports (psycopg is loaded when the engine is created).
OPTIONAL_PACKAGES = ("sqlalchemy", "psycopg", "alembic", "argon2", "dotenv")


def open_direct_backend(settings: DirectDatabaseSettings, **options):
    """A connected app.persistence.direct.DirectBackend (see its docstring)."""
    try:
        from app.persistence.direct import DirectBackend

        return DirectBackend.open(settings, **options)
    except ModuleNotFoundError as error:
        root = (error.name or "").partition(".")[0]
        if root not in OPTIONAL_PACKAGES:
            raise
        raise DirectModeUnavailableError(root) from None
