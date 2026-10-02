"""
backend/recovery_delivery.py

Delivery of password-recovery links. The API hands a RecoveryDelivery
(backend/recovery.py) to an adapter in the background, after answering the
request -- so neither the response nor its timing depends on delivery, and a
delivery failure is never shown to the requester.

    SmtpDelivery      the real adapter (standard library smtplib): STARTTLS
                      (default) or implicit TLS, optional login, a timeout on
                      every network operation (DELIVERY_TIMEOUT_SECONDS).
    delivery_for(settings)  the adapter the settings configure, or None.

Failure policy: a failed or timed-out delivery is logged without the
address, the link, the token or any server credential (only the error
class), and leaves recovery state consistent: the issued token simply stays
unused until it expires, and the user can request a new link (which revokes
it). Nothing is retried automatically -- a retry is a new request, bounded
by the recovery rate limits.

Links are built from RECOVERY_PUBLIC_URL only (never from request Host or
forwarded headers), with the token in the URL *fragment*
(https://app.example.com/auth/recovery/reset#token=...): browsers never send
a fragment to the server, so the token is in no request line, access log or
Referer header.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

from backend.recovery import RecoveryDelivery
from backend.settings import BackendSettings

LOG = logging.getLogger("schedule_maxing.recovery")


class DeliveryError(Exception):
    """The link could not be delivered (no details: they may name hosts or addresses)."""


class RecoveryDeliveryAdapter(Protocol):
    def send(self, delivery: RecoveryDelivery, link: str) -> None: ...


def recovery_link(settings: BackendSettings, token: str) -> str:
    return f"{settings.recovery_public_url}#token={token}"


class SmtpDelivery:
    def __init__(self, settings: BackendSettings) -> None:
        self._settings = settings

    def send(self, delivery: RecoveryDelivery, link: str) -> None:
        settings = self._settings
        message = EmailMessage()
        message["Subject"] = "Reset your Schedule Maxing password"
        message["From"] = settings.smtp_sender
        message["To"] = delivery.email
        message.set_content(
            "Someone asked to reset the password of your Schedule Maxing account.\n\n"
            f"To choose a new password, open this link (valid until {delivery.expires_at:%Y-%m-%d %H:%M} UTC, "
            f"usable once):\n\n{link}\n\n"
            "If you did not ask for this, ignore this message: your password stays the same.\n"
        )
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
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as error:
            raise DeliveryError(type(error).__name__) from None


def delivery_for(settings: BackendSettings) -> RecoveryDeliveryAdapter | None:
    return SmtpDelivery(settings) if settings.recovery_configured else None


def deliver_quietly(adapter: RecoveryDeliveryAdapter, settings: BackendSettings, delivery: RecoveryDelivery) -> None:
    """Deliver in the background; a failure is logged by class only (see the module docstring)."""
    try:
        adapter.send(delivery, recovery_link(settings, delivery.token))
    except Exception as error:  # noqa: BLE001 - never surfaces to the requester; recovery state stays valid
        LOG.warning("password recovery delivery failed (%s)", type(error).__name__)
