"""
backend/mail.py

Sending one transactional email through the configured provider. Only the
outbox (backend/outbox.py) and the operator's diagnostic
(backend/email_diagnostic.py) call this; provider credentials are read from
the server's settings and exist nowhere else -- never in a client, a
response, a log line or an error.

    SmtpMail      EMAIL_PROVIDER=smtp (default): the standard library's
                  smtplib with STARTTLS (default) or implicit TLS, optional
                  login, and a timeout on every network operation.
    ResendMail    EMAIL_PROVIDER=resend: Resend's HTTPS API, with the outbox
                  row's id as Idempotency-Key so a retry after a lost answer
                  is not a second email. Its webhooks report delivery and
                  bounces (backend/email_webhooks.py).
    mail_for(settings)   the adapter the settings configure, or None.

deliver() returns the provider's id for the message when it *accepted* it.
Acceptance is not delivery: only a signed webhook event records that a
message reached, or bounced from, the recipient's server -- and nothing
records that a person read it.

A message has exactly one recipient, taken from the database (an address
that passed accounts.check_email) -- and email.message refuses a header value
containing a line break, so a hostile address cannot add headers.

Failures raise MailError carrying only the failure's class name.
"""

from __future__ import annotations

import json
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Protocol

from backend.settings import BackendSettings

RESEND_ENDPOINT = "https://api.resend.com/emails"


@dataclass(frozen=True)
class OutboundEmail:
    to: str
    subject: str
    text: str
    #: Stable per outbox row: lets a provider drop a repeated request.
    idempotency_key: str


class MailError(Exception):
    """The provider did not accept the message (no details: they may name hosts, addresses or credentials)."""


class MailAdapter(Protocol):
    name: str

    def deliver(self, message: OutboundEmail) -> str | None: ...


class SmtpMail:
    name = "smtp"

    def __init__(self, settings: BackendSettings) -> None:
        self._settings = settings

    def deliver(self, message: OutboundEmail) -> str | None:
        settings = self._settings
        email = EmailMessage()
        email["Subject"] = message.subject
        email["From"] = settings.smtp_sender
        email["To"] = message.to
        email["Message-ID"] = make_msgid(idstring=message.idempotency_key[:32])
        email.set_content(message.text)
        context = ssl.create_default_context()
        timeout = settings.delivery_timeout_seconds
        try:
            if settings.smtp_ssl:
                client = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout, context=context)
            else:
                client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
            with client:
                if settings.smtp_starttls and not settings.smtp_ssl:
                    client.starttls(context=context)
                if settings.smtp_username:
                    client.login(settings.smtp_username, settings.smtp_password)
                client.send_message(email)
        except (OSError, smtplib.SMTPException, ValueError) as error:
            raise MailError(type(error).__name__) from None
        return str(email["Message-ID"])


class ResendMail:
    name = "resend"

    def __init__(self, settings: BackendSettings) -> None:
        self._settings = settings

    def deliver(self, message: OutboundEmail) -> str | None:
        settings = self._settings
        body = json.dumps({"from": settings.smtp_sender, "to": [message.to], "subject": message.subject,
                           "text": message.text}).encode("utf-8")
        request = urllib.request.Request(RESEND_ENDPOINT, data=body, method="POST", headers={
            "Authorization": f"Bearer {settings.resend_api_key}", "Content-Type": "application/json",
            "Idempotency-Key": message.idempotency_key, "User-Agent": "schedule-maxing-backend"})
        try:
            with urllib.request.urlopen(request, timeout=settings.delivery_timeout_seconds) as response:  # noqa: S310
                answer = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as error:
            raise MailError(f"HTTP {error.code}") from None
        except (OSError, ValueError) as error:
            raise MailError(type(error).__name__) from None
        identifier = answer.get("id") if isinstance(answer, dict) else None
        return str(identifier) if identifier else None


def mail_for(settings: BackendSettings) -> MailAdapter | None:
    if not settings.mail_configured:
        return None
    return ResendMail(settings) if settings.email_provider == "resend" else SmtpMail(settings)
