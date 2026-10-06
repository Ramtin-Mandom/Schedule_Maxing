"""The desktop's account/connection/sync boundary (app/ui/account_controller.py) over the
real desktop services and an in-process backend: backend configuration, registration and
sign-in with validation and readable errors, profile, sign-out, session expiry recovery,
explicit association (preview, cancel, stale preview, confirm), durable pending/last-sync
status across restarts, lost-response retry, two-device sync and deletion, supported
conflict decisions only, and backend/account isolation including work that was in flight."""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest

from app.planning.models import Task
from app.planning.scope import OwnerScope
from app.sync.transport import AuthenticationError, TransportError
from app.ui import background
from app.ui.account_controller import AccountController, InvalidInput, validate_credentials
from app.ui.app_services import open_app_services
from tests.sync.conftest import PASSWORD, FlakyTransport, InProcessTransport

MON = date(2026, 3, 2)


@pytest.fixture(autouse=True)
def _restore_installed_registry():
    previous = background.current_registry()
    yield
    background.install_registry(previous)


class Desktop:
    """The desktop app's services and account controller on one temporary database (reopen = restart)."""

    def __init__(self, path: Path, project_root: Path, transport) -> None:
        self.path, self.project_root, self.transport = path, project_root, transport
        self.open()

    def factory(self, url: str):
        if not url.startswith(("http://", "https://")):
            raise ValueError("the backend URL must start with https:// (or http:// for a local server)")
        self.transport.base_url = url
        return self.transport

    def open(self) -> None:
        # No background loop: each test decides exactly when a synchronization runs.
        self.services = open_app_services(self.path, timezone="UTC", project_root=str(self.project_root),
                                          background_sync=False, transport_factory=self.factory)
        self.account = AccountController(self.services.sync_service, transport_factory=self.factory,
                                         background_sync=False)

    def reopen(self) -> None:
        self.services.close()
        self.open()

    def ok(self, result):
        assert result.ok, result.error
        return result.value

    def connect(self, url: str = "http://backend.test") -> None:
        self.ok(self.account.configure_backend(url))

    def sign_in(self, email: str = "alice@example.com") -> None:
        self.ok(self.account.sign_in(email, PASSWORD))
        self.services.switch_workspace()  # what the app does on the Tk thread after signing in

    def add(self, name: str) -> Task:
        return self.ok(self.services.planning_controller.add_or_update_task(
            Task(name=name, category="study", estimated_duration_minutes=30, priority=5, preferred_dates=[MON])))

    def names(self) -> set[str]:
        return {task.name for task in self.ok(self.services.planning_controller.list_tasks())}

    def owners(self) -> dict[str, str | None]:
        return dict(self.services.connection.execute("SELECT name, user_id FROM tasks").fetchall())

    def state(self):
        return self.ok(self.account.connection())

    def close(self) -> None:
        self.services.close()


@pytest.fixture
def make_desktop(tmp_path: Path):
    desktops: list[Desktop] = []

    def make(name: str, transport) -> Desktop:
        root = tmp_path / "project"
        root.mkdir(exist_ok=True)
        desktop = Desktop(tmp_path / f"{name}.db", root, transport)
        desktops.append(desktop)
        return desktop

    yield make
    for desktop in desktops:
        desktop.close()


def test_credential_validation_happens_before_anything_is_sent() -> None:
    assert validate_credentials("", "")["email"] == "Enter your email address."
    assert "valid email" in validate_credentials("alice", "secret123")["email"]
    assert validate_credentials("a@b.co", "short", registering=True) == {"password": "Use at least 8 characters."}
    assert validate_credentials("a@b.co", "short") == {}  # signing in: the server judges an existing password
    assert "display_name" in validate_credentials("a@b.co", "long enough", registering=True, display_name="x" * 201)


def test_connection_register_sign_in_profile_and_sign_out(make_desktop, server) -> None:
    transport = InProcessTransport(server.client)
    desktop = make_desktop("desk", transport)
    view = desktop.state()
    assert view.state == "unconfigured" and "no backend configured" in view.headline

    bad = desktop.account.configure_backend("ftp://nope")
    assert not bad.ok and bad.error.startswith("The backend URL must start with https://")
    desktop.connect()
    assert desktop.state().state == "signed_out"
    assert desktop.services.sync_service.remembered_backend_url() == "http://backend.test"  # saved, never a password

    invalid = desktop.account.register("alice", "short")
    assert not invalid.ok and isinstance(invalid.cause, InvalidInput) and set(invalid.cause.errors) == {"email", "password"}
    assert transport.pushed == []
    desktop.ok(desktop.account.register("alice@example.com", PASSWORD, display_name="Alice"))
    again = desktop.account.register("alice@example.com", PASSWORD)
    assert again.error == "An account with this email already exists. Sign in instead."

    wrong = desktop.account.sign_in("alice@example.com", "not the password")
    assert wrong.error == "The email or password is incorrect." and desktop.state().state == "signed_out"
    result = desktop.ok(desktop.account.sign_in("alice@example.com", PASSWORD))
    assert result.unassociated == 0 and result.associated_before is False
    view = desktop.state()
    assert view.state == "signed_in" and view.signed_in_email == "alice@example.com"
    assert desktop.ok(desktop.account.profile())["display_name"] == "Alice"

    after = desktop.ok(desktop.account.sign_out())
    assert after.state == "signed_out" and not desktop.services.sync_service.signed_in
    assert not desktop.account.profile().ok


def test_an_unreachable_backend_is_explained_and_never_blocks_local_work(make_desktop, alice_server) -> None:
    transport = FlakyTransport(alice_server.client)
    desktop = make_desktop("desk", transport)
    desktop.connect()

    def unreachable(*_args):
        raise TransportError("connection refused (injected)")

    transport.login = unreachable
    failure = desktop.account.sign_in("alice@example.com", PASSWORD)
    assert failure.error.startswith("The backend could not be reached") and "saved on this device" in failure.error
    transport.health = unreachable
    assert desktop.ok(desktop.account.check_backend()).reachable is False
    assert "unreachable" in desktop.state().headline
    desktop.add("Still works offline")
    assert desktop.names() == {"Still works offline"}


def test_association_is_explicit_previewed_revalidated_and_keeps_ids(make_desktop, alice_server) -> None:
    desktop = make_desktop("desk", InProcessTransport(alice_server.client))
    offline = desktop.add("Offline work")
    desktop.connect()
    desktop.sign_in()
    assert desktop.names() == set()  # the account's (empty) workspace: nothing was claimed by signing in
    before = desktop.owners(), desktop.services.connection.execute("SELECT count(*) FROM sync_outbox").fetchone()[0]

    preview = desktop.ok(desktop.account.association_preview())
    assert preview.counts["task"] == 1 and preview.total >= 1 and preview.problems == []
    # Cancelling is simply not confirming: previewing changed no owner, version or queued operation.
    assert (desktop.owners(), desktop.services.connection.execute("SELECT count(*) FROM sync_outbox").fetchone()[0]) \
        == before

    desktop.add("Created signed in")  # the account's own new work, not the old ownerless record
    # The guest record changes after the preview (anything created now would be the active account's own).
    guest = desktop.services.planning_service.get_task(offline.id)
    desktop.services.planning_service.update_task(guest.model_copy(update={"priority": 9}),
                                                  expected_version=guest.version)
    stale = desktop.account.associate(preview.token)
    assert not stale.ok and "changed since this preview" in stale.error
    assert desktop.owners()["Offline work"] is None

    fresh = desktop.ok(desktop.account.association_preview())
    assert fresh.counts["task"] == 1 and fresh.token != preview.token
    counts = desktop.ok(desktop.account.associate(fresh.token))
    assert counts["task"] == 1
    desktop.services.switch_workspace()
    assert desktop.names() == {"Offline work", "Created signed in"}
    assert desktop.ok(desktop.services.planning_controller.get_task(offline.id)).id == offline.id  # same id

    report = desktop.ok(desktop.account.sync_now())
    assert report.status == "ok"
    on_server = {task["id"] for task in alice_server.get("alice@example.com", "/tasks")["items"]}
    assert str(offline.id) in on_server


def test_pending_changes_and_the_last_sync_are_durable_across_restarts(make_desktop, alice_server) -> None:
    transport = FlakyTransport(alice_server.client)
    desktop = make_desktop("desk", transport)
    desktop.connect()
    desktop.sign_in()
    desktop.ok(desktop.account.associate(desktop.ok(desktop.account.association_preview()).token))
    desktop.add("First")
    assert desktop.ok(desktop.account.sync_now()).status == "ok"
    synced_at = desktop.state().last_success
    assert synced_at is not None

    desktop.add("Waiting")
    transport.fail_push = 1
    assert desktop.ok(desktop.account.sync_now()).status == "offline"
    assert desktop.state().pending == 1
    desktop.reopen()  # a restart: the token was in memory only

    view = desktop.state()
    assert view.state == "session_ended" and "sign in again" in view.headline
    assert view.pending == 1 and view.last_success == synced_at and view.last_success_text != "never"
    assert desktop.names() == {"First", "Waiting"}  # the device's account workspace, offline
    desktop.sign_in()
    assert desktop.ok(desktop.account.sync_now()).status == "ok"
    assert desktop.state().pending == 0


def test_a_lost_response_is_retried_without_duplicating_on_the_server(make_desktop, alice_server) -> None:
    transport = FlakyTransport(alice_server.client)
    desktop = make_desktop("desk", transport)
    desktop.connect()
    desktop.sign_in()
    task = desktop.add("Sent once")
    transport.lose_push_response = 1  # the server applied it, but the answer never arrived
    assert desktop.ok(desktop.account.sync_now()).status == "offline"
    assert desktop.ok(desktop.account.sync_now()).status == "ok"
    ids = [item["id"] for item in alice_server.get("alice@example.com", "/tasks")["items"]]
    assert ids.count(str(task.id)) == 1 and desktop.state().pending == 0


def test_an_ended_session_is_recovered_by_signing_in_again(make_desktop, alice_server) -> None:
    transport = FlakyTransport(alice_server.client)
    desktop = make_desktop("desk", transport)
    desktop.connect()
    desktop.sign_in()
    desktop.add("Before expiry")

    real_push = transport.push

    def expired(token, operations):
        transport.push = real_push
        raise AuthenticationError("token expired (injected)")

    transport.push = expired
    report = desktop.ok(desktop.account.sync_now())
    assert report.status == "auth_required"
    view = desktop.state()
    assert view.state == "session_ended" and view.pending == 1
    assert not desktop.account.sync_now().value.status == "ok"  # nothing is sent without a session
    desktop.sign_in()
    assert desktop.ok(desktop.account.sync_now()).status == "ok" and desktop.state().pending == 0


def test_two_devices_sync_edits_deletions_and_supported_conflict_decisions(make_desktop, alice_server) -> None:
    first = make_desktop("first", InProcessTransport(alice_server.client))
    second = make_desktop("second", InProcessTransport(alice_server.client))
    for desktop in (first, second):
        desktop.connect()
        desktop.sign_in()
    essay = first.add("Essay")
    other = first.add("Other")
    first.ok(first.account.sync_now())
    second.ok(second.account.sync_now())
    assert second.names() == {"Essay", "Other"}

    # Both change the essay; the second device syncs first, so the first one's push conflicts.
    theirs = second.ok(second.services.planning_controller.get_task(essay.id))
    second.ok(second.services.planning_controller.add_or_update_task(
        theirs.model_copy(update={"name": "Essay (their edit)"}), expected_version=theirs.version))
    second.ok(second.account.sync_now())
    mine = first.ok(first.services.planning_controller.get_task(essay.id))
    first.ok(first.services.planning_controller.add_or_update_task(
        mine.model_copy(update={"name": "Essay (my edit)", "priority": 9}), expected_version=mine.version))
    report = first.ok(first.account.sync_now())
    assert report.conflicts == 1 and first.state().conflicts == 1

    [conflict] = first.ok(first.account.conflicts())
    assert conflict.title.startswith("Task") and conflict.choices == {"accept_remote": None, "keep_local": None}
    fields = {difference.field: difference for difference in conflict.differences}
    assert fields["name"].local == "Essay (my edit)" and fields["name"].remote == "Essay (their edit)"
    assert conflict.base_version is not None and conflict.remote_version is not None
    first.ok(first.account.resolve(conflict.id, "accept_remote"))
    assert first.ok(first.services.planning_controller.get_task(essay.id)).name == "Essay (their edit)"
    assert first.state().conflicts == 0

    # A deletion reaches the other device; editing a record deleted there cannot "keep local".
    second.ok(second.services.planning_controller.remove_task(
        other.id, expected_version=second.ok(second.services.planning_controller.get_task(other.id)).version))
    second.ok(second.account.sync_now())
    mine = first.ok(first.services.planning_controller.get_task(other.id))
    first.ok(first.services.planning_controller.add_or_update_task(
        mine.model_copy(update={"name": "Other (edited)"}), expected_version=mine.version))
    first.ok(first.account.sync_now())
    [conflict] = first.ok(first.account.conflicts())
    assert conflict.remote_deleted and conflict.choices["keep_local"] and "deleted on the server" in \
        conflict.choices["keep_local"]
    refused = first.account.resolve(conflict.id, "keep_local")
    assert not refused.ok and "Accept the deletion" in refused.error
    first.ok(first.account.resolve(conflict.id, "accept_remote"))
    assert first.ok(first.services.planning_controller.get_task(other.id)) is None
    first.ok(first.account.sync_now())
    assert first.names() == second.names() == {"Essay (their edit)"}


def test_switching_backend_or_account_isolates_records_and_in_flight_results(make_desktop, server) -> None:
    for email in ("alice@example.com", "bob@example.com"):
        server.register(email)
    desktop = make_desktop("desk", InProcessTransport(server.client))
    desktop.connect()
    desktop.sign_in("alice@example.com")
    alice = uuid.UUID(desktop.services.sync_service.account.user_id)
    desktop.add("Alice's plan")
    guard = desktop.services.workspace_guard()  # e.g. a view load started while Alice is signed in

    desktop.ok(desktop.account.sign_out())
    desktop.sign_in("bob@example.com")
    assert not guard()  # its result would be dropped, not shown in Bob's view
    assert desktop.services.workspace.scope != OwnerScope.account(alice)
    assert desktop.names() == set()  # Bob never sees Alice's records
    desktop.add("Bob's plan")
    assert desktop.owners()["Bob's plan"] != str(alice)

    view = desktop.ok(desktop.account.configure_backend("http://other.test"))  # another backend ends the session
    assert view.state == "signed_out" and not desktop.services.sync_service.signed_in
    desktop.services.switch_workspace()
    assert desktop.services.workspace.scope == OwnerScope.ownerless() and desktop.names() == set()
    assert set(desktop.owners()) == {"Alice's plan", "Bob's plan"}  # nothing deleted or re-owned
