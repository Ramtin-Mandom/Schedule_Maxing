"""
backend/outbox.py

The durable queue of transactional email (email_outbox). A request never
talks to the mail provider: it adds a row in its own transaction
(enqueue) and answers; process_due() sends afterwards -- right after the
response, and from a background worker for anything left over. So a slow or
failing provider neither delays an answer nor loses an email, and nothing
about delivery shows in a response or its timing.

What a row holds: the kind of message, the recipient and the id of the
thing it is about. Never a link or a token. A message is composed when it is
sent, and a verification or recovery token is minted at that moment
(backend/verification.py stores only its digest) -- so the queue, a database
dump of it, and a retry all hold nothing that could verify or reset
anything. A retry mints another token; earlier ones stay valid within the
bound on outstanding tokens.

Sending, for every worker and replica alike:
    - a row is claimed by one conditional UPDATE (status 'pending' and due,
      or 'sending' with an expired lock -- a worker that died mid-send),
      so two workers never send the same row at the same time;
    - provider accepted it: 'sent', with accepted_at and the provider's
      message id. That is acceptance only; delivered_at / bounced_at are
      written by the provider's signed webhook;
    - provider failed: back to 'pending' after RETRY_DELAYS[attempt], and
      'dead' after MAX_ATTEMPTS. Only the failure's class name is kept;
    - the thing it was about is gone (the registration expired, the account
      was verified meanwhile): 'dead' as "obsolete", nothing is sent.

Duplicates: dedupe_key is unique for ever, so the welcome email of an account
("welcome:<user id>") is queued at most once whatever is retried or
restarted. Sending is at-least-once: a process that dies between the
provider's answer and the commit sends the row again when its lock expires.
The row's id travels as the provider's idempotency key where the provider
supports one (Resend), which closes that gap there.

Nothing here logs an address, a link or a token.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend import models
from backend.mail import MailAdapter, OutboundEmail
from backend.settings import BackendSettings

LOG = logging.getLogger("schedule_maxing.mail")

VERIFICATION = "verification"
ACCOUNT_VERIFICATION = "account_verification"
ACCOUNT_EXISTS = "account_exists"
WELCOME = "welcome"
DIAGNOSTIC = "diagnostic"
RECOVERY = "recovery"
PASSWORD_CHANGED = "password_changed"
MFA_ENABLED = "mfa_enabled"
MFA_DISABLED = "mfa_disabled"
NEW_SIGN_IN = "new_sign_in"
EMAIL_CHANGE = "email_change"
EMAIL_CHANGED = "email_changed"

#: Notifications: what happened and when -- never a credential, a code, a token or a link that signs in.
_NOTICES = {
    PASSWORD_CHANGED: ("Your Schedule Maxing password was changed",
                       "The password of your Schedule Maxing account was changed on {when}. Every device was "
                       "signed out and has to sign in again with the new password."),
    MFA_ENABLED: ("Two-step sign-in was turned on",
                  "Two-step sign-in (an authenticator app) was turned on for your Schedule Maxing account on {when}."),
    MFA_DISABLED: ("Two-step sign-in was turned off",
                   "Two-step sign-in was turned off for your Schedule Maxing account on {when}. Signing in now "
                   "needs only the password."),
    NEW_SIGN_IN: ("New sign-in to your Schedule Maxing account",
                  "Your Schedule Maxing account was signed in to from a device or network it had not used before, "
                  "on {when}."),
}
_NOT_YOU = ("\n\nIf this was you, nothing needs to be done. If it was not, use \"Forgot password?\" to set a new "
            "password -- that signs every device out -- and review the account's sessions.")

MAX_ATTEMPTS = 6
#: Seconds to wait after the first, second, ... failed attempt.
RETRY_DELAYS = (60, 300, 900, 3600, 4 * 3600)
#: How long a claimed row belongs to its worker before another may take it.
LOCK_SECONDS = 120


def enqueue(session: Session, *, kind: str, to_email: str, now: datetime, subject_id: uuid.UUID | None = None,
            dedupe_key: str | None = None) -> bool:
    """
    Queue one email in the caller's transaction (it is sent only if that commits). False when `dedupe_key`
    was queued before -- ever -- and nothing was added.
    """
    row = models.EmailOutbox(id=uuid.uuid4(), kind=kind, to_email=to_email, subject_id=subject_id,
                             dedupe_key=dedupe_key, status="pending", attempts=0, next_attempt_at=now, created_at=now)
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        return False
    return True


# -----------------------------------------------------------------------------
# Composing (at send time)
# -----------------------------------------------------------------------------

_SIGNATURE = "\n\nSchedule Maxing\n"


def verification_link(settings: BackendSettings, token: str) -> str:
    """Built from APP_PUBLIC_URL only; the token rides in the fragment, which browsers never send to a server."""
    return f"{settings.app_public_url}/auth/verify#token={token}"


def recovery_link(settings: BackendSettings, token: str) -> str:
    """RECOVERY_PUBLIC_URL if set, else the server's own reset page under APP_PUBLIC_URL; never a request's Host."""
    page = settings.recovery_public_url or f"{settings.app_public_url}/auth/recovery/reset"
    return f"{page}#token={token}"


def _compose(session: Session, row: models.EmailOutbox, settings: BackendSettings, now: datetime) -> OutboundEmail | None:
    """The message of `row`, or None when what it was about no longer needs one."""
    from backend import verification

    def message(subject: str, text: str) -> OutboundEmail:
        return OutboundEmail(to=row.to_email, subject=subject, text=text + _SIGNATURE, idempotency_key=str(row.id))

    ttl = timedelta(minutes=settings.verification_token_ttl_minutes)
    if row.kind == VERIFICATION:
        pending = session.get(models.PendingRegistration, row.subject_id) if row.subject_id else None
        if pending is None or pending.expires_at <= now or pending.email != row.to_email:
            return None
        token = verification.mint_token(session, verification.REGISTRATION, pending.email, now, ttl,
                                        pending_id=pending.id)
        session.commit()
        return message(
            "Confirm your email for Schedule Maxing",
            "Someone asked to create a Schedule Maxing account with this email address.\n\n"
            "If that was you, confirm the address and choose your password here "
            f"(the link works once, until {now + ttl:%Y-%m-%d %H:%M} UTC):\n\n{verification_link(settings, token)}\n\n"
            "In the desktop app you can paste this link into \"Enter verification code\" instead.\n\n"
            "If it was not you, ignore this message: no account exists until the link is confirmed, and nobody "
            "can sign in with your address.")
    if row.kind == ACCOUNT_VERIFICATION:
        user = session.get(models.User, row.subject_id) if row.subject_id else None
        if user is None or user.email_verified_at is not None or user.email != row.to_email:
            return None
        token = verification.mint_token(session, verification.ACCOUNT, user.email, now, ttl, user_id=user.id)
        session.commit()
        return message(
            "Confirm the email of your Schedule Maxing account",
            "A signed-in user of the Schedule Maxing account with this email address asked to confirm the "
            "address.\n\nPaste this code into the app, on the Account page, while signed in "
            f"(it works once, until {now + ttl:%Y-%m-%d %H:%M} UTC):\n\n{token}\n\n"
            "If you did not create this account, do nothing: the address stays unconfirmed. You can take the "
            "account over with \"Forgot password?\", which replaces its password with one you choose.")
    if row.kind == ACCOUNT_EXISTS:
        return message(
            "You already have a Schedule Maxing account",
            "Someone asked to create a Schedule Maxing account with this email address, and an account "
            "already uses it. Nothing was changed.\n\nIf that was you, sign in instead -- or use "
            "\"Forgot password?\" if you do not remember the password. If it was not you, you can ignore this "
            "message.")
    if row.kind == RECOVERY:
        from backend.recovery import RecoveryService

        delivery = RecoveryService(session, lambda: now,
                                   timedelta(minutes=settings.recovery_token_ttl_minutes)).issue(row.subject_id)
        if delivery is None or delivery.email != row.to_email:
            return None
        return message(
            "Reset your Schedule Maxing password",
            "Someone asked to reset the password of your Schedule Maxing account.\n\n"
            f"To choose a new password, open this link (valid until {delivery.expires_at:%Y-%m-%d %H:%M} UTC, "
            f"usable once):\n\n{recovery_link(settings, delivery.token)}\n\n"
            "If you did not ask for this, ignore this message: your password stays the same.")
    if row.kind == EMAIL_CHANGE:
        user = session.get(models.User, row.subject_id) if row.subject_id else None
        if user is None or user.email == row.to_email:
            return None
        token = verification.mint_token(session, verification.EMAIL_CHANGE, row.to_email, now, ttl, user_id=user.id)
        session.commit()
        return message(
            "Confirm the new email address of your Schedule Maxing account",
            "A signed-in user of a Schedule Maxing account asked to make this its email address.\n\nTo confirm, "
            "paste this code into the app while signed in to that account "
            f"(it works once, until {now + ttl:%Y-%m-%d %H:%M} UTC):\n\n{token}\n\n"
            "If that was not you, ignore this message: nothing changes without the code.")
    if row.kind == EMAIL_CHANGED:
        return message(
            "The email address of your Schedule Maxing account was changed",
            f"The email address of your Schedule Maxing account was changed on {row.created_at:%Y-%m-%d %H:%M} UTC, "
            "by someone signed in to it who also entered its password. This address no longer belongs to the "
            "account, and every device was signed out.\n\nIf this was you, nothing needs to be done. If it "
            "was not, contact the operator of this service now: recovery by email now goes to the new address.")
    if row.kind in _NOTICES:
        subject, text = _NOTICES[row.kind]
        return message(subject, text.format(when=f"{row.created_at:%Y-%m-%d %H:%M} UTC") + _NOT_YOU)
    if row.kind == WELCOME:
        return message(
            "Welcome to Schedule Maxing",
            "Your email address is confirmed and your account is ready.\n\nSign in with your email address "
            "and the password you chose; your schedule then synchronizes between your devices. Everything "
            "also keeps working offline.")
    return None


# -----------------------------------------------------------------------------
# Sending
# -----------------------------------------------------------------------------


def _due(now: datetime):
    outbox = models.EmailOutbox
    return or_(and_(outbox.status == "pending", outbox.next_attempt_at <= now),
               and_(outbox.status == "sending", outbox.locked_until <= now))


def process_due(factory: sessionmaker[Session], adapter: MailAdapter | None, settings: BackendSettings,
                clock: Callable[[], datetime], *, limit: int = 25) -> int:
    """Send up to `limit` due emails; how many the provider accepted. Never raises (the queue keeps them)."""
    if adapter is None:
        return 0
    accepted = 0
    try:
        with factory() as session:
            now = clock()
            due = list(session.scalars(select(models.EmailOutbox.id).where(_due(now))
                                       .order_by(models.EmailOutbox.next_attempt_at).limit(limit)))
            session.rollback()
            for row_id in due:
                accepted += _send_one(session, row_id, adapter, settings, clock)
    except SQLAlchemyError as error:
        LOG.warning("the email outbox could not be processed (%s)", type(error).__name__)
    return accepted


def _send_one(session: Session, row_id: uuid.UUID, adapter: MailAdapter, settings: BackendSettings,
              clock: Callable[[], datetime]) -> int:
    outbox = models.EmailOutbox
    now = clock()
    claimed = session.execute(
        update(outbox).where(outbox.id == row_id, _due(now))
        .values(status="sending", locked_until=now + timedelta(seconds=LOCK_SECONDS), attempts=outbox.attempts + 1)
        .execution_options(synchronize_session=False)).rowcount
    session.commit()
    if not claimed:
        return 0  # another worker has it
    row = session.get(outbox, row_id, populate_existing=True)
    try:
        message = _compose(session, row, settings, now)
        if message is None:
            row.status, row.last_error, row.locked_until = "dead", "obsolete", None
            session.commit()
            return 0
        provider_id = adapter.deliver(message)
    except Exception as error:  # noqa: BLE001 - whatever failed, the row decides: retry later or give up
        session.rollback()
        row = session.get(outbox, row_id, populate_existing=True)
        row.last_error, row.locked_until = type(error).__name__[:80], None
        from backend import security_events

        security_events.record("email_given_up" if row.attempts >= MAX_ATTEMPTS else "email_not_accepted",
                               kind=row.kind, error=row.last_error)
        if row.attempts >= MAX_ATTEMPTS:
            row.status = "dead"
            LOG.error("an email (%s) was given up after %d attempts (%s)", row.kind, row.attempts, row.last_error)
        else:
            row.status = "pending"
            row.next_attempt_at = now + timedelta(seconds=RETRY_DELAYS[min(row.attempts, len(RETRY_DELAYS)) - 1])
            LOG.warning("an email (%s) was not accepted (%s); attempt %d", row.kind, row.last_error, row.attempts)
        session.commit()
        return 0
    row.status, row.locked_until, row.last_error = "sent", None, None
    row.accepted_at, row.provider_message_id = clock(), (provider_id or None)
    session.commit()
    return 1


class OutboxWorker:
    """
    A daemon thread that sends what is due every `interval` seconds, until stop() -- and about once an hour
    removes expired authentication leftovers (backend/maintenance.py) and logs the security counters
    (backend/security_events.py). It runs without a mail adapter too (then it only does the hourly part).
    """

    def __init__(self, factory: sessionmaker[Session], adapter: MailAdapter | None, settings: BackendSettings,
                 clock: Callable[[], datetime], *, interval: float = 15.0, housekeeping_seconds: float = 3600.0) -> None:
        self._arguments = (factory, adapter, settings, clock)
        self._interval = interval
        self._housekeeping_every = max(1, int(housekeeping_seconds // interval))
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="email-outbox", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()

    def _run(self) -> None:
        from backend import maintenance, security_events

        factory, _adapter, _settings, clock = self._arguments
        ticks = 0
        while not self._stopped.wait(self._interval):
            process_due(*self._arguments)
            ticks += 1
            if ticks % self._housekeeping_every == 0:
                maintenance.cleanup(factory, clock)
                security_events.log_counters()
