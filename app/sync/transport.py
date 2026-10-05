"""
app/sync/transport.py

How the sync client talks to the backend. SyncEngine only uses the small
SyncTransport interface, so tests inject in-process or failing transports.
HttpTransport uses the standard library (urllib) with request timeouts: the
desktop app gains no network dependency.

Failures are classified, because they are handled differently:
    TransportError       network trouble, timeouts, HTTP 5xx: retry later (backoff)
    AuthenticationError  HTTP 401: the token is missing/expired; sign in again
    ProtocolError        any other refusal of the whole request (4xx): a bug or
                         an incompatible server; not retried automatically
Individual operation outcomes (applied / conflict / rejected) are results,
not exceptions.

Credentials: the access token is only held in memory by SyncService and is
passed per call; it is never written to disk or logged. Passwords are sent
once to /auth/login and never stored.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


class TransportError(Exception):
    """Retryable: the server could not be reached or failed (timeouts, 5xx)."""


class AuthenticationError(Exception):
    """The server refused the credentials or token (401)."""


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


@dataclass(frozen=True)
class PullPage:
    changes: list[dict[str, Any]]
    cursor: int
    has_more: bool


class SyncTransport(Protocol):
    base_url: str

    def login(self, email: str, password: str) -> LoginResult: ...

    def push(self, token: str, operations: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def pull(self, token: str, after: int, limit: int) -> PullPage: ...

    # Public account operations (the local web profile, app/web): registration, the profile, and a
    # reachability probe. Each returns the server's JSON document.

    def register(self, email: str, password: str, username: str | None, display_name: str | None) -> dict: ...

    def profile(self, token: str) -> dict: ...

    def update_profile(self, token: str, base_version: int, display_name: str | None) -> dict: ...

    def health(self) -> dict: ...

    #: Remove all of the account's task data on the server ({removed, cursor}); see backend/task_data_reset.py.
    def reset_task_data(self, token: str) -> dict: ...

    #: The server's sync protocol version and features ({protocol_version, features, ...}); an older server that
    #: has no such endpoint answers as protocol 1 without features (see capabilities_via).
    def capabilities(self, token: str) -> dict: ...

    #: Password recovery (backend/recovery_api.py): no session needed. The request answer is the same whether or
    #: not the account exists; the reset consumes a token from the delivered link.
    def request_recovery(self, identifier: str) -> dict: ...

    def reset_password(self, token: str, new_password: str) -> dict: ...


def _classify(status: int, body: dict | None) -> Exception:
    code = ((body or {}).get("error") or {}).get("code", "")
    if status == 401:
        return AuthenticationError("The backend refused the credentials or the session has expired.")
    if status >= 500 or status in (408, 429):
        return TransportError(f"The backend is unavailable (HTTP {status}).")
    return ProtocolError(f"The backend refused the request (HTTP {status} {code}).".replace(" )", ")"),
                         status=status, code=code or None, body=(body or {}).get("error"))


def login_via(request, email: str, password: str) -> LoginResult:
    """The login exchange on top of any `request(method, path, token, body) -> dict` function."""
    token = request("POST", "/auth/login", None, {"email": email, "password": password})["access_token"]
    profile = request("GET", "/me", token, None)
    return LoginResult(token=token, user_id=profile["id"], email=profile["email"])


def register_via(request, email: str, password: str, username: str | None, display_name: str | None) -> dict:
    body = {"email": email, "password": password}
    if username:
        body["username"] = username
    if display_name:
        body["display_name"] = display_name
    return request("POST", "/auth/register", None, body)


def profile_via(request, token: str) -> dict:
    return request("GET", "/me", token, None)


def update_profile_via(request, token: str, base_version: int, display_name: str | None) -> dict:
    return request("PATCH", "/me", token, {"base_version": base_version, "display_name": display_name})


def health_via(request) -> dict:
    return request("GET", "/health", None, None)


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
    # include_task_types: this client stores task-type records; an older server ignores the parameter.
    page = request("GET", f"/changes?after={int(after)}&limit={int(limit)}&include_task_types=true", token, None)
    return PullPage(changes=page["changes"], cursor=int(page["cursor"]), has_more=bool(page["has_more"]))


class HttpTransport:
    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("the backend URL must start with https:// (or http:// for a local server)")
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _request(self, method: str, path: str, token: str | None, body: dict | None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read().decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                payload = None
            raise _classify(error.code, payload) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as error:
            raise TransportError(f"Could not reach the backend: {type(error).__name__}.") from None
        except ValueError:
            raise TransportError("The backend returned an unreadable response.") from None

    def login(self, email: str, password: str) -> LoginResult:
        return login_via(self._request, email, password)

    def push(self, token: str, operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._request("POST", "/sync/push", token, {"operations": operations})["results"]

    def pull(self, token: str, after: int, limit: int) -> PullPage:
        return pull_via(self._request, token, after, limit)

    def register(self, email: str, password: str, username: str | None, display_name: str | None) -> dict:
        return register_via(self._request, email, password, username, display_name)

    def profile(self, token: str) -> dict:
        return profile_via(self._request, token)

    def update_profile(self, token: str, base_version: int, display_name: str | None) -> dict:
        return update_profile_via(self._request, token, base_version, display_name)

    def health(self) -> dict:
        return health_via(self._request)

    def reset_task_data(self, token: str) -> dict:
        return reset_task_data_via(self._request, token)

    def capabilities(self, token: str) -> dict:
        return capabilities_via(self._request, token)

    def request_recovery(self, identifier: str) -> dict:
        return request_recovery_via(self._request, identifier)

    def reset_password(self, token: str, new_password: str) -> dict:
        return reset_password_via(self._request, token, new_password)
