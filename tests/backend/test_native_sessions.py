"""Rotation, revocation, ownership and secret-storage regressions for native clients."""

import uuid

import pytest
from sqlalchemy import select, update

from backend import models
from backend.database import session_factory
from tests.backend.conftest import PASSWORD, register


def sign_in(client, email="native@example.com"):
    register(client, email)
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def headers(pair):
    return {"Authorization": "Bearer " + pair["access_token"]}


def refresh(client, pair):
    return client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})


def test_refresh_rotates_and_preserves_identity(client, engine, clock):
    first = sign_in(client)
    owner = client.get("/auth/me", headers=headers(first)).json()["id"]
    assert client.get("/me", headers=headers(first)).json()["id"] == owner
    clock.advance(minutes=61)
    assert client.get("/auth/me", headers=headers(first)).status_code == 401
    response = refresh(client, first)
    assert response.status_code == 200
    second = response.json()
    assert first["access_token"] != second["access_token"]
    assert first["refresh_token"] != second["refresh_token"]
    assert first["refresh_expires_at"] == second["refresh_expires_at"]
    assert client.get("/auth/me", headers=headers(second)).json()["id"] == owner
    with session_factory(engine)() as session:
        family = session.scalar(select(models.NativeSession))
        credentials = session.scalars(select(models.RefreshCredential)).all()
        assert family.user_id == uuid.UUID(owner)
        assert len(credentials) == 2
        assert sum(row.consumed_at is not None for row in credentials) == 1
        assert all(row.token_hash not in (first["refresh_token"], second["refresh_token"]) for row in credentials)
        assert all(len(row.token_hash) == 64 for row in credentials)


def test_replay_revokes_family_but_not_another_login(client):
    first = sign_in(client)
    independent = client.post("/auth/login", json={"email": "native@example.com", "password": PASSWORD}).json()
    second = refresh(client, first).json()
    assert refresh(client, first).status_code == 401
    assert refresh(client, second).status_code == 401
    for pair in (first, second):
        assert client.get("/auth/me", headers=headers(pair)).status_code == 401
    assert client.get("/auth/me", headers=headers(independent)).status_code == 200


def test_logout_is_idempotent_and_revokes_only_its_session(client):
    alice, bob = sign_in(client), sign_in(client, "other@example.com")
    payload = {"refresh_token": alice["refresh_token"]}
    for _ in range(2):
        assert client.post("/auth/logout", json=payload).status_code == 204
    assert client.get("/auth/me", headers=headers(alice)).status_code == 401
    assert refresh(client, alice).status_code == 401
    assert client.get("/auth/me", headers=headers(bob)).status_code == 200
    assert client.post("/auth/logout", json={"refresh_token": "x" * 43}).status_code == 204


def test_absolute_expiry_and_password_epoch(client, clock, engine):
    pair = sign_in(client)
    owner = client.get("/auth/me", headers=headers(pair)).json()["id"]
    with session_factory(engine)() as session:
        session.execute(update(models.User).where(models.User.id == uuid.UUID(owner)).values(credential_epoch=1))
        session.commit()
    assert refresh(client, pair).status_code == 401
    assert client.get("/auth/me", headers=headers(pair)).status_code == 401
    new = client.post("/auth/login", json={"email": "native@example.com", "password": PASSWORD}).json()
    clock.advance(days=31)
    assert refresh(client, new).status_code == 401


@pytest.mark.parametrize("payload", [{}, {"refresh_token": "short"}, {"refresh_token": "x" * 44},
                                     {"refresh_token": "x" * 43, "user_id": str(uuid.uuid4())}])
def test_refresh_input_is_strict(client, payload):
    response = client.post("/auth/refresh", json=payload)
    assert response.status_code == 422
    assert payload.get("refresh_token", "NOT PRESENT") not in response.text


def test_unknown_refresh_and_access_credentials_cannot_be_exchanged(client):
    pair = sign_in(client)
    response = client.post("/auth/refresh", json={"refresh_token": "x" * 43})
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/auth/me", headers={"Authorization": "Bearer " + pair["refresh_token"]}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": pair["access_token"]}).status_code == 422


def test_refresh_keeps_resource_ownership(client):
    from tests.backend.conftest import task_payload

    alice, bob = sign_in(client), sign_in(client, "other@example.com")
    task = client.post("/tasks", json=task_payload(), headers=headers(alice)).json()
    new_bob = refresh(client, bob).json()
    for method, path, body in [
        ("GET", f"/tasks/{task['id']}", None),
        ("PUT", f"/tasks/{task['id']}", task_payload(base_version=1)),
        ("DELETE", f"/tasks/{task['id']}?base_version=1", None),
    ]:
        assert client.request(method, path, json=body, headers=headers(new_bob)).status_code == 404
    assert client.get(f"/tasks/{task['id']}", headers=headers(alice)).status_code == 200


def test_refresh_rate_limit(client):
    from backend.rate_limit import RateLimiter, Limit

    client.app.state.rate_limiter = RateLimiter(client.app.state.session_factory, client.app.state.clock,
                                               limits={"refresh_ip": Limit(2, 60)})
    for _ in range(2):
        assert client.post("/auth/refresh", json={"refresh_token": "x" * 43}).status_code == 401
    response = client.post("/auth/refresh", json={"refresh_token": "x" * 43})
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
