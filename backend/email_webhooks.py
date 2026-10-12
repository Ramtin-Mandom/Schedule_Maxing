"""
backend/email_webhooks.py

POST /webhooks/email: the mail provider's report of what happened to a
message after it accepted it. This is the only source of delivered_at,
bounced_at and complained_at in email_outbox -- acceptance by the provider
(accepted_at) is recorded by the outbox and is never treated as delivery.

Authentication (the Standard Webhooks scheme, which Resend's Svix-signed
webhooks use): the headers webhook-id / webhook-timestamp /
webhook-signature (or the same with the svix- prefix) carry an event id, a
Unix timestamp and one or more "v1,<base64 HMAC-SHA256>" signatures of
"<id>.<timestamp>.<raw body>" under EMAIL_WEBHOOK_SECRET ("whsec_<base64
key>"). A request is processed only if a signature matches (constant-time
comparison, over the exact bytes received) and the timestamp is within
TOLERANCE_SECONDS of the server clock.

Replay: an authentic request repeated inside that tolerance is stopped by
its id -- every processed id is stored (email_webhook_events), and a known
one answers 200 without changing anything (providers retry; that must not be
an error).

Without EMAIL_WEBHOOK_SECRET the route answers 404. Refusals say nothing
about why. Events of unknown messages or types are acknowledged and ignored.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json

from fastapi import APIRouter, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from backend import models, security_events
from backend.errors import ApiError

TOLERANCE_SECONDS = 300
#: provider event type -> the email_outbox column it sets (first report wins).
EVENT_COLUMNS = {"email.delivered": "delivered_at", "email.bounced": "bounced_at", "email.complained": "complained_at"}

webhooks = APIRouter(tags=["webhooks"])


def _secret_key(secret: str) -> bytes:
    raw = secret.removeprefix("whsec_")
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return raw.encode("utf-8")


def sign(secret: str, event_id: str, timestamp: str, body: bytes) -> str:
    """The "v1,<signature>" value of a webhook (used by tests and by a provider simulator)."""
    signed = f"{event_id}.{timestamp}.".encode("utf-8") + body
    return "v1," + base64.b64encode(hmac.new(_secret_key(secret), signed, hashlib.sha256).digest()).decode("ascii")


def _refused() -> ApiError:
    security_events.record("webhook_refused")
    return ApiError(401, "webhook_refused", "The webhook was not accepted.")


@webhooks.post("/webhooks/email", include_in_schema=False)
async def email_webhook(request: Request) -> dict:
    settings = request.app.state.settings
    if not settings.email_webhook_secret:
        raise ApiError(404, "not_found", "Not Found")
    headers = request.headers
    event_id = headers.get("webhook-id") or headers.get("svix-id") or ""
    timestamp = headers.get("webhook-timestamp") or headers.get("svix-timestamp") or ""
    signatures = (headers.get("webhook-signature") or headers.get("svix-signature") or "").split()
    body = await request.body()
    now = request.app.state.clock()
    if not event_id or len(event_id) > 200 or not timestamp.isdigit() or not signatures:
        raise _refused()
    if abs(now.timestamp() - int(timestamp)) > TOLERANCE_SECONDS:
        raise _refused()
    expected = sign(settings.email_webhook_secret, event_id, timestamp, body)
    if not any(hmac.compare_digest(candidate, expected) for candidate in signatures):
        raise _refused()
    try:
        event = json.loads(body)
    except ValueError:
        raise _refused() from None
    with request.app.state.session_factory() as session:
        session.add(models.EmailWebhookEvent(id=event_id, received_at=now))
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            return {"status": "duplicate"}
        column = EVENT_COLUMNS.get(event.get("type")) if isinstance(event, dict) else None
        data = event.get("data") if isinstance(event, dict) else None
        message_id = data.get("email_id") if isinstance(data, dict) else None
        if column and isinstance(message_id, str) and message_id:
            outbox = models.EmailOutbox
            row_id = session.scalar(select(outbox.id).where(outbox.provider_message_id == message_id))
            if row_id is not None:
                session.execute(update(outbox).where(outbox.id == row_id, getattr(outbox, column).is_(None))
                                .values({column: now}))
        session.commit()
    return {"status": "ok"}
