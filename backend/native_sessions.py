"""Rotating native-client credentials, independent of HTTP routing.

Each login creates a family with an absolute expiration and credential epoch.
Only SHA-256 digests of random 256-bit refresh tokens are stored. Consumed
digests remain until the family expires so reuse revokes the entire family.
An UPDATE locks the family on both SQLite and PostgreSQL before rotation or
logout; simultaneous refreshes cannot create two live descendants. Clients
must serialize refresh requests: replay (including a duplicate retry) ends
the session and requires a new password login.

Access tokens carry the family UUID (sid); API authentication checks the
family on every request, so logout/replay revoke its access tokens as well.
Password reset invalidates both token kinds through the existing epoch.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend import models
from backend.errors import unauthenticated
from backend.security import issue_access_token
from backend.settings import BackendSettings


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def require_live_session(session: Session, family_id: uuid.UUID, user_id: uuid.UUID,
                         epoch: int, now: datetime) -> None:
    found = session.scalar(select(models.NativeSession.id).where(
        models.NativeSession.id == family_id, models.NativeSession.user_id == user_id,
        models.NativeSession.credential_epoch == epoch, models.NativeSession.revoked_at.is_(None),
        models.NativeSession.expires_at > now,
    ))
    if found is None:
        raise unauthenticated("The session has ended. Sign in again.")


class NativeSessionService:
    def __init__(self, session: Session, settings: BackendSettings, now: datetime):
        self.session, self.settings, self.now = session, settings, now

    def start(self, user_id: uuid.UUID, epoch: int) -> dict:
        family = models.NativeSession(
            id=uuid.uuid4(), user_id=user_id, credential_epoch=epoch,
            created_at=self.now, expires_at=self.now + timedelta(days=self.settings.refresh_token_expire_days),
        )
        self.session.add(family)
        self.session.flush()
        result = self._issue(family)
        self.session.commit()
        return result

    def _issue(self, family: models.NativeSession) -> dict:
        secret = secrets.token_urlsafe(32)
        self.session.add(models.RefreshCredential(
            token_hash=_digest(secret), session_id=family.id, created_at=self.now,
        ))
        access = issue_access_token(family.user_id, self.settings, self.now,
                                    family.credential_epoch, session_id=family.id)
        return {"access_token": access.token, "expires_in": access.expires_in,
                "expires_at": access.expires_at, "refresh_token": secret,
                "refresh_expires_at": family.expires_at}

    def _lock(self, token: str):
        digest = _digest(token)
        family_id = self.session.scalar(select(models.RefreshCredential.session_id).where(
            models.RefreshCredential.token_hash == digest))
        if family_id is None:
            return None, None
        # Lock before re-reading the credential. A waiting concurrent request
        # observes the winner's committed consumed_at, including on PostgreSQL.
        self.session.execute(update(models.NativeSession).where(models.NativeSession.id == family_id)
                             .values(expires_at=models.NativeSession.expires_at)
                             .execution_options(synchronize_session=False))
        family = self.session.get(models.NativeSession, family_id, populate_existing=True)
        credential = self.session.get(models.RefreshCredential, digest, populate_existing=True)
        return family, credential

    def refresh(self, token: str) -> dict:
        family, credential = self._lock(token)
        if family is None or family.revoked_at is not None or family.expires_at <= self.now:
            raise unauthenticated("The refresh credential is invalid or has expired.")
        epoch = self.session.scalar(select(models.User.credential_epoch).where(models.User.id == family.user_id))
        if epoch != family.credential_epoch or credential.consumed_at is not None:
            family.revoked_at = self.now
            self.session.commit()  # revocation must survive the 401 response
            raise unauthenticated("The session has ended. Sign in again.")
        credential.consumed_at = self.now
        result = self._issue(family)
        self.session.commit()
        return result

    def logout(self, token: str) -> None:
        # Possession of a refresh secret authorizes ending only its own family.
        # Unknown and already-revoked credentials have the same idempotent result.
        family, _ = self._lock(token)
        if family is not None and family.revoked_at is None:
            family.revoked_at = self.now
        self.session.commit()
