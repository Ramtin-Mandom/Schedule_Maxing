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
    - a new request does not invalidate earlier links: somebody else asking
      for links to an account must not keep cancelling the ones its owner is
      about to use. At most MAX_OUTSTANDING_TOKENS are valid per account
      (the oldest is revoked beyond that), each expires, and a successful
      reset revokes every remaining one;
    - a request names an email or username; the answer to the caller never
      says whether an account exists. The API queues the email in the outbox
      (queue()), which mints the token when it sends: no token is stored in
      the queue, and nothing about delivery shows in the response.

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
       direct workspaces); the remaining recovery tokens, the app sessions
       (refresh credentials) and the browser sessions are revoked too, and
       the owner is notified. A second factor is *not* removed: after a
       reset, signing in still needs it.
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

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend import models
from backend.accounts import AccountError, AccountIdentity, _identity, check_password
from backend.passwords import hash_password, normalize_identifier

#: How long a recovery token stays valid unless settings say otherwise.
DEFAULT_TOKEN_TTL = timedelta(minutes=30)
#: Valid links per account at a time; a request beyond that revokes the oldest.
MAX_OUTSTANDING_TOKENS = 5
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
        or a username) and return it -- or None for no such account. For
        callers that deliver it themselves (the direct path, tests); the
        hosted API queues the email instead (queue()).
        """
        user = self.account(identifier)
        if user is None:
            self._session.rollback()
            return None
        return self.issue(user[0])

    def account(self, identifier: str) -> tuple[uuid.UUID, str] | None:
        """(id, email) of the account an email or username names, or None. Says nothing to any client."""
        normalized = normalize_identifier(identifier or "")
        if not normalized:
            return None
        column = models.User.email if "@" in normalized else models.User.username
        found = self._session.execute(select(models.User.id, models.User.email).where(column == normalized)).first()
        return (found[0], found[1]) if found is not None else None

    def queue(self, user_id: uuid.UUID, email: str) -> None:
        """Queue the recovery email of an account (committed); its token is minted when it is sent (issue())."""
        from backend import outbox

        outbox.enqueue(self._session, kind=outbox.RECOVERY, to_email=email, subject_id=user_id, now=self._clock())
        self._session.commit()

    def issue(self, user_id: uuid.UUID) -> RecoveryDelivery | None:
        """
        A fresh token for the account (committed). Earlier tokens stay valid, up to MAX_OUTSTANDING_TOKENS:
        beyond that the oldest are revoked.
        """
        email = self._session.scalar(select(models.User.email).where(models.User.id == user_id))
        if email is None:
            self._session.rollback()
            return None
        now = self._clock()
        token_model = models.PasswordRecoveryToken
        outstanding = list(self._session.scalars(
            select(token_model.id).where(token_model.user_id == user_id, token_model.consumed_at.is_(None),
                                         token_model.revoked_at.is_(None), token_model.expires_at > now)
            .order_by(token_model.created_at.desc(), token_model.id)))
        surplus = outstanding[MAX_OUTSTANDING_TOKENS - 1:]
        if surplus:
            self._session.execute(update(token_model).where(token_model.id.in_(surplus)).values(revoked_at=now))
        token = secrets.token_urlsafe(32)
        expires_at = now + self._ttl
        self._session.add(token_model(id=uuid.uuid4(), user_id=user_id, token_hash=token_digest(token),
                                      created_at=now, expires_at=expires_at))
        self._session.commit()
        return RecoveryDelivery(user_id=user_id, email=email, token=token, expires_at=expires_at)

    def reset(self, token: str, new_password: str) -> AccountIdentity:
        """Consume `token` and set `new_password` (see the module docstring). InvalidRecoveryTokenError otherwise."""
        check_password(new_password)
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise InvalidRecoveryTokenError()
        now = self._clock()
        digest = token_digest(token)
        # A cheap look first: an unknown, used, replaced or expired token is refused without hashing anything.
        # It decides nothing -- the conditional UPDATE below re-checks and consumes the token atomically.
        usable = self._session.scalar(
            select(models.PasswordRecoveryToken.id)
            .where(models.PasswordRecoveryToken.token_hash == digest,
                   models.PasswordRecoveryToken.consumed_at.is_(None),
                   models.PasswordRecoveryToken.revoked_at.is_(None),
                   models.PasswordRecoveryToken.expires_at > now))
        if usable is None:
            raise InvalidRecoveryTokenError()
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
                    password_hash=hashed, credential_epoch=models.User.credential_epoch + 1, updated_at=now,
                    # The link was mailed to the account's address: whoever used it owns the mailbox.
                    email_verified_at=func.coalesce(models.User.email_verified_at, now))
                .execution_options(synchronize_session=False))
            self._revoke_outstanding(user_id, now)
            from backend import account_security, outbox

            from backend.verification import revoke_email_changes

            revoke_email_changes(self._session, user_id, now)
            account_security.end_sessions(self._session, user_id, now)
            changed = self._session.execute(select(models.User.email, models.User.credential_epoch)
                                            .where(models.User.id == user_id)).one()
            account_security.notify(self._session, outbox.PASSWORD_CHANGED, user_id, changed[0], now,
                                    dedupe_key=f"password_changed:{user_id}:{changed[1]}")
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
