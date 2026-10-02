"""
backend/protection.py

Request-level protections installed by backend/app.py (docs/backend.md,
"Protections"). What the application guarantees, and what the operator's
reverse proxy must still supply, are separated there.

    client_address(request)   the client IP for throttling: the socket peer,
                              or -- only when that peer is one of
                              TRUSTED_PROXIES -- the right-most
                              X-Forwarded-For address that is not itself a
                              trusted proxy. A forged X-Forwarded-For from
                              any other peer is ignored.
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


def _networks(proxies: Iterable[str]):
    return [ipaddress.ip_network(proxy, strict=False) for proxy in proxies]


def client_address(request: Request, trusted_proxies: Iterable[str]) -> str:
    peer = request.client.host if request.client else ""
    networks = _networks(trusted_proxies)

    def trusted(address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in network for network in networks)

    if not networks or not trusted(peer):
        return peer
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
    for address in reversed(forwarded):
        if not trusted(address):
            try:
                return str(ipaddress.ip_address(address))
            except ValueError:
                return peer  # a malformed entry: fall back to the proxy itself
    return peer


class RequestTooLarge(Exception):  # kept for callers that import it
    pass


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
