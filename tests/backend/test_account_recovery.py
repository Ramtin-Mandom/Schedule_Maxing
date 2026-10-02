"""Password recovery and server protections (backend/recovery.py, recovery_api.py, rate_limit.py,
protection.py): request -> fake delivery -> reset -> new login; generic answers for known and unknown
accounts; expiry, malformed tokens, reuse, re-request and simultaneous consumption; old password, bearer
tokens and browser sessions stop working, including a sign-in whose check finished before the reset; a
failed delivery changes nothing visible; no secret in responses or logs; shared rate limits across app
instances and forged proxy headers; oversized bodies; hosts, CORS, errors; recovery cannot target another
account; a generation over its time limit saves nothing."""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import models
from backend.app import create_app
from backend.database import create_backend_engine, session_factory
from backend.migrate import upgrade
from backend.recovery import RecoveryService
from backend.security import issue_access_token
from backend.settings import BackendSettings
from tests.backend.conftest import PASSWORD, TEST_SECRET, login_headers, register

NEW_PASSWORD = "a brand new passphrase"


class FakeDelivery:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    def send(self, delivery, link: str) -> None:
        if self.fail:
            raise TimeoutError("smtp timed out (injected)")
        self.sent.append((delivery.email, link))

    def token(self) -> str:
        return self.sent[-1][1].split("#token=", 1)[1]


def recovery_settings(**overrides) -> BackendSettings:
    return BackendSettings(**{
        "database_url": "sqlite://", "jwt_secret": TEST_SECRET,
        "recovery_public_url": "https://app.example.com/auth/recovery/reset",
        "smtp_host": "smtp.example.com", "smtp_sender": "noreply@example.com", **overrides})


@pytest.fixture
def delivery() -> FakeDelivery:
    return FakeDelivery()


@pytest.fixture
def recovery_client(engine, clock, delivery):
    app = create_app(recovery_settings(), engine=engine, clock=clock, recovery_delivery=delivery)
    with TestClient(app) as client:
        yield client


def ask(client, identifier: str):
    return client.post("/auth/recovery/request", json={"identifier": identifier})


def reset(client, token: str, password: str = NEW_PASSWORD):
    return client.post("/auth/recovery/reset", json={"token": token, "new_password": password})


def test_request_delivery_reset_and_a_new_login(recovery_client, delivery) -> None:
    register(recovery_client, "alice@example.com")
    known = ask(recovery_client, "Alice@Example.com")
    unknown = ask(recovery_client, "nobody@example.com")

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()  # the same answer: existence is never revealed
    assert "token" not in known.text
    [(to, link)] = delivery.sent
    assert to == "alice@example.com" and link.startswith("https://app.example.com/auth/recovery/reset#token=")

    done = reset(recovery_client, delivery.token())
    assert done.status_code == 200 and done.json()["status"] == "reset"
    assert "access_token" not in done.text  # no automatic sign-in
    assert recovery_client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
                                ).status_code == 401
    assert login_headers(recovery_client, "alice@example.com", NEW_PASSWORD)


def test_tokens_are_single_use_bound_expiring_and_replaced(recovery_client, delivery, clock) -> None:
    register(recovery_client, "alice@example.com")
    ask(recovery_client, "alice@example.com")
    first = delivery.token()
    ask(recovery_client, "alice@example.com")  # a new request replaces the earlier link
    second = delivery.token()
    assert reset(recovery_client, first).json()["error"]["code"] == "invalid_recovery_token"
    assert reset(recovery_client, "not-a-token").status_code == 400
    assert reset(recovery_client, "x" * 500).status_code == 422  # malformed: refused before any lookup
    assert reset(recovery_client, second, "short").status_code == 422  # the registration password rule

    assert reset(recovery_client, second).status_code == 200
    assert reset(recovery_client, second, "another new passphrase").status_code == 400  # used once only

    ask(recovery_client, "alice@example.com")
    clock.now += timedelta(minutes=31)  # past RECOVERY_TOKEN_TTL_MINUTES (30)
    assert reset(recovery_client, delivery.token()).status_code == 400


def test_two_simultaneous_resets_with_one_token_have_exactly_one_success(tmp_path, clock) -> None:
    engine = create_backend_engine(f"sqlite:///{tmp_path / 'race.db'}")
    upgrade(engine)
    factory = session_factory(engine)
    with factory() as session:
        session.add(models.User(id=__import__("uuid").uuid4(), email="alice@example.com", password_hash="x",
                                change_seq=0, created_at=clock(), updated_at=clock(), version=1))
        session.commit()
    with factory() as session:
        token = RecoveryService(session, clock).request("alice@example.com").token
    results: list[str] = []
    barrier = threading.Barrier(4)

    def attempt(index: int) -> None:
        with factory() as session:
            barrier.wait()
            try:
                RecoveryService(session, clock).reset(token, f"new passphrase {index}")
                results.append("ok")
            except Exception as error:  # noqa: BLE001 - the loser's failure is the point
                results.append(type(error).__name__)

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 1
    with factory() as session:
        assert session.scalar(select(models.User.credential_epoch)) == 1  # one credential change, not four
    engine.dispose()


def test_a_reset_ends_every_token_and_session_even_one_issued_after_the_check(
    recovery_client, delivery, engine, clock
) -> None:
    user = register(recovery_client, "alice@example.com")
    bearer = login_headers(recovery_client, "alice@example.com")
    browser = recovery_client.post("/auth/browser/login", json={"email": "alice@example.com", "password": PASSWORD})
    assert browser.status_code == 200
    # A sign-in whose password check finished before the reset: its token carries the epoch it read.
    racing = issue_access_token(__import__("uuid").UUID(user["id"]), recovery_client.app.state.settings, clock(), 0)

    ask(recovery_client, "alice@example.com")
    assert reset(recovery_client, delivery.token()).status_code == 200

    assert recovery_client.get("/me", headers=bearer).status_code == 401
    assert recovery_client.get("/me", headers={"Authorization": f"Bearer {racing.token}"}).status_code == 401
    assert recovery_client.get("/auth/browser/session").json()["authenticated"] is False
    with session_factory(engine)() as session:
        assert session.scalar(select(func.count()).select_from(models.BrowserSession)
                              .where(models.BrowserSession.revoked_at.is_(None))) == 0
    fresh = login_headers(recovery_client, "alice@example.com", NEW_PASSWORD)
    assert recovery_client.get("/me", headers=fresh).status_code == 200


def test_a_failed_delivery_is_invisible_and_leaves_recovery_working(recovery_client, delivery, caplog) -> None:
    register(recovery_client, "alice@example.com")
    delivery.fail = True
    with caplog.at_level(logging.WARNING):
        answer = ask(recovery_client, "alice@example.com")
    assert answer.status_code == 202 and answer.json() == ask(recovery_client, "nobody@example.com").json()
    assert "delivery failed (TimeoutError)" in caplog.text
    assert "alice@example.com" not in caplog.text and "#token=" not in caplog.text
    delivery.fail = False
    ask(recovery_client, "alice@example.com")  # a retry is a new request
    assert reset(recovery_client, delivery.token()).status_code == 200


def test_no_secret_reaches_a_response_or_a_log(recovery_client, delivery, caplog) -> None:
    register(recovery_client, "alice@example.com")
    with caplog.at_level(logging.DEBUG):
        ask(recovery_client, "alice@example.com")
        token = delivery.token()
        bad = recovery_client.post("/auth/recovery/reset", json={"token": token, "new_password": "short"})
        reset(recovery_client, token)
    assert token not in bad.text and "short" not in bad.text  # validation errors never echo values
    assert token not in caplog.text and NEW_PASSWORD not in caplog.text and PASSWORD not in caplog.text
    from backend.protection import redact

    assert redact('"GET /auth/recovery/reset?token=abc123&x=1 HTTP/1.1"') == \
        '"GET /auth/recovery/reset?token=[redacted]&x=1 HTTP/1.1"'
    assert "secret-value" not in repr(recovery_settings(smtp_password="secret-value"))


def test_the_reset_page_is_private_and_self_contained(recovery_client) -> None:
    page = recovery_client.get("/auth/recovery/reset")
    assert page.status_code == 200
    headers = page.headers
    assert headers["cache-control"] == "no-store" and headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in headers["content-security-policy"] and headers["x-frame-options"] == "DENY"
    assert "http" not in page.text.split("<script>")[0].replace("http-equiv", "")  # no third-party content
    assert "location.hash" in page.text and "replaceState" in page.text


def test_recovery_cannot_name_another_account_or_be_used_unconfigured(recovery_client, client, delivery) -> None:
    register(recovery_client, "alice@example.com")
    register(recovery_client, "bob@example.com")
    sneaky = recovery_client.post("/auth/recovery/request", json={"identifier": "alice@example.com",
                                                                  "user_id": "00000000-0000-4000-8000-000000000000"})
    assert sneaky.status_code == 422 and delivery.sent == []
    ask(recovery_client, "alice@example.com")
    assert recovery_client.post("/auth/recovery/reset", json={
        "token": delivery.token(), "new_password": NEW_PASSWORD, "email": "bob@example.com"}).status_code == 422
    assert reset(recovery_client, delivery.token()).status_code == 200
    assert login_headers(recovery_client, "bob@example.com")  # bob untouched
    unconfigured = ask(client, "alice@example.com")  # the default test app has no delivery configured
    assert unconfigured.status_code == 503 and unconfigured.json()["error"]["code"] == "recovery_unavailable"


def test_rate_limits_are_shared_by_instances_and_ignore_forged_proxy_headers(engine, clock) -> None:
    settings = recovery_settings(rate_limit_enabled=True, trusted_proxies=("10.0.0.0/8",))
    first = TestClient(create_app(settings, engine=engine, clock=clock, recovery_delivery=FakeDelivery()))
    second = TestClient(create_app(settings, engine=engine, clock=clock, recovery_delivery=FakeDelivery()))
    statuses = [(first if index % 2 else second).post("/auth/login", json={
        "email": "nobody@example.com", "password": "wrong password"},
        headers={"X-Forwarded-For": f"203.0.113.{index}"}).status_code for index in range(12)]
    # Not a trusted proxy: the forged addresses are ignored, and the two instances count together.
    assert statuses[:10] == [401] * 10 and statuses[10:] == [429, 429]
    limited = first.post("/auth/login", json={"email": "nobody@example.com", "password": "x"})
    assert int(limited.headers["retry-after"]) > 0 and limited.json()["error"]["code"] == "rate_limited"
    clock.now += timedelta(minutes=16)
    assert first.post("/auth/login", json={"email": "nobody@example.com", "password": "x"}).status_code == 401
    first.close()
    second.close()


def test_a_trusted_proxys_forwarded_address_is_the_client() -> None:
    from starlette.requests import Request

    from backend.protection import client_address

    def request(peer: str, forwarded: str) -> Request:
        return Request({"type": "http", "client": (peer, 1), "headers": [(b"x-forwarded-for", forwarded.encode())]})

    assert client_address(request("10.0.0.5", "198.51.100.7, 10.0.0.9"), ["10.0.0.0/8"]) == "198.51.100.7"
    assert client_address(request("192.0.2.1", "198.51.100.7"), ["10.0.0.0/8"]) == "192.0.2.1"
    assert client_address(request("10.0.0.5", "not-an-ip"), ["10.0.0.0/8"]) == "10.0.0.5"


def test_oversized_requests_hosts_cors_and_errors(engine, clock) -> None:
    settings = recovery_settings(max_request_bytes=2048, allowed_hosts=("api.example.com",),
                                 cors_origins=("https://app.example.com",))
    client = TestClient(create_app(settings, engine=engine, clock=clock, recovery_delivery=FakeDelivery()),
                        base_url="https://api.example.com")
    big = client.post("/auth/login", content=b"{" + b" " * 4096 + b"}", headers={"Content-Type": "application/json"})
    assert big.status_code == 413 and big.json()["error"]["code"] == "request_too_large"

    def chunks():
        for _ in range(8):
            yield b" " * 1024

    chunked = client.post("/auth/login", content=chunks(), headers={"Content-Type": "application/json"})
    assert chunked.status_code == 413
    assert TestClient(client.app, base_url="https://evil.example.com").get("/health").status_code == 400
    allowed = client.options("/me", headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "GET"})
    assert allowed.headers["access-control-allow-origin"] == "https://app.example.com"
    assert allowed.headers["access-control-allow-credentials"] == "true"
    refused = client.options("/me", headers={"Origin": "https://evil.example.com",
                                             "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in refused.headers
    client.close()


def test_a_generation_past_its_time_limit_saves_nothing(engine, clock) -> None:
    from tests.backend.conftest import account

    settings = BackendSettings(database_url="sqlite://", jwt_secret=TEST_SECRET, generation_time_limit_seconds=1e-9)
    client = TestClient(create_app(settings, engine=engine, clock=clock))  # the deadline passes at once
    alice = account(client, "alice@example.com")
    client.post("/tasks", json={"name": "Essay", "category": "study", "estimated_duration_minutes": 60,
                                "priority": 5, "required_date": "2026-03-02"}, headers=alice)
    refused = client.post("/planning/generate", headers=alice, json={
        "start_date": "2026-03-02", "end_date": "2026-03-02", "timezone": "UTC"})
    assert refused.status_code == 503 and refused.json()["error"]["code"] == "generation_limit"
    snapshot = client.get("/planning/snapshot", headers=alice, params={
        "start_date": "2026-03-02", "end_date": "2026-03-02", "timezone": "UTC"}).json()
    assert snapshot["placements"] == []


def test_oversized_task_lists_are_refused_before_any_work(client, alice) -> None:
    too_many = client.post("/tasks", headers=alice, json={
        "name": "Essay", "category": "study", "estimated_duration_minutes": 60, "priority": 5,
        "tags": [f"t{index}" for index in range(51)]})
    assert too_many.status_code == 422


def test_the_direct_path_resets_and_ends_direct_sessions(engine, clock) -> None:
    from backend.accounts import AccountService

    factory = session_factory(engine)
    with factory() as session:
        identity = AccountService(session, clock).register(email="alice@example.com", password=PASSWORD)
        assert identity.credential_epoch == 0
        token = RecoveryService(session, clock).request("alice@example.com").token
    with factory() as session:
        RecoveryService(session, clock).reset(token, NEW_PASSWORD)
    with factory() as session:
        assert AccountService(session, clock).authenticate(email="alice@example.com",
                                                           password=NEW_PASSWORD).credential_epoch == 1
