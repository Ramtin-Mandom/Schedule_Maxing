"""
backend/maintenance.py

Bounded cleanup of authentication leftovers. Run hourly by the server's
background worker, or by hand:  python -m backend.maintenance

What goes, and when (RETENTION; "after" counts from the moment the row
stopped being usable):

    pending registrations      expired                      (their tokens go with them)
    verification tokens        1 day after expiry
    recovery tokens            1 day after expiry
    browser sessions           1 day after expiry
    app sessions               1 day after the session's absolute expiry -- together with
                               every refresh credential of that session, consumed or not
    rate-limit buckets         expired
    webhook event ids          7 days old (a webhook is accepted for 5 minutes)
    email outbox               90 days after a row was sent or given up

Refresh-credential reuse is detected by finding a *consumed* credential's
digest (backend/native_sessions.py). Those digests are therefore kept for
as long as their session could still be refreshed -- until its absolute
expiry -- and are only removed with the whole session afterwards, when
neither the original nor a stolen copy is accepted any more. A revoked
session is kept until then too, so a late replay still answers the same.

What never goes here: accounts, tasks, schedules, execution history,
preferences, the change log, sign-in fingerprints. User data is retained
until its owner deletes it (the task-data reset, or deleting records); it is
not "expired" by authentication cleanup.

Each table is cleaned in batches of at most `batch` rows, at most `rounds`
batches per run, each in its own short transaction: a large backlog is
worked off over several runs instead of one long lock.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend import models

LOG = logging.getLogger("schedule_maxing.maintenance")

GRACE = timedelta(days=1)
WEBHOOK_EVENT_RETENTION = timedelta(days=7)
OUTBOX_RETENTION = timedelta(days=90)


def _targets(now: datetime):
    """(name, model, primary-key column, condition) for everything that may be removed at `now`."""
    token, recovery = models.EmailVerificationToken, models.PasswordRecoveryToken
    browser, native, outbox = models.BrowserSession, models.NativeSession, models.EmailOutbox
    return (
        ("pending_registrations", models.PendingRegistration, models.PendingRegistration.id,
         models.PendingRegistration.expires_at <= now),
        ("email_verification_tokens", token, token.id, token.expires_at <= now - GRACE),
        ("password_recovery_tokens", recovery, recovery.id, recovery.expires_at <= now - GRACE),
        ("browser_sessions", browser, browser.id, browser.expires_at <= now - GRACE),
        # With its refresh credentials (ON DELETE CASCADE): reuse evidence is kept until the session itself is over.
        ("native_sessions", native, native.id, native.expires_at <= now - GRACE),
        ("rate_limit_buckets", models.RateLimitBucket, models.RateLimitBucket.key,
         models.RateLimitBucket.expires_at <= now),
        ("email_webhook_events", models.EmailWebhookEvent, models.EmailWebhookEvent.id,
         models.EmailWebhookEvent.received_at <= now - WEBHOOK_EVENT_RETENTION),
        ("email_outbox", outbox, outbox.id,
         outbox.status.in_(("sent", "dead")) & or_(outbox.accepted_at <= now - OUTBOX_RETENTION,
                                                   (outbox.accepted_at.is_(None)
                                                    & (outbox.created_at <= now - OUTBOX_RETENTION)))),
    )


def cleanup(factory: sessionmaker[Session], clock: Callable[[], datetime], *, batch: int = 500,
            rounds: int = 20) -> dict[str, int]:
    """Remove what RETENTION allows; how many rows went, per table. Never raises (the next run retries)."""
    removed: dict[str, int] = {}
    now = clock()
    for name, model, key, condition in _targets(now):
        count = 0
        try:
            for _ in range(rounds):
                with factory() as session:
                    keys = list(session.scalars(select(key).where(condition).limit(batch)))
                    if not keys:
                        break
                    if model is models.NativeSession:  # explicit: not every database enforces the cascade
                        session.execute(delete(models.RefreshCredential)
                                        .where(models.RefreshCredential.session_id.in_(keys)))
                    if model is models.PendingRegistration:
                        session.execute(delete(models.EmailVerificationToken)
                                        .where(models.EmailVerificationToken.pending_id.in_(keys)))
                    session.execute(delete(model).where(key.in_(keys)))
                    session.commit()
                    count += len(keys)
                    if len(keys) < batch:
                        break
        except SQLAlchemyError as error:
            LOG.warning("cleanup of %s failed (%s)", name, type(error).__name__)
        if count:
            removed[name] = count
    if removed:
        LOG.info("cleanup removed %s", ", ".join(f"{count} {name}" for name, count in removed.items()))
    return removed


def main() -> int:
    from backend.database import create_backend_engine, session_factory
    from backend.settings import BackendConfigError, load_settings

    try:
        settings = load_settings()
    except BackendConfigError as error:
        print(error)
        return 2
    engine = create_backend_engine(settings.database_url)
    try:
        removed = cleanup(session_factory(engine), lambda: datetime.now(timezone.utc))
    finally:
        engine.dispose()
    print("removed: " + (", ".join(f"{count} {name}" for name, count in removed.items()) or "nothing"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
