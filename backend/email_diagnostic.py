"""
backend/email_diagnostic.py

An operator's check of transactional email, run on the server (it needs the
server's environment -- no client or API route can trigger it):

    python -m backend.email_diagnostic send              # one test message
    python -m backend.email_diagnostic status <id>       # what is known about it since

`send` mails exactly one address: EMAIL_DIAGNOSTIC_RECIPIENT. There is no
argument for a recipient, so it cannot be pointed at a user. The message
carries a random code and is recorded in email_outbox (kind "diagnostic"),
so the provider's webhook can attach evidence to it.

The report keeps four things apart, because they are different facts:
    configuration     the settings are complete and well-formed
    accepted          the provider took the message (and its id)
    delivery/bounce   only from a signed provider webhook; "none recorded"
                      otherwise -- never inferred from acceptance
    received          never known to this tool: a person has to look in the
                      inbox for the code

Nothing printed contains a credential; the recipient is masked.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import uuid
from datetime import datetime, timezone

from backend.accounts import AccountValidationError, check_email
from backend.database import create_backend_engine, session_factory
from backend.mail import MailError, OutboundEmail, mail_for
from backend.settings import BackendConfigError, BackendSettings, load_settings


def mask(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}"


def send(settings: BackendSettings, *, adapter=None, factory=None, out=print) -> int:
    """Send the test message; the process exit code (0: the provider accepted it)."""
    from backend import models, outbox

    problems = []
    if not settings.mail_configured:
        problems.append("APP_PUBLIC_URL, SMTP_SENDER and a provider (SMTP_HOST, or EMAIL_PROVIDER=resend with "
                        "RESEND_API_KEY) are required")
    try:
        recipient = check_email(settings.email_diagnostic_recipient)
    except AccountValidationError:
        recipient = None
        problems.append("EMAIL_DIAGNOSTIC_RECIPIENT must be the one test address to mail")
    if problems:
        out("configuration: FAILED -- " + "; ".join(problems))
        return 2
    out(f"configuration: passed (provider {settings.email_provider}; link origin {settings.app_public_url}; "
        f"webhook secret {'set' if settings.email_webhook_secret else 'NOT set: no delivery evidence can arrive'})")
    adapter = adapter or mail_for(settings)
    row_id, code, now = uuid.uuid4(), secrets.token_hex(4).upper(), datetime.now(timezone.utc)
    message = OutboundEmail(
        to=recipient, subject=f"Schedule Maxing email test {code}", idempotency_key=str(row_id),
        text=f"This is a test message from the Schedule Maxing server.\n\nTest code: {code}\n\n"
             "It was requested by the server's operator. Nothing needs to be done.\n")
    try:
        provider_id = adapter.deliver(message)
    except MailError as error:
        out(f"accepted by the provider: NO ({error})")
        return 1
    out(f"accepted by the provider: yes, for {mask(recipient)} (provider id: {provider_id or 'none given'})")
    if factory is not None:
        with factory() as session:
            session.add(models.EmailOutbox(
                id=row_id, kind=outbox.DIAGNOSTIC, to_email=recipient, status="sent", attempts=1,
                next_attempt_at=now, accepted_at=now, provider_message_id=provider_id, created_at=now))
            session.commit()
        out(f"delivery or bounce evidence: none recorded yet. Later: python -m backend.email_diagnostic status {row_id}")
    else:
        out("delivery or bounce evidence: none recorded (no database to record it in)")
    out(f"received by the recipient: NOT VERIFIED by this tool. Look in that inbox (and its spam folder) for "
        f"test code {code}.")
    return 0


def status(row_id: str, factory, *, out=print) -> int:
    from backend import models

    try:
        key = uuid.UUID(row_id)
    except ValueError:
        out("That is not a diagnostic id.")
        return 2
    with factory() as session:
        row = session.get(models.EmailOutbox, key)
        if row is None or row.kind != "diagnostic":
            out("No diagnostic message has that id.")
            return 2
        out(f"accepted by the provider: {row.accepted_at or 'no'}")
        if row.bounced_at:
            out(f"bounce evidence (signed webhook): bounced at {row.bounced_at}")
        elif row.delivered_at:
            out(f"delivery evidence (signed webhook): delivered to the recipient's mail server at {row.delivered_at}")
        else:
            out("delivery or bounce evidence: none recorded (no webhook event has arrived for it)")
        if row.complained_at:
            out(f"complaint evidence (signed webhook): marked as spam at {row.complained_at}")
        out("received by the recipient: NOT VERIFIED by this tool; only someone reading the inbox knows.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.email_diagnostic", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("send", help="send one test message to EMAIL_DIAGNOSTIC_RECIPIENT")
    commands.add_parser("status", help="what is recorded about an earlier test message").add_argument("id")
    arguments = parser.parse_args(argv)
    try:
        settings = load_settings()
    except BackendConfigError as error:
        print(f"configuration: FAILED -- {error}")
        return 2
    engine = create_backend_engine(settings.database_url)
    try:
        factory = session_factory(engine)
        return send(settings, factory=factory) if arguments.command == "send" else status(arguments.id, factory)
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
