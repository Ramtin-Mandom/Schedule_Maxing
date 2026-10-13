"""
backend/verification.py

Proof that an email address belongs to the person using it -- the one
implementation behind the hosted API (backend/verification_api.py). Like
backend/accounts.py it needs no HTTP framework.

New accounts (register -> email -> confirm -> account):
    1. start_registration(email) records a PendingRegistration: the address
       and an expiry, nothing else. No password, no user row, no session:
       there is nothing to sign in to and nothing an earlier requester could
       have planted.
    2. The verification email is sent from the outbox (backend/outbox.py),
       which mints the token when it sends -- no token is ever stored in the
       queue.
    3. confirm_registration(token, password, ...) is called by whoever
       received the email. The account is created *then*, with the password
       chosen *then*. So "pre-hijacking" has nothing to hold on to: someone
       who registered a victim's address first chose no credentials, and a
       victim who confirms sets their own.
    An existing account is never touched by a registration request: its
    owner gets a notice instead, and the public answer is the same.

Existing accounts whose address was never proved (users.email_verified_at
IS NULL: created before verification existed, or directly in the database):
    not declared verified, not deleted, not locked out. The owner asks for a
    link while signed in (request_account_verification) and confirms it
    while signed in (confirm_account_verification): the session proves the
    password, the token proves the mailbox, and neither alone verifies the
    account -- so the mailbox owner clicking a link cannot bless credentials
    somebody else chose for their address. A password reset through an
    emailed link also verifies the address (backend/recovery.py): there the
    mailbox owner replaces the credentials.

Tokens: 256 random bits; stored only as SHA-256("<purpose>:<token>"), so a
token is useless for another purpose; bound to one pending registration or
account and to the address it was mailed to; expiring; consumed by one
conditional UPDATE (of two concurrent confirmations exactly one wins;
replay, expiry, revocation and an unknown token fail alike). A new email
does not invalidate earlier links -- at most MAX_OUTSTANDING_TOKENS are
valid per subject (the oldest goes when another is minted) -- and a
successful verification ends all of them.

Nothing here logs or returns a password, a hash or a token, other than
handing a fresh token to the outbox for the email it is sending.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models, outbox
from backend.accounts import (
    AccountError,
    AccountIdentity,
    AccountValidationError,
    _identity,
    check_email,
    check_password,
    check_username,
    clean_display_name,
)
from backend.passwords import hash_password

REGISTRATION = "registration"
ACCOUNT = "account"
EMAIL_CHANGE = "email_change"

DEFAULT_TOKEN_TTL = timedelta(hours=24)
DEFAULT_PENDING_TTL = timedelta(hours=48)
MAX_OUTSTANDING_TOKENS = 5
#: A token longer than this is malformed (token_urlsafe(32) is 43 characters).
MAX_TOKEN_LENGTH = 128


class InvalidVerificationTokenError(AccountError):
    """Unknown, malformed, expired, used or replaced -- or its registration is gone. One message for all."""

    def __init__(self) -> None:
        super().__init__("This verification link is invalid or has expired. Request a new one.")


def token_digest(purpose: str, token: str) -> str:
    return hashlib.sha256(f"{purpose}:{token}".encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _live(now: datetime):
    token = models.EmailVerificationToken
    return (token.consumed_at.is_(None), token.revoked_at.is_(None), token.expires_at > now)


def mint_token(session: Session, purpose: str, email: str, now: datetime, ttl: timedelta, *,
               pending_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None) -> str:
    """
    A fresh token for the subject (not committed; the caller's transaction decides). Earlier tokens stay valid,
    up to MAX_OUTSTANDING_TOKENS: beyond that the oldest are revoked.
    """
    token_model = models.EmailVerificationToken
    subject = token_model.pending_id == pending_id if pending_id is not None else token_model.user_id == user_id
    outstanding = list(session.scalars(
        select(token_model.id).where(subject, token_model.purpose == purpose, *_live(now))
        .order_by(token_model.created_at.desc(), token_model.id)))
    surplus = outstanding[MAX_OUTSTANDING_TOKENS - 1:]
    if surplus:
        session.execute(update(token_model).where(token_model.id.in_(surplus)).values(revoked_at=now))
    token = secrets.token_urlsafe(32)
    session.add(token_model(
        id=uuid.uuid4(), purpose=purpose, pending_id=pending_id, user_id=user_id, email=email,
        token_hash=token_digest(purpose, token), created_at=now, expires_at=now + ttl))
    return token


class VerificationService:
    """Verification in one SQLAlchemy session (the caller owns it), committing its own changes."""

    def __init__(self, session: Session, clock: Callable[[], datetime] = _utcnow, *,
                 token_ttl: timedelta = DEFAULT_TOKEN_TTL, pending_ttl: timedelta = DEFAULT_PENDING_TTL) -> None:
        self._session = session
        self._clock = clock
        self._token_ttl = token_ttl
        self._pending_ttl = pending_ttl

    # -- new accounts ---------------------------------------------------------------------

    def start_registration(self, email: str, *, send: bool = True) -> None:
        """
        Record that `email` (already validated: accounts.check_email) wants an account and queue the email for
        its owner: the verification link, or -- when an account already uses the address -- a notice that
        changes nothing. `send=False` (the caller's email limits are used up) records without queueing. The
        caller learns nothing either way.
        """
        now = self._clock()
        session = self._session
        user_id = session.scalar(select(models.User.id).where(models.User.email == email))
        if user_id is not None:
            if send:
                outbox.enqueue(session, kind=outbox.ACCOUNT_EXISTS, to_email=email, subject_id=user_id, now=now)
            session.commit()
            return
        pending = session.scalars(
            select(models.PendingRegistration).where(models.PendingRegistration.email == email)).first()
        if pending is not None and pending.expires_at <= now:
            session.delete(pending)  # its tokens go with it (ON DELETE CASCADE)
            session.flush()
            pending = None
        if pending is None:
            pending = models.PendingRegistration(id=uuid.uuid4(), email=email, created_at=now,
                                                 expires_at=now + self._pending_ttl)
            session.add(pending)
            try:
                session.flush()
            except IntegrityError:
                # A concurrent request for the same address recorded it first: use that one.
                session.rollback()
                pending = session.scalars(
                    select(models.PendingRegistration).where(models.PendingRegistration.email == email)).first()
                if pending is None:
                    return
        if send:
            outbox.enqueue(session, kind=outbox.VERIFICATION, to_email=email, subject_id=pending.id, now=now)
        session.commit()

    def mint_registration_token(self, email: str) -> str | None:
        """A token for the pending registration of `email` (committed), or None. For the outbox and for tests."""
        now = self._clock()
        pending = self._session.scalars(
            select(models.PendingRegistration)
            .where(models.PendingRegistration.email == email, models.PendingRegistration.expires_at > now)).first()
        if pending is None:
            return None
        token = mint_token(self._session, REGISTRATION, pending.email, now, self._token_ttl, pending_id=pending.id)
        self._session.commit()
        return token

    def confirm_registration(self, token: str, password: str, *, username: str | None = None,
                             display_name: str | None = None) -> AccountIdentity:
        """
        Create the account of a pending registration, with the password given now. InvalidVerificationTokenError
        for every unusable token; AccountValidationError for a bad password, username or display name (the token
        stays usable).
        """
        normalized_username = check_username(username)
        check_password(password, username=normalized_username)
        name = clean_display_name(display_name)
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise InvalidVerificationTokenError()
        now = self._clock()
        session = self._session
        token_model, pending_model = models.EmailVerificationToken, models.PendingRegistration
        digest = token_digest(REGISTRATION, token)
        # A cheap look first: an unusable token is refused without hashing anything. It decides nothing --
        # the conditional UPDATE below re-checks and consumes the token atomically.
        usable = session.scalar(
            select(token_model.id).join(pending_model, pending_model.id == token_model.pending_id)
            .where(token_model.token_hash == digest, token_model.purpose == REGISTRATION, *_live(now),
                   pending_model.expires_at > now, pending_model.email == token_model.email))
        if usable is None:
            raise InvalidVerificationTokenError()
        if normalized_username is not None and session.scalar(
                select(models.User.id).where(models.User.username == normalized_username)) is not None:
            raise AccountValidationError("That username is not available.")
        hashed = hash_password(password)
        try:
            consumed = session.execute(
                update(token_model)
                .where(token_model.token_hash == digest, token_model.purpose == REGISTRATION, *_live(now))
                .values(consumed_at=now)
                .returning(token_model.pending_id, token_model.email)).first()
            if consumed is None:
                raise InvalidVerificationTokenError()
            pending_id, email = consumed
            pending = session.get(pending_model, pending_id)
            if pending is None or pending.email != email or pending.expires_at <= now:
                raise InvalidVerificationTokenError()
            user = models.User(
                id=uuid.uuid4(), email=email, username=normalized_username, display_name=name, password_hash=hashed,
                change_seq=0, created_at=now, updated_at=now, version=1, email_verified_at=now)
            session.add(user)
            try:
                session.flush()
            except IntegrityError:
                # The address (or username) got an account meanwhile: this link creates nothing.
                raise InvalidVerificationTokenError() from None
            # Verified: every other link of this registration ends with it.
            session.execute(delete(token_model).where(token_model.pending_id == pending_id))
            session.execute(delete(pending_model).where(pending_model.id == pending_id))
            outbox.enqueue(session, kind=outbox.WELCOME, to_email=email, subject_id=user.id,
                           dedupe_key=f"welcome:{user.id}", now=now)
            session.commit()
        except BaseException:
            session.rollback()
            raise
        return _identity(session.get(models.User, user.id, populate_existing=True))

    # -- existing accounts ----------------------------------------------------------------

    def request_account_verification(self, user_id: uuid.UUID) -> bool:
        """Queue a verification link for an account whose address was never proved. False: nothing to do."""
        now = self._clock()
        user = self._session.get(models.User, user_id)
        if user is None or user.email_verified_at is not None:
            self._session.rollback()
            return False
        outbox.enqueue(self._session, kind=outbox.ACCOUNT_VERIFICATION, to_email=user.email, subject_id=user.id,
                       now=now)
        self._session.commit()
        return True

    def mint_account_token(self, user_id: uuid.UUID) -> str | None:
        """A token proving the mailbox of an existing, unverified account (committed), or None."""
        now = self._clock()
        user = self._session.get(models.User, user_id)
        if user is None or user.email_verified_at is not None:
            return None
        token = mint_token(self._session, ACCOUNT, user.email, now, self._token_ttl, user_id=user.id)
        self._session.commit()
        return token

    def confirm_account_verification(self, user_id: uuid.UUID, token: str) -> AccountIdentity:
        """
        Mark the signed-in account's address as verified. The token must be one mailed to this account's
        current address: a token of another account, or of an address the account no longer has, is invalid.
        """
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise InvalidVerificationTokenError()
        now = self._clock()
        session = self._session
        token_model = models.EmailVerificationToken
        try:
            consumed = session.execute(
                update(token_model)
                .where(token_model.token_hash == token_digest(ACCOUNT, token), token_model.purpose == ACCOUNT,
                       token_model.user_id == user_id, *_live(now))
                .values(consumed_at=now)
                .returning(token_model.email)).first()
            if consumed is None:
                raise InvalidVerificationTokenError()
            verified = session.execute(
                update(models.User)
                .where(models.User.id == user_id, models.User.email == consumed[0])
                .values(email_verified_at=func.coalesce(models.User.email_verified_at, now), updated_at=now,
                        version=models.User.version + 1)
                .execution_options(synchronize_session=False)).rowcount
            if not verified:
                raise InvalidVerificationTokenError()
            session.execute(
                update(token_model)
                .where(token_model.user_id == user_id, token_model.purpose == ACCOUNT, *_live(now))
                .values(revoked_at=now))
            session.commit()
        except BaseException:
            session.rollback()
            raise
        return _identity(session.get(models.User, user_id, populate_existing=True))


def revoke_email_changes(session: Session, user_id: uuid.UUID, now: datetime) -> None:
    """End every pending change of the account's address (not committed): its codes and its unsent emails."""
    token_model, queued = models.EmailVerificationToken, models.EmailOutbox
    session.execute(update(token_model)
                    .where(token_model.user_id == user_id, token_model.purpose == EMAIL_CHANGE, *_live(now))
                    .values(revoked_at=now))
    session.execute(update(queued)
                    .where(queued.kind == outbox.EMAIL_CHANGE, queued.subject_id == user_id, queued.status == "pending")
                    .values(status="dead", last_error="superseded"))


class EmailChangeService:
    """
    Changing an account's address. The caller reauthenticated (the route asks for the current password).
    request(): a code is mailed to the *new* address; nothing changes yet, and an earlier pending change is
    cancelled. confirm(): signed in again, with that code -- so the new mailbox and the account's session
    are both proved. The change raises the credential epoch and ends every session (sign in with the new
    address), marks the new address verified, cancels stale change codes and recovery links (they were
    mailed to the old address), and tells the *old* address what happened. An address that already has an
    account is never revealed: the request answers the same and the code, if any, changes nothing.
    """

    def __init__(self, session: Session, clock: Callable[[], datetime] = _utcnow) -> None:
        self._session = session
        self._clock = clock

    def request(self, user_id: uuid.UUID, new_email: str, *, send: bool = True) -> None:
        now = self._clock()
        session = self._session
        revoke_email_changes(session, user_id, now)
        taken = session.scalar(select(models.User.id).where(models.User.email == new_email)) is not None
        if send and not taken:
            outbox.enqueue(session, kind=outbox.EMAIL_CHANGE, to_email=new_email, subject_id=user_id, now=now)
        session.commit()

    def confirm(self, user_id: uuid.UUID, token: str) -> AccountIdentity:
        from backend import account_security

        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise InvalidVerificationTokenError()
        now = self._clock()
        session = self._session
        token_model = models.EmailVerificationToken
        try:
            consumed = session.execute(
                update(token_model)
                .where(token_model.token_hash == token_digest(EMAIL_CHANGE, token),
                       token_model.purpose == EMAIL_CHANGE, token_model.user_id == user_id, *_live(now))
                .values(consumed_at=now).returning(token_model.email)).first()
            if consumed is None:
                raise InvalidVerificationTokenError()
            new_email = consumed[0]
            before = session.execute(select(models.User.email, models.User.credential_epoch)
                                     .where(models.User.id == user_id)).one()
            try:
                with session.begin_nested():
                    session.execute(
                        update(models.User).where(models.User.id == user_id)
                        .values(email=new_email, email_verified_at=now, updated_at=now,
                                version=models.User.version + 1,
                                credential_epoch=models.User.credential_epoch + 1)
                        .execution_options(synchronize_session=False))
            except IntegrityError:
                raise InvalidVerificationTokenError() from None  # the address got an account meanwhile
            revoke_email_changes(session, user_id, now)
            session.execute(
                update(models.PasswordRecoveryToken)
                .where(models.PasswordRecoveryToken.user_id == user_id,
                       models.PasswordRecoveryToken.consumed_at.is_(None),
                       models.PasswordRecoveryToken.revoked_at.is_(None))
                .values(revoked_at=now))
            account_security.end_sessions(session, user_id, now)
            outbox.enqueue(session, kind=outbox.EMAIL_CHANGED, to_email=before[0], subject_id=user_id, now=now,
                           dedupe_key=f"email_changed:{user_id}:{before[1] + 1}")
            session.commit()
        except BaseException:
            session.rollback()
            raise
        return _identity(session.get(models.User, user_id, populate_existing=True))


__all__ = ["ACCOUNT", "REGISTRATION", "InvalidVerificationTokenError", "VerificationService", "check_email",
           "mint_token", "token_digest"]
