"""
app/sync/transport.py

How the sync client talks to the backend. SyncEngine only uses the small
SyncTransport interface, so tests inject in-process or failing transports.
HttpTransport uses the standard library (http.client) with request timeouts:
the desktop app gains no network dependency. It keeps one HTTP(S) keep-alive
connection, so a sync's requests share one TLS handshake (see HttpTransport).

Failures are classified, because they are handled differently:
    TransportError       network trouble, timeouts, HTTP 5xx: retry later (backoff)
    AuthenticationError  HTTP 401: the token is missing/expired; sign in again
    ProtocolError        any other refusal of the whole request (4xx): a bug or
                         an incompatible server; not retried automatically
Individual operation outcomes (applied / conflict / rejected) are results,
not exceptions.

Credentials: the access token is only held in memory by SyncService and is
passed per call; it is never written to disk or logged. Passwords are sent
once to /auth/login and never stored. A login also returns a single-use
refresh credential (when the server issues them): refresh() exchanges it for
a new pair, logout() revokes its session. SyncService holds it in memory and,
only for "Keep me signed in", in the operating system's credential store
(app/sync/credentials.py).

Where credentials may be sent (validate_backend_url, applied to every address
whatever its source -- the built-in one, SCHEDULE_MAXING_BACKEND_URL, a saved
setting or the Account page): https:// to any host, http:// only to this
computer (localhost or a loopback IP address, for a development server). An
address with a user name or password, a query, a fragment or anything
malformed is refused. TLS certificates and host names are verified with the
system's trust store (never disabled), and no redirect is ever followed: a
3xx answer is a ProtocolError, so the Authorization header is never resent
to wherever a redirect points.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import select
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit


class TransportError(Exception):
    """Retryable: the server could not be reached or failed (timeouts, 5xx). `status`/`body`: its answer, if any."""

    def __init__(self, message: str, *, status: int | None = None, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.body = body or {}


class AuthenticationError(Exception):
    """The server refused the credentials or token (401)."""


class MfaRequired(Exception):
    """
    The password was right and the account has two-step sign-in: no session exists yet. `mfa_token` is good
    only for the second step (verify_mfa) and only for a few minutes; it is held in memory, never stored.
    """

    def __init__(self, mfa_token: str) -> None:
        super().__init__("Enter the code from your authenticator app to finish signing in.")
        self.mfa_token = mfa_token


class ProtocolError(Exception):
    """The server refused the whole request (4xx other than 401). `code`/`body` carry the server's error, if any."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body or {}


@dataclass(frozen=True)
class LoginResult:
    token: str
    user_id: str
    email: str
    #: None from a server that predates rotating native sessions.
    refresh_token: str | None = None


@dataclass(frozen=True)
class TokenPair:
    token: str
    refresh_token: str


@dataclass(frozen=True)
class PullPage:
    changes: list[dict[str, Any]]
    cursor: int
    has_more: bool


class SyncTransport(Protocol):
    base_url: str

    #: MfaRequired when the account has two-step sign-in: finish with verify_mfa(its mfa_token, code).
    def login(self, email: str, password: str) -> LoginResult: ...

    def verify_mfa(self, mfa_token: str, code: str) -> LoginResult: ...

    #: One authenticated request to an account route (/auth/password/change, /auth/sessions, /auth/mfa/...).
    def account_request(self, method: str, path: str, token: str, body: dict | None = None) -> dict: ...

    def push(self, token: str, operations: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def pull(self, token: str, after: int, limit: int) -> PullPage: ...

    # Public account operations (the local web profile, app/web): registration, the profile, and a
    # reachability probe. Each returns the server's JSON document.

    #: Registration (backend/verification_api.py). Asking sends a verification email and creates nothing: the
    #: answer is the same for every address, and no password is sent. The account is created by confirming the
    #: emailed code, with the password chosen then; the answer is its profile.
    def request_registration(self, email: str) -> dict: ...

    def confirm_registration(self, code: str, password: str, username: str | None,
                             display_name: str | None) -> dict: ...

    #: For a signed-in account whose address was never confirmed: email a code to it, then confirm the code.
    def request_email_verification(self, token: str) -> dict: ...

    def confirm_email_verification(self, token: str, code: str) -> dict: ...

    def profile(self, token: str) -> dict: ...

    def update_profile(self, token: str, base_version: int, display_name: str | None) -> dict: ...

    def health(self) -> dict: ...

    #: Remove all of the account's task data on the server ({removed, cursor}); see backend/task_data_reset.py.
    def reset_task_data(self, token: str) -> dict: ...

    # Optional (looked up with getattr): ready() -> dict, the server's database readiness (GET /ready).

    #: The server's sync protocol version and features ({protocol_version, features, ...}); an older server that
    #: has no such endpoint answers as protocol 1 without features (see capabilities_via).
    def capabilities(self, token: str) -> dict: ...

    #: Password recovery (backend/recovery_api.py): no session needed. The request answer is the same whether or
    #: not the account exists; the reset consumes a token from the delivered link.
    def request_recovery(self, identifier: str) -> dict: ...

    def reset_password(self, token: str, new_password: str) -> dict: ...

    # Optional (SyncService looks them up with getattr; a transport without them simply cannot renew a session):
    #     refresh(refresh_token) -> TokenPair    consume the credential for a new pair (never retry a lost answer)
    #     logout(refresh_token) -> None          revoke the credential's session on the server


def _classify(status: int, body: dict | None) -> Exception:
    code = ((body or {}).get("error") or {}).get("code", "")
    if status == 401:
        return AuthenticationError("The backend refused the credentials or the session has expired.")
    if status >= 500 or status in (408, 429):
        return TransportError(f"The backend is unavailable (HTTP {status}).", status=status, body=body)
    return ProtocolError(f"The backend refused the request (HTTP {status} {code}).".replace(" )", ")"),
                         status=status, code=code or None, body=(body or {}).get("error"))


def login_via(request, email: str, password: str) -> LoginResult:
    """The login exchange on top of any `request(method, path, token, body) -> dict` function."""
    answer = request("POST", "/auth/login", None, {"email": email, "password": password})
    if answer.get("mfa_required"):
        raise MfaRequired(answer["mfa_token"])
    return _login_result(request, answer)


def verify_mfa_via(request, mfa_token: str, code: str) -> LoginResult:
    """The second step of signing in: the authenticator's code (or a recovery code) for a login's mfa_token."""
    return _login_result(request, request("POST", "/auth/mfa/verify", None, {"mfa_token": mfa_token, "code": code}))


def _login_result(request, answer: dict) -> LoginResult:
    token = answer["access_token"]
    profile = request("GET", "/me", token, None)
    return LoginResult(token=token, user_id=profile["id"], email=profile["email"],
                       refresh_token=answer.get("refresh_token"))


def refresh_via(request, refresh_token: str) -> TokenPair:
    answer = request("POST", "/auth/refresh", None, {"refresh_token": refresh_token})
    return TokenPair(token=answer["access_token"], refresh_token=answer["refresh_token"])


def logout_via(request, refresh_token: str) -> None:
    request("POST", "/auth/logout", None, {"refresh_token": refresh_token})


def request_registration_via(request, email: str) -> dict:
    return request("POST", "/auth/register", None, {"email": email})


def confirm_registration_via(request, code: str, password: str, username: str | None,
                             display_name: str | None) -> dict:
    body = {"token": code, "password": password}
    if username:
        body["username"] = username
    if display_name:
        body["display_name"] = display_name
    return request("POST", "/auth/verify/confirm", None, body)


def request_email_verification_via(request, token: str) -> dict:
    return request("POST", "/auth/email/verification/request", token, {})


def confirm_email_verification_via(request, token: str, code: str) -> dict:
    return request("POST", "/auth/email/verification/confirm", token, {"token": code})


def profile_via(request, token: str) -> dict:
    return request("GET", "/me", token, None)


def update_profile_via(request, token: str, base_version: int, display_name: str | None) -> dict:
    return request("PATCH", "/me", token, {"base_version": base_version, "display_name": display_name})


def health_via(request) -> dict:
    return request("GET", "/health", None, None)


def ready_via(request) -> dict:
    """GET /ready: the server's own database and migration check (503 with a body when it is not ready)."""
    return request("GET", "/ready", None, None)


def _describe(error: BaseException, timeout: float) -> str:
    """Which stage of reaching the server failed, in words (never an address or a credential)."""
    reason = getattr(error, "reason", error)  # URLError wraps the socket/TLS error
    if isinstance(reason, socket.gaierror):
        return "the server's name could not be resolved (DNS); check the address and the network connection"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return ("the server's TLS certificate was not trusted; check this computer's date and time and any "
                "proxy or antivirus that inspects HTTPS")
    if isinstance(reason, ssl.SSLError):
        return "the secure (TLS) connection could not be established"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return f"no answer within {timeout:g} seconds"
    if isinstance(reason, ConnectionRefusedError):
        return "the connection was refused (nothing is listening at that address)"
    return type(reason).__name__ if isinstance(reason, BaseException) else type(error).__name__


def request_recovery_via(request, identifier: str) -> dict:
    return request("POST", "/auth/recovery/request", None, {"identifier": identifier})


def reset_password_via(request, token: str, new_password: str) -> dict:
    return request("POST", "/auth/recovery/reset", None, {"token": token, "new_password": new_password})


def reset_task_data_via(request, token: str) -> dict:
    return request("POST", "/me/task-data/reset", token, {"confirm": True})


#: What a server that predates GET /sync/capabilities supports.
LEGACY_CAPABILITIES = {"protocol_version": 1, "features": []}


def capabilities_via(request, token: str) -> dict:
    """GET /sync/capabilities; an older server without it (404) is protocol 1 with no optional features."""
    try:
        return request("GET", "/sync/capabilities", token, None)
    except ProtocolError as error:
        if error.status == 404:
            return dict(LEGACY_CAPABILITIES)
        raise


def pull_via(request, token: str, after: int, limit: int) -> PullPage:
    # include_task_types / include_budget: this client stores task-type and budget records; an older server
    # ignores the parameters.
    page = request("GET", f"/changes?after={int(after)}&limit={int(limit)}&include_task_types=true"
                          "&include_budget=true", token, None)
    return PullPage(changes=page["changes"], cursor=int(page["cursor"]), has_more=bool(page["has_more"]))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_backend_url(url: str) -> str:
    """
    The backend address to use (trimmed, no trailing slash), or ValueError
    saying what is wrong with it -- never echoing the address, which may
    hold a credential someone pasted by mistake.
    """
    url = (url or "").strip()
    if not url or "\\" in url or any(character.isspace() or ord(character) < 32 for character in url):
        raise ValueError("the backend URL is not a valid address")
    try:
        parts = urlsplit(url)
        host, _port = parts.hostname, parts.port  # .port raises for a malformed one
    except ValueError:
        raise ValueError("the backend URL is not a valid address") from None
    if parts.scheme not in ("https", "http") or not host:
        raise ValueError("the backend URL must start with https:// (or http:// for a server on this computer)")
    if "@" in parts.netloc:
        raise ValueError("the backend URL must not contain a user name or password")
    if parts.query or parts.fragment:
        raise ValueError("the backend URL must not contain a query or a fragment")
    if parts.scheme == "http" and not _is_loopback(host):
        raise ValueError("the backend URL must use https://; http:// is accepted only for a server on this "
                         "computer (localhost, 127.0.0.1 or [::1])")
    return url.rstrip("/")


#: Methods that may be sent once more when a reused keep-alive connection turns out to be closed: they have
#: no side effect, or the same one twice. POST and PATCH are never resent.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
#: A kept connection idle longer than this is replaced before use. Servers close idle keep-alive connections
#: (uvicorn after 5 s), and a request written into one that is just closing fails.
KEEPALIVE_IDLE_S = 4.0
#: What writing to or reading from a keep-alive connection the server has already closed raises.
_DROPPED = (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class _TrustStoreUnavailable(Exception):
    """The system's certificate store could not be loaded (no TLS context): nothing was sent."""


def _closed_by_server(connection: http.client.HTTPConnection) -> bool:
    """An idle kept socket that is readable has been closed by the server (or holds data nobody asked for)."""
    sock = connection.sock
    if sock is None:
        return False
    try:
        return bool(select.select([sock], [], [], 0)[0])
    except (OSError, ValueError):
        return True


class HttpTransport:
    """
    `wake_timeout`: a hosted server that was idle can be asleep and need most
    of a minute to start answering (a Render free web service does). So the
    first request, and the first after a failure, is preceded by GET /health
    with this longer timeout; requests themselves keep the short `timeout`.
    Only that side-effect-free probe waits longer: no request is ever resent,
    except the one case below.

    Connections: one keep-alive HTTP(S) connection per transport, made on the
    first request (TLS is set up then, not while the local database opens:
    a machine with a broken certificate store still works offline) and kept
    while requests follow each other -- a sync's capability, push and pull
    requests share one TLS handshake. It is used by one thread at a time (a
    lock); a request made while another thread uses it gets a connection of
    its own for that request. A kept connection that the server closed, or
    that was idle for KEEPALIVE_IDLE_S, is replaced before the request is
    written; if a reused one still turns out to be closed, only an idempotent
    request (IDEMPOTENT_METHODS) is sent again, once, on a new connection --
    a POST is reported as a TransportError and retried by the sync backoff.
    """

    def __init__(self, base_url: str, *, timeout: float = 10.0, wake_timeout: float = 75.0) -> None:
        self.base_url = validate_backend_url(base_url)
        parts = urlsplit(self.base_url)
        self._https = parts.scheme == "https"
        self._host, self._port = parts.hostname, parts.port
        self._prefix = parts.path  # e.g. "/api" of https://example.com/api
        self._context: ssl.SSLContext | None = None
        self._connection: http.client.HTTPConnection | None = None
        self._last_used = 0.0
        self._lock = threading.Lock()
        #: Connections made (a kept one counts once): how well keep-alive works, for tests and diagnostics.
        self.connections_opened = 0
        self._timeout = timeout
        self._wake_timeout = max(timeout, wake_timeout)
        self._awake = False

    def close(self) -> None:
        """Close the kept connection (the next request opens a new one)."""
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def _new_connection(self, timeout: float) -> http.client.HTTPConnection:
        self.connections_opened += 1
        if not self._https:
            return http.client.HTTPConnection(self._host, self._port, timeout=timeout)
        if self._context is None:
            try:
                # Certificate and host name verification stay on (the default context checks both).
                self._context = ssl.create_default_context()
            except OSError as error:  # e.g. a broken Windows certificate store
                raise _TrustStoreUnavailable() from error
        return http.client.HTTPSConnection(self._host, self._port, timeout=timeout, context=self._context)

    def _request(self, method: str, path: str, token: str | None, body: dict | None) -> dict:
        if not self._awake:
            self._send("GET", "/health", None, None, self._wake_timeout)
            self._awake = True
            if (method, path) == ("GET", "/health"):
                return {"status": "ok"}
        try:
            return self._send(method, path, token, body, self._timeout)
        except TransportError:
            self._awake = False
            raise

    def _send(self, method: str, path: str, token: str | None, body: dict | None, timeout: float) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            if self._lock.acquire(blocking=False):
                try:
                    status, raw = self._exchange_kept(method, self._prefix + path, data, headers, timeout)
                finally:
                    self._lock.release()
            else:  # another thread is using the kept connection: this request gets one of its own
                connection = self._new_connection(timeout)
                try:
                    status, raw = self._exchange(connection, method, self._prefix + path, data, headers)
                finally:
                    connection.close()
        except _TrustStoreUnavailable:
            raise TransportError("Could not set up a secure connection to the backend on this computer.") from None
        except (OSError, http.client.HTTPException) as error:  # DNS, refused, TLS, timeouts, dropped connections
            raise TransportError(f"Could not reach the backend: {_describe(error, timeout)}.") from None
        if 300 <= status < 400:
            raise ProtocolError("The backend answered with a redirect, which is never followed. Check the "
                                "backend address.", status=status, code="redirect_refused")
        if not 200 <= status < 300:
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                payload = None
            raise _classify(status, payload)
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise TransportError("The backend returned an unreadable response.") from None

    def _exchange_kept(self, method: str, target: str, data: bytes | None, headers: dict,
                       timeout: float) -> tuple[int, bytes]:
        """One request on the kept connection (the caller holds the lock); see the class docstring."""
        connection = self._connection
        if connection is not None and (connection.sock is None  # the server said Connection: close
                                       or time.monotonic() - self._last_used > KEEPALIVE_IDLE_S
                                       or _closed_by_server(connection)):
            self._drop()
            connection = None
        reused = connection is not None
        if connection is None:
            connection = self._connection = self._new_connection(timeout)
        else:
            connection.timeout = timeout
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
        try:
            answer = self._exchange(connection, method, target, data, headers)
        except _DROPPED:
            self._drop()
            if not (reused and method in IDEMPOTENT_METHODS):
                raise
            connection = self._connection = self._new_connection(timeout)  # once, on a new connection
            try:
                answer = self._exchange(connection, method, target, data, headers)
            except BaseException:
                self._drop()
                raise
        except BaseException:
            self._drop()  # a half-read answer must never be read by the next request
            raise
        self._last_used = time.monotonic()
        return answer

    @staticmethod
    def _exchange(connection: http.client.HTTPConnection, method: str, target: str, data: bytes | None,
                  headers: dict) -> tuple[int, bytes]:
        connection.request(method, target, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()  # all of it: the connection is then ready for the next request (or closed)
        return response.status, raw

    def login(self, email: str, password: str) -> LoginResult:
        return login_via(self._request, email, password)

    def verify_mfa(self, mfa_token: str, code: str) -> LoginResult:
        return verify_mfa_via(self._request, mfa_token, code)

    def account_request(self, method: str, path: str, token: str, body: dict | None = None) -> dict:
        return self._request(method, path, token, body)

    def refresh(self, refresh_token: str) -> TokenPair:
        return refresh_via(self._request, refresh_token)

    def logout(self, refresh_token: str) -> None:
        logout_via(self._request, refresh_token)

    def push(self, token: str, operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._request("POST", "/sync/push", token, {"operations": operations})["results"]

    def pull(self, token: str, after: int, limit: int) -> PullPage:
        return pull_via(self._request, token, after, limit)

    def request_registration(self, email: str) -> dict:
        return request_registration_via(self._request, email)

    def confirm_registration(self, code: str, password: str, username: str | None,
                             display_name: str | None) -> dict:
        return confirm_registration_via(self._request, code, password, username, display_name)

    def request_email_verification(self, token: str) -> dict:
        return request_email_verification_via(self._request, token)

    def confirm_email_verification(self, token: str, code: str) -> dict:
        return confirm_email_verification_via(self._request, token, code)

    def profile(self, token: str) -> dict:
        return profile_via(self._request, token)

    def update_profile(self, token: str, base_version: int, display_name: str | None) -> dict:
        return update_profile_via(self._request, token, base_version, display_name)

    def health(self) -> dict:
        return health_via(self._request)

    def ready(self) -> dict:
        return ready_via(self._request)

    def reset_task_data(self, token: str) -> dict:
        return reset_task_data_via(self._request, token)

    def capabilities(self, token: str) -> dict:
        return capabilities_via(self._request, token)

    def request_recovery(self, identifier: str) -> dict:
        return request_recovery_via(self._request, identifier)

    def reset_password(self, token: str, new_password: str) -> dict:
        return reset_password_via(self._request, token, new_password)
