"""
backend/rate_limit.py

Throttling of authentication and recovery requests, shared by every worker
process and replica: the counters live in the server database
(rate_limit_buckets), not in process memory -- a per-process counter would
multiply the limit by the number of workers.

Sliding windows (the two-bucket "sliding window counter"). A hit increments
the row keyed by SHA-256(scope | subject | window index) with one atomic
UPDATE (or INSERT when the window's row does not exist yet; a concurrent
INSERT of the same key loses on the primary key and retries the UPDATE). The
rate is estimated as

    previous window's count * (share of it still inside the sliding window)
    + this window's count

so a burst at the end of one window is still counted at the start of the
next: there is no boundary at which a client gets twice the limit.

Three operations:
    hit(scope, subject)       count a request; 429 when that puts it over the
                              limit (request limits: cheap, before any work).
    require(scope, subject)   read only: 429 when the limit is already used
                              up (failure limits, checked before a password
                              is verified).
    record(scope, subject)    count without refusing (a failed sign-in);
                              returns the estimate before and after.

Storage is bounded: a subject has at most two rows per scope (this window
and the previous one), rows are deleted once both windows passed -- as new
windows start; nothing else cleans up -- and only requests that already
passed the per-address request limit can create rows for new subjects.

Subjects are hashed: neither a client address nor an email/username is
stored in clear. Limits apply equally to existing and unknown accounts, so
throttling does not reveal whether an account exists.

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

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend import models, security_events
from backend.errors import ApiError


@dataclass(frozen=True)
class Limit:
    count: int
    window_seconds: int


#: scope -> limit, per subject. Request limits count every request of a client address ("ip:..."); the
#: login_fail_* scopes count failed sign-ins only (backend/login_guard.py says how they combine).
LIMITS: dict[str, Limit] = {
    "login_ip": Limit(30, 300),
    "login_fail_ip": Limit(20, 900),
    "login_fail_pair": Limit(5, 900),
    "login_fail_account": Limit(20, 3600),
    "register_ip": Limit(10, 3600),
    "refresh_ip": Limit(60, 300),
    "logout_ip": Limit(60, 300),
    "recovery_ip": Limit(10, 3600),
    "reset_ip": Limit(20, 3600),
    # Verification (backend/verification_api.py). The email_* scopes bound what is *sent*: per destination
    # (with a cooldown between two emails) and for the whole service; over them nothing is queued, and the
    # public answer stays the same.
    "verify_confirm_ip": Limit(20, 3600),
    "email_destination": Limit(4, 3600),
    "email_cooldown": Limit(1, 60),
    "email_global": Limit(300, 3600),
    # Second steps and re-entered passwords (backend/security_api.py): failures per account.
    "mfa_ip": Limit(30, 300),
    "mfa_fail": Limit(5, 900),
    "reauth_fail": Limit(5, 900),
}


@dataclass(frozen=True)
class _Window:
    limit: Limit
    index: int
    #: How far into the current window the clock is (0..1).
    fraction: float
    now: datetime

    def estimate(self, previous: int, current: int) -> float:
        return previous * (1.0 - self.fraction) + current

    def retry_after(self, previous: int, current: int, room_for: int) -> int:
        """Seconds until the estimate has dropped to `room_for` (at most: until this window ends)."""
        seconds = self.limit.window_seconds
        remaining = (1.0 - self.fraction) * seconds
        if previous > 0 and current <= room_for:
            needed = 1.0 - (room_for - current) / previous  # the fraction at which the estimate reaches room_for
            remaining = min(remaining, (needed - self.fraction) * seconds)
        return max(1, math.ceil(remaining))


def _key(scope: str, subject: str, index: int) -> str:
    return hashlib.sha256(f"{scope}|{subject}|{index}".encode()).hexdigest()


def _unavailable() -> ApiError:
    return ApiError(503, "temporarily_unavailable", "Signing in is temporarily unavailable. Try again shortly.")


def _limited(retry_after: int) -> ApiError:
    return ApiError(429, "rate_limited", "Too many attempts. Try again later.", retry_after=retry_after)


class RateLimiter:
    """Counts hits in their own short transactions (never the request's session)."""

    def __init__(self, factory: sessionmaker[Session], clock: Callable[[], datetime],
                 limits: dict[str, Limit] | None = None) -> None:
        self._factory = factory
        self._clock = clock
        self._limits = limits or LIMITS

    def limit(self, scope: str) -> Limit:
        return self._limits[scope]

    def _window(self, scope: str) -> _Window:
        limit = self._limits[scope]
        now = self._clock()
        position = now.timestamp() / limit.window_seconds
        return _Window(limit, int(position), position - int(position), now)

    def hit(self, scope: str, subject: str) -> None:
        """Count one request of `subject` in `scope`; ApiError 429 when over the limit, 503 when unavailable."""
        window = self._window(scope)
        previous, current = self._count(scope, subject, window, increment=True)
        if window.estimate(previous, current) > window.limit.count:
            security_events.record("rate_limited", scope=scope)
            raise _limited(window.retry_after(previous, current, window.limit.count - 1))

    def estimate(self, scope: str, subject: str) -> float:
        """The current rate of `subject` in `scope`, counting nothing (503 when unavailable)."""
        window = self._window(scope)
        return window.estimate(*self._count(scope, subject, window, increment=False))

    def require(self, scope: str, subject: str, *, count: int | None = None) -> None:
        """
        Refuse (429) when `subject` already used up the limit of `scope` -- or the smaller `count` -- without
        counting this request. For limits on failures: record() counts one when the attempt did fail.
        """
        window = self._window(scope)
        allowed = window.limit.count if count is None else count
        previous, current = self._count(scope, subject, window, increment=False)
        if window.estimate(previous, current) >= allowed:
            security_events.record("rate_limited", scope=scope)
            raise _limited(window.retry_after(previous, current, allowed - 1))

    def record(self, scope: str, subject: str) -> tuple[float, float]:
        """Count one event without refusing; the estimate before and after (503 when unavailable)."""
        window = self._window(scope)
        previous, current = self._count(scope, subject, window, increment=True)
        return window.estimate(previous, current - 1), window.estimate(previous, current)

    def _count(self, scope: str, subject: str, window: _Window, *, increment: bool) -> tuple[int, int]:
        """(the previous window's count, this window's count -- after the increment, if any)."""
        key, previous_key = _key(scope, subject, window.index), _key(scope, subject, window.index - 1)
        try:
            with self._factory() as session:
                if increment:
                    # A row is read while the next window runs, so it lives until that one ends.
                    seconds = window.limit.window_seconds
                    expires_at = window.now + timedelta(seconds=(2.0 - window.fraction) * seconds)
                    current = self._increment(session, key, scope, expires_at)
                else:
                    current = self._read(session, key)
                return self._read(session, previous_key), current
        except SQLAlchemyError:
            raise _unavailable() from None

    @staticmethod
    def _read(session: Session, key: str) -> int:
        return session.scalar(select(models.RateLimitBucket.count).where(models.RateLimitBucket.key == key)) or 0

    def _increment(self, session: Session, key: str, scope: str, expires_at: datetime) -> int:
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
