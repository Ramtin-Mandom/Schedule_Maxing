"""
Where the desktop's sync transport may send credentials (app/sync/transport.py): https to any host,
http only to this computer, never to an address holding credentials, and never along a redirect.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.sync.transport import HttpTransport, ProtocolError, validate_backend_url
from app.ui.app_services import _sync_transport


@pytest.mark.parametrize("url", [
    "http://example.com", "http://example.com:8000", "http://192.168.1.20:8000", "http://10.0.0.5",
    "http://localhost.example.com", "http://127.0.0.1.example.com", "http://0.0.0.0:8000",
    "https://user:secret-pw@example.com", "https://token@example.com", "http://user:secret-pw@127.0.0.1:8000",
    "https://example.com:port", "https://example.com:99999", "https://", "https:///path", "ftp://example.com",
    "example.com", "", "https://example.com/?next=1", "https://example.com/#frag", "https://exa mple.com",
    "https://example.com\\@evil.example", "https://example.com\n.evil.example", "http://[::1", "//example.com",
])
def test_unsafe_or_malformed_addresses_are_refused_without_echoing_them(url: str) -> None:
    with pytest.raises(ValueError) as refused:
        validate_backend_url(url)
    assert "secret-pw" not in str(refused.value)
    with pytest.raises(ValueError):
        HttpTransport(url)
    assert _sync_transport(url) is None  # an environment variable or saved setting: synchronization stays off


@pytest.mark.parametrize("url, kept", [
    ("https://example.com", "https://example.com"), ("https://example.com:8443/api/", "https://example.com:8443/api"),
    ("http://localhost:8000", "http://localhost:8000"), ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
    ("http://127.8.9.10", "http://127.8.9.10"), ("http://[::1]:8000", "http://[::1]:8000"),
    ("  https://example.com  ", "https://example.com"),
])
def test_https_anywhere_and_http_on_this_computer_are_accepted(url: str, kept: str) -> None:
    assert validate_backend_url(url) == kept and HttpTransport(url).base_url == kept


class _Recorder(BaseHTTPRequestHandler):
    """Records every request; answers /health with 200 and everything else with `redirect_to` (if set)."""

    seen: list[tuple[str, str, str | None, bytes]] = []
    redirect_to: str | None = None
    status = 307

    def _answer(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).seen.append((self.command, self.path, self.headers.get("Authorization"), body))
        if self.path == "/health" or self.redirect_to is None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        self.send_response(self.status)
        self.send_header("Location", self.redirect_to + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PATCH = _answer

    def log_message(self, *_args) -> None:
        pass


def _serve(handler: type[BaseHTTPRequestHandler]):
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_refused_and_credentials_never_reach_its_target(status: int) -> None:
    class Elsewhere(_Recorder):
        seen = []

    class Backend(_Recorder):
        seen = []

    elsewhere = _serve(Elsewhere)
    Backend.redirect_to, Backend.status = f"http://127.0.0.1:{elsewhere.server_port}", status
    backend = _serve(Backend)
    try:
        transport = HttpTransport(f"http://127.0.0.1:{backend.server_port}", timeout=5)
        with pytest.raises(ProtocolError) as refused:
            transport.login("alice@example.com", "the password")
        assert refused.value.code == "redirect_refused" and refused.value.status == status
        with pytest.raises(ProtocolError):
            transport.profile("the-access-token")
        with pytest.raises(ProtocolError):
            transport.refresh("the-refresh-credential")
        assert Elsewhere.seen == []  # nothing followed: no password, no Authorization header, no refresh credential
        assert [path for _method, path, _auth, _body in Backend.seen] == ["/health", "/auth/login", "/me", "/auth/refresh"]
    finally:
        backend.shutdown()
        elsewhere.shutdown()
        backend.server_close()
        elsewhere.server_close()
