"""
HttpTransport's keep-alive connection (app/sync/transport.py): a sync's requests share one
connection, a connection the server dropped is replaced, and only idempotent requests are
ever sent again -- never a POST. Against a real local HTTP/1.1 server.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.sync import transport as transport_module
from app.sync.transport import HttpTransport, TransportError


class _KeepAliveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive unless a handler says otherwise
    connections = 0
    requests: list[tuple[str, str]] = []
    #: After answering this many requests on a connection, close it without saying so (a dropped keep-alive).
    drop_after: int | None = None

    def setup(self) -> None:
        super().setup()
        type(self).connections += 1
        self._answered = 0

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        type(self).requests.append((self.command, self.path.split("?")[0]))
        if self.path.startswith("/changes"):
            payload = {"changes": [], "cursor": 0, "has_more": False}
        elif self.path == "/sync/push":
            payload = {"results": []}
        else:
            payload = {"features": []} if self.path == "/sync/capabilities" else {"status": "ok"}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self._answered += 1
        if self.drop_after is not None and self._answered >= self.drop_after:
            self.close_connection = True

    do_GET = do_POST = do_PATCH = _answer

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def server():
    class Handler(_KeepAliveHandler):
        connections = 0
        requests = []
        drop_after = None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd, Handler
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_one_sync_reuses_one_connection(server) -> None:
    httpd, handler = server
    transport = HttpTransport(f"http://127.0.0.1:{httpd.server_port}", timeout=5)
    transport.capabilities("token")
    transport.push("token", [])
    transport.pull("token", 0, 100)
    transport.pull("token", 0, 100)
    assert [path for _method, path in handler.requests] == ["/health", "/sync/capabilities", "/sync/push",
                                                            "/changes", "/changes"]
    assert handler.connections == 1 and transport.connections_opened == 1
    transport.close()


def test_a_connection_the_server_closed_is_replaced_before_the_next_request(server) -> None:
    httpd, handler = server
    handler.drop_after = 2  # the server closes after /health and the first request, without a header
    transport = HttpTransport(f"http://127.0.0.1:{httpd.server_port}", timeout=5)
    transport.profile("token")
    transport.push("token", [])  # a POST on what the client still holds: replaced first, never resent
    assert handler.connections == 2
    assert [method for method, _path in handler.requests].count("POST") == 1


def test_a_dropped_connection_is_retried_once_for_an_idempotent_request_only(server, monkeypatch) -> None:
    httpd, handler = server
    handler.drop_after = 2
    monkeypatch.setattr(transport_module, "_closed_by_server", lambda connection: False)  # the drop is unseen
    transport = HttpTransport(f"http://127.0.0.1:{httpd.server_port}", timeout=5)
    transport.profile("token")
    assert transport.profile("token") == {"status": "ok"}  # a GET: sent once more, on a new connection
    assert handler.connections == 2

    transport.profile("token")  # the second connection's second request: it is dropped after this one
    with pytest.raises(TransportError):
        transport.push("token", [])  # a POST is never sent twice
    assert [method for method, _path in handler.requests].count("POST") <= 1
    assert transport.profile("token") == {"status": "ok"}  # and the transport keeps working afterwards


def test_an_idle_connection_is_not_reused(server, monkeypatch) -> None:
    httpd, handler = server
    monkeypatch.setattr(transport_module, "KEEPALIVE_IDLE_S", -1.0)  # always idle too long (clock ticks are coarse)
    transport = HttpTransport(f"http://127.0.0.1:{httpd.server_port}", timeout=5)
    transport.profile("token")
    transport.profile("token")
    assert handler.connections == 3  # /health, then each request on a fresh connection


def test_a_second_thread_never_shares_the_kept_connection(server) -> None:
    httpd, handler = server
    transport = HttpTransport(f"http://127.0.0.1:{httpd.server_port}", timeout=5)
    transport.health()
    with transport._lock:  # the sync thread is using the kept connection right now
        answer = []
        worker = threading.Thread(target=lambda: answer.append(transport.profile("token")))
        worker.start()
        worker.join(5)
    assert answer == [{"status": "ok"}] and handler.connections == 2
    transport.profile("token")
    assert handler.connections == 2  # the kept one is still there for the next request
