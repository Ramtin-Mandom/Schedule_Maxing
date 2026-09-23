"""Synchronization client robustness: same-transaction change capture, inert mode,
retryable failures and restarts, lost responses, edits while a push is in flight,
atomic pull pages (crash/replay, equal timestamps), conflicts in every
direction with explicit resolution, and account/backend isolation."""

from __future__ import annotations

import threading
import time

import pytest

from app.planning.csv_import import ImportMode, parse_legacy_csv
from app.planning.preferences import PreferenceOverrides
from app.sync.engine import ConflictResolutionError
from app.sync.service import SyncService
from app.sync.transport import PullPage
from tests.sync.conftest import MON, PASSWORD, FlakyTransport, InProcessTransport


@pytest.fixture
def flaky(alice_server):
    return FlakyTransport(alice_server.client)


# -----------------------------------------------------------------------------
# Capture and inert mode
# -----------------------------------------------------------------------------


def test_every_kind_of_local_change_is_captured_in_its_own_transaction(alice_server, make_device, monkeypatch) -> None:
    device = make_device("a", InProcessTransport(alice_server.client))
    task = device.add_task("Planned", required_date=MON)
    device.add_block()
    device.controller.set_user_overrides(PreferenceOverrides(category_multipliers={"study": 2.0}))
    parsed = parse_legacy_csv("date,name,category,tag,fixed,start_time,end_time,duration,priority,dependencies\n"
                              "1,Imported,study,,false,540,600,30,5,\n", anchor_date=MON, timezone="UTC")
    device.controller.apply_import(parsed, ImportMode.APPEND)
    device.controller.schedule_range(MON, MON)  # placements + a generation record
    [placement] = [p for p in device.planning.placements_for_date(MON) if p.task_id == task.id]
    execution = device.executions.get_or_create_canonical_execution(task, placement)
    device.executions.start(execution.id)  # an execution session

    captured = {entity for entity, _, _ in device.dirty()}
    assert captured == {"task", "fixed_block", "preference", "placement", "schedule_generation", "execution"}

    # A failed compound mutation leaves no capture behind (the capture is part of its transaction).
    before = device.dirty()
    original = device.planning._repository.insert_task
    calls = {"n": 0}

    def fail_second(task):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full (injected)")
        original(task)

    monkeypatch.setattr(device.planning._repository, "insert_task", fail_second)
    with pytest.raises(RuntimeError):
        device.planning.save_tasks([_task("One"), _task("Two")])
    assert device.dirty() == before


def test_sync_is_inert_without_a_backend_or_an_account(alice_server, make_device) -> None:
    offline = make_device("offline", None)
    offline.add_task()
    assert offline.sync.sync_now().status == "inert" and not offline.sync.configured

    unsigned = make_device("unsigned", InProcessTransport(alice_server.client))
    unsigned.add_task()
    assert unsigned.sync.sync_now().status == "inert"
    assert unsigned.transport.pushed == []
    assert unsigned.connection.execute("SELECT COUNT(*) FROM sync_accounts").fetchone()[0] == 0


# -----------------------------------------------------------------------------
# Failures, restarts, idempotent retries, in-flight edits
# -----------------------------------------------------------------------------


def test_network_failures_back_off_and_pending_work_survives_a_restart(alice_server, make_device, flaky) -> None:
    device = make_device("a", flaky)
    device.sign_in("alice@example.com")
    task = device.add_task()
    flaky.fail_push = 3

    delays = []
    for _ in range(3):
        assert device.sync_now().status == "offline"
        delays.append(device.sync.next_delay())
    assert delays == [1.0, 2.0, 4.0]  # bounded exponential backoff (max 8)
    op_ids = [op.op_id for op in device.sync._engine.store.pending_ops(device.sync.account.account_key)]
    assert len(op_ids) == 1

    device.reopen()  # restart: the outbox is durable
    assert [op.op_id for op in device.sync._engine.store.pending_ops(device.sync.account.account_key)] == op_ids
    assert device.sync_now().status == "ok"
    assert device.sync.next_delay() == device.sync._interval
    assert alice_server.get("alice@example.com", f"/tasks/{task.id}")["version"] == 1


def test_a_lost_response_is_retried_without_duplicating_anything(alice_server, make_device, flaky) -> None:
    device = make_device("a", flaky)
    device.sign_in("alice@example.com")
    task = device.add_task()
    flaky.lose_push_response = 1

    assert device.sync_now().status == "offline"  # the server applied it, but we never heard back
    assert len(alice_server.changes("alice@example.com")) == 1
    assert device.sync_now().status == "ok"  # resent with the same op_id: answered from the server's record

    assert len(alice_server.changes("alice@example.com")) == 1
    assert flaky.pushed[0] == flaky.pushed[1]
    assert device.dirty() == []
    assert alice_server.get("alice@example.com", f"/tasks/{task.id}")["version"] == 1


def test_an_edit_made_while_a_push_is_in_flight_is_not_lost(alice_server, make_device, flaky) -> None:
    device = make_device("a", flaky)
    device.sign_in("alice@example.com")
    task = device.add_task("Before")

    def edit_during_request():
        stored = device.planning.get_task(task.id)
        device.planning.update_task(stored.model_copy(update={"name": "During"}), expected_version=stored.version)

    flaky.during_push = edit_during_request
    assert device.sync_now().status == "ok"  # the same run sends the newer edit next, based on version 1

    remote = alice_server.get("alice@example.com", f"/tasks/{task.id}")
    assert (remote["name"], remote["version"]) == ("During", 2)
    assert device.dirty() == []


# -----------------------------------------------------------------------------
# Pull
# -----------------------------------------------------------------------------


def test_pull_pages_apply_atomically_and_replay_safely(alice_server, make_device, monkeypatch) -> None:
    writer = make_device("writer", InProcessTransport(alice_server.client))
    writer.sign_in("alice@example.com")
    for index in range(5):
        writer.add_task(f"T{index}")  # pulled in pages of 2 by seq (equal timestamps: tests/backend)
    writer.sync_now()

    reader = make_device("reader", InProcessTransport(alice_server.client))
    reader.sign_in("alice@example.com")
    reader.sync._pull_page_size = 2
    engine = reader.sync._engine
    original = engine.records.store
    calls = {"n": 0}

    def crash_on_second_record(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("power loss (injected)")
        return original(*args, **kwargs)

    monkeypatch.setattr(engine.records, "store", crash_on_second_record)
    with pytest.raises(RuntimeError):
        reader.sync_now()
    assert reader.planning.list_tasks() == [] and reader.sync.account.pull_cursor == 0  # the page rolled back whole

    monkeypatch.undo()
    assert reader.sync_now().pulled == 5
    assert sorted(t.name for t in reader.planning.list_tasks()) == [f"T{i}" for i in range(5)]
    cursor = reader.sync.account.pull_cursor
    replay = PullPage(changes=alice_server.changes("alice@example.com"), cursor=cursor, has_more=False)
    assert engine.apply_pull_page(reader.sync.account, replay).applied == 0  # a replayed page changes nothing
    assert reader.dirty() == []


# -----------------------------------------------------------------------------
# Conflicts
# -----------------------------------------------------------------------------


@pytest.fixture
def synced_pair(alice_server, make_device):
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    a.sign_in("alice@example.com")
    b.sign_in("alice@example.com")
    task = a.add_task("Shared")
    other = a.add_task("Unrelated")
    a.sync_now()
    b.sync_now()
    return alice_server, a, b, task, other


def _edit(device, task_id, **changes):
    stored = device.planning.get_task(task_id)
    return device.planning.update_task(stored.model_copy(update=changes), expected_version=stored.version)


def test_a_stale_push_becomes_a_conflict_that_keeps_both_sides(synced_pair) -> None:
    server, a, b, task, other = synced_pair
    _edit(a, task.id, name="A's name")
    a.sync_now()
    _edit(b, task.id, name="B's name")
    _edit(b, other.id, priority=9)  # an unrelated change still goes through

    report = b.sync_now()
    assert report.conflicts == 1
    [conflict] = b.sync.list_conflicts()
    assert conflict.kind == "push_conflict" and conflict.base_version == 1
    assert conflict.local_record["name"] == "B's name" and conflict.remote_record["name"] == "A's name"
    assert server.get("alice@example.com", f"/tasks/{other.id}")["priority"] == 9
    assert b.planning.get_task(task.id).name == "B's name"  # local intent preserved

    b.reopen()  # conflicts are durable, and a blocked record is not retried automatically
    assert b.sync_now().pushed == 0 and len(b.sync.list_conflicts()) == 1

    resolved = b.sync.resolve_conflict(conflict.id, "keep_local")
    assert resolved.status == "resolved" and resolved.resolution["choice"] == "keep_local"
    b.sync_now()
    assert server.get("alice@example.com", f"/tasks/{task.id}")["name"] == "B's name"
    a.sync_now()
    assert a.planning.get_task(task.id).name == "B's name"


def test_keep_local_can_conflict_again_and_accept_remote_replaces_the_local_record(synced_pair) -> None:
    server, a, b, task, _ = synced_pair
    _edit(a, task.id, priority=2)
    a.sync_now()
    _edit(b, task.id, priority=3)
    b.sync_now()
    [first] = b.sync.list_conflicts()
    b.sync.resolve_conflict(first.id, "keep_local")
    _edit(a, task.id, priority=4)  # A changes it again before B retries
    a.sync_now()

    b.sync_now()
    [second] = b.sync.list_conflicts()
    assert second.id != first.id and second.remote_record["priority"] == 4
    b.sync.resolve_conflict(second.id, "accept_remote")
    assert b.planning.get_task(task.id).priority == 4
    assert b.sync_now().pushed == 0 and b.dirty() == []
    assert len(b.sync.list_conflicts(status=None)) == 2  # the audit of both decisions stays


def test_update_against_a_remote_delete_never_resurrects(synced_pair) -> None:
    server, a, b, task, _ = synced_pair
    a.planning.delete_task(task.id, expected_version=a.planning.get_task(task.id).version)
    a.sync_now()
    _edit(b, task.id, name="Edited offline")

    b.sync_now()
    [conflict] = b.sync.list_conflicts()
    assert conflict.remote_record["deleted_at"] is not None
    with pytest.raises(ConflictResolutionError, match="deleted"):
        b.sync.resolve_conflict(conflict.id, "keep_local")
    b.sync.resolve_conflict(conflict.id, "accept_remote")
    assert b.planning.get_task(task.id) is None
    assert server.get("alice@example.com", f"/tasks/{task.id}", include_deleted=True)["deleted_at"] is not None


def test_delete_against_a_remote_update_is_a_conflict_in_the_other_direction(synced_pair) -> None:
    server, a, b, task, _ = synced_pair
    _edit(a, task.id, name="Updated remotely")
    a.sync_now()
    b.planning.delete_task(task.id, expected_version=b.planning.get_task(task.id).version)

    b.sync_now()
    [conflict] = b.sync.list_conflicts()
    assert conflict.local_record["name"] == "Shared" and conflict.remote_record["name"] == "Updated remotely"
    b.sync.resolve_conflict(conflict.id, "keep_local")  # delete it anyway, against the current version
    b.sync_now()
    assert server.get("alice@example.com", f"/tasks/{task.id}", include_deleted=True)["deleted_at"] is not None


def test_a_pulled_change_never_overwrites_a_pending_local_change(synced_pair) -> None:
    server, a, b, task, _ = synced_pair
    _edit(a, task.id, name="Remote")
    a.sync_now()
    _edit(b, task.id, name="Local, not pushed yet")
    engine, account = b.sync._engine, b.sync.account
    page = b.transport.pull(b.sync._token, account.pull_cursor, 100)  # pull without pushing first

    outcome = engine.apply_pull_page(account, page)

    assert outcome.conflicts == [str(task.id)]
    assert b.planning.get_task(task.id).name == "Local, not pushed yet"
    [conflict] = b.sync.list_conflicts()
    assert conflict.kind == "pull_conflict" and conflict.remote_record["name"] == "Remote"


def test_a_rejected_operation_is_recorded_although_the_server_rolled_it_back(synced_pair) -> None:
    server, a, b, task, other = synced_pair
    dependent = a.add_task("Needs Shared", dependency_ids=[task.id])
    a.sync_now()
    # B has not seen the dependent, and deletes the task it depends on.
    b.planning.delete_task(task.id, expected_version=b.planning.get_task(task.id).version)

    b.sync_now()
    [conflict] = b.sync.list_conflicts()
    assert conflict.error["code"] == "in_use" and dependent.id
    assert server.get("alice@example.com", f"/tasks/{task.id}")["deleted_at"] is None  # nothing changed there
    b.reopen()
    assert b.sync.list_conflicts()[0].id == conflict.id


# -----------------------------------------------------------------------------
# Accounts
# -----------------------------------------------------------------------------


def test_accounts_and_backends_never_mix(alice_server, make_device) -> None:
    alice_server.register("bob@example.com")
    device = make_device("shared", InProcessTransport(alice_server.client))
    device.sign_in("alice@example.com")
    alices = device.add_task("Alice's")
    device.sync_now()
    alice_key = device.sync.account.account_key

    device.sync.sign_out()
    assert not device.sync.signed_in and device.sync.sync_now().status == "inert"
    unowned = device.add_task("Created while signed out")  # nobody's: not claimed by anyone implicitly
    device.sync.sign_in("bob@example.com", PASSWORD)
    device.sync_now()
    assert alice_server.get("bob@example.com", "/tasks")["items"] == []
    assert device.planning.get_task(unowned.id).user_id is None

    device.sync.associate_local_data()  # explicit: only the ownerless record becomes Bob's
    device.sync_now()
    assert [t["id"] for t in alice_server.get("bob@example.com", "/tasks")["items"]] == [str(unowned.id)]
    assert str(device.planning.get_task(alices.id).user_id) != device.sync.account.user_id
    assert device.sync.account.account_key != alice_key
    rows = device.connection.execute("SELECT account_key, pull_cursor FROM sync_accounts ORDER BY account_key").fetchall()
    assert len(rows) == 2 and all(row["pull_cursor"] > 0 for row in rows)  # separate cursors per account


def test_the_background_loop_syncs_and_stops_cleanly(alice_server, make_device) -> None:
    device = make_device("a", InProcessTransport(alice_server.client))
    device.sign_in("alice@example.com")
    device.sync._interval = 0.05
    task = device.add_task()

    device.sync.start()
    deadline = time.monotonic() + 10
    while device.dirty() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert device.sync.stop(timeout=10)
    assert alice_server.get("alice@example.com", f"/tasks/{task.id}")["version"] == 1
    assert not any(thread.name == "schedule-maxing-sync" for thread in threading.enumerate())


def test_an_unconfigured_service_never_starts_a_thread(tmp_path) -> None:
    from app.execution.db import get_connection

    connection = get_connection(tmp_path / "x.db")
    try:
        service = SyncService(connection, None)
        service.start()
        assert service._thread is None and service.stop()
    finally:
        connection.close()


def _task(name: str):
    from app.planning.models import Task

    return Task(name=name, category="study", estimated_duration_minutes=30, priority=5)

