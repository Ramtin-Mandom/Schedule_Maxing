"""
backend/account_security.py

What an account's owner does to its credentials and sessions, and what the
owner is told about it. Framework-free (routes: backend/security_api.py).

Password change (change_password): the current password is verified, the
new one must pass the policy (backend/password_policy.py) and differ, and
the replacement raises the account's credential epoch in the same UPDATE,
conditional on the epoch read -- a change racing a reset changes the
password once. Every session then ends, the changing device's included (it
signs in again with the new password), exactly as after a recovery reset.

How sessions end (end_sessions) -- and how fast:
    - an app session is a refresh-credential family (native_sessions); a
      browser session is a cookie row (browser_sessions). Revoking marks the
      row, and every request checks its row (backend/api.py
      current_user_id), so an access token or cookie of a revoked session is
      refused on its next request -- not when it would have expired;
    - "everywhere" also raises the credential epoch, which additionally
      ends anything not tied to a session row.
    So "sign out everywhere" and "revoke this device" take effect at once on
    the server. A device that is offline keeps showing its local data until
    it next talks to the server; nothing is fetched or accepted meanwhile.

Sign-in recognition (note_sign_in): a completed sign-in is remembered as a
digest of (account, network, client). The first one for an account is not
news; a later one with an unseen digest queues one "new sign-in" email --
once per digest, for ever (the outbox's dedupe key) -- and none is queued for
a failed attempt, a refresh, or a place seen before.

Notifications (password changed, two-step sign-in on/off, new sign-in) go
through the outbox, are deduplicated by key, and say what happened and
when -- never a credential, a token, a code or a link that signs in.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models, outbox
from backend.accounts import AccountValidationError, InvalidCredentialsError, check_password
from backend.passwords import hash_password, verify_password


def client_label(user_agent: str | None) -> str | None:
    """A short printable description of the client, from its User-Agent (shown to the owner in the session list)."""
    text = "".join(character for character in (user_agent or "") if character.isprintable()).strip()
    return text[:120] or None


def end_sessions(session: Session, user_id: uuid.UUID, now: datetime, *, keep_app: uuid.UUID | None = None,
                 keep_browser: uuid.UUID | None = None) -> int:
    """Revoke the account's live app and browser sessions (not committed); how many. `keep_*`: one to spare."""
    ended = 0
    for model, keep in ((models.NativeSession, keep_app), (models.BrowserSession, keep_browser)):
        query = update(model).where(model.user_id == user_id, model.revoked_at.is_(None))
        if keep is not None:
            query = query.where(model.id != keep)
        ended += session.execute(query.values(revoked_at=now).execution_options(synchronize_session=False)).rowcount
    return ended


def list_sessions(session: Session, user_id: uuid.UUID, now: datetime, *, current: tuple[str, uuid.UUID] | None) -> list[dict]:
    """The account's live sessions, newest first: plain values, never a credential or its digest."""
    native, browser = models.NativeSession, models.BrowserSession
    found = []
    for kind, model in (("app", native), ("browser", browser)):
        for row in session.scalars(select(model).where(model.user_id == user_id, model.revoked_at.is_(None),
                                                       model.expires_at > now)):
            found.append({"id": row.id, "kind": kind, "created_at": row.created_at, "expires_at": row.expires_at,
                          "client": row.client_label, "current": current == (kind, row.id)})
    return sorted(found, key=lambda item: item["created_at"], reverse=True)


def revoke_session(session: Session, user_id: uuid.UUID, session_id: uuid.UUID, now: datetime) -> bool:
    """End one session *of this account* (committed). False: the account has no such live session."""
    ended = 0
    for model in (models.NativeSession, models.BrowserSession):
        ended += session.execute(
            update(model).where(model.id == session_id, model.user_id == user_id, model.revoked_at.is_(None))
            .values(revoked_at=now).execution_options(synchronize_session=False)).rowcount
    session.commit()
    return bool(ended)


def sign_out_everywhere(session: Session, user_id: uuid.UUID, now: datetime) -> int:
    """End every session and raise the credential epoch (committed); how many sessions were live."""
    ended = end_sessions(session, user_id, now)
    session.execute(update(models.User).where(models.User.id == user_id)
                    .values(credential_epoch=models.User.credential_epoch + 1)
                    .execution_options(synchronize_session=False))
    session.commit()
    return ended


def change_password(session: Session, user_id: uuid.UUID, current_password: str, new_password: str,
                    now: datetime) -> None:
    """
    Replace the password (see the module docstring). InvalidCredentialsError when `current_password` is
    wrong; AccountValidationError when the new one breaks the policy or equals the current one.
    """
    user = session.execute(select(models.User.password_hash, models.User.credential_epoch, models.User.email,
                                  models.User.username).where(models.User.id == user_id)).first()
    session.rollback()
    if not verify_password(current_password, user.password_hash if user is not None else None):
        raise InvalidCredentialsError()
    check_password(new_password, email=user.email, username=user.username)
    if new_password == current_password:
        raise AccountValidationError("The new password must be different from the current one.")
    hashed = hash_password(new_password)
    try:
        changed = session.execute(
            update(models.User)
            .where(models.User.id == user_id, models.User.credential_epoch == user.credential_epoch)
            .values(password_hash=hashed, credential_epoch=models.User.credential_epoch + 1, updated_at=now)
            .execution_options(synchronize_session=False)).rowcount
        if not changed:
            raise InvalidCredentialsError()  # the credentials changed meanwhile (a reset): this change is void
        end_sessions(session, user_id, now)
        session.execute(
            update(models.PasswordRecoveryToken)
            .where(models.PasswordRecoveryToken.user_id == user_id, models.PasswordRecoveryToken.consumed_at.is_(None),
                   models.PasswordRecoveryToken.revoked_at.is_(None))
            .values(revoked_at=now))
        from backend.verification import revoke_email_changes

        revoke_email_changes(session, user_id, now)
        notify(session, outbox.PASSWORD_CHANGED, user_id, user.email, now,
               dedupe_key=f"password_changed:{user_id}:{user.credential_epoch + 1}")
        session.commit()
    except BaseException:
        session.rollback()
        raise


def notify(session: Session, kind: str, user_id: uuid.UUID, email: str, now: datetime, *, dedupe_key: str) -> bool:
    """Queue one security notification in the caller's transaction; False when that key was already queued."""
    return outbox.enqueue(session, kind=kind, to_email=email, subject_id=user_id, dedupe_key=dedupe_key, now=now)


def note_sign_in(session: Session, user_id: uuid.UUID, email: str, network: str, user_agent: str | None,
                 now: datetime) -> bool:
    """
    Remember where a completed sign-in came from (committed); True when it is a newly recognized place for
    an account that had signed in before -- one notification was queued for it.
    """
    fingerprint = hashlib.sha256(f"{user_id}|{network}|{client_label(user_agent) or ''}".encode("utf-8")).hexdigest()
    known = models.SignInFingerprint
    if session.get(known, (user_id, fingerprint)) is not None:
        session.rollback()
        return False
    first_ever = session.scalar(select(known.fingerprint).where(known.user_id == user_id).limit(1)) is None
    try:
        with session.begin_nested():
            session.add(known(user_id=user_id, fingerprint=fingerprint, first_seen_at=now))
    except IntegrityError:
        session.rollback()
        return False  # a concurrent sign-in from the same place recorded it
    queued = False
    if not first_ever:
        queued = notify(session, outbox.NEW_SIGN_IN, user_id, email, now, dedupe_key=f"sign_in:{user_id}:{fingerprint}")
    session.commit()
    return queued
