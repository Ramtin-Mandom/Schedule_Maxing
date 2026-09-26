"""
app/ui/account_controller.py

The desktop's account, connection and synchronization boundary (Milestone 4,
Prompt 2). Tk-free: every method returns a ControllerResult and is safe to
call from a background worker (network calls must run there). It only
delegates -- to SyncService (app/sync/service.py: sign-in, association,
push/pull, backoff, conflicts) and its transport (HttpTransport, standard
library only) -- and never talks to the optional local web service or holds
a database password. Widgets call this, never the sync engine.

What it adds is what a screen needs and the services do not say themselves:

- field validation before anything is sent (the server remains the
  authority, e.g. for the password rules it also enforces);
- one readable, actionable message per failure (wrong credentials,
  unreachable backend, session ended, preview out of date, ...), with the
  structured error kept as ControllerResult.cause;
- a ConnectionView that distinguishes "no backend configured", "signed out",
  "signed in", "session ended" and "backend unreachable", with pending
  changes, conflicts and the last successful sync taken from the durable
  outbox/store (they survive a restart);
- conflict views that compare the local and the server version field by
  field and say why an unavailable resolution is unavailable.

Credentials: a password is passed straight to SyncService.sign_in/register
and never kept; the access token stays in SyncService's memory. Signing out
only forgets that session on this device -- the backend has no token
revocation, so the token simply expires there.

Workspace: signing in or out, associating local records and switching
backends change which records the desktop works on. The caller then calls
AppServices.switch_workspace() (the app does this on the Tk thread and
rebuilds its pages), so no view keeps showing the previous account's data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.sync.engine import AssociationError, AssociationPreview, ConflictResolutionError
from app.sync.service import SyncReport, SyncService, SyncStatus
from app.sync.store import Conflict
from app.sync.transport import AuthenticationError, HttpTransport, ProtocolError, SyncTransport, TransportError
from app.ui.background import ControllerResult

#: The backend's own rules (backend/security.py), checked here too so a typo never needs a round trip.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024
MAX_DISPLAY_NAME_LENGTH = 200
DEFAULT_PLAN = "Normal"


def masked_email(email: str | None) -> str:
    if not email or "@" not in email:
        return "Email unavailable"
    local, domain = email.rsplit("@", 1)
    return f"{local[:1]}***@{domain[:1]}***"


def profile_summary(profile: dict) -> str:
    """Only server-provided identity; a centralized plan fallback until plans exist."""
    name = profile.get("display_name") or "No display name set"
    plan = profile.get("plan") or DEFAULT_PLAN
    return f"{name}\n{masked_email(profile.get('email'))}\nPlan: {plan}"


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

ENTITY_LABELS = {
    "project": "Project", "task": "Task", "fixed_block": "Fixed block", "placement": "Scheduled placement",
    "preference": "Preferences", "generation": "Schedule record", "execution": "Execution",
}
CONFLICT_KINDS = {
    "push_conflict": "Changed on this device and on the server",
    "push_rejected": "The server refused this device's change",
    "pull_conflict": "Changed on the server while this device had unsent changes",
}
RESOLUTION_LABELS = {"keep_local": "Keep this device's version", "accept_remote": "Use the server's version"}
#: Fields that identify or describe a record rather than its content.
_META_FIELDS = ("id", "user_id", "version", "created_at", "updated_at", "deleted_at")


class InvalidInput(ValueError):
    """Field errors found before anything was sent: {field: message}."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def validate_credentials(email: str, password: str, *, registering: bool = False,
                         display_name: str | None = None) -> dict[str, str]:
    """{field: message} for every problem (empty when the fields may be sent)."""
    errors: dict[str, str] = {}
    email = email.strip()
    if not email:
        errors["email"] = "Enter your email address."
    elif not _EMAIL.match(email) or len(email) > 320:
        errors["email"] = "Enter a valid email address, like name@example.com."
    if not password:
        errors["password"] = "Enter your password."
    elif registering and len(password) < MIN_PASSWORD_LENGTH:
        errors["password"] = f"Use at least {MIN_PASSWORD_LENGTH} characters."
    elif len(password) > MAX_PASSWORD_LENGTH:
        errors["password"] = f"Use at most {MAX_PASSWORD_LENGTH} characters."
    if display_name is not None and len(display_name.strip()) > MAX_DISPLAY_NAME_LENGTH:
        errors["display_name"] = f"Use at most {MAX_DISPLAY_NAME_LENGTH} characters."
    return errors


def friendly_error(error: BaseException, *, signing_in: bool = False) -> str:
    """One readable, actionable sentence for a failure of an account or sync operation."""
    if isinstance(error, InvalidInput):
        return "Check the highlighted fields: " + str(error)
    if isinstance(error, AuthenticationError):
        if signing_in:
            return "The email or password is incorrect."
        return "Your session with the backend ended. Sign in again to keep synchronizing."
    if isinstance(error, TransportError):
        return f"The backend could not be reached ({error}). Your work is saved on this device; try again later."
    if isinstance(error, ProtocolError):
        body = error.body or {}
        message = body.get("message") or str(error)
        if error.code == "account_exists":
            return "An account with this email already exists. Sign in instead."
        return f"The backend refused the request: {message}"
    if isinstance(error, AssociationError):
        if error.code == "preview_changed":
            return "Your local records changed since this preview. Review the updated preview, then confirm again."
        return f"These local records cannot be associated as they are: {error}"
    if isinstance(error, ConflictResolutionError):
        return str(error)
    if isinstance(error, ValueError):  # e.g. an invalid backend URL
        message = str(error)
        return message[:1].upper() + message[1:]
    if isinstance(error, RuntimeError):
        message = str(error)
        if message.startswith("No backend"):
            return "No backend is configured. Enter the backend address first."
        if message.startswith("Sign in"):
            return "Sign in to an account first."
        return message
    return f"Unexpected error: {error}"


def _relative(timestamp: str | None, now: datetime) -> str:
    if not timestamp:
        return "never"
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class ConnectionView:
    #: unconfigured | signed_out | signed_in | session_ended
    state: str
    backend_url: str | None
    #: True/False from the last request or probe; None if not known yet.
    reachable: bool | None
    signed_in_email: str | None
    #: The account whose records the desktop works on (selected or active on the device), if any.
    workspace_email: str | None
    in_progress: bool
    pending: int | None
    conflicts: int
    last_success: str | None
    last_success_text: str
    last_error: str | None
    #: One short line for the indicator, in words (never only a color).
    headline: str
    #: What to do next, if anything.
    detail: str

    @property
    def can_sync(self) -> bool:
        return self.state == "signed_in" and not self.in_progress


@dataclass(frozen=True)
class SignInResult:
    email: str
    #: Ownerless records on this device the association step could claim (signing in claims none).
    unassociated: int
    associated_before: bool


@dataclass(frozen=True)
class FieldDifference:
    field: str
    local: str
    remote: str


@dataclass(frozen=True)
class ConflictView:
    id: str
    entity_type: str
    #: e.g. 'Task "Essay"'.
    title: str
    kind: str
    created_at: str
    local_version: int | None
    #: The server version the local change was based on (what "changed on both sides" is measured against).
    base_version: int | None
    remote_version: int | None
    local_updated_at: str | None
    remote_updated_at: str | None
    remote_deleted: bool
    #: Every field whose value differs, with both values (the first rows are content, then metadata).
    differences: list[FieldDifference]
    #: {choice: None if available, else why not} -- only keep_local and accept_remote exist (no merge).
    choices: dict[str, str | None]
    server_message: str | None = None
    status: str = "open"
    resolution: dict | None = field(default=None, compare=False)


def _display(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(_display(item) for item in value) if value else "(none)"
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def conflict_view(conflict: Conflict, choices: dict[str, str | None]) -> ConflictView:
    local, remote = conflict.local_record or {}, conflict.remote_record or {}
    name = remote.get("name") or remote.get("label") or local.get("name") or local.get("label")
    title = ENTITY_LABELS.get(conflict.entity_type, conflict.entity_type.replace("_", " ").capitalize())
    if name:
        title += f' "{name}"'
    content = sorted(key for key in set(local) | set(remote) if key not in _META_FIELDS)
    differences = [FieldDifference(key.replace("_", " "), _display(local.get(key)), _display(remote.get(key)))
                   for key in content if local.get(key) != remote.get(key)]
    differences += [FieldDifference(key.replace("_", " "), _display(local.get(key)), _display(remote.get(key)))
                    for key in ("version", "updated_at", "deleted_at") if local.get(key) != remote.get(key)]
    error = conflict.error or {}
    return ConflictView(
        id=conflict.id, entity_type=conflict.entity_type, title=title,
        kind=CONFLICT_KINDS.get(conflict.kind, conflict.kind), created_at=conflict.created_at,
        local_version=local.get("version"), base_version=conflict.base_version, remote_version=remote.get("version"),
        local_updated_at=local.get("updated_at"), remote_updated_at=remote.get("updated_at"),
        remote_deleted=bool(remote.get("deleted_at")), differences=differences, choices=dict(choices),
        server_message=error.get("message") if isinstance(error, dict) else None, status=conflict.status,
        resolution=conflict.resolution,
    )


class AccountController:
    def __init__(
        self,
        sync_service: SyncService,
        *,
        transport_factory: Callable[[str], SyncTransport] = HttpTransport,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        background_sync: bool = True,
    ) -> None:
        self._sync = sync_service
        self._transport_factory = transport_factory
        self._clock = clock
        self._background_sync = background_sync

    # -- status ------------------------------------------------------------------------

    def connection(self) -> ControllerResult[ConnectionView]:
        return self._call(lambda: self.view(self._sync.status()))

    def view(self, status: SyncStatus) -> ConnectionView:
        workspace = status.workspace_account
        signed_in_email = status.account.email if status.account is not None and status.signed_in else None
        if not status.configured:
            state = "unconfigured"
            headline = "Offline — no backend configured"
            detail = "Everything is saved on this device. Enter a backend address to sign in and synchronize."
        elif status.signed_in:
            state = "signed_in"
            if status.in_progress:
                headline = "Synchronizing..."
            elif status.backend_reachable is False:
                headline = "Signed in — backend unreachable"
            elif status.last_report.status == "error":
                headline = "Signed in — last sync failed"
            else:
                headline = f"Signed in — {status.pending or 0} change(s) waiting"
            detail = f"Synchronizing {masked_email(signed_in_email)}'s records with {status.backend_url}."
        elif workspace is not None:
            state = "session_ended"
            headline = "Session ended — sign in again to synchronize"
            detail = (f"You are working offline in {masked_email(workspace.email)}'s records; changes are kept "
                      "and sent after you sign in again.")
        else:
            state = "signed_out"
            headline = "Signed out — working offline"
            detail = "Your records on this device are saved locally. Sign in to synchronize an account."
        if state != "unconfigured" and status.backend_reachable is False and state != "signed_in":
            headline += " (backend unreachable)"
        if status.conflicts:
            headline += f" · {status.conflicts} conflict(s) to review"
        return ConnectionView(
            state=state, backend_url=status.backend_url, reachable=status.backend_reachable,
            signed_in_email=signed_in_email, workspace_email=workspace.email if workspace else None,
            in_progress=status.in_progress, pending=status.pending, conflicts=status.conflicts,
            last_success=status.last_successful_sync_at,
            last_success_text=_relative(status.last_successful_sync_at, self._clock()),
            last_error=status.last_error, headline=headline, detail=detail,
        )

    # -- backend -----------------------------------------------------------------------

    def configure_backend(self, url: str | None) -> ControllerResult[ConnectionView]:
        """
        Use another backend (or none, with an empty URL). The current session
        ends first (after a running sync); local records are not touched.
        The address is saved on this device (never a credential).
        """

        def op() -> ConnectionView:
            address = (url or "").strip().rstrip("/") or None
            transport = self._transport_factory(address) if address else None  # ValueError for a bad URL
            self._sync.set_transport(transport)
            self._sync.remember_backend_url(address)
            if self._background_sync:
                self._sync.start()  # the background loop, if a backend is configured (no-op otherwise or if running)
            return self.view(self._sync.status())

        return self._call(op)

    def check_backend(self) -> ControllerResult[ConnectionView]:
        def op() -> ConnectionView:
            self._sync.check_connectivity()
            return self.view(self._sync.status())

        return self._call(op)

    # -- accounts ----------------------------------------------------------------------

    def register(self, email: str, password: str, *, display_name: str | None = None) -> ControllerResult[dict]:
        """Create an account on the backend (it does not sign in)."""

        def op() -> dict:
            errors = validate_credentials(email, password, registering=True, display_name=display_name)
            if errors:
                raise InvalidInput(errors)
            return self._sync.register(email.strip(), password, display_name=(display_name or "").strip() or None)

        return self._call(op)

    def sign_in(self, email: str, password: str) -> ControllerResult[SignInResult]:
        """Sign in. Never claims or uploads this device's ownerless records (see associate)."""

        def op() -> SignInResult:
            errors = validate_credentials(email, password)
            if errors:
                raise InvalidInput(errors)
            account = self._sync.sign_in(email.strip(), password)
            unassociated = self._sync.association_preview().total
            self._sync.wake()  # sync the account's own records; ownerless ones stay until associated
            return SignInResult(email=account.email or email.strip(), unassociated=unassociated,
                                associated_before=account.associated_at is not None)

        return self._call(op, signing_in=True)

    def sign_out(self) -> ControllerResult[ConnectionView]:
        """Forget the session on this device (the backend has no revocation; the token expires there)."""

        def op() -> ConnectionView:
            self._sync.sign_out()
            return self.view(self._sync.status())

        return self._call(op)

    def profile(self) -> ControllerResult[dict]:
        return self._call(self._sync.profile)

    # -- association -------------------------------------------------------------------

    def association_preview(self) -> ControllerResult[AssociationPreview]:
        """What associating would claim (counts by type, problems, and the token that confirms it). Writes nothing."""
        return self._call(self._sync.association_preview)

    def associate(self, token: str) -> ControllerResult[dict[str, int]]:
        """Claim exactly the previewed ownerless records for the signed-in account (ids and history unchanged)."""

        def op() -> dict[str, int]:
            counts = self._sync.associate_local_data(token)
            self._sync.wake()
            return counts

        return self._call(op)

    # -- synchronization and conflicts ---------------------------------------------------

    def sync_now(self) -> ControllerResult[SyncReport]:
        """One synchronization through SyncService (push, then pull); a running one is waited for, never overlapped."""
        return self._call(self._sync.sync_now)

    def conflicts(self) -> ControllerResult[list[ConflictView]]:
        return self._call(lambda: [conflict_view(conflict, self._sync.conflict_actions(conflict))
                                   for conflict in self._sync.list_conflicts("open")])

    def resolve(self, conflict_id: str, choice: str) -> ControllerResult[ConflictView]:
        """Apply keep_local or accept_remote through the service (it refuses what allowed_resolutions refuses)."""

        def op() -> ConflictView:
            resolved = self._sync.resolve_conflict(conflict_id, choice)
            return conflict_view(resolved, {})

        return self._call(op)

    # -- internals ---------------------------------------------------------------------

    @staticmethod
    def _call(operation, *, signing_in: bool = False):
        try:
            return ControllerResult.success(operation())
        except Exception as error:  # noqa: BLE001 - every failure becomes a readable message
            return ControllerResult.failure(friendly_error(error, signing_in=signing_in), error)
