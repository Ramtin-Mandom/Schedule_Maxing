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
when the backend answers 401 and the session cannot be renewed (status
"auth_required"). Passwords are never stored or logged.

Session renewal: a login also returns a single-use refresh credential. It is
held in memory, so an expired access token is renewed once without asking
for the password. With sign_in(..., keep=True) -- "Keep me signed in" -- and
a vault (app/sync/credentials.py: the operating system's credential store)
the current refresh credential is also kept there, replaced on every
renewal, and restore_session() signs the device's active account in again
after a restart (the background loop does it on its first run, and retries
while offline). It stays until sign_out(), which removes it and revokes the
session on the server, or until the server ends the session (password
reset, expiry). Renewals are serialized: a refresh credential is never sent
twice.

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
records -- a local query, never a request, and skipped when nothing was
written since the last look -- and runs early when it changed since the
last sync; a local commit is looked at `edit_debounce` seconds after the
last one, so an edit is uploaded within seconds without any manual action.
After a failure only the backoff decides the next attempt. The desktop
reports its window's activity (set_activity): while it is not focused the
interval is `background_interval`, while minimized the pending count is not
looked at, and regaining focus syncs at once when the last success is older
than `interval` (ADR in DECISIONS.md). Sync now is never delayed.

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
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from app.execution.db import data_version, transaction_state_for
from app.planning.scope import OwnerScope
from app.sync.engine import AssociationPreview, PushOutcome, SyncEngine
from app.sync.store import BACKEND_URL_SETTING, Account, Conflict
from app.sync.credentials import CredentialVault
from app.sync.transport import MfaRequired  # noqa: F401 - re-exported for callers
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
#: A task's kind (flexible / todo) and preferred third of the day, and a fixed block's points.
TASK_KINDS_FEATURE = "task_kinds"
#: Budget balances, entries and occurrence edits (app/budget).
BUDGET_FEATURE = "budget"


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


@dataclass(frozen=True)
class ConnectionCheck:
    """One stage of SyncService.diagnose_connection."""

    #: api | database | session | sync
    stage: str
    #: ok | failed | skipped
    state: str
    message: str


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
        background_interval: float = 300.0,
        pending_poll: float = 5.0,
        edit_debounce: float = 2.5,
        backoff_base: float = 5.0,
        backoff_max: float = 600.0,
        push_batch_size: int = 100,
        pull_page_size: int = 200,
        vault: CredentialVault | None = None,
    ) -> None:
        self._engine = SyncEngine(connection, clock)
        self._connection = connection
        self._transport = transport
        self._interval = interval
        self._background_interval = max(interval, background_interval)
        self._pending_poll = pending_poll
        self._edit_debounce = edit_debounce
        #: The pending count seen when the last sync ended (None: none ended yet): a different count is new work.
        self._pending_seen: int | None = None
        #: ((data version (app/execution/db.data_version), account key), count) of the last pending count.
        self._pending_counted: tuple[tuple[int, str], int | None] | None = None
        #: focused | unfocused | minimized (set_activity, from the desktop's Tk thread).
        self._activity = "focused"
        #: time.monotonic() of the last successful sync (None: none in this session).
        self._last_success: float | None = None
        #: time.monotonic() of the last commit seen by the listener (None: none since the last sync).
        self._edited_at: float | None = None
        #: Set by wake(), set_activity() and local commits: the idle loop looks again at once.
        self._nudge = threading.Event()
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._push_batch_size = push_batch_size
        self._pull_page_size = pull_page_size
        self._token: str | None = None
        self._account_key: str | None = None
        #: The session's current refresh credential (memory; also in the vault while `_keep`).
        self._refresh_token: str | None = None
        #: A passed password step waiting for its second step: (transport, mfa_token, keep). Memory only.
        self._mfa_pending: tuple | None = None
        #: The access token the last renewal replaced: a second caller holding it gets the new one, not a refresh.
        self._replaced_token: str | None = None
        self._vault = vault
        self._keep = False
        #: Whether the vault may still hold a session to restore (asked once; again only after a network failure).
        self._restorable = vault is not None
        self._renew_lock = threading.Lock()
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
            self._end_session()
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

    @property
    def can_keep_signed_in(self) -> bool:
        """Whether this computer has a credential store for "Keep me signed in"."""
        # A LazyVault (app/sync/credentials.py) finds the store on first use; others are there or not.
        return self._vault is not None and getattr(self._vault, "available", True)

    def prefetch_credentials(self) -> None:
        """
        Find the credential store now, in the background (a LazyVault), so no later step waits for it.
        Without a backend nobody can sign in, so nothing is looked up (and no thread is started).
        """
        if self._transport is None:
            return
        prefetch = getattr(self._vault, "prefetch", None)
        if prefetch is not None:
            prefetch()

    def sign_in(self, email: str, password: str, *, keep: bool = False) -> Account:
        """keep: also keep the session in the system credential store, so a restart signs in again by itself."""
        transport = self._transport
        if transport is None:
            raise RuntimeError("No backend is configured (set SCHEDULE_MAXING_BACKEND_URL).")
        try:
            result = self._reaching(lambda: transport.login(email, password))  # the network call holds no lock
        except MfaRequired as challenge:
            # Two-step sign-in: nobody is signed in yet. The short-lived challenge stays in memory only.
            self._mfa_pending = (transport, challenge.mfa_token, keep)
            raise
        return self._establish(transport, result, keep)

    def complete_sign_in(self, code: str) -> Account:
        """
        The second step after sign_in() raised MfaRequired: the authenticator's current code, or a recovery
        code. A wrong code leaves the step open to try again until the challenge expires (a few minutes).
        """
        pending = self._mfa_pending
        if pending is None:
            raise RuntimeError("Sign in with your email and password first.")
        transport, mfa_token, keep = pending
        result = self._reaching(lambda: transport.verify_mfa(mfa_token, code))
        self._mfa_pending = None
        return self._establish(transport, result, keep)

    def _establish(self, transport, result, keep: bool) -> Account:
        store = self._engine.store
        with self._sync_lock, self._state_lock:  # never switch accounts under a running sync
            if transport is not self._transport:
                raise RuntimeError("The backend was changed while signing in; sign in again.")
            account = store.upsert_account(transport.base_url, result.user_id, result.email)
            # Active from now until an explicit sign-out: the account's workspace stays in use offline and after
            # a restart (a lost connection or an ended session is not a sign-out). No existing record is claimed.
            store.set_active(account.account_key)
            self._token, self._account_key = result.token, account.account_key
            self._refresh_token = getattr(result, "refresh_token", None)
            self._replaced_token, self._keep, self._restorable = None, False, False
            if self._vault is not None:
                if keep and self._refresh_token:
                    self._keep = self._vault.save(account.account_key, self._refresh_token)
                else:
                    self._vault.delete(account.account_key)  # an earlier kept session of this account ends here
            self.last_report = SyncReport("inert")
        return store.account(account.account_key)

    def sign_out(self) -> None:
        """Forget the session here (and the kept one), then revoke it on the server when it can be reached."""
        with self._sync_lock, self._state_lock:
            transport, refresh = self._transport, self._refresh_token
            active = self._engine.store.active_account()
            if self._vault is not None and active is not None:
                refresh = refresh or self._vault.load(active.account_key)  # kept, but not restored yet (offline)
                self._vault.delete(active.account_key)
            self._end_session()
            self._engine.store.set_active(None)
        revoke = getattr(transport, "logout", None)
        if refresh and revoke is not None:
            try:
                revoke(refresh)
            except (TransportError, AuthenticationError, ProtocolError):
                pass  # unreachable or already ended: the credential is gone from this device either way

    def _end_session(self) -> None:
        """Drop the in-memory session (callers hold the state lock)."""
        self._token, self._account_key = None, None
        self._refresh_token, self._replaced_token, self._keep = None, None, False
        self._restorable = False

    def restore_session(self) -> bool:
        """
        Sign the device's active account in again from its kept session
        ("Keep me signed in"), without a password. True when it did. Nothing
        to restore, or a session the server ended, is False (and the kept
        credential is removed); TransportError means "try again later" -- the
        kept credential stays.
        """
        with self._renew_lock:
            with self._state_lock:
                transport = self._transport
                if self._vault is None or not self._restorable or transport is None or self._token is not None:
                    return False
            renew = getattr(transport, "refresh", None)
            account = self._engine.store.active_account()
            secret = None
            if renew is not None and account is not None and account.backend_url == transport.base_url:
                secret = self._vault.load(account.account_key)
            if secret is None:
                self._restorable = False
                return False
            try:
                pair = self._reaching(lambda: renew(secret))
            except (AuthenticationError, ProtocolError):
                self._vault.delete(account.account_key)
                self._restorable = False
                return False
            self._vault.save(account.account_key, pair.refresh_token)  # at once: the old one is spent
            self._restorable = False
            with self._state_lock:
                current = self._engine.store.active_account()
                if (self._transport is not transport or self._token is not None or current is None
                        or current.account_key != account.account_key):
                    return False  # signed out or in meanwhile: that decision stands
                self._token, self._account_key = pair.token, account.account_key
                self._refresh_token, self._replaced_token, self._keep = pair.refresh_token, None, True
            return True

    def _renew(self, token: str) -> str | None:
        """
        A new access token for the session whose `token` the server refused,
        from its refresh credential; None when the session cannot be renewed
        (the caller then drops the token). TransportError: nothing changed.
        """
        with self._renew_lock:
            with self._state_lock:
                if self._token != token:  # another thread renewed it meanwhile, or the session changed
                    return self._token if self._replaced_token == token else None
                transport, refresh, key, keep = self._transport, self._refresh_token, self._account_key, self._keep
            renew = getattr(transport, "refresh", None)
            if refresh is None or renew is None or key is None:
                return None
            try:
                pair = self._reaching(lambda: renew(refresh))
            except (AuthenticationError, ProtocolError):
                with self._state_lock:
                    if self._token == token:
                        self._refresh_token = None
                        if keep and self._vault is not None:
                            self._vault.delete(key)
                return None
            if keep and self._vault is not None:
                self._vault.save(key, pair.refresh_token)  # at once: the old one is spent
            with self._state_lock:
                if self._token != token:
                    return None
                self._token, self._refresh_token, self._replaced_token = pair.token, pair.refresh_token, token
            return pair.token

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

    def request_registration(self, email: str) -> dict:
        """
        Ask the backend to email a verification code to `email`. Nothing is created and no password is sent;
        the answer is the same whether or not the address already has an account.
        """
        transport = self._require_transport()
        return self._reaching(lambda: transport.request_registration(email))

    def request_email_verification(self) -> dict:
        """Email a code to the signed-in account's own address (for an account whose address was never confirmed)."""
        transport, token = self._require_session()
        return self._authorized(token, transport.request_email_verification)

    def confirm_email_verification(self, code: str) -> dict:
        transport, token = self._require_session()
        return self._authorized(token, lambda fresh: transport.confirm_email_verification(fresh, code))

    def confirm_registration(self, code: str, password: str, *, username: str | None = None,
                             display_name: str | None = None) -> dict:
        """Create the account a verification code was emailed for, with this password; its profile. No sign-in."""
        transport = self._require_transport()
        return self._reaching(lambda: transport.confirm_registration(code, password, username, display_name))

    def create_account(self, code: str, password: str, *, username: str | None = None,
                       display_name: str | None = None, keep: bool = False) -> AccountCreation:
        """
        Create the account of a verified email address and make this device's
        guest workspace its own: confirm the emailed `code` with the password
        chosen now (the server creates the account then, for the address the
        code was mailed to), sign in, then adopt every ownerless record for the
        account in ONE local transaction (the same ids, relationships,
        schedules, preferences and history; each record is queued for upload
        by the change capture in that transaction). The next sync uploads
        them; it is idempotent, so a retry after a lost answer never creates
        a duplicate.

        Failure never loses or half-assigns anything:
            - the code or password refused, or the backend unreachable: the
              error is raised as it is; nothing local changed and nobody is
              signed in (the code stays usable unless it was the problem);
            - the account was created but signing in failed, or the records
              could not be adopted: AccountCreationError. The guest
              workspace is exactly as before (still guest, still visible),
              and nobody is left signed in to an empty account.
        """
        profile = self.confirm_registration(code, password, username=username, display_name=display_name)
        try:
            account = self.sign_in(profile["email"], password, keep=keep)
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
        return self._authorized(token, transport.profile)

    def account_request(self, method: str, path: str, body: dict | None = None) -> dict:
        """One authenticated request to an account-security route of the backend (docs/account-security.md)."""
        transport, token = self._require_session()
        return self._authorized(token, lambda fresh: transport.account_request(method, path, fresh, body))

    def change_password(self, current_password: str, new_password: str) -> Account:
        """
        Change the account's password. The server ends every session -- this device's too -- so this signs
        in again with the new password (keeping "Keep me signed in" as it was). MfaRequired, as from sign_in(),
        when the account has two-step sign-in: finish with complete_sign_in(). Local records are untouched.
        """
        account = self._require_account()
        keep = self._keep
        self.account_request("POST", "/auth/password/change",
                             {"current_password": current_password, "new_password": new_password})
        return self.sign_in(account.email, new_password, keep=keep)

    def update_profile(self, base_version: int, display_name: str | None) -> dict:
        transport, token = self._require_session()
        return self._authorized(token, lambda fresh: transport.update_profile(fresh, base_version, display_name))

    def check_connectivity(self) -> bool:
        """Probe the backend's liveness endpoint; records and returns whether it answered."""
        transport = self._require_transport()
        try:
            self._reaching(transport.health)
        except (TransportError, ProtocolError, AuthenticationError):
            return False
        return True

    def diagnose_connection(self) -> list[ConnectionCheck]:
        """
        Check each stage between this device and its records on the server,
        separately: the API answers (GET /health), the API reaches its migrated
        database (GET /ready), the server accepts this device's session (an
        authenticated request), and how the last synchronization ended. It
        stops at the first stage nothing later can pass without. Nothing is
        written and no synchronization runs.
        """
        transport = self._require_transport()
        try:
            self._reaching(transport.health)
        except (TransportError, ProtocolError, AuthenticationError) as error:
            return [ConnectionCheck("api", "failed", str(error))]
        checks = [ConnectionCheck("api", "ok", f"{transport.base_url} answered.")]

        ready = getattr(transport, "ready", None)
        try:
            if ready is None:
                raise ProtocolError("no readiness check", status=404)
            ready()
            checks.append(ConnectionCheck("database", "ok", "The server reaches its database."))
        except TransportError as error:
            if error.body.get("migrations") == "pending":
                message = "The server reaches its database, but the database has not been migrated to its version."
            elif error.status == 503:
                message = "The server is running but cannot reach its database."
            else:
                message = str(error)
            return [*checks, ConnectionCheck("database", "failed", message)]
        except (ProtocolError, AuthenticationError) as error:
            if getattr(error, "status", None) != 404:
                return [*checks, ConnectionCheck("database", "failed", str(error))]
            checks.append(ConnectionCheck("database", "skipped", "This server does not report its database state."))

        with self._state_lock:
            token = self._token
        if token is None:
            return [*checks, ConnectionCheck("session", "skipped", "Not signed in, so the account and "
                                                                   "synchronization were not checked.")]
        try:
            self._authorized(token, transport.capabilities)
        except AuthenticationError:
            return [*checks, ConnectionCheck("session", "failed", "The server no longer accepts this device's "
                                                                  "session. Sign in again.")]
        except (TransportError, ProtocolError) as error:
            return [*checks, ConnectionCheck("session", "failed", str(error))]
        checks.append(ConnectionCheck("session", "ok", "The server accepted this device's session."))

        report = self.last_report
        if report.status in ("offline", "auth_required", "error"):
            checks.append(ConnectionCheck("sync", "failed", f"The last synchronization failed: {report.message} "
                                                            "Use Sync now to try again."))
        else:
            checks.append(ConnectionCheck("sync", "ok", "The last synchronization reported no problem."))
        return checks

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
        """Run call(token); a refused token is renewed once from the session's refresh credential."""
        try:
            return self._reaching(lambda: call(token))
        except AuthenticationError:
            renewed = self._renew(token)
            if renewed is None:
                self._drop_token(token)
                raise
        try:
            return self._reaching(lambda: call(renewed))
        except AuthenticationError:
            self._drop_token(renewed)
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

    def sync_now(self, *, _renewed: bool = False) -> SyncReport:
        with self._state_lock:
            token, key = self._token, self._account_key
        if token is None and self._restorable:
            try:
                self.restore_session()  # "Keep me signed in": the first run after a start
            except TransportError as error:
                self.consecutive_failures += 1
                return self._finish(SyncReport("offline", message=str(error)))
            with self._state_lock:
                token, key = self._token, self._account_key
        if self._transport is None or token is None or key is None:
            return self._finish(SyncReport("inert", message="No backend configured or no account signed in."))
        with self._sync_lock:
            account = self._engine.store.account(key)
            pushed = pulled = conflicts = 0
            batch: list = []
            renewed = False
            try:
                features = self._features(token)
                recurrence = RECURRENCE_FEATURE in features
                manual = MANUAL_PLACEMENTS_FEATURE in features
                modes = SCHEDULING_MODES_FEATURE in features
                types = TASK_TYPES_FEATURE in features
                details = PROJECT_DETAILS_FEATURE in features
                kinds = TASK_KINDS_FEATURE in features
                budget = BUDGET_FEATURE in features
                for _ in range(MAX_PUSH_ROUNDS):
                    self._engine.prepare(account, recurrence=recurrence, manual_placements=manual,
                                         scheduling_modes=modes, task_types=types, project_details=details,
                                         task_kinds=kinds, budget=budget)
                    batch = self._engine.next_batch(account, self._push_batch_size)
                    if not batch:
                        break
                    outcome = self._push(token, account, batch)  # no transaction open
                    pushed += outcome.applied
                    conflicts += outcome.conflicts
                batch = []
                if budget:
                    self._engine.pull_budget_history(account)
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
                try:
                    renewed = not _renewed and self._renew(token) is not None
                except TransportError as offline:
                    self.consecutive_failures += 1
                    return self._finish(SyncReport("offline", pushed, pulled, conflicts, str(offline)))
                if not renewed:
                    self._drop_token(token)
                    self._reachable(True)
                    return self._finish(SyncReport("auth_required", pushed, pulled, conflicts, str(error)))
            except ProtocolError as error:
                if batch:
                    self._engine.store.block_ops([op.op_id for op in batch], str(error))
                self.consecutive_failures += 1
                self._reachable(True)
                return self._finish(SyncReport("error", pushed, pulled, conflicts, str(error)))
            if not renewed:
                self.consecutive_failures = 0
                self._reachable(True)
                self._engine.store.set_last_synced(key, self._clock().isoformat())
                held = self._engine.held
                message = (f"{held} record(s) wait here: the server does not support them yet (recurring series, "
                           "the newer scheduling modes, task types or budget records; update the server)."
                           ) if held else ""
                return self._finish(SyncReport("ok", pushed, pulled, conflicts, message, held=held))
        return self.sync_now(_renewed=True)  # the session was renewed: once more with the new token

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
            result = self._authorized(token, transport.reset_task_data)
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
        """
        The signed-in account's pending records right now (a local count;
        None: nobody is signed in). The count is one indexed query, and it is
        not even run when nothing was written since the last one (data_version).
        """
        with self._state_lock:
            token, key = self._token, self._account_key
        if self._transport is None or token is None or key is None:
            return None
        version = data_version(self._connection)
        counted = self._pending_counted
        if version is not None and counted is not None and counted[0] == (version, key):
            return counted[1]
        account = self._engine.store.account(key)
        pending = self._engine.store.pending_count(account.account_key, account.user_id) if account else None
        if version is not None:
            self._pending_counted = ((version, key), pending)
        return pending

    def _finish(self, report: SyncReport) -> SyncReport:
        self.last_report = report
        if report.status == "ok":
            self._last_success = time.monotonic()
        try:
            self._pending_seen = self._pending_now()
        except Exception:  # noqa: BLE001 - only a hint for the idle loop
            self._pending_seen = None
        if report.status not in ("ok", "inert"):
            logger.info("Synchronization did not complete: %s", report.status)
        return report

    def next_delay(self) -> float:
        """
        Seconds until the next background run: the interval for the current
        activity (set_activity: `interval` while the window is focused,
        `background_interval` otherwise), or bounded exponential backoff
        after failures (unchanged by the activity).
        """
        if self.consecutive_failures == 0:
            return self._interval if self._activity == "focused" else self._background_interval
        return min(self._backoff_max, self._backoff_base * (2 ** (self.consecutive_failures - 1)))

    # ------------------------------------------------------------------
    # Activity (the desktop window: app/ui/activity.py)
    # ------------------------------------------------------------------

    ACTIVITIES = ("focused", "unfocused", "minimized")

    @property
    def activity(self) -> str:
        return self._activity

    def set_activity(self, activity: str) -> None:
        """
        How the desktop window is used now: "focused", "unfocused" (visible,
        another application in front) or "minimized". Thread-safe; the
        background loop re-plans its wait at once. Focused uses `interval`,
        the others `background_interval`; minimized also stops looking at the
        pending count. Gaining focus syncs now when the last successful sync
        is older than `interval` (or there was none yet). Nothing here delays
        Sync now, and pending records are always pushed by the next run.
        """
        if activity not in self.ACTIVITIES:
            raise ValueError(f"unknown activity: {activity!r}")
        with self._state_lock:
            previous, self._activity = self._activity, activity
        if previous == activity:
            return
        if activity == "focused" and (self._last_success is None
                                      or time.monotonic() - self._last_success >= self._interval):
            self._wake.set()
        self._nudge.set()

    def _local_commit(self) -> None:
        """A commit on this connection (commit listener, any thread): maybe a local edit to push soon."""
        self._edited_at = time.monotonic()
        self._nudge.set()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def start(self, *, initial_delay: float = 0.0) -> None:
        """
        Run the background loop. initial_delay: wait this long before the first
        run (wake() or Sync now ends the wait), e.g. so that a desktop's first
        sync -- and the credential-store lookup it may need -- does not compete
        with building the window.
        """
        if self._thread is not None or self._transport is None:
            return
        self._stop.clear()
        state = transaction_state_for(self._connection)
        if state is not None:  # a local edit is pushed edit_debounce seconds after it, not at the next poll
            state.add_commit_listener(self._local_commit)
        self._thread = threading.Thread(target=self._run, args=(initial_delay,), name="schedule-maxing-sync",
                                        daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()
        self._nudge.set()

    def _run(self, initial_delay: float = 0.0) -> None:
        if initial_delay > 0:
            self._idle(initial_delay)
        while not self._stop.is_set():
            self._edited_at = None  # commits made by this run (or before it) are its own
            try:
                self.sync_now()
            except Exception:  # noqa: BLE001 - the loop must survive; details are logged, not raised into Tk
                logger.exception("Unexpected synchronization failure")
                self.consecutive_failures += 1
            self._idle(self.next_delay(), replan=True)

    def _idle(self, delay: float, *, replan: bool = False) -> None:
        """
        Wait for the next run: `delay` seconds, a wake(), or -- only while the
        last sync succeeded and the window is not minimized -- new pending
        work: the durable count differs from what that sync left. It is
        looked at every `pending_poll` seconds, and after a local commit once
        no further commit came for `edit_debounce` seconds (so a burst of
        edits is pushed together, a few seconds after the last one). Held or
        conflicting records that every sync leaves pending therefore never
        cause a run of their own. replan (the loop's own wait): a change of
        activity changes the wait to that activity's interval, counted from
        the start of the wait; a backoff is never shortened or lengthened.
        """
        started = time.monotonic()
        failing = self.consecutive_failures > 0
        deadline = started + delay
        planned_for = self._activity
        next_look = started + self._pending_poll if self._pending_poll > 0 else None
        while not self._stop.is_set():
            looking = not failing and self._activity != "minimized"
            edited_at = self._edited_at if looking else None
            due = [deadline]
            if looking and next_look is not None:
                due.append(next_look)
            if edited_at is not None:
                due.append(edited_at + self._edit_debounce)
            wait = min(due) - time.monotonic()
            if wait > 0 and self._nudge.wait(wait):
                self._nudge.clear()
                if self._wake.is_set():
                    break
                if replan and not failing and self._activity != planned_for:
                    planned_for = self._activity
                    deadline = started + self.next_delay()
                continue
            now = time.monotonic()
            if now >= deadline:
                break
            if not looking:
                continue
            settled = edited_at is not None and now >= edited_at + self._edit_debounce
            polled = next_look is not None and now >= next_look
            if polled:
                next_look = now + self._pending_poll
            # A poll right after an edit waits for the edit's debounce instead (one push for a burst of edits).
            if settled or (polled and edited_at is None):
                if settled and self._edited_at == edited_at:  # (a newer commit keeps its own debounce)
                    self._edited_at = None
                try:
                    pending = self._pending_now()
                except Exception:  # noqa: BLE001 - e.g. the database is closing; the timer still applies
                    pending = None
                if pending is not None and pending != self._pending_seen:
                    break
        self._wake.clear()
        self._nudge.clear()

    def stop(self, timeout: float = 10.0) -> bool:
        """
        Stop the loop and wait up to `timeout` for a running sync to finish. True
        if it has stopped. Safe to call again (e.g. polling with timeout=0): it
        keeps answering False until the loop thread has really ended.
        """
        state = transaction_state_for(self._connection)
        if state is not None:
            state.remove_commit_listener(self._local_commit)
        self._stop.set()
        self._wake.set()
        self._nudge.set()
        thread, self._thread = self._thread or self._stopping, None
        if thread is None:
            return True
        thread.join(timeout)
        self._stopping = thread if thread.is_alive() else None
        return self._stopping is None
