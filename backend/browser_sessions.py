"""
backend/browser_sessions.py

Browser sessions for the web UI, alongside (not instead of) the bearer
tokens that desktop synchronization uses.

    - POST /auth/browser/login checks the same credentials as /auth/login and
      sets one cookie, COOKIE_NAME: a random 256-bit token, HttpOnly (page
      scripts cannot read it), SameSite=Strict, Secure unless
      BROWSER_COOKIE_SECURE=false (plain-http local development only), Path=/.
      The server stores only the token's SHA-256 (browser_sessions table),
      never the token, the password, or a bearer token. The response carries
      the session's CSRF token; a page keeps it in memory only (never in
      localStorage/sessionStorage or a URL) and can fetch it again from
      GET /auth/browser/session.
    - A cookie-authenticated request is accepted until the session expires
      (BROWSER_SESSION_TTL_MINUTES, fixed at login) or is revoked by
      POST /auth/browser/logout -- revocation is stored, so a copied cookie
      stops working at once -- or the account's password is reset: a reset
      revokes every session and raises the account's credential epoch, and a
      session started under an older epoch is refused (backend/recovery.py).
    - Every cookie-authenticated request with an unsafe method (anything but
      GET/HEAD/OPTIONS) must send X-CSRF-Token (an HMAC of the session token
      under the server secret, compared in constant time) and, when the
      browser sends Origin, come from the server's own origin or one listed
      in ALLOWED_ORIGINS. Bearer-token requests are unaffected: browsers never
      attach them on their own, so they carry no CSRF risk.

Errors are explicit: 401 unauthenticated / session_expired, 403
csrf_failed / origin_not_allowed. Nothing is logged.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import models
from backend.errors import ApiError
from backend.settings import BackendSettings

COOKIE_NAME = "sm_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_token_for(token: str, settings: BackendSettings) -> str:
    return hmac.new(settings.jwt_secret.encode("utf-8"), b"csrf:" + token.encode("utf-8"), hashlib.sha256).hexdigest()


def start_session(
    session: Session, user_id: uuid.UUID, settings: BackendSettings, now: datetime, credential_epoch: int = 0
) -> tuple[str, models.BrowserSession]:
    token = secrets.token_urlsafe(32)
    row = models.BrowserSession(
        id=uuid.uuid4(), user_id=user_id, token_hash=_digest(token), created_at=now,
        expires_at=now + timedelta(minutes=settings.browser_session_ttl_minutes), credential_epoch=credential_epoch,
    )
    session.add(row)
    session.commit()
    return token, row


def set_cookie(response: Response, token: str, row: models.BrowserSession, settings: BackendSettings, now: datetime) -> None:
    response.set_cookie(
        COOKIE_NAME, token, max_age=int((row.expires_at - now).total_seconds()), path="/",
        httponly=True, secure=settings.browser_cookie_secure, samesite="strict",
    )


def clear_cookie(response: Response, settings: BackendSettings) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=settings.browser_cookie_secure, samesite="strict")


def find_session(session: Session, token: str) -> models.BrowserSession | None:
    return session.scalars(select(models.BrowserSession).where(models.BrowserSession.token_hash == _digest(token))).first()


def live_session(session: Session, token: str | None, now: datetime) -> models.BrowserSession:
    """The unrevoked, unexpired session of `token` whose credential epoch is current, or a clear 401."""
    row = find_session(session, token) if token else None
    if row is None or row.revoked_at is not None:
        raise ApiError(401, "unauthenticated", "You are signed out. Sign in again.")
    if row.expires_at <= now:
        raise ApiError(401, "session_expired", "Your session has expired. Sign in again.")
    epoch = session.scalar(select(models.User.credential_epoch).where(models.User.id == row.user_id))
    if epoch is None or epoch != row.credential_epoch:
        raise ApiError(401, "unauthenticated", "You are signed out. Sign in again.")
    return row


def _same_origin(request: Request, origin: str, settings: BackendSettings) -> bool:
    if origin in settings.allowed_origins:
        return True
    return urlsplit(origin).netloc.lower() == request.headers.get("host", "").lower()


def check_request(request: Request, token: str, settings: BackendSettings) -> None:
    """CSRF and origin checks for a cookie-authenticated request (see the module docstring)."""
    if request.method.upper() in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is not None and not _same_origin(request, origin, settings):
        raise ApiError(403, "origin_not_allowed", "Requests from this origin are not accepted.")
    supplied = request.headers.get(CSRF_HEADER, "")
    if not supplied or not hmac.compare_digest(supplied, csrf_token_for(token, settings)):
        raise ApiError(403, "csrf_failed", f"A valid {CSRF_HEADER} header is required for this request.")


def authenticate(request: Request, session: Session, now: datetime) -> uuid.UUID:
    token = request.cookies.get(COOKIE_NAME)
    row = live_session(session, token, now)
    check_request(request, token, request.app.state.settings)
    return row.user_id
