"""
backend/accounts.py

Account registration and credential verification: the one implementation
behind the HTTP API (backend/api.py: /auth/register, /auth/login, the
browser sign-in) and the direct desktop path (app/persistence). It needs no
HTTP framework and no JWT secret; issuing a token or a browser cookie stays
in the HTTP adapters.

Rules (backend/passwords.py):
    - emails and usernames are normalized (NFKC, trimmed, case-folded) and
      unique -- the database's unique constraints decide, so two concurrent
      registrations of one identifier cannot both succeed;
    - passwords are MIN..MAX_PASSWORD_LENGTH characters and stored only as
      an Argon2id hash, upgraded on a successful sign-in when the hashing
      parameters changed;
    - an unknown account and a wrong password fail identically
      (InvalidCredentialsError), and an unknown account still costs one
      hash verification.

Callers get an AccountIdentity: plain values, no password, no hash, no ORM
object. Nothing here logs or keeps a password after the call returns
(Python cannot promise to erase the caller's string from memory).
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models
from backend.passwords import (
    hash_password,
    needs_rehash,
    normalize_identifier,
    verify_password,
)

USERNAME_PATTERN = re.compile(r"^[\w.\-]+$")
MAX_EMAIL_LENGTH = 320
MAX_DISPLAY_NAME_LENGTH = 200


class AccountError(Exception):
    """Base class of account failures. Messages never contain a password, a hash or configuration."""


class AccountValidationError(AccountError, ValueError):
    """The registration input breaks a rule (the message says which, never echoing a password)."""


class AccountExistsError(AccountError):
    def __init__(self) -> None:
        super().__init__("An account with this email or username already exists.")


class InvalidCredentialsError(AccountError):
    def __init__(self) -> None:
        super().__init__("The email/username or password is incorrect.")


@dataclass(frozen=True)
class AccountIdentity:
    """A signed-in or registered account, safe to hand to any caller or UI."""

    id: uuid.UUID
    email: str
    username: str | None
    display_name: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    #: The credential epoch read together with the password check: a token or session issued for this
    #: identity carries it, so a password reset after the check makes that token useless (backend/recovery.py).
    credential_epoch: int = 0
    #: Whether the owner of the mailbox proved it is theirs (backend/verification.py).
    email_verified: bool = False


def _identity(user: models.User) -> AccountIdentity:
    return AccountIdentity(id=user.id, email=user.email, username=user.username, display_name=user.display_name,
                           version=user.version, created_at=user.created_at, updated_at=user.updated_at,
                           credential_epoch=user.credential_epoch or 0,
                           email_verified=user.email_verified_at is not None)


def check_email(email: str, *, check_dns: bool = False) -> str:
    """
    The one mailbox `email` names, normalized -- or AccountValidationError. Applied wherever an address is
    *stored* (registration, an email change); sign-in and recovery only look an address up, with the same
    normalize_identifier(), so every stored address is found exactly as it was entered.

    Syntax is the email-validator library's: exactly one address, so no recipient list, no display name
    ("Name <a@b.c>"), no quoted local part, no bracketed IP, no control characters or line breaks (nothing that
    could become another header or recipient). Normalization is NFKC, trimmed and case-folded, with the
    domain in its Unicode form; plus tags and dots are part of the address and are kept (what they mean is
    the provider's business).

    check_dns: also refuse a domain that DNS answers cannot receive mail. A DNS timeout or failure refuses
    nothing (it is not an answer), and a pass says nothing about the mailbox existing or whose it is --
    only the emailed link proves that.
    """
    from email_validator import EmailNotValidError, EmailUndeliverableError, caching_resolver, validate_email

    if not isinstance(email, str) or not 3 <= len(email) <= MAX_EMAIL_LENGTH:
        raise AccountValidationError("email must be an email address")
    try:
        result = validate_email(
            email.strip(), check_deliverability=check_dns, allow_display_name=False, allow_quoted_local=False,
            allow_domain_literal=False, dns_resolver=caching_resolver(timeout=3) if check_dns else None)
    except EmailUndeliverableError:
        raise AccountValidationError("that email address's domain cannot receive mail") from None
    except EmailNotValidError:
        raise AccountValidationError("email must be one email address") from None
    return normalize_identifier(result.normalized)


def check_password(password: str, *, email: str | None = None, username: str | None = None) -> None:
    """The rule for a new password, wherever one is set (backend/password_policy.py); AccountValidationError."""
    from backend.password_policy import WeakPasswordError, check_new_password

    try:
        check_new_password(password, email=email, username=username)
    except WeakPasswordError as error:
        raise AccountValidationError(str(error)) from None


def check_username(username: str | None) -> str | None:
    if username is None:
        return None
    if not 3 <= len(username) <= 64 or not USERNAME_PATTERN.match(username):
        raise AccountValidationError("username must be 3-64 letters, digits, '.', '_' or '-'")
    return normalize_identifier(username)


def clean_display_name(display_name: str | None) -> str | None:
    if display_name is not None and len(display_name) > MAX_DISPLAY_NAME_LENGTH:
        raise AccountValidationError(f"display_name may be at most {MAX_DISPLAY_NAME_LENGTH} characters")
    return unicodedata.normalize("NFKC", display_name).strip() if display_name else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AccountService:
    """Accounts in one SQLAlchemy session (the caller owns the session: a request's, or one per direct call)."""

    def __init__(self, session: Session, clock: Callable[[], datetime] = _utcnow) -> None:
        self._session = session
        self._clock = clock

    def register(self, *, email: str, password: str, username: str | None = None,
                 display_name: str | None = None) -> AccountIdentity:
        """Create an account (committed). AccountExistsError when the email or username is taken."""
        normalized_email = check_email(email)
        normalized_username = check_username(username)
        check_password(password, email=normalized_email, username=normalized_username)
        name = clean_display_name(display_name)
        # Before the expensive hash: a taken identifier is refused without one (the constraints below still
        # decide a race). The answer is the same AccountExistsError either way.
        taken = models.User.email == normalized_email
        if normalized_username is not None:
            taken = taken | (models.User.username == normalized_username)
        if self._session.scalar(select(models.User.id).where(taken).limit(1)) is not None:
            self._session.rollback()
            raise AccountExistsError()
        now = self._clock()
        user = models.User(
            id=uuid.uuid4(), email=normalized_email, username=normalized_username, display_name=name,
            password_hash=hash_password(password), change_seq=0, created_at=now, updated_at=now, version=1,
        )
        self._session.add(user)
        try:
            self._session.commit()
        except IntegrityError:
            # The unique constraints decide, so two concurrent registrations cannot both succeed.
            self._session.rollback()
            raise AccountExistsError() from None
        return _identity(user)

    def account_subject(self, *, email: str | None = None, username: str | None = None) -> str:
        """
        The key under which sign-in failures of this email or username are counted (backend/login_guard.py):
        the account's id when one matches -- one counter for both of its names -- else the normalized
        identifier, so an unknown account is counted like an existing one. Never shown to a client.
        """
        if (email is None) == (username is None):
            raise AccountValidationError("give exactly one of email or username")
        normalized = normalize_identifier(email if email is not None else username)
        column = models.User.email if email is not None else models.User.username
        user_id = self._session.scalar(select(models.User.id).where(column == normalized))
        return f"account:{user_id}" if user_id is not None else f"id:{normalized}"

    def authenticate(self, *, password: str, email: str | None = None, username: str | None = None) -> AccountIdentity:
        """
        The account whose email (or username) and password match, else
        InvalidCredentialsError -- the same for an unknown account and a
        wrong password. Upgrades an outdated password hash (committed).
        """
        if (email is None) == (username is None):
            raise AccountValidationError("give exactly one of email or username")
        column = models.User.email if email is not None else models.User.username
        user = self._session.scalars(select(models.User).where(
            column == normalize_identifier(email if email is not None else username))).first()
        if not verify_password(password, user.password_hash if user is not None else None):
            raise InvalidCredentialsError()
        if needs_rehash(user.password_hash):
            self._session.execute(update(models.User).where(models.User.id == user.id)
                                  .values(password_hash=hash_password(password)))
            self._session.commit()
        return _identity(user)

    def get(self, user_id: uuid.UUID) -> AccountIdentity | None:
        user = self._session.get(models.User, user_id)
        return _identity(user) if user is not None else None
