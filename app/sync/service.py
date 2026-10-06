"""
app/sync/service.py

SyncService: the application-level entry point for synchronization
(docs/sync-protocol.md). Tk-free; app/ui/app_services.py creates one per
desktop session; the Account page (app/ui/account_page.py, through
AccountController) is its screen: status, sign-in, password recovery,
association, Sync now and the conflict comparison.

Inert by default: without a configured backend (no transport) or without a
signed-in account, sync_now() returns status "inert" and touches nothing,
and ordinary local use never needs a network, a login, or backend settings.

Credentials: sign_in(email, password) exchanges the password for an access
token once; the token is held in memory only and dropped by sign_out() or
when the backend answers 401 (status "auth_required"). Passwords and tokens
are never stored or logged.

Accounts: each (backend URL, server user) is a separate sync_accounts row
with its own cursor, shadows, outbox operations and conflicts. Signing in
makes that account current and the device's active one (deactivating any
other), so its workspace stays in use offline and across restarts until an
explicit sign-out -- losing the connection or the session is never a
sign-out. Only records owned by the current account are pushed, and pulled
records are stored as its own. Existing ownerless (guest) local records are
*not* claimed by signing in to an existing account: that is the explicit
associate_local_data() step, which the user chooses or declines. Creating a
new account is different: create_account() registers it, signs in and
adopts the guest workspace for it in one local transaction, so the work
done before the account existed stays visible and is uploaded.

sync_now(): push until the outbox is drained (bounded), then pull until
caught up (bounded). Only one sync runs at a time. SQLite work happens in
short transactions between the network calls -- no SQLite transaction or
lock is held during a request -- so desktop edits proceed concurrently.

Failures: TransportError (offline, timeout, 5xx) leaves every operation in
the durable outbox (also across restarts) and schedules a retry with
bounded exponential backoff; AuthenticationError drops the token (the
outbox and conflicts stay; signing in again resumes them under the same
account only); a push the server refuses as a whole (ProtocolError) is
narrowed to the refused units, which become push_rejected conflicts (never
retried automatically, never holding other records back); a refused pull is
reported. Conflicts and rejections are per record
(list_conflicts/resolve_conflict) and never stop unrelated records.

Background: start() runs sync_now() every `interval` seconds (or after the
backoff delay) on a daemon thread; wake() triggers a run early; stop()
ends it and waits for an in-progress run, and is called by
AppServices.close() before the database is closed. While idle the loop also
looks, every `pending_poll` seconds, at the durable count of pending
records -- a local query, never a request -- and runs early when it changed
since the last sync, so an edit is uploaded within seconds without any
manual action. After a failure only the backoff decides the next attempt.

Account switches never interleave with a sync (Milestone 4, the local web
profile): sign_in, sign_out, set_transport and association wait for a
running sync, so a sync's pushes, pulls and acknowledgements always belong
to the account (and backend) it started with; a 401 drops only the token
that sync used. status() reports what a UI needs without guessing: backend
reachability (from the last request or probe), whether sign-in is needed,
a running sync, pending records (dirty or queued, each counted once),
open conflicts, the last successful sync (persisted, schema v6) and the
last error. The public account operations (register, profile,
update_profile, check_connectivity) go through the transport, and
association can be previewed and confirmed exactly (association_preview /
associate_local_data(confirmation=...)).

Capability negotiation (docs/recurrence.md): each sync asks the server for
its protocol features once per session (transport.capabilities, cached for
the transport and token). Records carrying recurrence data -- configured
series, occurrences, lineage, and the placements and executions of
occurrences -- are pushed only to a server that lists
"recurrence_occurrences"; for an older one they stay pending here (counted
in SyncReport.held, with a message) instead of being sent to a server that
would drop their fields.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from app.planning.scope import OwnerScope
from app.sync.engine import AssociationPreview, PushOutcome, SyncEngine
from app.sync.store import BACKEND_URL_SETTING, Account, Conflict
from app.sync.transport import AuthenticationError, ProtocolError, SyncTransport, TransportError

logger = logging.getLogger(__name__)

MAX_PUSH_ROUNDS = 50
MAX_PULL_PAGES = 1000

#: The server feature that recurring-series records need (backend/sync.py SYNC_FEATURES).
def _push_units(batch: list) -> list[list]:
    """The batch split into what the server applies as one unit: a group's operations together, others alone."""
    units: list[list] = []
    for op in batch:
        if op.group_id is not None and units and units[-1][0].group_id == op.group_id:
            units[-1].append(op)
        else:
            units.append([op])
    return units


RECURRENCE_FEATURE = "recurrence_occurrences"
#: Placement origin/manual intent and execution cancel reasons (docs/execution-rescheduling.md).
MANUAL_PLACEMENTS_FEATURE = "manual_placements"
#: Early Finish, Night Owl and Catch-Up as preference/schedule-record values (docs/scheduling-modes.md).
SCHEDULING_MODES_FEATURE = "scheduling_modes"
#: Task types and placement planning snapshots (docs/productivity-redesign-plan.md).
TASK_TYPES_FEATURE = "task_types"
#: A project's planned dates, completion and milestones.
PROJECT_DETAILS_FEATURE = "project_details"


@dataclass(frozen=True)
class SyncReport:
    #: inert | ok | offline | auth_required | error
    status: str
    pushed: int = 0
    pulled: int = 0
    conflicts: int = 0
    message: str = ""
    #: Records held back because the server does not support recurrence (they stay pending).
    held: int = 0


class AccountCreationError(Exception):
    """
    create_account() could not finish after the server created the account.
    Nothing local was lost or reassigned: the guest workspace is as it was,
    and signing in (then choosing to add the device's records) completes it.
    """

    def __init__(self, message: str, *, stage: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        #: "sign_in" (the account exists; the sign-in after it failed) or "adoption" (signed in; nothing adopted).
        self.stage = stage
        self.cause = cause


@dataclass(frozen=True)
class AccountCreation:
    """What create_account() did."""

    account: Account
    profile: dict
    #: Guest records that became the account's, per entity type (empty: the device had none).
    adopted: dict[str, int]

    @property
    def adopted_total(self) -> int:
        return sum(self.adopted.values())


@dataclass(frozen=True)
class SyncStatus:
    configured: bool
    backend_url: str | None
    #: True/False from the last request or probe that reached (or failed to reach) the backend; None if unknown.
    backend_reachable: bool | None
    backend_checked_at: str | None
    signed_in: bool
    #: An account is selected but its token was refused or dropped: sign in again.
    auth_required: bool
    account: Account | None
    in_progress: bool
    #: Records of the workspace account waiting for the server (dirty or queued, each once); None without one.
    pending: int | None
    conflicts: int
    last_successful_sync_at: str | None
    last_report: SyncReport
    last_error: str | None
    #: The account whose records local work uses (workspace_account()): the selected one, else the device's
    #: active one. Pending, conflicts and the last successful sync are its -- durable, so they are known after a
    #: restart before anyone signs in again.
    workspace_account: Account | None = None


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
        pending_poll: float = 5.0,
        backoff_base: float = 5.0,
        backoff_max: float = 600.0,
        push_batch_size: int = 100,
        pull_page_size: int = 200,
    ) -> None:
        self._engine = SyncEngine(connection, clock)
        self._connection = connection
        self._transport = transport
        self._interval = interval
        self._pending_poll = pending_poll
        #: The pending count seen when the last sync ended (None: none ended yet): a different count is new work.
        self._pending_seen: int | None = None
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
        self._clock = clock
        self.backend_reachable: bool | None = None
        self.backend_checked_at: str | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        #: A loop thread that stop() asked to end but that had not finished when stop() returned.
        self._stopping: threading.Thread | None = None
        #: (transport, token, features) of the last capability answer.
        self._capabilities: tuple[object, str, frozenset[str]] | None = None

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

    def workspace_scope(self) -> OwnerScope:
        """
        Whose records local work (a desktop view, a local web request) sees
        and creates -- one rule for every client of this database:

            1. the account selected in this session (signed in, or signed in
               and asked to sign in again);
            2. otherwise the account marked active on this device -- an
               associated account stays active across restarts until
               sign-out, and the database stamps new records with it, so
               work continues in its scope offline;
            3. otherwise the ownerless local workspace.

        Other accounts' records are never visible in it, and ownerless records
        are only ever claimed by the explicit associate_local_data step.
        """
        account = self.workspace_account()
        return OwnerScope.account(uuid.UUID(account.user_id)) if account is not None else OwnerScope.ownerless()

    def workspace_account(self) -> Account | None:
        """The account whose workspace workspace_scope() is (None: the ownerless local workspace)."""
        return self.account or self._engine.store.active_account()

    def record_sync_state(self, entity_type: str, local_id: str) -> str:
        """
        Where one local record stands relative to the server, for honest labels:
        "local_only" (no account workspace), "conflict" (an open conflict),
        "pending" (changed here and not yet acknowledged, or never uploaded) or
        "synced" (the server acknowledged its current state). Reads only.
        """
        account = self.workspace_account()
        if account is None:
            return "local_only"
        store = self._engine.store
        wire_id = local_id
        if entity_type == "execution":
            try:
                wire_id = str(self._engine.records.executions.wire_id(local_id))
            except Exception:  # noqa: BLE001 - an unknown id is simply not synchronized
                return "pending"
        key = account.account_key
        if store.open_conflict(key, entity_type, wire_id) is not None:
            return "conflict"
        if store.dirty_rev(entity_type, local_id) is not None or store.has_ops(key, entity_type, wire_id):
            return "pending"
        return "synced" if store.shadow(key, entity_type, wire_id) is not None else "pending"

    @property
    def signed_in(self) -> bool:
        with self._state_lock:
            return self._token is not None

    @property
    def transport(self) -> SyncTransport | None:
        return self._transport

    def set_transport(self, transport: SyncTransport | None) -> None:
        """Switch backends: the current session ends first (after any running sync); nothing local changes."""
        with self._sync_lock, self._state_lock:
            self._token, self._account_key = None, None
            self._engine.store.set_active(None)
            self._transport = transport
            self.backend_reachable, self.backend_checked_at = None, None
            self.consecutive_failures = 0
            self.last_report = SyncReport("inert")

    def remembered_backend_url(self) -> str | None:
        """The backend address saved on this device (local_settings; never a credential)."""
        return self._engine.store.setting(BACKEND_URL_SETTING)

    def remember_backend_url(self, url: str | None) -> None:
        self._engine.store.set_setting(BACKEND_URL_SETTING, url)

    def sign_in(self, email: str, password: str) -> Account:
        transport = self._transport
        if transport is None:
            raise RuntimeError("No backend is configured (set SCHEDULE_MAXING_BACKEND_URL).")
        result = self._reaching(lambda: transport.login(email, password))  # the network call holds no lock
        store = self._engine.store
        with self._sync_lock, self._state_lock:  # never switch accounts under a running sync
            if transport is not self._transport:
                raise RuntimeError("The backend was changed while signing in; sign in again.")
            account = store.upsert_account(transport.base_url, result.user_id, result.email)
            # Active from now until an explicit sign-out: the account's workspace stays in use offline and after
            # a restart (a lost connection or an ended session is not a sign-out). No existing record is claimed.
            store.set_active(account.account_key)
            self._token, self._account_key = result.token, account.account_key
            self.last_report = SyncReport("inert")
        return store.account(account.account_key)

    def sign_out(self) -> None:
        with self._sync_lock, self._state_lock:
            self._token, self._account_key = None, None
            self._engine.store.set_active(None)

    def associate_local_data(self, confirmation: str | None = None) -> dict[str, int]:
        """
        Explicitly claim this device's ownerless records for the signed-in
        account (see SyncEngine). With `confirmation` -- an
        association_preview() token -- only exactly the previewed records.
        """
        account = self._require_account()
        with self._sync_lock:
            return self._engine.associate_local_data(account, confirmation=confirmation)

    def association_preview(self) -> AssociationPreview:
        """What associate_local_data would claim; writes nothing."""
        return self._engine.association_preview(self._require_account())

    # ------------------------------------------------------------------
    # Public account operations (through the transport; the token stays here)
    # ------------------------------------------------------------------

    def register(self, email: str, password: str, *, username: str | None = None,
                 display_name: str | None = None) -> dict:
        transport = self._require_transport()
        return self._reaching(lambda: transport.register(email, password, username, display_name))

    def create_account(self, email: str, password: str, *, username: str | None = None,
                       display_name: str | None = None) -> AccountCreation:
        """
        Register a new account and make this device's guest workspace its
        own: register, sign in, then adopt every ownerless record for the
        account in ONE local transaction (the same ids, relationships,
        schedules, preferences and history; each record is queued for upload
        by the change capture in that transaction). The next sync uploads
        them; it is idempotent, so a retry after a lost answer never creates
        a duplicate.

        Failure never loses or half-assigns anything:
            - registration refused or unreachable: the error is raised as it
              is; nothing local changed and nobody is signed in;
            - the account was created but signing in failed, or the records
              could not be adopted: AccountCreationError. The guest
              workspace is exactly as before (still guest, still visible),
              and nobody is left signed in to an empty account.
        """
        profile = self.register(email, password, username=username, display_name=display_name)
        try:
            account = self.sign_in(email, password)
        except (TransportError, AuthenticationError, ProtocolError, RuntimeError) as error:
            raise AccountCreationError(
                "The account was created, but signing in to it did not succeed. Everything on this device is "
                "unchanged; sign in to finish.", stage="sign_in", cause=error) from error
        try:
            with self._sync_lock:
                preview = self._engine.association_preview(account)
                adopted = self._engine.associate_local_data(account, confirmation=preview.token)
        except Exception as error:  # noqa: BLE001 - whatever stopped it, the guest workspace must stay in use
            self.sign_out()
            raise AccountCreationError(
                "The account was created, but the records on this device could not be added to it. They are "
                "unchanged; sign in and add them from the Account page.", stage="adoption", cause=error) from error
        self.wake()
        return AccountCreation(account=self._engine.store.account(account.account_key), profile=profile,
                               adopted={kind: count for kind, count in adopted.items() if count})

    def request_password_recovery(self, identifier: str) -> dict:
        """Ask the backend to send a recovery link (no session needed; the answer never says if the account exists)."""
        transport = self._require_transport()
        return self._reaching(lambda: transport.request_recovery(identifier))

    def reset_password(self, token: str, new_password: str) -> dict:
        """
        Set a new password with a recovery token. Every session of the account
        ends (this device's too, on its next request): sign in again with the
        new password; pending local changes and conflicts stay and resume then.
        """
        transport = self._require_transport()
        return self._reaching(lambda: transport.reset_password(token, new_password))

    def profile(self) -> dict:
        transport, token = self._require_session()
        return self._authorized(token, lambda: transport.profile(token))

    def update_profile(self, base_version: int, display_name: str | None) -> dict:
        transport, token = self._require_session()
        return self._authorized(token, lambda: transport.update_profile(token, base_version, display_name))

    def check_connectivity(self) -> bool:
        """Probe the backend's liveness endpoint; records and returns whether it answered."""
        transport = self._require_transport()
        try:
            self._reaching(transport.health)
        except (TransportError, ProtocolError, AuthenticationError):
            return False
        return True

    def _require_transport(self) -> SyncTransport:
        if self._transport is None:
            raise RuntimeError("No backend is configured.")
        return self._transport

    def _require_session(self) -> tuple[SyncTransport, str]:
        with self._state_lock:
            transport, token = self._transport, self._token
        if transport is None or token is None:
            raise RuntimeError("Sign in to an account first.")
        return transport, token

    def _reaching(self, call):
        """Run a network call, recording whether the backend was reachable."""
        try:
            result = call()
        except TransportError:
            self._reachable(False)
            raise
        except (AuthenticationError, ProtocolError):
            self._reachable(True)
            raise
        self._reachable(True)
        return result

    def _reachable(self, reachable: bool) -> None:
        self.backend_reachable, self.backend_checked_at = reachable, self._clock().isoformat()

    def _authorized(self, token: str, call):
        try:
            return self._reaching(call)
        except AuthenticationError:
            self._drop_token(token)
            raise

    def _drop_token(self, token: str) -> None:
        with self._state_lock:
            if self._token == token:  # never the token of an account signed in since
                self._token = None

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

    def conflict_actions(self, conflict: Conflict) -> dict[str, str | None]:
        """{choice: None if allowed, else the reason it is not} for an open conflict."""
        return self._engine.allowed_resolutions(conflict)

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
                features = self._features(token)
                recurrence = RECURRENCE_FEATURE in features
                manual = MANUAL_PLACEMENTS_FEATURE in features
                modes = SCHEDULING_MODES_FEATURE in features
                types = TASK_TYPES_FEATURE in features
                details = PROJECT_DETAILS_FEATURE in features
                for _ in range(MAX_PUSH_ROUNDS):
                    self._engine.prepare(account, recurrence=recurrence, manual_placements=manual,
                                         scheduling_modes=modes, task_types=types, project_details=details)
                    batch = self._engine.next_batch(account, self._push_batch_size)
                    if not batch:
                        break
                    outcome = self._push(token, account, batch)  # no transaction open
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
                self._reachable(False)
                return self._finish(SyncReport("offline", pushed, pulled, conflicts, str(error)))
            except AuthenticationError as error:
                self._drop_token(token)
                self._reachable(True)
                return self._finish(SyncReport("auth_required", pushed, pulled, conflicts, str(error)))
            except ProtocolError as error:
                if batch:
                    self._engine.store.block_ops([op.op_id for op in batch], str(error))
                self.consecutive_failures += 1
                self._reachable(True)
                return self._finish(SyncReport("error", pushed, pulled, conflicts, str(error)))
            self.consecutive_failures = 0
            self._reachable(True)
            self._engine.store.set_last_synced(key, self._clock().isoformat())
            held = self._engine.held
            message = (f"{held} record(s) wait here: the server does not support them yet (recurring series, "
                       "the newer scheduling modes or task types; update the server).") if held else ""
            return self._finish(SyncReport("ok", pushed, pulled, conflicts, message, held=held))

    def _push(self, token: str, account, batch: list):
        """
        Send `batch` and record the answers. A request the server refuses as a
        whole (ProtocolError: malformed or unsupported, never an
        authentication failure) is narrowed unit by unit (a group is one
        unit) -- the same op_ids, so anything already applied answers from
        the server's record -- until the refused units are found; those
        become actionable push_rejected conflicts (SyncEngine.refuse) instead
        of being retried forever or holding everything behind them.
        Connection and authentication failures propagate unchanged.
        """
        try:
            results = self._transport.push(token, [op.wire() for op in batch])
        except ProtocolError as error:
            units = _push_units(batch)
            if len(units) == 1:
                return self._engine.refuse(account, batch, str(error))
            total = PushOutcome(sent=len(batch))
            for unit in units:
                part = self._push(token, account, unit)
                total.applied += part.applied
                total.conflicts += part.conflicts
            return total
        return self._engine.acknowledge(account, batch, results)

    def _features(self, token: str) -> frozenset[str]:
        """The server's sync features, asked once per transport and token (no answer method: none)."""
        transport = self._transport
        cached = self._capabilities
        if cached is not None and cached[0] is transport and cached[1] == token:
            return cached[2]
        ask = getattr(transport, "capabilities", None)
        features = frozenset(ask(token).get("features", ())) if ask is not None else frozenset()
        self._capabilities = (transport, token, features)
        return features

    def reset_task_data(self) -> dict[str, int]:
        """
        Settings' "Reset All Task Data" for the workspace this device works in:

        - an account workspace: the server's reset first (POST
          /me/task-data/reset, one transaction there); only when it answers,
          this device's copy of the account's task data and its sync
          bookkeeping are deleted and the pull cursor moves past the reset
          (app/planning/task_data_reset.py). Held under the sync lock, so no
          push or pull runs in between. Not signed in, unreachable or
          refused: the error is raised and NOTHING local changes;
        - the ownerless local workspace (no account): only local data exists,
          and it is deleted.

        Returns the number of local rows removed per record type.
        """
        from app.planning.task_data_reset import wipe_task_data

        account = self.workspace_account()
        if account is None:
            return wipe_task_data(self._connection, OwnerScope.ownerless())
        with self._state_lock:
            transport, token, key = self._transport, self._token, self._account_key
        if transport is None or token is None or key != account.account_key:
            raise RuntimeError("Sign in to your account first: its task data is also stored on the server.")
        with self._sync_lock:
            result = self._authorized(token, lambda: transport.reset_task_data(token))
            return wipe_task_data(self._connection, OwnerScope.account(uuid.UUID(account.user_id)),
                                  account_key=account.account_key, cursor=int(result["cursor"]))

    def status(self) -> SyncStatus:
        with self._state_lock:
            transport, token, key = self._transport, self._token, self._account_key
        store = self._engine.store
        account = store.account(key) if key else None
        durable = account or store.active_account()
        report = self.last_report
        return SyncStatus(
            configured=transport is not None,
            backend_url=transport.base_url if transport is not None else None,
            backend_reachable=self.backend_reachable, backend_checked_at=self.backend_checked_at,
            signed_in=token is not None, auth_required=account is not None and token is None, account=account,
            in_progress=self._sync_lock.locked(),
            pending=store.pending_count(durable.account_key, durable.user_id) if durable else None,
            conflicts=store.conflict_count(durable.account_key, "open") if durable else 0,
            last_successful_sync_at=durable.last_synced_at if durable else None,
            last_report=report,
            last_error=report.message if report.status in ("offline", "auth_required", "error") else None,
            workspace_account=durable,
        )

    def _pending_now(self) -> int | None:
        """The signed-in account's pending records right now (a local count; None: nobody is signed in)."""
        with self._state_lock:
            token, key = self._token, self._account_key
        if self._transport is None or token is None or key is None:
            return None
        account = self._engine.store.account(key)
        return self._engine.store.pending_count(account.account_key, account.user_id) if account else None

    def _finish(self, report: SyncReport) -> SyncReport:
        self.last_report = report
        try:
            self._pending_seen = self._pending_now()
        except Exception:  # noqa: BLE001 - only a hint for the idle loop
            self._pending_seen = None
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
            self._idle(self.next_delay())

    def _idle(self, delay: float) -> None:
        """
        Wait for the next run: `delay` seconds, a wake(), or -- only while the
        last sync succeeded -- new pending work (the durable count differs
        from what that sync left). Held or conflicting records that every
        sync leaves pending therefore never cause a run of their own.
        """
        remaining = delay
        while remaining > 0 and not self._stop.is_set():
            step = min(remaining, self._pending_poll) if self._pending_poll > 0 else remaining
            if self._wake.wait(step):
                break
            remaining -= step
            if self.consecutive_failures == 0 and remaining > 0:
                try:
                    pending = self._pending_now()
                except Exception:  # noqa: BLE001 - e.g. the database is closing; the timer still applies
                    pending = None
                if pending is not None and pending != self._pending_seen:
                    break
        self._wake.clear()

    def stop(self, timeout: float = 10.0) -> bool:
        """
        Stop the loop and wait up to `timeout` for a running sync to finish. True
        if it has stopped. Safe to call again (e.g. polling with timeout=0): it
        keeps answering False until the loop thread has really ended.
        """
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread or self._stopping, None
        if thread is None:
            return True
        thread.join(timeout)
        self._stopping = thread if thread.is_alive() else None
        return self._stopping is None
