"""
backend/app.py

The FastAPI application factory.

    create_app()                               # production: settings from the environment
    create_app(settings, engine=..., clock=...) # tests: injected database and clock

Serve it with the factory flag so importing this module never reads
configuration: `uvicorn --factory backend.app:create_app --host 0.0.0.0
--port $PORT`. Missing or invalid settings raise BackendConfigError naming
the settings (never their values) before the server accepts connections.
The schema is not created here: run `python -m backend.migrate upgrade`
first; /ready reports 503 until the database is at the latest revision.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import FastAPI
from sqlalchemy import Engine

from backend.api import install_routes
from backend.database import create_backend_engine, session_factory
from backend.errors import install_error_handlers
from backend.settings import BackendSettings, load_settings
from backend.sync import sync

API_VERSION = "1.0.0"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_app(
    settings: BackendSettings | None = None,
    *,
    engine: Engine | None = None,
    clock: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    settings = settings or load_settings()
    engine = engine or create_backend_engine(settings.database_url)

    app = FastAPI(
        title="Schedule Maxing API",
        version=API_VERSION,
        description="User-scoped, versioned planning and execution records for Schedule Maxing clients.",
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory(engine)
    app.state.clock = clock
    install_error_handlers(app)
    install_routes(app)
    app.include_router(sync)
    return app
