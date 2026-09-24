"""
app/web/local_session.py

Protection of the local web service. It listens on a loopback address only,
but any web page the user opens can still make the browser send requests to
127.0.0.1, and other local programs can connect to it. So every request is
checked in layers:

    1. Host: the Host header must name this service (127.0.0.1 / localhost /
       [::1] with its port, or an explicitly configured host). This defeats
       DNS rebinding, where a hostile domain resolves to 127.0.0.1.
    2. Session: everything except GET/POST /local/session and static assets
       needs the local session cookie (COOKIE_NAME, HttpOnly,
       SameSite=Strict). A session is created only by exchanging the one-time
       bootstrap code the launcher prints (in the URL *fragment*, which the
       browser never sends to a server or puts in a request log). The code
       works once; the cookie lives as long as this process.
    3. CSRF and origin: an unsafe request (anything but GET/HEAD/OPTIONS)
       must send X-CSRF-Token (an HMAC of the session token), and a present
       Origin must be this service's own origin. A foreign page can neither
       read the token nor set the header without a CORS preflight, which this
       service never grants.

Cloud access tokens never reach the browser: they stay in the SyncService's
memory in this process. Nothing here is logged.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import Request, Response

from backend.errors import ApiError

COOKIE_NAME = "sm_local_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LocalSession:
    token_hash: str
    created_at: datetime


class LocalGuard:
    def __init__(self, *, port: int, bootstrap_code: str, extra_hosts: tuple[str, ...] = ()) -> None:
        self._allowed_hosts = {f"{host}:{port}" for host in LOOPBACK_HOSTS} | set(extra_hosts)
        self._bootstrap_hash: str | None = _digest(bootstrap_code)
        self._secret = secrets.token_bytes(32)
        self._sessions: dict[str, LocalSession] = {}
        self._lock = threading.Lock()

    # -- layer 1: Host and Origin ---------------------------------------------------

    def check_host(self, request: Request) -> None:
        if request.headers.get("host", "").lower() not in self._allowed_hosts:
            raise ApiError(400, "host_not_allowed", "This local service only answers requests addressed to itself.")

    def _check_origin(self, request: Request) -> None:
        origin = request.headers.get("origin")
        if origin is not None and urlsplit(origin).netloc.lower() not in self._allowed_hosts:
            raise ApiError(403, "origin_not_allowed", "Requests from other sites are not accepted.")

    # -- layer 2: the bootstrap exchange and the session ------------------------------

    def exchange(self, code: str, response: Response) -> str:
        """Trade the one-time bootstrap code for a session cookie; returns the session's CSRF token."""
        with self._lock:
            if self._bootstrap_hash is None or not hmac.compare_digest(_digest(code), self._bootstrap_hash):
                raise ApiError(401, "bootstrap_invalid", "This link is invalid or was already used. Start the local "
                                                         "service again to get a new one.")
            self._bootstrap_hash = None  # single use
            token = secrets.token_urlsafe(32)
            self._sessions[_digest(token)] = LocalSession(_digest(token), datetime.now(timezone.utc))
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict", secure=False, path="/")
        return self.csrf_token(token)

    def csrf_token(self, token: str) -> str:
        return hmac.new(self._secret, b"csrf:" + token.encode("utf-8"), hashlib.sha256).hexdigest()

    def session_token(self, request: Request) -> str | None:
        token = request.cookies.get(COOKIE_NAME)
        return token if token and _digest(token) in self._sessions else None

    def end(self, request: Request, response: Response) -> None:
        token = request.cookies.get(COOKIE_NAME)
        if token:
            with self._lock:
                self._sessions.pop(_digest(token), None)
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")

    # -- layers 2 and 3 together --------------------------------------------------------

    def require(self, request: Request) -> None:
        """A local session, and for unsafe methods a same-origin request with the CSRF token."""
        token = self.session_token(request)
        if token is None:
            raise ApiError(401, "local_session_required",
                           "Open the link printed by the local service to start a session.")
        if request.method.upper() in SAFE_METHODS:
            return
        self._check_origin(request)
        supplied = request.headers.get(CSRF_HEADER, "")
        if not supplied or not hmac.compare_digest(supplied, self.csrf_token(token)):
            raise ApiError(403, "csrf_failed", f"A valid {CSRF_HEADER} header is required for this request.")

    def check_unsafe_origin(self, request: Request) -> None:
        if request.method.upper() not in SAFE_METHODS:
            self._check_origin(request)
