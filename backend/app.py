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
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.api import install_routes
from backend.database import create_backend_engine, session_factory
from backend.http_errors import install_error_handlers
from backend.planning_api import build_planning_router, hosted_capabilities, hosted_context_dependency
from backend.protection import BodySizeLimit, install_access_log_redaction
from backend.rate_limit import RateLimiter
from backend.recovery_api import recovery
from backend.recovery_delivery import RecoveryDeliveryAdapter, delivery_for
from backend.settings import BackendSettings, load_settings
from backend.sync import sync

API_VERSION = "1.0.0"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_UNSET = object()


def create_app(
    settings: BackendSettings | None = None,
    *,
    engine: Engine | None = None,
    clock: Callable[[], datetime] = _utcnow,
    recovery_delivery: RecoveryDeliveryAdapter | None | object = _UNSET,
) -> FastAPI:
    """
    recovery_delivery: the recovery link adapter (tests inject a fake);
    default: SMTP when the settings configure recovery, else none.
    """
    settings = settings or load_settings()
    engine = engine or create_backend_engine(settings.database_url, **_engine_options(settings))

    app = FastAPI(
        title="Schedule Maxing API",
        version=API_VERSION,
        description="User-scoped, versioned planning and execution records for Schedule Maxing clients.",
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory(engine)
    app.state.clock = clock
    app.state.rate_limiter = RateLimiter(app.state.session_factory, clock) if settings.rate_limit_enabled else None
    app.state.recovery_delivery = delivery_for(settings) if recovery_delivery is _UNSET else recovery_delivery
    install_error_handlers(app)
    install_routes(app)
    app.include_router(recovery)
    app.include_router(sync)
    app.include_router(build_planning_router(hosted_context_dependency(), hosted_capabilities))
    # Added innermost first: hosts are checked first, then CORS, then the body size (before any parsing).
    app.add_middleware(BodySizeLimit, max_bytes=settings.max_request_bytes)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=True,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"])
    if settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    install_access_log_redaction()
    return app


def _engine_options(settings: BackendSettings) -> dict:
    """Hosted PostgreSQL timeouts: connecting, waiting for a pooled connection, and each statement."""
    if not settings.database_url.startswith("postgresql"):
        return {}
    return {
        "pool_timeout": settings.db_pool_timeout_seconds,
        "connect_args": {"connect_timeout": settings.db_connect_timeout_seconds,
                         "options": f"-c statement_timeout={settings.db_statement_timeout_ms}"},
    }
