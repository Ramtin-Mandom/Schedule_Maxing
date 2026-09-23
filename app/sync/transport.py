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
    """The server refused the whole request (4xx other than 401)."""


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


def _classify(status: int, body: dict | None) -> Exception:
    code = ((body or {}).get("error") or {}).get("code", "")
    if status == 401:
        return AuthenticationError("The backend refused the credentials or the session has expired.")
    if status >= 500 or status in (408, 429):
        return TransportError(f"The backend is unavailable (HTTP {status}).")
    return ProtocolError(f"The backend refused the request (HTTP {status} {code}).".replace(" )", ")"))


def login_via(request, email: str, password: str) -> LoginResult:
    """The login exchange on top of any `request(method, path, token, body) -> dict` function."""
    token = request("POST", "/auth/login", None, {"email": email, "password": password})["access_token"]
    profile = request("GET", "/me", token, None)
    return LoginResult(token=token, user_id=profile["id"], email=profile["email"])


def pull_via(request, token: str, after: int, limit: int) -> PullPage:
    page = request("GET", f"/changes?after={int(after)}&limit={int(limit)}", token, None)
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
