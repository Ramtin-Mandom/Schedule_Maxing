"""
backend/protection.py

Request-level protections installed by backend/app.py (docs/backend.md,
"Protections"). What the application guarantees, and what the operator's
reverse proxy must still supply, are separated there.

    client_address(request)   the client IP for throttling: the socket peer,
                              unless that peer is one of TRUSTED_PROXIES.
                              Then, and only then, a forwarding header is
                              read: CLIENT_IP_HEADER when one is configured
                              (a header the trusted proxy overwrites with a
                              single address, e.g. CF-Connecting-IP), else
                              the right-most X-Forwarded-For address that is
                              not itself a trusted proxy, over all
                              X-Forwarded-For lines in order. The left-most
                              entry is never believed: the client writes it.
                              A forwarding header from any other peer is
                              ignored, and a missing or malformed one means
                              the proxy's own address (one shared bucket --
                              never an address the client chose).
    throttle_address(address) the address as a limiter subject: an IPv6
                              client counts as its /64 (one subscriber owns
                              the whole /64 and can rotate through it).
    BodySizeLimit             an ASGI middleware refusing request bodies over
                              MAX_REQUEST_BYTES with 413 request_too_large:
                              at once when Content-Length says so, and while
                              reading a chunked body (buffered up to the limit) --
                              before the application parses it.
    SecretRedactingFilter     a logging filter for access logs: replaces the
                              value of token/password/secret query
                              parameters and Authorization headers in log
                              records. (Recovery links carry their token in
                              the URL fragment, which never reaches the
                              server; this is defense in depth.)
    install_access_log_redaction()  attaches the filter to uvicorn's loggers.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from collections.abc import Iterable

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send


LOG = logging.getLogger("schedule_maxing.security")
_warned_untrusted_forwarding = False


def _networks(proxies: Iterable[str]):
    return [ipaddress.ip_network(proxy, strict=False) for proxy in proxies]


def _parse_address(value: str):
    """An IP address from a header entry or socket peer ("1.2.3.4", "1.2.3.4:567", "[::1]:567"), else None."""
    value = value.strip()
    if value.startswith("["):
        value = value[1:].partition("]")[0]
    elif value.count(":") == 1:
        value = value.partition(":")[0]  # an IPv4 address with a port
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    return ip.ipv4_mapped or ip if ip.version == 6 else ip  # ::ffff:10.0.0.1 is 10.0.0.1


def client_address(request: Request, trusted_proxies: Iterable[str], client_ip_header: str = "") -> str:
    peer = request.client.host if request.client else ""
    networks = _networks(trusted_proxies)

    def trusted(ip) -> bool:
        return any(ip.version == network.version and ip in network for network in networks)

    peer_ip = _parse_address(peer)
    if not networks or peer_ip is None or not trusted(peer_ip):
        global _warned_untrusted_forwarding
        if not _warned_untrusted_forwarding and "x-forwarded-for" in request.headers:
            # Once per process: behind an unlisted reverse proxy every client would share its address's limits.
            _warned_untrusted_forwarding = True
            LOG.warning("A request with X-Forwarded-For came from %s, which is not in TRUSTED_PROXIES; the header "
                        "is ignored and rate limits count that address. If it is your reverse proxy, list it "
                        "(docs/render-deployment.md, \"Client addresses\").", peer or "an unknown peer")
        return str(peer_ip) if peer_ip is not None else peer
    if client_ip_header:
        # A header the trusted proxy sets itself: exactly one line holding one address, or it is not believed.
        values = request.headers.getlist(client_ip_header)
        ip = _parse_address(values[0]) if len(values) == 1 and "," not in values[0] else None
        return str(ip if ip is not None else peer_ip)
    # Every X-Forwarded-For line, in order: a proxy may append to the client's line or add a line of its own.
    forwarded = [part for line in request.headers.getlist("x-forwarded-for") for part in line.split(",")
                 if part.strip()]
    for entry in reversed(forwarded):
        ip = _parse_address(entry)
        if ip is None:
            return str(peer_ip)  # a malformed entry: fall back to the proxy itself
        if not trusted(ip):
            return str(ip)
    return str(peer_ip)


def throttle_address(address: str) -> str:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    return str(ipaddress.ip_network((ip, 64), strict=False)) if ip.version == 6 else str(ip)


class RequestTooLarge(Exception):  # kept for callers that import it
    pass


#: Paths whose pages load their own scripts and styles (the interactive API documentation).
DOCUMENTATION_PATHS = ("/docs", "/redoc")


class PrivateResponses:
    """
    Headers every response carries:
        Cache-Control: no-store (always, replacing anything set): credentials and user records must not be
            kept in shared or browser caches;
        X-Content-Type-Options: nosniff, Referrer-Policy: no-referrer, X-Frame-Options: DENY;
        Content-Security-Policy: default-src 'none'; frame-ancestors 'none' -- an API answers data, not pages.
            A route that serves a page sets its own stricter-for-its-needs policy (the reset and verification
            pages do), which is kept; the documentation pages get none (they load their viewer);
        Strict-Transport-Security, when `hsts` (production: the service is only ever reached over HTTPS).
    """

    def __init__(self, app: ASGIApp, hsts: bool = False):
        self.app = app
        self._defaults = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                          (b"x-frame-options", b"DENY")]
        if hsts:
            self._defaults.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
        self._policy = (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        defaults = list(self._defaults)
        if not scope.get("path", "").startswith(DOCUMENTATION_PATHS):
            defaults.append(self._policy)

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() not in (b"cache-control", b"pragma")]
                present = {key.lower() for key, _value in headers}
                headers += [(key, value) for key, value in defaults if key not in present]
                message = {**message, "headers": headers + [(b"cache-control", b"no-store"),
                                                            (b"pragma", b"no-cache")]}
            await send(message)

        await self.app(scope, receive, private_send)


class BodySizeLimit:
    """Refuses request bodies larger than `max_bytes` (413) before the application reads them."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                too_large = int(declared) > self.max_bytes
            except ValueError:
                too_large = True
            if too_large:
                await _refuse(send, self.max_bytes)
                return
        if declared is not None:
            # The server enforces the declared length, so the body cannot grow past it.
            await self.app(scope, receive, send)
            return
        # No Content-Length (chunked): read the body up to the limit before the application sees any of it --
        # an error raised while the application parses would be answered by the framework, not as a 413.
        messages: list[Message] = []
        received = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            received += len(message.get("body", b""))
            if received > self.max_bytes:
                await _refuse(send, self.max_bytes)
                return
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            return messages.pop(0) if messages else await receive()

        await self.app(scope, replay, send)


async def _refuse(send: Send, max_bytes: int) -> None:
    body = json.dumps({"error": {"code": "request_too_large",
                                 "message": f"The request body may be at most {max_bytes} bytes."}}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})


_SECRET_QUERY = re.compile(r"(?i)((?:token|password|secret|code)=)[^&\s\"']+")
_AUTHORIZATION = re.compile(r"(?i)(authorization:\s*bearer\s+)\S+")


def redact(text: str) -> str:
    return _AUTHORIZATION.sub(r"\1[redacted]", _SECRET_QUERY.sub(r"\1[redacted]", text))


class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact(arg) if isinstance(arg, str) else arg for arg in record.args)
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def install_access_log_redaction() -> None:
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, SecretRedactingFilter) for item in logger.filters):
            logger.addFilter(SecretRedactingFilter())
