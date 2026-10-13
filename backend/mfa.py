"""
backend/mfa.py

Optional two-step sign-in with a TOTP authenticator app (RFC 6238, through
the pyotp library) and single-use recovery codes. Framework-free; the routes
are in backend/security_api.py and the sign-in gate in backend/api.py.

    - The shared secret is generated here (160 random bits) and stored only
      encrypted with Fernet (the cryptography library) under
      MFA_ENCRYPTION_KEY -- a key used for nothing else. Without that key
      the feature is unavailable; an account that already has a second
      factor then cannot complete sign-in (it fails closed, never open).
    - Enrollment has two steps: begin (the secret, shown once) and confirm
      (a code from the authenticator). Only a confirmed factor counts, so a
      half-finished enrollment never locks anyone out or protects nothing
      while appearing to.
    - A code is accepted for the current 30-second step or one either side
      (clock drift), and never twice: the accepted step is recorded with a
      conditional UPDATE, and a code of that step or an earlier one is
      refused afterwards -- also when two requests present it at once.
    - Recovery codes: RECOVERY_CODE_COUNT codes of 80 random bits, shown once
      at confirmation; only their SHA-256 digests are stored (they are
      random, not chosen, so a fast digest is enough). Each works once
      (conditional UPDATE). They are the recovery path for a lost
      authenticator; a password reset does not remove the second factor.

Nothing here logs or returns a secret or a code, other than handing the new
secret and the fresh recovery codes to the caller once.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime
from urllib.parse import quote

import pyotp
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from backend import models

RECOVERY_CODE_COUNT = 10
STEP_SECONDS = 30
#: Steps accepted either side of the current one.
DRIFT_STEPS = 1


class MfaError(Exception):
    """Base class; messages never contain a secret or a code."""


class MfaUnavailableError(MfaError):
    def __init__(self) -> None:
        super().__init__("Two-step sign-in is not available on this server.")


class MfaStateError(MfaError):
    """The request does not fit the account's state (already enabled, nothing to confirm, not enabled)."""


class InvalidMfaCodeError(MfaError):
    def __init__(self) -> None:
        super().__init__("That code is not valid. Enter the current code from your authenticator app.")


def cipher_for(key: str, previous: str = "") -> Fernet | MultiFernet | None:
    """
    The cipher of MFA_ENCRYPTION_KEY, or None when no key is configured. With a `previous` key (rotation) it
    encrypts with the new key and decrypts with either; a secret still under the old key is re-encrypted the
    next time its owner passes a code, so the old key can be dropped once every factor was used or re-enrolled.
    """
    if not key:
        return None
    if previous:
        return MultiFernet([Fernet(key.encode("ascii")), Fernet(previous.encode("ascii"))])
    return Fernet(key.encode("ascii"))


def _code_digest(code: str) -> str:
    normalized = "".join(character for character in code.casefold() if character.isalnum())
    return hashlib.sha256(("mfa-recovery:" + normalized).encode("utf-8")).hexdigest()


def _new_recovery_code() -> str:
    raw = secrets.token_hex(10)  # 80 bits
    return "-".join(raw[index:index + 5] for index in range(0, len(raw), 5))


class MfaService:
    """Second-factor state in one SQLAlchemy session (the caller owns it), committing its own changes."""

    def __init__(self, session: Session, clock: Callable[[], datetime], cipher: Fernet | MultiFernet | None,
                 issuer: str = "Schedule Maxing") -> None:
        self._session = session
        self._clock = clock
        self._cipher = cipher
        self._issuer = issuer

    def _require_cipher(self) -> Fernet | MultiFernet:
        if self._cipher is None:
            raise MfaUnavailableError()
        return self._cipher

    def enabled(self, user_id: uuid.UUID) -> bool:
        """Whether sign-in to this account needs a second step (a confirmed factor exists)."""
        return self._session.scalar(select(models.MfaTotp.user_id).where(
            models.MfaTotp.user_id == user_id, models.MfaTotp.confirmed_at.is_not(None))) is not None

    def recovery_codes_left(self, user_id: uuid.UUID) -> int:
        return self._session.scalar(select(func.count()).select_from(models.MfaRecoveryCode).where(
            models.MfaRecoveryCode.user_id == user_id, models.MfaRecoveryCode.used_at.is_(None))) or 0

    def begin_enrollment(self, user_id: uuid.UUID, account_name: str) -> tuple[str, str]:
        """A new secret for the account (replacing an unconfirmed one): (base32 secret, otpauth:// URI)."""
        cipher = self._require_cipher()
        if self.enabled(user_id):
            raise MfaStateError("Two-step sign-in is already on. Turn it off first to set up a new authenticator.")
        secret = pyotp.random_base32(32)
        self._session.execute(delete(models.MfaTotp).where(models.MfaTotp.user_id == user_id))
        self._session.add(models.MfaTotp(user_id=user_id, created_at=self._clock(),
                                         secret_encrypted=cipher.encrypt(secret.encode("ascii")).decode("ascii")))
        self._session.commit()
        uri = (f"otpauth://totp/{quote(self._issuer)}:{quote(account_name)}?secret={secret}"
               f"&issuer={quote(self._issuer)}&algorithm=SHA1&digits=6&period={STEP_SECONDS}")
        return secret, uri

    def confirm_enrollment(self, user_id: uuid.UUID, code: str) -> list[str]:
        """Turn the factor on with a code from the authenticator; the recovery codes, shown this once."""
        row = self._session.get(models.MfaTotp, user_id, populate_existing=True)
        if row is None or row.confirmed_at is not None:
            self._session.rollback()
            raise MfaStateError("There is no authenticator setup to confirm. Start the setup again.")
        if not self._accept_totp(user_id, row, code):
            self._session.rollback()
            raise InvalidMfaCodeError()
        now = self._clock()
        self._session.execute(update(models.MfaTotp).where(models.MfaTotp.user_id == user_id)
                              .values(confirmed_at=now))
        codes = self._replace_recovery_codes(user_id, now)
        self._session.commit()
        return codes

    def verify(self, user_id: uuid.UUID, code: str) -> bool:
        """
        Whether `code` completes a second step now: the authenticator's current code (never accepted twice) or
        an unused recovery code (used up by this). Committed. MfaUnavailableError without the encryption key.
        """
        row = self._session.get(models.MfaTotp, user_id, populate_existing=True)
        if row is None or row.confirmed_at is None:
            self._session.rollback()
            return False
        accepted = self._accept_totp(user_id, row, code) or self._accept_recovery_code(user_id, code)
        self._session.commit()
        return accepted

    def regenerate_recovery_codes(self, user_id: uuid.UUID) -> list[str]:
        if not self.enabled(user_id):
            self._session.rollback()
            raise MfaStateError("Two-step sign-in is not on for this account.")
        codes = self._replace_recovery_codes(user_id, self._clock())
        self._session.commit()
        return codes

    def disable(self, user_id: uuid.UUID) -> bool:
        """Remove the factor and its recovery codes. False: there was none."""
        removed = self._session.execute(delete(models.MfaTotp).where(models.MfaTotp.user_id == user_id)).rowcount
        self._session.execute(delete(models.MfaRecoveryCode).where(models.MfaRecoveryCode.user_id == user_id))
        self._session.commit()
        return bool(removed)

    # -- internals ------------------------------------------------------------------------

    def _accept_totp(self, user_id: uuid.UUID, row: models.MfaTotp, code: str) -> bool:
        digits = "".join(character for character in (code or "") if not character.isspace())
        if len(digits) != 6 or not digits.isdigit():
            return False
        try:
            secret = self._require_cipher().decrypt(row.secret_encrypted.encode("ascii")).decode("ascii")
        except InvalidToken:
            raise MfaUnavailableError() from None  # the key changed: fail closed, never open
        totp = pyotp.TOTP(secret, interval=STEP_SECONDS)
        current = int(self._clock().timestamp()) // STEP_SECONDS
        for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
            if hmac.compare_digest(totp.at(step * STEP_SECONDS), digits):
                # Recorded atomically: of two requests with one code, one updates the row.
                return bool(self._session.execute(
                    update(models.MfaTotp)
                    .where(models.MfaTotp.user_id == user_id,
                           or_(models.MfaTotp.last_used_step.is_(None), models.MfaTotp.last_used_step < step))
                    .values(last_used_step=step, secret_encrypted=self._reencrypted(row.secret_encrypted))
                    .execution_options(synchronize_session=False)).rowcount)
        return False

    def _reencrypted(self, stored: str) -> str:
        """`stored` under the current key (unchanged unless a key rotation is in progress)."""
        if isinstance(self._cipher, MultiFernet):
            return self._cipher.rotate(stored.encode("ascii")).decode("ascii")
        return stored

    def _accept_recovery_code(self, user_id: uuid.UUID, code: str) -> bool:
        if not code or len(code) > 64:
            return False
        return bool(self._session.execute(
            update(models.MfaRecoveryCode)
            .where(models.MfaRecoveryCode.user_id == user_id, models.MfaRecoveryCode.code_hash == _code_digest(code),
                   models.MfaRecoveryCode.used_at.is_(None))
            .values(used_at=self._clock()).execution_options(synchronize_session=False)).rowcount)

    def _replace_recovery_codes(self, user_id: uuid.UUID, now: datetime) -> list[str]:
        self._session.execute(delete(models.MfaRecoveryCode).where(models.MfaRecoveryCode.user_id == user_id))
        codes = [_new_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
        for code in codes:
            self._session.add(models.MfaRecoveryCode(id=uuid.uuid4(), user_id=user_id, code_hash=_code_digest(code),
                                                     created_at=now))
        return codes
