"""The real HTTP transport (urllib) against a real local HTTP server (uvicorn on
127.0.0.1, in-memory SQLite backend), plus authentication loss and the history
reset propagating as server-side deletions."""

from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn

from app.sync.transport import AuthenticationError, HttpTransport, TransportError
from tests.sync.conftest import PASSWORD, InProcessTransport


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def http_backend(alice_server):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(alice_server.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=20)


def test_a_device_synchronizes_over_real_http(http_backend, alice_server, make_device) -> None:
    device = make_device("http", HttpTransport(http_backend, timeout=10))
    device.sign_in("alice@example.com")
    task = device.add_task("Over HTTP")

    report = device.sync_now()

    assert report.status == "ok" and report.pushed == 1
    assert alice_server.get("alice@example.com", f"/tasks/{task.id}")["name"] == "Over HTTP"


def test_http_errors_are_classified(http_backend) -> None:
    transport = HttpTransport(http_backend, timeout=5)
    with pytest.raises(AuthenticationError):
        transport.login("alice@example.com", "wrong password")
    with pytest.raises(AuthenticationError):
        transport.pull("not-a-token", 0, 10)
    with pytest.raises(TransportError):
        HttpTransport(f"http://127.0.0.1:{_free_port()}", timeout=2).pull("token", 0, 10)  # nothing listens there
    with pytest.raises(ValueError):
        HttpTransport("ftp://example.invalid")
    assert transport.login("alice@example.com", PASSWORD).email == "alice@example.com"


def test_an_expired_session_stops_sync_until_signing_in_again(alice_server, make_device) -> None:
    device = make_device("a", InProcessTransport(alice_server.client))
    device.sign_in("alice@example.com")
    device.add_task()
    device.sync._token = "expired-or-revoked"

    assert device.sync_now().status == "auth_required"
    assert not device.sync.signed_in and device.sync.sync_now().status == "inert"
    device.sync.sign_in("alice@example.com", PASSWORD)
    assert device.sync_now().status == "ok"


def test_a_history_reset_deletes_the_synchronized_executions_on_the_server(alice_server, make_device) -> None:
    device = make_device("a", InProcessTransport(alice_server.client))
    device.sign_in("alice@example.com")
    task = device.add_task()
    execution = device.executions.create_canonical_execution(device.planning.get_task(task.id))
    device.sync_now()

    assert device.executions.reset_all_history() == 1
    device.sync_now()

    remote = alice_server.get("alice@example.com", f"/executions/{execution.id}", include_deleted=True)
    assert remote["deleted_at"] is not None and device.dirty() == []
