"""Browser (cookie) sessions next to bearer tokens: login sets an HttpOnly,
SameSite=Strict, Secure cookie and returns a CSRF token; unsafe cookie requests
need that token and a same origin; logout revokes the session on the server;
expiry is enforced; bearer-token clients (desktop sync) are unaffected."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.backend.conftest import PASSWORD, register, task_payload

LOGIN = {"email": "web@example.com", "password": PASSWORD}


@pytest.fixture
def browser(app) -> TestClient:
    with TestClient(app, base_url="https://testserver") as client:
        yield client


def sign_in(browser: TestClient) -> str:
    register(browser, LOGIN["email"])
    response = browser.post("/auth/browser/login", json=LOGIN)
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def test_login_sets_a_protected_cookie_and_never_returns_a_bearer_token(browser) -> None:
    register(browser, LOGIN["email"])
    response = browser.post("/auth/browser/login", json=LOGIN)

    cookie = response.headers["set-cookie"].lower()
    assert "sm_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
    body = response.json()
    assert body["authenticated"] and body["user"]["email"] == LOGIN["email"] and len(body["csrf_token"]) == 64
    assert "access_token" not in body and PASSWORD not in response.text
    session = browser.get("/auth/browser/session").json()
    assert session["authenticated"] and session["csrf_token"] == body["csrf_token"]


def test_wrong_credentials_are_one_generic_401(browser) -> None:
    register(browser, LOGIN["email"])
    response = browser.post("/auth/browser/login", json={**LOGIN, "password": "not the password"})
    assert response.status_code == 401 and "sm_session" not in response.headers.get("set-cookie", "")


def test_cookie_requests_need_the_csrf_token_for_changes(browser) -> None:
    csrf = sign_in(browser)
    assert browser.get("/me").status_code == 200  # safe methods need only the cookie

    missing = browser.post("/tasks", json=task_payload())
    assert missing.status_code == 403 and missing.json()["error"]["code"] == "csrf_failed"
    forged = browser.post("/tasks", json=task_payload(), headers={"X-CSRF-Token": "0" * 64})
    assert forged.status_code == 403
    assert browser.get("/tasks").json()["items"] == []

    created = browser.post("/tasks", json=task_payload(), headers={"X-CSRF-Token": csrf})
    assert created.status_code == 201


def test_cross_origin_cookie_requests_are_refused(browser) -> None:
    csrf = sign_in(browser)
    evil = browser.post("/tasks", json=task_payload(), headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"})
    assert evil.status_code == 403 and evil.json()["error"]["code"] == "origin_not_allowed"
    same = browser.post("/tasks", json=task_payload(), headers={"X-CSRF-Token": csrf, "Origin": "https://testserver"})
    assert same.status_code == 201


def test_logout_revokes_the_session_on_the_server(browser) -> None:
    csrf = sign_in(browser)
    stolen = browser.cookies.get("sm_session")

    assert browser.post("/auth/browser/logout", headers={"X-CSRF-Token": "0" * 64}).status_code == 403
    assert browser.post("/auth/browser/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert browser.get("/auth/browser/session").json() == {"authenticated": False, "user": None, "csrf_token": None,
                                                           "expires_at": None}
    # Even a copy of the old cookie no longer works.
    replay = browser.get("/me", cookies={"sm_session": stolen})
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "unauthenticated"


def test_sessions_expire(browser, clock) -> None:
    sign_in(browser)
    clock.advance(minutes=721)
    expired = browser.get("/me")
    assert expired.status_code == 401 and expired.json()["error"]["code"] == "session_expired"
    assert browser.get("/auth/browser/session").json()["authenticated"] is False


def test_an_unknown_cookie_is_a_clear_401(browser) -> None:
    response = browser.get("/me", cookies={"sm_session": "made-up"})
    assert response.status_code == 401 and response.json()["error"]["code"] == "unauthenticated"


def test_bearer_clients_and_sync_are_unaffected(browser) -> None:
    sign_in(browser)
    token = browser.post("/auth/login", json=LOGIN).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(browser.app, base_url="https://testserver") as desktop:  # no cookie, no CSRF token
        assert desktop.post("/tasks", json=task_payload(name="From sync"), headers=headers).status_code == 201
        pushed = desktop.post("/sync/push", headers=headers, json={"operations": [{
            "op_id": str(uuid.uuid4()), "entity_type": "project", "entity_id": str(uuid.uuid4()), "kind": "create",
            "payload": {"name": "Synced"}}]})
        assert pushed.status_code == 200 and pushed.json()["results"][0]["status"] == "applied"
    # A bearer header wins over a cookie: a browser-held cookie never needs to be mixed with it.
    assert browser.get("/me", headers=headers).status_code == 200


def test_planning_endpoints_accept_browser_sessions(browser) -> None:
    csrf = sign_in(browser)
    snapshot = browser.get("/planning/snapshot", params={"start_date": "2026-03-02", "end_date": "2026-03-02",
                                                         "timezone": "UTC"})
    assert snapshot.status_code == 200
    body = {"start_date": "2026-03-02", "end_date": "2026-03-02", "timezone": "UTC"}
    assert browser.post("/planning/generate", json=body).status_code == 403
    assert browser.post("/planning/generate", json=body, headers={"X-CSRF-Token": csrf}).status_code == 200
