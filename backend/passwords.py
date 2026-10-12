"""
backend/passwords.py

Account identifiers and password hashing, shared by the HTTP API
(backend/api.py via backend/security.py) and the direct desktop path
(app/persistence) through backend/accounts.py. Nothing here needs a JWT
secret or an HTTP framework.

argon2-cffi's PasswordHasher: Argon2id with the library's default
parameters and a random salt per hash. Only the encoded hash is stored;
hashes are upgraded transparently on sign-in when the library's parameters
change (needs_rehash). Nothing here logs or returns a password.

Resource bound. One hash or verification holds hash_memory_mib() of memory
(64 MiB with the library's parameters) for its whole run, and every sign-in,
registration and reset costs one -- for unknown accounts too. So both go
through one gate per process: at most `concurrency` run at a time, at most
`queue` more wait, each for at most `wait_seconds`; anything beyond that is
refused at once with PasswordWorkBusyError (the API answers 503 with
Retry-After) instead of piling up memory or threads. The gate bounds one
process: a deployment's peak is workers x concurrency x hash_memory_mib(),
which backend/app.py checks against PASSWORD_HASH_MEMORY_BUDGET_MB. The
hashing parameters themselves are not weakened to save memory.
"""

from __future__ import annotations

import threading
import unicodedata
import uuid

from argon2 import PasswordHasher

from backend import security_events
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()


class PasswordWorkBusyError(Exception):
    """Too many passwords are being processed; nothing was hashed or verified. Try again shortly."""

    def __init__(self) -> None:
        super().__init__("The server is busy. Try again in a moment.")


class _WorkGate:
    """At most `concurrency` holders, at most `queue` waiters, each waiting at most `wait_seconds`."""

    def __init__(self, concurrency: int, queue: int, wait_seconds: float) -> None:
        self.concurrency, self.queue, self.wait_seconds = concurrency, queue, wait_seconds
        self._slots = threading.BoundedSemaphore(concurrency)
        self._lock = threading.Lock()
        self._waiting = 0

    def __enter__(self) -> None:
        if self._slots.acquire(blocking=False):
            return
        with self._lock:
            if self._waiting >= self.queue:
                security_events.record("password_work_saturated", reason="queue full")
                raise PasswordWorkBusyError()
            self._waiting += 1
        try:
            if not self._slots.acquire(timeout=self.wait_seconds):
                security_events.record("password_work_saturated", reason="wait timed out")
                raise PasswordWorkBusyError()
        finally:
            with self._lock:
                self._waiting -= 1

    def __exit__(self, *_exc) -> None:
        self._slots.release()


DEFAULT_CONCURRENCY = 2
DEFAULT_QUEUE = 8
DEFAULT_WAIT_SECONDS = 5.0
_gate = _WorkGate(DEFAULT_CONCURRENCY, DEFAULT_QUEUE, DEFAULT_WAIT_SECONDS)


def configure_password_work(concurrency: int = DEFAULT_CONCURRENCY, queue: int = DEFAULT_QUEUE,
                            wait_seconds: float = DEFAULT_WAIT_SECONDS) -> None:
    """Set this process's gate (the server does, at startup). Work already running finishes under the old one."""
    global _gate
    if (concurrency, queue, wait_seconds) != (_gate.concurrency, _gate.queue, _gate.wait_seconds):
        _gate = _WorkGate(concurrency, queue, wait_seconds)


def hash_memory_mib() -> float:
    """The memory one hash or verification holds, measured from the configured Argon2 parameters."""
    return _hasher.memory_cost / 1024  # memory_cost is in KiB; the lanes share that one block

#: A valid hash of a random password, verified against when an account does
#: not exist, so an unknown email and a wrong password take similar time.
_DUMMY_HASH = _hasher.hash(uuid.uuid4().hex)

#: The length rule of a *new* password (backend/password_policy.py has the whole policy). Signing in applies
#: no minimum: an existing password keeps working until its owner changes it.
from backend.password_policy import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH  # noqa: E402,F401


def normalize_identifier(value: str) -> str:
    """Canonical form of an email/username for uniqueness and lookup: NFKC, trimmed, lower-cased."""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def hash_password(password: str) -> str:
    with _gate:
        return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Whether `password` matches; an unknown account (None) costs the same verification, against a dummy hash."""
    with _gate:
        try:
            return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
