"""Configuration fail-closed and signed-token validation regressions."""

import uuid
from datetime import timedelta

import jwt
import pytest

from backend.security import TokenError, issue_access_token, verify_access_token
from backend.settings import BackendConfigError, BackendSettings, load_settings
from tests.backend.conftest import TEST_SECRET


@pytest.mark.parametrize("origin", ["*", "https://example.com/path", "https://user:pass@example.com",
                                   "https://example.com?token=x", "https://example.com#x",
                                   "https://", "https://example.com/", "https://example.com:bad"])
def test_origins_must_be_exact_origins(origin):
    for name in ("cors_origins", "allowed_origins"):
        with pytest.raises(BackendConfigError):
            BackendSettings("sqlite://", TEST_SECRET, **{name: (origin,)})


def test_production_requires_postgres_and_explicit_hosts():
    with pytest.raises(BackendConfigError, match="DATABASE_URL"):
        BackendSettings("sqlite://", TEST_SECRET, environment="production", allowed_hosts=("api.example.com",))
    with pytest.raises(BackendConfigError, match="ALLOWED_HOSTS"):
        BackendSettings("postgresql://localhost/test", TEST_SECRET, environment="production")
    with pytest.raises(BackendConfigError, match="ALLOWED_HOSTS"):
        BackendSettings("postgresql://localhost/test", TEST_SECRET, environment="production", allowed_hosts=("*",))
    with pytest.raises(BackendConfigError, match="BROWSER_COOKIE_SECURE"):
        BackendSettings("postgresql://localhost/test", TEST_SECRET, environment="production",
                        allowed_hosts=("example.com",), browser_cookie_secure=False)


def test_refresh_lifetime_is_bounded_and_configuration_loads():
    settings = load_settings({"DATABASE_URL": "sqlite://", "JWT_SECRET": TEST_SECRET,
                              "REFRESH_TOKEN_EXPIRE_DAYS": "7", "ENVIRONMENT": "test"})
    assert settings.refresh_token_expire_days == 7 and settings.environment == "test"
    for days in (0, 366):
        with pytest.raises(BackendConfigError, match="REFRESH_TOKEN_EXPIRE_DAYS"):
            BackendSettings("sqlite://", TEST_SECRET, refresh_token_expire_days=days)


@pytest.mark.parametrize("claim,value", [("iat", True), ("iat", "123"), ("exp", False),
                                        ("nbf", True), ("sid", "not-a-uuid")])
def test_malformed_signed_claims_are_rejected(settings, clock, claim, value):
    issued = issue_access_token(uuid.uuid4(), settings, clock())
    claims = jwt.decode(issued.token, options={"verify_signature": False})
    claims[claim] = value
    token = jwt.encode(claims, settings.jwt_secret, algorithm="HS256")
    with pytest.raises(TokenError):
        verify_access_token(token, settings, clock())


def test_future_issued_at_is_rejected(settings, clock):
    issued = issue_access_token(uuid.uuid4(), settings, clock())
    claims = jwt.decode(issued.token, options={"verify_signature": False})
    claims["iat"] = int((clock() + timedelta(minutes=1)).timestamp())
    token = jwt.encode(claims, settings.jwt_secret, algorithm="HS256")
    with pytest.raises(TokenError):
        verify_access_token(token, settings, clock())


def test_application_disposes_only_its_own_engine(settings, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app import create_app
    from backend.database import create_backend_engine

    engine = create_backend_engine("sqlite://")
    calls = []
    monkeypatch.setattr(engine, "dispose", lambda: calls.append(True))
    with TestClient(create_app(settings, engine=engine)):
        pass
    assert not calls
    monkeypatch.setattr("backend.app.create_backend_engine", lambda *args, **kwargs: engine)
    with TestClient(create_app(settings)):
        pass
    assert calls == [True]


@pytest.mark.parametrize("url", ["postgres://user:password@remote.example/db", "postgresql://user@/db"])
def test_remote_or_environment_selected_database_requires_tls(monkeypatch, url):
    from backend.database import create_backend_engine

    captured = []
    monkeypatch.setattr("backend.database.create_engine", lambda value, **kwargs: captured.append((value, kwargs)))
    create_backend_engine(url)
    parsed, options = captured[0]
    assert parsed.query["sslmode"] == "require"
    assert parsed.drivername == "postgresql+psycopg" and options["hide_parameters"]


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_weak_remote_database_tls_is_rejected(mode):
    from backend.database import create_backend_engine

    with pytest.raises(BackendConfigError, match="TLS"):
        create_backend_engine("postgresql://remote.example/db?sslmode=" + mode)


def test_openapi_requires_authentication_for_all_resource_routes(client):
    public = {"/health", "/ready", "/auth/register", "/auth/login", "/auth/refresh", "/auth/logout",
              "/auth/browser/login", "/auth/browser/session", "/auth/browser/logout",
              "/auth/recovery/request", "/auth/recovery/reset", "/planning/capabilities"}
    schema = client.get("/openapi.json").json()
    protected = 0
    for path, operations in schema["paths"].items():
        if path in public:
            continue
        for method, operation in operations.items():
            if method in {"get", "post", "put", "patch", "delete"}:
                assert operation.get("security"), (method, path)
                protected += 1
    assert protected > 40
