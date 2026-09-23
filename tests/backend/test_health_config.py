"""Liveness/readiness, configuration errors that never reveal secrets, OpenAPI,
and independence of the desktop app from the backend."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.database import create_backend_engine
from backend.migrate import head_revision, upgrade
from backend.settings import BackendConfigError, load_settings, normalize_database_url
from tests.backend.conftest import TEST_SECRET

ROOT = Path(__file__).resolve().parents[2]


def test_liveness_needs_no_database(settings, tmp_path) -> None:
    unreachable = create_backend_engine(f"sqlite:///{tmp_path / 'missing' / 'no.db'}")
    with TestClient(create_app(settings, engine=unreachable)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        ready = client.get("/ready")
        assert ready.status_code == 503 and ready.json()["database"] == "unreachable"


def test_readiness_requires_a_fully_migrated_database(settings) -> None:
    engine = create_backend_engine("sqlite://")
    with TestClient(create_app(settings, engine=engine)) as client:
        pending = client.get("/ready")
        assert pending.status_code == 503 and pending.json()["migrations"] == "pending"
        upgrade(engine)
        ready = client.get("/ready").json()
        assert ready == {"status": "ready", "database": "reachable", "migrations": "current", "revision": head_revision()}


def test_missing_settings_fail_clearly_without_revealing_values() -> None:
    with pytest.raises(BackendConfigError) as missing:
        load_settings({})
    assert "DATABASE_URL" in str(missing.value) and "JWT_SECRET" in str(missing.value)

    secret_url = "postgresql://planner:hunter2-password@db.internal/app"
    with pytest.raises(BackendConfigError) as weak:
        load_settings({"DATABASE_URL": secret_url, "JWT_SECRET": "tiny-secret"})
    assert "JWT_SECRET" in str(weak.value)
    assert "hunter2" not in str(weak.value) and "tiny-secret" not in str(weak.value)

    with pytest.raises(BackendConfigError, match="ACCESS_TOKEN_TTL_MINUTES"):
        load_settings({"DATABASE_URL": secret_url, "JWT_SECRET": TEST_SECRET, "ACCESS_TOKEN_TTL_MINUTES": "soon"})


def test_settings_never_print_secrets_and_normalize_postgres_urls() -> None:
    settings = load_settings({"DATABASE_URL": "postgres://u:hunter2@host:5432/app", "JWT_SECRET": TEST_SECRET})
    assert settings.database_url == "postgresql+psycopg://u:hunter2@host:5432/app"
    assert "hunter2" not in repr(settings) and TEST_SECRET not in repr(settings)
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"


def test_the_factory_refuses_to_start_without_configuration(monkeypatch) -> None:
    for name in ("DATABASE_URL", "JWT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(BackendConfigError, match="DATABASE_URL"):
        create_app()


def test_openapi_documents_every_resource_and_bearer_auth(client) -> None:
    schema = client.get("/openapi.json").json()
    for path in ("/auth/register", "/auth/login", "/me", "/projects", "/tasks/{record_id}", "/fixed-blocks",
                 "/placements", "/preferences", "/schedule-generations", "/executions/{record_id}/actions/{action}",
                 "/changes", "/health", "/ready"):
        assert path in schema["paths"], path
    assert "HTTPBearer" in schema["components"]["securitySchemes"]


def test_desktop_startup_does_not_import_the_backend() -> None:
    code = (
        "import sys, app.main, app.ui.app_services\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in ('backend', 'fastapi', 'sqlalchemy', 'jwt')))"
    )
    env = {name: value for name, value in os.environ.items() if name not in ("DATABASE_URL", "JWT_SECRET")}
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
