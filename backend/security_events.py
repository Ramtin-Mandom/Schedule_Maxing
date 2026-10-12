"""
backend/security_events.py

Security-relevant events as structured, redacted log lines and in-process
counters -- what an operator watches for authentication abuse, verification
failures, email delivery trouble and resource saturation.

    record("sign_in_failed")
    record("rate_limited", scope="login_fail_pair")
    record("email_not_accepted", kind="verification", error="MailError")

Each call logs one JSON object on the "schedule_maxing.security.events"
logger ({"event": ..., plus the allowed fields}) and increments a counter
keyed by the event and its fields. counters() returns the totals since the
process started; log_counters() writes them as one line (the background
worker does, hourly), so they can be read from the log of each worker
process without any endpoint.

Redaction is structural, not a filter applied afterwards: only the field
names in ALLOWED_FIELDS are accepted, and their values must be short plain
identifiers (a scope, a kind, an error class). There is no field for a
password, a token, a code, a link, an email address, an IP address or a
database URL, so none can be logged through here -- a value that does not
look like an identifier is replaced by "[redacted]".
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter

LOG = logging.getLogger("schedule_maxing.security.events")

#: The only fields an event may carry.
ALLOWED_FIELDS = frozenset({"scope", "kind", "error", "reason"})
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.\- ]{0,63}$")

_counters: Counter[str] = Counter()
_lock = threading.Lock()


def _clean(value: object) -> str:
    text = str(value)
    return text if _IDENTIFIER.match(text) else "[redacted]"


def record(event: str, **fields: object) -> None:
    """Log one security event and count it. Unknown fields are dropped; unusual values are redacted."""
    safe = {name: _clean(value) for name, value in sorted(fields.items()) if name in ALLOWED_FIELDS}
    key = event + "".join(f"|{name}={value}" for name, value in safe.items())
    with _lock:
        _counters[key] += 1
    LOG.info(json.dumps({"event": _clean(event), **safe}, sort_keys=True))


def counters() -> dict[str, int]:
    """Totals since this process started, keyed "event|field=value|..."."""
    with _lock:
        return dict(_counters)


def log_counters() -> None:
    totals = counters()
    if totals:
        LOG.info(json.dumps({"event": "counters", "totals": totals}, sort_keys=True))


def reset() -> None:
    """Forget the totals (tests)."""
    with _lock:
        _counters.clear()
