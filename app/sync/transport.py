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
malformed is refused. TLS certificates are verified with the system's trust
store (never disabled), and no redirect is ever followed: urllib would resend
the Authorization header -- and re-issue the request -- to wherever a
redirect points.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import urllib.error
import urllib.request
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
    # include_task_types: this client stores task-type records; an older server ignores the parameter.
    page = request("GET", f"/changes?after={int(after)}&limit={int(limit)}&include_task_types=true", token, None)
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


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """No redirect is followed: the 3xx answer surfaces as an HTTPError (see _send)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    """
    `wake_timeout`: a hosted server that was idle can be asleep and need most
    of a minute to start answering (a Render free web service does). So the
    first request, and the first after a failure, is preceded by GET /health
    with this longer timeout; requests themselves keep the short `timeout`.
    Only that side-effect-free probe waits longer: no request is ever resent.
    """

    def __init__(self, base_url: str, *, timeout: float = 10.0, wake_timeout: float = 75.0) -> None:
        self.base_url = validate_backend_url(base_url)
        # Certificate and host name verification stay on (the default context); redirects are refused.
        self._opener = urllib.request.build_opener(
            _RefuseRedirects, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self._timeout = timeout
        self._wake_timeout = max(timeout, wake_timeout)
        self._awake = False

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
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                raise ProtocolError("The backend answered with a redirect, which is never followed. Check the "
                                    "backend address.", status=error.code, code="redirect_refused") from None
            try:
                payload = json.loads(error.read().decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                payload = None
            raise _classify(error.code, payload) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as error:
            raise TransportError(f"Could not reach the backend: {_describe(error, timeout)}.") from None
        except ValueError:
            raise TransportError("The backend returned an unreadable response.") from None

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
