"""
backend/rate_limit.py

Throttling of authentication and recovery requests, shared by every worker
process and replica: the counters live in the server database
(rate_limit_buckets), not in process memory -- a per-process counter would
multiply the limit by the number of workers.

Fixed windows. A hit increments the row keyed by
SHA-256(scope | subject | window index) with one atomic UPDATE (or INSERT
when the window's row does not exist yet; a concurrent INSERT of the same
key loses on the primary key and retries the UPDATE). Expired rows of the
scope are deleted when a new window starts (bounded retention; nothing else
cleans up). A hit over the limit raises 429 rate_limited with Retry-After =
the seconds left in the window; the request is not processed.

Subjects are hashed: neither a client address nor an email/username is
stored in clear. Limits per subject (LIMITS) apply equally to existing and
unknown accounts, so throttling does not reveal whether an account exists.

Limiter failure (the database cannot be reached or written): the request is
refused with 503 temporarily_unavailable -- authentication fails closed
rather than running unthrottled.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend import models
from backend.errors import ApiError


@dataclass(frozen=True)
class Limit:
    count: int
    window_seconds: int


#: scope -> limit, per subject (a client address "ip:..." or an account identifier "id:...").
LIMITS: dict[str, Limit] = {
    "login_ip": Limit(30, 300),
    "login_identifier": Limit(10, 900),
    "register_ip": Limit(10, 3600),
    "refresh_ip": Limit(60, 300),
    "logout_ip": Limit(60, 300),
    "recovery_ip": Limit(10, 3600),
    "recovery_identifier": Limit(3, 3600),
    "reset_ip": Limit(20, 3600),
}


class RateLimiter:
    """Counts hits in their own short transactions (never the request's session)."""

    def __init__(self, factory: sessionmaker[Session], clock: Callable[[], datetime],
                 limits: dict[str, Limit] | None = None) -> None:
        self._factory = factory
        self._clock = clock
        self._limits = limits or LIMITS

    def hit(self, scope: str, subject: str) -> None:
        """Count one request of `subject` in `scope`; ApiError 429 when over the limit, 503 when unavailable."""
        limit = self._limits[scope]
        now = self._clock()
        epoch_seconds = now.timestamp()
        window = int(epoch_seconds // limit.window_seconds)
        window_end = (window + 1) * limit.window_seconds
        key = hashlib.sha256(f"{scope}|{subject}|{window}".encode()).hexdigest()
        try:
            count = self._increment(key, scope, now + timedelta(seconds=window_end - epoch_seconds))
        except SQLAlchemyError:
            raise ApiError(503, "temporarily_unavailable",
                           "Signing in is temporarily unavailable. Try again shortly.") from None
        if count > limit.count:
            retry_after = max(1, math.ceil(window_end - epoch_seconds))
            raise ApiError(429, "rate_limited", "Too many attempts. Try again later.", retry_after=retry_after)

    def _increment(self, key: str, scope: str, expires_at: datetime) -> int:
        with self._factory() as session:
            for _ in range(2):
                updated = session.execute(
                    update(models.RateLimitBucket).where(models.RateLimitBucket.key == key)
                    .values(count=models.RateLimitBucket.count + 1)
                    .returning(models.RateLimitBucket.count)).first()
                if updated is not None:
                    session.commit()
                    return updated[0]
                # A new window: drop this scope's expired windows (bounded retention), then start this one.
                session.execute(delete(models.RateLimitBucket).where(
                    models.RateLimitBucket.scope == scope, models.RateLimitBucket.expires_at <= self._clock()))
                session.add(models.RateLimitBucket(key=key, scope=scope, count=1, expires_at=expires_at))
                try:
                    session.commit()
                    return 1
                except IntegrityError:
                    session.rollback()  # another worker started the window first: count on its row
            raise SQLAlchemyError("the rate-limit window could not be counted")


def identifier_subject(identifier: str | None) -> str:
    from backend.passwords import normalize_identifier

    return "id:" + normalize_identifier(identifier or "")
