"""The local-first account workflow over the real desktop services and an in-process backend:

- guest work survives a restart, and creating an account adopts it (same ids, schedule and history) and uploads it;
- a refused or interrupted registration, or a failed adoption, leaves the guest workspace exactly as it was;
- signing in to an existing account never touches guest work: it is merged only when chosen, else kept separate;
- an account stays the working workspace offline and across restarts until an explicit sign-out, which hides it;
- offline edits survive a restart and upload once (also after a lost answer), and never into another account;
- a connection check changes no record; the idle loop notices new pending work without any request.

Failures are injected in the transport or the engine; no live database is involved."""

from __future__ import annotations

import time
from datetime import date

import pytest

from app.sync.service import AccountCreationError
from app.sync.transport import TransportError
from app.ui.day_controller import DayScheduleController
from tests.sync.conftest import PASSWORD, FlakyTransport, InProcessTransport
from tests.sync.test_desktop_account_controller import Desktop as Desktop
from tests.sync.test_desktop_account_controller import _restore_installed_registry as _restore_installed_registry  # noqa: F401
from tests.sync.test_desktop_account_controller import make_desktop as make_desktop  # noqa: F401

MON = date(2026, 3, 2)
OWNED_TABLES = ("projects", "tasks", "fixed_blocks", "scheduled_tasks", "preference_overrides",
                "schedule_generations", "executions", "task_types")


class LoginFailsOnce(InProcessTransport):
    """The server answers, but the sign-in right after a registration is lost once."""

    fail_login = 1

    def login(self, email: str, password: str):
        if self.fail_login:
            self.fail_login -= 1
            raise TransportError("connection reset (injected)")
        return super().login(email, password)


def owners(desktop: Desktop) -> dict[str, set]:
    """{table: the distinct owners of its rows} -- {None} is guest data."""
    connection = desktop.services.connection
    return {table: {row[0] for row in connection.execute(f"SELECT user_id FROM {table}")} for table in OWNED_TABLES}


def records(desktop: Desktop) -> dict[str, list]:
    """Every owned row's identity and content version, for "nothing changed" comparisons."""
    connection = desktop.services.connection
    found = {table: [tuple(row) for row in connection.execute(f"SELECT id, user_id, version FROM {table} ORDER BY id")]
             for table in OWNED_TABLES}
    found["sync_dirty"] = [tuple(row) for row in connection.execute(
        "SELECT entity_type, entity_id, local_rev FROM sync_dirty ORDER BY 1, 2")]
    found["outbox"] = [tuple(row) for row in connection.execute("SELECT COUNT(*) FROM sync_outbox")]
    return found


def guest_work(desktop: Desktop) -> dict:
    """Two guest tasks and a saved schedule for one of the days."""
    essay, notes = desktop.add("Essay"), desktop.add("Notes")
    planning = desktop.services.planning_controller
    assert DayScheduleController(planning, anchor_date=MON, timezone="UTC").make_schedule_for(MON).ok
    placements = desktop.ok(planning.get_placements(MON))
    assert len(placements) == 2
    return {"tasks": {essay.id, notes.id}, "placements": {placement.id for placement in placements}}


def test_guest_work_survives_a_restart_and_a_new_account_adopts_and_uploads_it(make_desktop, server) -> None:
    desktop = make_desktop("desk", InProcessTransport(server.client))
    work = guest_work(desktop)
    assert desktop.state().state == "unconfigured" and "guest" in desktop.state().headline
    desktop.reopen()  # a restart: guest work is saved on the device
    assert desktop.names() == {"Essay", "Notes"} and all(owner == {None} for owner in owners(desktop).values() if owner)

    desktop.connect()
    assert desktop.state().state == "signed_out" and desktop.state().headline.startswith("Guest mode")
    created = desktop.ok(desktop.account.create_account("new@example.com", PASSWORD, display_name="New"))
    desktop.services.switch_workspace()  # what the app does after the account changed
    assert created.adopted >= 4  # both tasks, their placements (and the schedule's own records)

    # The same workspace is still there -- now the account's, with the same ids -- and is waiting to upload.
    planning = desktop.services.planning_controller
    assert desktop.names() == {"Essay", "Notes"}
    assert {task.id for task in desktop.ok(planning.list_tasks())} == work["tasks"]
    assert {placement.id for placement in desktop.ok(planning.get_placements(MON))} == work["placements"]
    account = desktop.services.sync_service.account
    assert all(owner in ({account.user_id}, set()) for owner in owners(desktop).values()), owners(desktop)
    view = desktop.state()
    assert view.state == "signed_in" and view.pending and view.pending >= 4

    report = desktop.ok(desktop.account.sync_now())
    assert report.status == "ok" and report.conflicts == 0 and desktop.state().pending == 0

    # Another client of the same account sees the same records; uploading again creates nothing new.
    other = make_desktop("other", InProcessTransport(server.client))
    other.connect()
    other.sign_in("new@example.com")
    other.ok(other.account.sync_now())
    assert {task.id for task in other.ok(other.services.planning_controller.list_tasks())} == work["tasks"]
    assert {p.id for p in other.ok(other.services.planning_controller.get_placements(MON))} == work["placements"]
    again = desktop.ok(desktop.account.sync_now())
    assert again.pushed == 0
    other.ok(other.account.sync_now())
    assert len(other.ok(other.services.planning_controller.list_tasks())) == 2


def test_a_failed_registration_leaves_the_guest_workspace_exactly_as_it_was(make_desktop, alice_server) -> None:
    desktop = make_desktop("desk", FlakyTransport(alice_server.client))
    guest_work(desktop)
    desktop.connect()
    before = records(desktop)

    taken = desktop.account.create_account("alice@example.com", PASSWORD)  # the email already has an account
    assert not taken.ok and "already exists" in taken.error
    invalid = desktop.account.create_account("not-an-email", "short")
    assert not invalid.ok
    assert records(desktop) == before and desktop.state().state == "signed_out"
    assert desktop.names() == {"Essay", "Notes"}


def test_an_account_created_without_a_completed_sign_in_keeps_the_guest_data_for_later(make_desktop, server) -> None:
    desktop = make_desktop("desk", LoginFailsOnce(server.client))
    guest_work(desktop)
    desktop.connect()
    before = records(desktop)

    result = desktop.account.create_account("late@example.com", PASSWORD)
    assert not result.ok and isinstance(result.cause, AccountCreationError) and result.cause.stage == "sign_in"
    assert "unchanged" in result.error
    assert records(desktop) == before and desktop.state().state == "signed_out"  # nothing assigned, nobody signed in
    assert desktop.names() == {"Essay", "Notes"}

    # The account exists: signing in and choosing to add the records finishes it, with the same ids.
    signed_in = desktop.ok(desktop.account.sign_in("late@example.com", PASSWORD))
    assert signed_in.unassociated >= 4
    assert desktop.ok(desktop.account.merge_guest_data()) == signed_in.unassociated
    desktop.services.switch_workspace()
    assert desktop.names() == {"Essay", "Notes"}
    assert desktop.ok(desktop.account.sync_now()).status == "ok"


def test_a_failed_adoption_assigns_nothing_and_stays_in_the_guest_workspace(make_desktop, server, monkeypatch) -> None:
    desktop = make_desktop("desk", InProcessTransport(server.client))
    guest_work(desktop)
    desktop.connect()
    before = records(desktop)
    engine = desktop.services.sync_service._engine

    def interrupted(account, *, confirmation=None):
        # Partway through the real work, then a failure: the transaction must leave no record assigned.
        with engine.store.transaction():
            engine._connection.execute("UPDATE tasks SET user_id = ? WHERE user_id IS NULL", (account.user_id,))
            raise RuntimeError("disk full (injected)")

    monkeypatch.setattr(engine, "associate_local_data", interrupted)
    result = desktop.account.create_account("half@example.com", PASSWORD)
    assert not result.ok and isinstance(result.cause, AccountCreationError) and result.cause.stage == "adoption"
    assert records(desktop) == before  # rolled back: not one record was assigned
    assert desktop.state().state == "signed_out" and desktop.names() == {"Essay", "Notes"}  # still visible as guest


def test_signing_in_to_an_existing_account_keeps_guest_work_separate_unless_merged(make_desktop, alice_server) -> None:
    first = make_desktop("first", InProcessTransport(alice_server.client))
    first.connect()
    first.sign_in()
    first.add("Alice's own")
    first.ok(first.account.sync_now())

    desktop = make_desktop("desk", InProcessTransport(alice_server.client))
    guest_work(desktop)
    desktop.connect()
    before = records(desktop)
    result = desktop.ok(desktop.account.sign_in("alice@example.com", PASSWORD))
    desktop.services.switch_workspace()
    assert result.unassociated >= 4  # the page asks: add them to the account, or keep them separate
    desktop.ok(desktop.account.sync_now())
    # Kept separate (the default): the account shows only its own records, and no guest record was assigned
    # or uploaded.
    assert desktop.names() == {"Alice's own"}
    guest_rows = {table: [row for row in rows if row[1] is None] for table, rows in records(desktop).items()
                  if table in OWNED_TABLES}
    assert guest_rows == {table: rows for table, rows in before.items() if table in OWNED_TABLES}
    first.ok(first.account.sync_now())
    assert first.names() == {"Alice's own"}

    # Signing out hides the account's cached records and shows the guest workspace again, untouched.
    desktop.ok(desktop.account.sign_out())
    desktop.services.switch_workspace()
    assert desktop.names() == {"Essay", "Notes"} and desktop.state().state == "signed_out"

    # Choosing to merge adds exactly those records to the account, and they upload.
    desktop.sign_in()
    merged = desktop.ok(desktop.account.merge_guest_data())
    assert merged == result.unassociated
    desktop.services.switch_workspace()
    desktop.ok(desktop.account.sync_now())
    assert desktop.names() == {"Alice's own", "Essay", "Notes"}
    first.ok(first.account.sync_now())
    assert first.names() == {"Alice's own", "Essay", "Notes"}


def test_offline_edits_survive_a_restart_and_upload_once_after_signing_in_again(make_desktop, alice_server) -> None:
    transport = FlakyTransport(alice_server.client)
    desktop = make_desktop("desk", transport)
    desktop.connect()
    desktop.sign_in()  # an existing account, never associated with guest data on this device
    desktop.add("Online")
    desktop.ok(desktop.account.sync_now())

    transport.fail_push = 5  # the connection drops; work continues locally
    offline = desktop.add("Offline")
    report = desktop.account.sync_now()
    assert desktop.state().pending >= 1 and (not report.ok or report.value.status == "offline")
    account_id = desktop.services.sync_service.account.user_id

    desktop.reopen()  # a restart while disconnected: the session is gone, the account and its work are not
    view = desktop.state()
    assert view.state == "session_ended" and view.pending >= 1  # not signed out: still the account's workspace
    assert desktop.names() == {"Online", "Offline"}
    assert desktop.owners()["Offline"] == account_id
    edited = desktop.ok(desktop.services.planning_controller.get_task(offline.id))
    desktop.ok(desktop.services.planning_controller.add_or_update_task(
        edited.model_copy(update={"name": "Offline (edited)"}), expected_version=edited.version))
    assert desktop.owners()["Offline (edited)"] == account_id  # offline records keep their owner

    transport.fail_push = 0
    transport.lose_push_response = 1  # the server applies the upload but the answer is lost: it is retried
    desktop.sign_in()
    desktop.account.sync_now()
    assert desktop.ok(desktop.account.sync_now()).status == "ok" and desktop.state().pending == 0

    other = make_desktop("other", InProcessTransport(alice_server.client))
    other.connect()
    other.sign_in()
    other.ok(other.account.sync_now())
    tasks = other.ok(other.services.planning_controller.list_tasks())
    assert sorted(task.name for task in tasks) == ["Offline (edited)", "Online"]  # no loss, no duplicate


def test_sign_out_hides_the_account_and_another_account_never_receives_its_records(make_desktop, server) -> None:
    for email in ("alice@example.com", "bob@example.com"):
        server.register(email)
    transport = FlakyTransport(server.client)
    desktop = make_desktop("desk", transport)
    desktop.connect()
    desktop.sign_in("alice@example.com")
    desktop.add("Alice synced")
    desktop.ok(desktop.account.sync_now())
    transport.fail_push = 3
    desktop.add("Alice pending")  # never reaches the server before she signs out
    desktop.account.sync_now()
    pending_before = desktop.state().pending
    assert pending_before >= 1

    desktop.ok(desktop.account.sign_out())
    desktop.services.switch_workspace()
    assert desktop.names() == set() and desktop.state().state == "signed_out"  # a separate, empty guest workspace
    desktop.add("Guest note")

    transport.fail_push = 0
    desktop.sign_in("bob@example.com")
    desktop.add("Bob's")
    desktop.ok(desktop.account.sync_now())
    assert desktop.names() == {"Bob's"}  # neither Alice's cached records nor the guest note
    bob_view = make_desktop("bob", InProcessTransport(server.client))
    bob_view.connect()
    bob_view.sign_in("bob@example.com")
    bob_view.ok(bob_view.account.sync_now())
    assert bob_view.names() == {"Bob's"}  # nothing of Alice's was uploaded into Bob's account

    # Alice's pending change was kept safely and is sent when she signs in again.
    desktop.ok(desktop.account.sign_out())
    desktop.sign_in("alice@example.com")
    assert desktop.names() == {"Alice synced", "Alice pending"} and desktop.state().pending >= 1
    desktop.ok(desktop.account.sync_now())
    alice_view = make_desktop("alice", InProcessTransport(server.client))
    alice_view.connect()
    alice_view.sign_in("alice@example.com")
    alice_view.ok(alice_view.account.sync_now())
    assert alice_view.names() == {"Alice synced", "Alice pending"}


def test_a_connection_check_changes_no_record(make_desktop, alice_server) -> None:
    transport = FlakyTransport(alice_server.client)
    desktop = make_desktop("desk", transport)
    guest_work(desktop)
    desktop.connect()
    desktop.sign_in()
    desktop.add("Mine, pending")
    before = records(desktop)
    for _ in range(3):
        assert desktop.ok(desktop.account.check_backend()).reachable is True
    assert records(desktop) == before and transport.pushed == []  # nothing uploaded, assigned or rewritten


def test_the_idle_loop_notices_new_pending_work_without_a_request(make_desktop, alice_server) -> None:
    transport = InProcessTransport(alice_server.client)
    desktop = make_desktop("desk", transport)
    desktop.connect()
    desktop.sign_in()
    service = desktop.services.sync_service
    service._pending_poll = 0.01
    desktop.ok(desktop.account.sync_now())
    requests = len(transport.pushed)
    service._wake.clear()  # signing in asked for a prompt first sync; that request is done

    started = time.monotonic()
    service._idle(0.2)  # nothing changed: the full delay passes
    assert time.monotonic() - started >= 0.19

    desktop.add("Just typed")
    started = time.monotonic()
    service._idle(30.0)  # new pending work ends the wait at the next local look
    assert time.monotonic() - started < 5.0
    assert len(transport.pushed) == requests  # looking is a local count, never a request

    # After a failure only the backoff decides: pending work does not cause an early retry.
    service.consecutive_failures = 1
    desktop.add("While failing")
    started = time.monotonic()
    service._idle(0.2)
    assert time.monotonic() - started >= 0.19
    service.consecutive_failures = 0
    assert service.next_delay() == pytest.approx(service._interval)
