"""
app/sync/service.py

SyncService: the application-level entry point for synchronization
(docs/sync-protocol.md). Tk-free; app/ui/app_services.py creates one per
desktop session. There is no sync screen yet -- this is the service API a
later milestone's UI will call.

Inert by default: without a configured backend (no transport) or without a
signed-in account, sync_now() returns status "inert" and touches nothing,
and ordinary local use never needs a network, a login, or backend settings.

Credentials: sign_in(email, password) exchanges the password for an access
token once; the token is held in memory only and dropped by sign_out() or
when the backend answers 401 (status "auth_required"). Passwords and tokens
are never stored or logged.

Accounts: each (backend URL, server user) is a separate sync_accounts row
with its own cursor, shadows, outbox operations and conflicts. Signing in
makes that account current and deactivates any other; only records owned by
the current account are pushed and pulled records are stored as its own.
Existing ownerless local records are *not* claimed on sign-in: that is the
explicit associate_local_data() step. (An account that was associated on
this device before becomes active again when you sign back in, so records
you create while signed in are owned by it.)

sync_now(): push until the outbox is drained (bounded), then pull until
caught up (bounded). Only one sync runs at a time. SQLite work happens in
short transactions between the network calls -- no SQLite transaction or
lock is held during a request -- so desktop edits proceed concurrently.

Failures: TransportError (offline, timeout, 5xx) leaves every operation in
the durable outbox (also across restarts) and schedules a retry with
bounded exponential backoff; AuthenticationError drops the token;
ProtocolError is reported and not retried automatically. Conflicts and
rejections are per record (list_conflicts/resolve_conflict) and never stop
unrelated records.

Background: start() runs sync_now() every `interval` seconds (or after the
backoff delay) on a daemon thread; wake() triggers a run early; stop()
ends it and waits for an in-progress run, and is called by
AppServices.close() before the database is closed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from app.sync.engine import SyncEngine
from app.sync.store import Account, Conflict
from app.sync.transport import AuthenticationError, ProtocolError, SyncTransport, TransportError

logger = logging.getLogger(__name__)

MAX_PUSH_ROUNDS = 50
MAX_PULL_PAGES = 1000


@dataclass(frozen=True)
class SyncReport:
    #: inert | ok | offline | auth_required | error
    status: str
    pushed: int = 0
    pulled: int = 0
    conflicts: int = 0
    message: str = ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SyncService:
    def __init__(
        self,
        connection,
        transport: SyncTransport | None,
        *,
        clock: Callable[[], datetime] = _utcnow,
        interval: float = 60.0,
        backoff_base: float = 5.0,
        backoff_max: float = 600.0,
        push_batch_size: int = 100,
        pull_page_size: int = 200,
    ) -> None:
        self._engine = SyncEngine(connection, clock)
        self._transport = transport
        self._interval = interval
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._push_batch_size = push_batch_size
        self._pull_page_size = pull_page_size
        self._token: str | None = None
        self._account_key: str | None = None
        self._sync_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self.consecutive_failures = 0
        self.last_report = SyncReport("inert")
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Accounts and credentials
    # ------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return self._transport is not None

    @property
    def account(self) -> Account | None:
        with self._state_lock:
            return self._engine.store.account(self._account_key) if self._account_key else None

    @property
    def signed_in(self) -> bool:
        with self._state_lock:
            return self._token is not None

    def sign_in(self, email: str, password: str) -> Account:
        if self._transport is None:
            raise RuntimeError("No backend is configured (set SCHEDULE_MAXING_BACKEND_URL).")
        result = self._transport.login(email, password)
        store = self._engine.store
        account = store.upsert_account(self._transport.base_url, result.user_id, result.email)
        store.set_active(account.account_key if account.associated_at else None)
        with self._state_lock:
            self._token, self._account_key = result.token, account.account_key
        return store.account(account.account_key)

    def sign_out(self) -> None:
        with self._sync_lock, self._state_lock:
            self._token, self._account_key = None, None
            self._engine.store.set_active(None)

    def associate_local_data(self) -> dict[str, int]:
        """Explicitly claim this device's ownerless records for the signed-in account (see SyncEngine)."""
        account = self._require_account()
        with self._sync_lock:
            return self._engine.associate_local_data(account)

    def _require_account(self) -> Account:
        account = self.account
        if account is None or not self.signed_in:
            raise RuntimeError("Sign in to an account first.")
        return account

    # ------------------------------------------------------------------
    # Conflicts
    # ------------------------------------------------------------------

    def list_conflicts(self, status: str | None = "open") -> list[Conflict]:
        account = self.account
        return self._engine.conflicts(account, status) if account else []

    def get_conflict(self, conflict_id: str) -> Conflict | None:
        conflict = self._engine.store.conflict(conflict_id)
        account = self.account
        return conflict if conflict and account and conflict.account_key == account.account_key else None

    def resolve_conflict(self, conflict_id: str, choice: str) -> Conflict:
        account = self.account
        if account is None:
            raise RuntimeError("Sign in to an account first.")
        with self._sync_lock:
            return self._engine.resolve(account, conflict_id, choice)

    # ------------------------------------------------------------------
    # Synchronization
    # ------------------------------------------------------------------

    def sync_now(self) -> SyncReport:
        with self._state_lock:
            token, key = self._token, self._account_key
        if self._transport is None or token is None or key is None:
            return self._finish(SyncReport("inert", message="No backend configured or no account signed in."))
        with self._sync_lock:
            account = self._engine.store.account(key)
            pushed = pulled = conflicts = 0
            batch: list = []
            try:
                for _ in range(MAX_PUSH_ROUNDS):
                    self._engine.prepare(account)
                    batch = self._engine.next_batch(account, self._push_batch_size)
                    if not batch:
                        break
                    results = self._transport.push(token, [op.wire() for op in batch])  # no transaction open
                    outcome = self._engine.acknowledge(account, batch, results)
                    pushed += outcome.applied
                    conflicts += outcome.conflicts
                batch = []
                for _ in range(MAX_PULL_PAGES):
                    account = self._engine.store.account(key)
                    page = self._transport.pull(token, account.pull_cursor, self._pull_page_size)
                    outcome = self._engine.apply_pull_page(account, page)
                    pulled += outcome.applied
                    conflicts += len(outcome.conflicts)
                    if not page.has_more:
                        break
            except TransportError as error:
                if batch:
                    self._engine.store.record_attempt([op.op_id for op in batch], str(error))
                self.consecutive_failures += 1
                return self._finish(SyncReport("offline", pushed, pulled, conflicts, str(error)))
            except AuthenticationError as error:
                with self._state_lock:
                    self._token = None
                return self._finish(SyncReport("auth_required", pushed, pulled, conflicts, str(error)))
            except ProtocolError as error:
                if batch:
                    self._engine.store.block_ops([op.op_id for op in batch], str(error))
                self.consecutive_failures += 1
                return self._finish(SyncReport("error", pushed, pulled, conflicts, str(error)))
            self.consecutive_failures = 0
            return self._finish(SyncReport("ok", pushed, pulled, conflicts))

    def _finish(self, report: SyncReport) -> SyncReport:
        self.last_report = report
        if report.status not in ("ok", "inert"):
            logger.info("Synchronization did not complete: %s", report.status)
        return report

    def next_delay(self) -> float:
        """Seconds until the next background run: the interval, or bounded exponential backoff after failures."""
        if self.consecutive_failures == 0:
            return self._interval
        return min(self._backoff_max, self._backoff_base * (2 ** (self.consecutive_failures - 1)))

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None or self._transport is None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="schedule-maxing-sync", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_now()
            except Exception:  # noqa: BLE001 - the loop must survive; details are logged, not raised into Tk
                logger.exception("Unexpected synchronization failure")
                self.consecutive_failures += 1
            self._wake.wait(self.next_delay())
            self._wake.clear()

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop the loop and wait for a running sync to finish. True if it stopped in time."""
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()
