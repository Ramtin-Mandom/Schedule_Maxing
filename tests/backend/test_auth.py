"""Accounts and access tokens: registration, normalized uniqueness enforced by
the database, secure password storage, login, and strict token verification
(signature, algorithm, expiry, issuer/audience, type, subject)."""

from __future__ import annotations

import logging
import uuid

import jwt
import pytest
from sqlalchemy import select

from backend import models
from backend.security import TOKEN_TYPE
from tests.backend.conftest import PASSWORD, TEST_SECRET, login_headers, register

# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------


def test_registration_stores_only_a_password_hash(client, engine, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    user = register(client, "Alice@Example.COM", username="Alice_1", display_name="Alice")

    assert user["email"] == "alice@example.com" and user["username"] == "alice_1"
    assert "password" not in user and "password_hash" not in user
    with engine.connect() as connection:
        stored = connection.execute(select(models.User.password_hash)).scalar_one()
    assert stored.startswith("$argon2id$") and PASSWORD not in stored
    assert PASSWORD not in caplog.text


@pytest.mark.parametrize("variant", ["alice@example.com", "ALICE@example.com", "  alice@example.com ",
                                     "ａlice@example.com"])  # fullwidth "a" normalizes (NFKC) to "a"
def test_duplicate_accounts_are_refused_after_normalization(client, variant) -> None:
    register(client, "alice@example.com")
    response = client.post("/auth/register", json={"email": variant, "password": PASSWORD})
    assert response.status_code == 409 and response.json()["error"]["code"] == "account_exists"


def test_duplicate_usernames_are_refused(client) -> None:
    register(client, "a@example.com", username="planner")
    response = client.post("/auth/register", json={"email": "b@example.com", "password": PASSWORD, "username": "PLANNER"})
    assert response.status_code == 409


def test_uniqueness_is_enforced_by_the_database_itself(client, engine) -> None:
    """The API relies on the unique index, not a pre-check, so concurrent registrations cannot both succeed."""
    register(client, "alice@example.com")
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(models.User(id=uuid.uuid4(), email="alice@example.com", password_hash="x",
                                created_at=now, updated_at=now, version=1))
        session.commit()


@pytest.mark.parametrize("payload", [
    {"email": "not-an-email", "password": PASSWORD},
    {"email": "a@example.com", "password": "short"},
    {"email": "a@example.com", "password": PASSWORD, "is_admin": True},
])
def test_invalid_registrations_are_rejected_without_echoing_the_password(client, payload) -> None:
    response = client.post("/auth/register", json=payload)
    assert response.status_code == 422
    assert PASSWORD not in response.text and "short" not in response.text.replace("too short", "")


# -----------------------------------------------------------------------------
# Login
# -----------------------------------------------------------------------------


def test_login_by_email_or_username(client) -> None:
    register(client, "alice@example.com", username="alice")
    by_email = client.post("/auth/login", json={"email": " ALICE@example.com", "password": PASSWORD})
    by_name = client.post("/auth/login", json={"username": "Alice", "password": PASSWORD})
    assert by_email.status_code == by_name.status_code == 200
    body = by_email.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == 3600


@pytest.mark.parametrize("payload", [
    {"email": "alice@example.com", "password": "wrong password"},
    {"email": "nobody@example.com", "password": PASSWORD},
])
def test_invalid_credentials_get_one_generic_401(client, payload) -> None:
    register(client, "alice@example.com")
    response = client.post("/auth/login", json=payload)
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "The email/username or password is incorrect."
    assert response.headers["WWW-Authenticate"] == "Bearer"


# -----------------------------------------------------------------------------
# Tokens
# -----------------------------------------------------------------------------


def _claims(user_id: str, **overrides) -> dict:
    return {"sub": user_id, "iss": "schedule-maxing", "aud": "schedule-maxing-api", "iat": 1772452800,
            "nbf": 1772452800, "exp": 1772456400, "jti": "j", "typ": TOKEN_TYPE, **overrides}


def test_me_needs_a_valid_token(client) -> None:
    user = register(client, "alice@example.com")
    headers = login_headers(client, "alice@example.com")

    assert client.get("/me", headers=headers).json()["id"] == user["id"]
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.get("/me", headers={"Authorization": "Bearer not.a.token"}).status_code == 401


@pytest.mark.parametrize("forgery", ["other_secret", "alg_none", "wrong_audience", "wrong_issuer", "refresh_type",
                                     "missing_jti", "unknown_user", "not_a_uuid"])
def test_forged_or_foreign_tokens_are_rejected(client, clock, forgery) -> None:
    user = register(client, "alice@example.com")
    clock.now = clock.now.replace(year=2026, month=3, day=2, hour=12)  # inside the claims' validity window
    claims = _claims(user["id"])
    secret = TEST_SECRET
    if forgery == "other_secret":
        secret = "another-secret-" + "y" * 32
    elif forgery == "wrong_audience":
        claims["aud"] = "someone-else"
    elif forgery == "wrong_issuer":
        claims["iss"] = "someone-else"
    elif forgery == "refresh_type":
        claims["typ"] = "refresh"
    elif forgery == "missing_jti":
        del claims["jti"]
    elif forgery == "unknown_user":
        claims["sub"] = str(uuid.uuid4())
    elif forgery == "not_a_uuid":
        claims["sub"] = "admin"
    token = jwt.encode(claims, secret, algorithm="HS256")
    if forgery == "alg_none":
        token = jwt.encode(claims, None, algorithm="none")

    genuine = jwt.encode(_claims(user["id"]), TEST_SECRET, algorithm="HS256")
    assert client.get("/me", headers={"Authorization": f"Bearer {genuine}"}).status_code == 200
    assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_tokens_expire_by_the_server_clock(client, clock) -> None:
    register(client, "alice@example.com")
    headers = login_headers(client, "alice@example.com")
    clock.advance(minutes=59)
    assert client.get("/me", headers=headers).status_code == 200
    clock.advance(minutes=1)
    response = client.get("/me", headers=headers)
    assert response.status_code == 401 and "expired" in response.json()["error"]["message"]


def test_identity_comes_only_from_the_token(client) -> None:
    register(client, "alice@example.com")
    bob = register(client, "bob@example.com")
    alice_headers = login_headers(client, "alice@example.com")
    response = client.get("/me", headers={**alice_headers, "X-User-Id": bob["id"]}, params={"user_id": bob["id"]})
    assert response.json()["email"] == "alice@example.com"


def test_profile_update_uses_a_version_precondition(client) -> None:
    register(client, "alice@example.com")
    headers = login_headers(client, "alice@example.com")
    updated = client.patch("/me", json={"base_version": 1, "display_name": "Al"}, headers=headers)
    assert updated.json()["version"] == 2
    stale = client.patch("/me", json={"base_version": 1, "display_name": "Stale"}, headers=headers)
    assert stale.status_code == 409 and stale.json()["error"]["current"]["display_name"] == "Al"
    assert client.patch("/me", json={"base_version": 2, "email": "x@y.z"}, headers=headers).status_code == 422
