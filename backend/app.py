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

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from sqlalchemy import Engine
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.api import install_routes
from backend.database import create_backend_engine, session_factory
from backend.http_errors import install_error_handlers
from backend.planning_api import build_planning_router, hosted_capabilities, hosted_context_dependency
from backend.protection import BodySizeLimit, PrivateResponses, install_access_log_redaction
from backend.passwords import configure_password_work, hash_memory_mib
from backend.rate_limit import RateLimiter
from backend.recovery_api import recovery
from backend.email_webhooks import webhooks
from backend import password_policy
from backend.mail import MailAdapter, mail_for
from backend.mfa import cipher_for
from backend.security_api import security
from backend.outbox import OutboxWorker
from backend.verification_api import verification
from backend.settings import BackendConfigError, BackendSettings, load_settings
from backend.sync import sync

API_VERSION = "1.0.0"
LOG = logging.getLogger("schedule_maxing.api")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_UNSET = object()


def create_app(
    settings: BackendSettings | None = None,
    *,
    engine: Engine | None = None,
    clock: Callable[[], datetime] = _utcnow,
    mail: MailAdapter | None | object = _UNSET,
    recovery_delivery: MailAdapter | None | object = _UNSET,
) -> FastAPI:
    """
    mail: the transactional email adapter of the outbox (tests inject a fake);
    default: the provider the settings configure, else none.
    recovery_delivery: an older name for `mail` (recovery email goes through the same outbox).
    """
    if mail is _UNSET:
        mail = recovery_delivery
    settings = settings or load_settings()
    _bound_password_work(settings)
    owns_engine = engine is None
    engine = engine or create_backend_engine(settings.database_url, **_engine_options(settings))

    @asynccontextmanager
    async def lifespan(app):
        worker = None
        if settings.outbox_worker_enabled:
            worker = OutboxWorker(app.state.session_factory, app.state.mail, settings, clock)
            worker.start()
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()
            if owns_engine:
                engine.dispose()

    app = FastAPI(
        title="Schedule Maxing API",
        version=API_VERSION,
        description="User-scoped, versioned planning and execution records for Schedule Maxing clients.",
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        responses={code: {"description": description} for code, description in {
            401: "Missing, expired or revoked credentials.", 404: "Resource not found in the authenticated scope.",
            409: "Version, state or relationship conflict.", 422: "Invalid request.",
            429: "Rate limit exceeded; observe Retry-After.", 500: "Unexpected internal error (details withheld).",
        }.items()},
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory(engine)
    app.state.clock = clock
    app.state.rate_limiter = RateLimiter(app.state.session_factory, clock) if settings.rate_limit_enabled else None
    app.state.mail = mail_for(settings) if mail is _UNSET else mail
    app.state.mfa_cipher = cipher_for(settings.mfa_encryption_key, settings.mfa_encryption_key_previous)
    password_policy.configure(password_policy.HibpRangeChecker() if settings.password_breach_check else None)
    install_error_handlers(app)
    install_routes(app)
    app.include_router(verification)
    app.include_router(security)
    app.include_router(webhooks)
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
    app.add_middleware(PrivateResponses, hsts=settings.environment == "production")
    install_access_log_redaction()
    return app


def password_memory_peak_mib(settings: BackendSettings) -> float:
    """The most memory password hashing can hold across every worker process of this deployment."""
    return settings.web_concurrency * settings.password_hash_concurrency * hash_memory_mib()


def _bound_password_work(settings: BackendSettings) -> None:
    """
    Set this process's hashing gate (backend/passwords.py) and check the deployment-wide peak -- the gate bounds
    one process, so the worker count is part of the sum -- against PASSWORD_HASH_MEMORY_BUDGET_MB when one is set.
    """
    peak = password_memory_peak_mib(settings)
    if settings.password_hash_memory_budget_mb and peak > settings.password_hash_memory_budget_mb:
        raise BackendConfigError(
            "Invalid backend configuration: WEB_CONCURRENCY x PASSWORD_HASH_CONCURRENCY x the Argon2 memory cost "
            f"({hash_memory_mib():g} MiB) is {peak:g} MiB, more than PASSWORD_HASH_MEMORY_BUDGET_MB.")
    configure_password_work(settings.password_hash_concurrency, settings.password_hash_queue,
                            settings.password_hash_wait_seconds)
    LOG.info("password hashing: at most %g MiB (%d worker(s) x %d at once x %g MiB)", peak,
             settings.web_concurrency, settings.password_hash_concurrency, hash_memory_mib())


def _engine_options(settings: BackendSettings) -> dict:
    """Hosted PostgreSQL timeouts: connecting, waiting for a pooled connection, and each statement."""
    if not settings.database_url.startswith("postgresql"):
        return {}
    return {
        "pool_timeout": settings.db_pool_timeout_seconds,
        "connect_args": {"connect_timeout": settings.db_connect_timeout_seconds,
                         "options": f"-c statement_timeout={settings.db_statement_timeout_ms}"},
    }
