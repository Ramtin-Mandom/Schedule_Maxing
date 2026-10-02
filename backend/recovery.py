"""
backend/recovery.py

Password recovery for password accounts -- the one implementation behind the
hosted API (POST /auth/recovery/request, POST /auth/recovery/reset) and the
direct desktop path. Like backend/accounts.py it needs no HTTP framework and
no JWT secret (the direct package profile imports it without FastAPI/PyJWT).

Credentials (OWASP Forgot Password Cheat Sheet):
    - a recovery token is 256 random bits (secrets.token_urlsafe(32)); only
      its SHA-256 digest is stored (password_recovery_tokens), bound to one
      user, with an expiry (RECOVERY_TOKEN_TTL_MINUTES) and a consumed or
      revoked state;
    - at most one token per account is outstanding: a new request revokes
      the earlier ones, so only the newest link works;
    - a request names an email or username; the answer to the caller never
      says whether an account exists (request() returns the delivery
      instruction separately, for the caller to hand to the delivery
      adapter in the background -- never to the client).

Reset (reset()), in one transaction:
    1. the token is consumed by a conditional UPDATE ... WHERE token_hash = ?
       AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > now:
       of two concurrent resets with one token exactly one updates the row
       (PostgreSQL row lock / SQLite's single writer); a replayed, expired,
       revoked or unknown token fails with the same InvalidRecoveryTokenError;
    2. the new password is checked with the registration rule and stored as
       an Argon2id hash;
    3. the account's credential_epoch is raised: every access token and
       browser session issued before carries the old epoch and is refused
       from then on (backend/api.py current_user_id, browser sessions,
       direct workspaces); the remaining tokens are revoked and the browser
       sessions are revoked too.
    A sign-in whose password check finished before the reset issues its
    token with the epoch it read, so that token is refused afterwards; there
    is no window in which an old password yields a usable new session.
    There is no automatic sign-in after a reset.

Nothing here logs or returns a password, a hash or a token, other than
handing the fresh token to the caller for delivery.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend import models
from backend.accounts import AccountError, AccountIdentity, _identity, check_password
from backend.passwords import hash_password, normalize_identifier

#: How long a recovery token stays valid unless settings say otherwise.
DEFAULT_TOKEN_TTL = timedelta(minutes=30)
#: A token longer than this is malformed (token_urlsafe(32) is 43 characters).
MAX_TOKEN_LENGTH = 128


class InvalidRecoveryTokenError(AccountError):
    """The token is unknown, malformed, expired, already used or replaced. One message for all of them."""

    def __init__(self) -> None:
        super().__init__("This recovery link is invalid or has expired. Request a new one.")


@dataclass(frozen=True)
class RecoveryDelivery:
    """What the caller must deliver -- out of band, never in a response: the address and the raw token."""

    user_id: uuid.UUID
    email: str
    token: str = field(repr=False)
    expires_at: datetime


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RecoveryService:
    """Recovery in one SQLAlchemy session (the caller owns it), committing its own changes."""

    def __init__(self, session: Session, clock: Callable[[], datetime] = _utcnow,
                 token_ttl: timedelta = DEFAULT_TOKEN_TTL) -> None:
        self._session = session
        self._clock = clock
        self._ttl = token_ttl

    def request(self, identifier: str) -> RecoveryDelivery | None:
        """
        Issue a recovery token for the account named by `identifier` (an email
        or a username) and return what to deliver -- or None for no such
        account. The work done is the same either way (a token is generated
        and digested), so timing does not single out existing accounts.
        """
        token = secrets.token_urlsafe(32)
        digest = token_digest(token)
        normalized = normalize_identifier(identifier or "")
        column = models.User.email if "@" in normalized else models.User.username
        user = self._session.scalars(select(models.User).where(column == normalized)).first() if normalized else None
        if user is None:
            self._session.rollback()
            return None
        now = self._clock()
        self._revoke_outstanding(user.id, now)
        expires_at = now + self._ttl
        self._session.add(models.PasswordRecoveryToken(
            id=uuid.uuid4(), user_id=user.id, token_hash=digest, created_at=now, expires_at=expires_at))
        self._session.commit()
        return RecoveryDelivery(user_id=user.id, email=user.email, token=token, expires_at=expires_at)

    def reset(self, token: str, new_password: str) -> AccountIdentity:
        """Consume `token` and set `new_password` (see the module docstring). InvalidRecoveryTokenError otherwise."""
        check_password(new_password)
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise InvalidRecoveryTokenError()
        now = self._clock()
        digest = token_digest(token)
        hashed = hash_password(new_password)  # before the transaction's write: hashing is slow
        try:
            consumed = self._session.execute(
                update(models.PasswordRecoveryToken)
                .where(models.PasswordRecoveryToken.token_hash == digest,
                       models.PasswordRecoveryToken.consumed_at.is_(None),
                       models.PasswordRecoveryToken.revoked_at.is_(None),
                       models.PasswordRecoveryToken.expires_at > now)
                .values(consumed_at=now)
                .returning(models.PasswordRecoveryToken.user_id)
            ).first()
            if consumed is None:
                raise InvalidRecoveryTokenError()
            user_id = consumed[0]
            self._session.execute(
                update(models.User).where(models.User.id == user_id).values(
                    password_hash=hashed, credential_epoch=models.User.credential_epoch + 1, updated_at=now)
                .execution_options(synchronize_session=False))
            self._revoke_outstanding(user_id, now)
            self._session.execute(
                update(models.BrowserSession)
                .where(models.BrowserSession.user_id == user_id, models.BrowserSession.revoked_at.is_(None))
                .values(revoked_at=now))
            self._session.commit()
        except BaseException:
            self._session.rollback()
            raise
        user = self._session.get(models.User, user_id, populate_existing=True)
        return _identity(user)

    def _revoke_outstanding(self, user_id: uuid.UUID, now: datetime) -> None:
        self._session.execute(
            update(models.PasswordRecoveryToken)
            .where(models.PasswordRecoveryToken.user_id == user_id,
                   models.PasswordRecoveryToken.consumed_at.is_(None),
                   models.PasswordRecoveryToken.revoked_at.is_(None))
            .values(revoked_at=now))


def credential_epoch(session: Session, user_id: uuid.UUID) -> int | None:
    """The account's current credential epoch (None: no such account) -- read fresh, never from a cache."""
    return session.scalar(select(models.User.credential_epoch).where(models.User.id == user_id))
