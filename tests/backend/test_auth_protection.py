"""
Authentication protections (docs/backend.md "Protections"): the client address behind proxies
(backend/protection.py), sliding-window limits (backend/rate_limit.py), failed sign-in tracking
(backend/login_guard.py), the bound on concurrent password hashing (backend/passwords.py) and the
recovery reset's check before hashing (backend/recovery.py).
"""

from __future__ import annotations

import threading
import time

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.requests import Request

from backend import models, passwords
from backend.app import create_app, password_memory_peak_mib
from backend.database import create_backend_engine, session_factory
from backend.errors import ApiError
from backend.protection import client_address, throttle_address
from backend.rate_limit import Limit, RateLimiter
from backend.recovery import RecoveryService
from backend.settings import BackendConfigError, BackendSettings
from tests.backend.conftest import PASSWORD, TEST_SECRET, register

PROXY = ("10.0.0.5", 50000)
TRUSTED = ("10.0.0.0/8",)


def protected_settings(**overrides) -> BackendSettings:
    return BackendSettings(**{"database_url": "sqlite://", "jwt_secret": TEST_SECRET, "rate_limit_enabled": True,
                              "trusted_proxies": TRUSTED, **overrides})


@pytest.fixture
def proxied(engine, clock):
    """The API behind one trusted proxy: every request's socket peer is the proxy; X-Forwarded-For names the client."""
    with TestClient(create_app(protected_settings(), engine=engine, clock=clock), client=PROXY) as client:
        yield client


def sign_up(client, email: str, username: str | None = None) -> None:
    register(client, email, PASSWORD, **({"username": username} if username else {}))


def attempt(client, address: str, password: str = "a wrong password", **identifier) -> int:
    return client.post("/auth/login", json={**identifier, "password": password},
                       headers={"X-Forwarded-For": address}).status_code


# -- the client address ---------------------------------------------------------------------


def request_from(peer: str, *forwarded: str, **headers: str) -> Request:
    raw = [(b"x-forwarded-for", line.encode()) for line in forwarded]
    raw += [(name.replace("_", "-").encode(), value.encode()) for name, value in headers.items()]
    return Request({"type": "http", "client": (peer, 1234), "headers": raw})


def test_forwarding_headers_of_an_untrusted_peer_are_ignored() -> None:
    assert client_address(request_from("192.0.2.1", "198.51.100.7"), TRUSTED) == "192.0.2.1"
    assert client_address(request_from("192.0.2.1", "198.51.100.7"), ()) == "192.0.2.1"
    assert client_address(request_from("192.0.2.1", cf_connecting_ip="198.51.100.7"), TRUSTED,
                          "cf-connecting-ip") == "192.0.2.1"


def test_a_trusted_chain_yields_the_right_most_untrusted_address_never_the_clients_own_entry() -> None:
    # The client wrote "1.1.1.1"; the proxies appended what they saw.
    assert client_address(request_from("10.0.0.5", "1.1.1.1, 198.51.100.7, 10.0.0.9"), TRUSTED) == "198.51.100.7"
    # A second header line added by the proxy, after the client's own line.
    assert client_address(request_from("10.0.0.5", "6.6.6.6", "198.51.100.7, 10.0.0.9"), TRUSTED) == "198.51.100.7"
    assert client_address(request_from("10.0.0.5", "10.0.0.8, 10.0.0.9"), TRUSTED) == "10.0.0.5"  # only proxies
    assert client_address(request_from("10.0.0.5", "198.51.100.7, garbage"), TRUSTED) == "10.0.0.5"
    assert client_address(request_from("10.0.0.5"), TRUSTED) == "10.0.0.5"
    # Ports, brackets and an IPv4-mapped peer are understood.
    assert client_address(request_from("::ffff:10.0.0.5", "198.51.100.7:4711"), TRUSTED) == "198.51.100.7"
    assert client_address(request_from("10.0.0.5", "[2001:db8::1]:443"), TRUSTED) == "2001:db8::1"


def test_a_configured_client_header_is_believed_only_as_one_address_from_a_trusted_peer() -> None:
    header = "cf-connecting-ip"
    assert client_address(request_from("10.0.0.5", "1.1.1.1", cf_connecting_ip="198.51.100.7"), TRUSTED,
                          header) == "198.51.100.7"
    assert client_address(request_from("10.0.0.5", "198.51.100.7"), TRUSTED, header) == "10.0.0.5"  # header absent
    assert client_address(request_from("10.0.0.5", cf_connecting_ip="1.1.1.1, 2.2.2.2"), TRUSTED,
                          header) == "10.0.0.5"
    doubled = Request({"type": "http", "client": ("10.0.0.5", 1),
                       "headers": [(b"cf-connecting-ip", b"1.1.1.1"), (b"cf-connecting-ip", b"2.2.2.2")]})
    assert client_address(doubled, TRUSTED, header) == "10.0.0.5"


def test_ipv6_clients_are_limited_by_their_64_and_trust_cannot_be_universal() -> None:
    assert throttle_address("2001:db8:1:2:aaaa::1") == throttle_address("2001:db8:1:2:bbbb::9")
    assert throttle_address("198.51.100.7") == "198.51.100.7"
    for bad in ({"trusted_proxies": ("0.0.0.0/0",)}, {"trusted_proxies": ("::/0",)},
                {"trusted_proxies": (), "client_ip_header": "cf-connecting-ip"}):
        with pytest.raises(BackendConfigError):
            protected_settings(**bad)


def test_a_spoofed_address_does_not_escape_the_limit_behind_a_proxy(proxied) -> None:
    sign_up(proxied, "alice@example.com")
    # One real client (198.51.100.7, appended by the proxy) pretending to be a new address every time.
    statuses = [attempt(proxied, f"203.0.113.{index}, 198.51.100.7", email="alice@example.com") for index in range(7)]
    assert statuses == [401] * 5 + [429] * 2


# -- failed sign-ins ------------------------------------------------------------------------


def test_one_addresss_failures_do_not_lock_the_account_or_other_accounts(proxied) -> None:
    sign_up(proxied, "alice@example.com")
    sign_up(proxied, "bob@example.com")
    attacker, owner = "198.51.100.7", "198.51.100.99"
    assert [attempt(proxied, attacker, email="alice@example.com") for _ in range(6)] == [401] * 5 + [429]
    assert attempt(proxied, owner, PASSWORD, email="alice@example.com") == 200  # the owner is not locked out
    assert attempt(proxied, attacker, PASSWORD, email="alice@example.com") == 429  # that address is, for alice
    assert attempt(proxied, attacker, email="bob@example.com") == 401  # and not for another account
    limited = proxied.post("/auth/login", json={"email": "alice@example.com", "password": "x"},
                           headers={"X-Forwarded-For": attacker})
    assert limited.json()["error"]["code"] == "rate_limited" and int(limited.headers["retry-after"]) > 0


def test_email_and_username_share_one_counter_and_unknown_accounts_are_counted_alike(proxied) -> None:
    sign_up(proxied, "alice@example.com", "alice_a")
    address = "198.51.100.7"
    statuses = [attempt(proxied, address, email="alice@example.com") for _ in range(3)]
    statuses += [attempt(proxied, address, username="Alice_A") for _ in range(2)]
    assert statuses == [401] * 5
    assert attempt(proxied, address, username="alice_a") == 429 and attempt(proxied, address, email="ALICE@example.com") == 429
    # An account that does not exist answers, and runs out, exactly the same way.
    other = "198.51.100.8"
    assert [attempt(proxied, other, email="nobody@example.com") for _ in range(6)] == [401] * 5 + [429]


def test_guessing_spread_over_many_addresses_tightens_each_address_without_locking_the_owner_out(proxied, clock) -> None:
    sign_up(proxied, "alice@example.com")
    for index in range(20):  # one guess per address: no single address is over its own limits
        assert attempt(proxied, f"203.0.113.{index}", email="alice@example.com") == 401
    fresh = "198.51.100.50"
    assert [attempt(proxied, fresh, email="alice@example.com") for _ in range(3)] == [401, 401, 429]
    assert attempt(proxied, "198.51.100.99", PASSWORD, email="alice@example.com") == 200
    clock.advance(hours=3)  # bounded: it ends on its own
    assert [attempt(proxied, "198.51.100.51", email="alice@example.com") for _ in range(4)] == [401] * 4


def test_one_address_spraying_many_accounts_is_stopped(proxied) -> None:
    address = "198.51.100.7"
    statuses = [attempt(proxied, address, email=f"user{index}@example.com") for index in range(22)]
    assert statuses == [401] * 20 + [429] * 2


# -- the limiter ----------------------------------------------------------------------------


def test_the_window_slides_so_a_boundary_gives_no_second_allowance(engine, clock) -> None:
    limiter = RateLimiter(session_factory(engine), clock, limits={"probe": Limit(4, 60)})
    clock.advance(seconds=50)
    for _ in range(4):
        limiter.hit("probe", "ip:a")
    with pytest.raises(ApiError) as refused:
        limiter.hit("probe", "ip:a")
    assert refused.value.status == 429 and refused.value.details["retry_after"] >= 1
    limiter.hit("probe", "ip:b")  # another subject is unaffected
    clock.advance(seconds=20)  # 10 s into the next window: a fixed window would allow four more
    with pytest.raises(ApiError):
        limiter.hit("probe", "ip:a")
    clock.advance(seconds=110)  # both windows passed
    limiter.hit("probe", "ip:a")
    assert limiter.estimate("probe", "ip:a") == 1.0


def test_failure_counters_are_read_without_counting_and_storage_is_bounded(engine, clock) -> None:
    factory = session_factory(engine)
    limiter = RateLimiter(factory, clock, limits={"fail": Limit(2, 60)})
    limiter.require("fail", "x")
    limiter.require("fail", "x")
    assert limiter.estimate("fail", "x") == 0  # require() counts nothing
    assert limiter.record("fail", "x") == (0.0, 1.0) and limiter.record("fail", "x") == (1.0, 2.0)
    with pytest.raises(ApiError):
        limiter.require("fail", "x")
    limiter.require("fail", "x", count=3)
    for index in range(30):
        limiter.record("fail", f"subject-{index}")
    clock.advance(seconds=180)
    limiter.record("fail", "x")  # a new window: every expired row of the scope goes
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(models.RateLimitBucket)) == 1


def test_a_failing_limiter_store_refuses_instead_of_running_unthrottled(clock) -> None:
    broken = create_backend_engine("sqlite://")  # no schema: every statement fails
    limiter = RateLimiter(session_factory(broken), clock, limits={"probe": Limit(4, 60)})
    for call in (lambda: limiter.hit("probe", "a"), lambda: limiter.require("probe", "a"),
                 lambda: limiter.record("probe", "a")):
        with pytest.raises(ApiError) as refused:
            call()
        assert refused.value.status == 503
    broken.dispose()


# -- password hashing -----------------------------------------------------------------------


def test_the_gate_bounds_running_and_waiting_password_work() -> None:
    gate = passwords._WorkGate(concurrency=1, queue=1, wait_seconds=0.3)
    gate.__enter__()
    outcomes: list[str] = []

    def contend() -> None:
        try:
            with gate:
                outcomes.append("ran")
        except passwords.PasswordWorkBusyError:
            outcomes.append("busy")

    waiter = threading.Thread(target=contend)
    waiter.start()
    time.sleep(0.05)  # the waiter holds the only queue place
    started = time.monotonic()
    contend()  # the queue is full: refused at once, without waiting
    assert outcomes == ["busy"] and time.monotonic() - started < 0.2
    waiter.join()
    assert outcomes == ["busy", "busy"]  # the waiter timed out
    gate.__exit__()
    contend()
    assert outcomes[-1] == "ran"


def test_hashing_and_verification_never_exceed_the_configured_concurrency(monkeypatch) -> None:
    running, peak, lock = 0, 0, threading.Lock()

    class SlowHasher:
        def _work(self, *_args):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.03)
            with lock:
                running -= 1
            return "hash"

        hash = verify = _work

    monkeypatch.setattr(passwords, "_hasher", SlowHasher())
    monkeypatch.setattr(passwords, "_gate", passwords._WorkGate(2, 16, 5.0))
    workers = [threading.Thread(target=target, args=arguments) for target, arguments in
               [(passwords.hash_password, ("p",)), (passwords.verify_password, ("p", "h")),
                (passwords.verify_password, ("p", None))] * 3]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert peak == 2


def test_a_busy_server_answers_503_and_counts_no_failure(proxied, monkeypatch) -> None:
    sign_up(proxied, "alice@example.com")
    gate = passwords._WorkGate(1, 0, 0.05)
    monkeypatch.setattr(passwords, "_gate", gate)
    gate.__enter__()
    busy = [proxied.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD},
                         headers={"X-Forwarded-For": "198.51.100.7"}) for _ in range(6)]
    gate.__exit__()
    assert {response.status_code for response in busy} == {503}
    assert busy[0].headers["retry-after"] == "2" and busy[0].json()["error"]["code"] == "temporarily_unavailable"
    assert attempt(proxied, "198.51.100.7", PASSWORD, email="alice@example.com") == 200  # nothing was held against it


def test_the_memory_budget_counts_every_worker(engine, clock, monkeypatch) -> None:
    # The suite hashes with cheap parameters (tests/conftest.py); production uses the library's, measured here.
    monkeypatch.setattr(passwords, "_hasher", PasswordHasher())
    assert passwords.hash_memory_mib() == 64
    settings = protected_settings(web_concurrency=4, password_hash_concurrency=3)
    assert password_memory_peak_mib(settings) == 4 * 3 * 64
    with pytest.raises(BackendConfigError, match="PASSWORD_HASH_MEMORY_BUDGET_MB"):
        create_app(protected_settings(web_concurrency=4, password_hash_concurrency=3,
                                      password_hash_memory_budget_mb=512), engine=engine, clock=clock)
    create_app(protected_settings(web_concurrency=2, password_hash_concurrency=2,
                                  password_hash_memory_budget_mb=256), engine=engine, clock=clock)
    passwords.configure_password_work()  # back to the defaults for the other tests


def test_an_invalid_reset_token_costs_no_hash_and_a_valid_one_is_consumed_once(client, clock, monkeypatch) -> None:
    from backend import recovery

    sign_up(client, "alice@example.com")
    hashed: list[str] = []
    real = recovery.hash_password
    monkeypatch.setattr(recovery, "hash_password", lambda password: hashed.append("x") or real(password))

    def reset(token: str):
        return client.post("/auth/recovery/reset", json={"token": token, "new_password": "a brand new password"})

    with client.app.state.session_factory() as session:
        delivery = RecoveryService(session, clock).request("alice@example.com")
    assert reset("not-a-token").status_code == 400 and reset("x" * 43).status_code == 400
    assert hashed == []
    assert reset(delivery.token).status_code == 200 and hashed == ["x"]
    assert reset(delivery.token).status_code == 400 and hashed == ["x"]  # used: refused before hashing
    with client.app.state.session_factory() as session:
        expired = RecoveryService(session, clock).request("alice@example.com")
    clock.advance(minutes=31)
    assert reset(expired.token).status_code == 400 and hashed == ["x"]
