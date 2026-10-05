"""Recurring series across devices (docs/recurrence.md, docs/sync-protocol.md): two devices expanding the same
rule converge on one record per slot (pushed or pulled); a slot skipped before it ever synced stays reserved on
the server and on a fresh device; concurrent rule edits conflict instead of minting another occurrence; a
cross-date move reaches the other device as one unit; and an older server without recurrence support never
receives recurrence data (it is held, not dropped)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.planning import series as series_ops
from app.planning import workflow
from app.planning.models import OccurrenceState, RecurrenceSpec
from app.planning.recurrence import occurrence_task_id
from app.planning.series import EditScope
from app.sync.transport import LEGACY_CAPABILITIES
from tests.sync.conftest import MON, InProcessTransport


@pytest.fixture
def pair(alice_server, make_device):
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    return alice_server, a, b


def add_series(device, **rule):
    spec = RecurrenceSpec(**{"frequency": "daily", "start_date": MON, "timezone": "UTC", **rule})
    return device.add_task("Walk", recurrence=spec)


def server_tasks(server, series_id) -> list[dict]:
    tasks = server.get("alice@example.com", "/tasks", include_deleted=True, limit=500)["items"]
    return [task for task in tasks if task["series_id"] == str(series_id)]


def test_two_devices_expanding_the_same_rule_converge_on_one_record_per_slot(pair) -> None:
    server, a, b = pair
    a.sign_in("alice@example.com")
    b.sign_in("alice@example.com")
    walk = add_series(a)
    a.sync_now()
    b.sync_now()
    week = (MON, MON + timedelta(days=6))
    assert len(series_ops.expand_occurrences(a.planning, *week).created) == 7
    assert len(series_ops.expand_occurrences(b.planning, *week).created) == 7  # offline, the same slots

    assert a.sync_now().status == "ok"
    report = b.sync_now()  # B pushes the same occurrences: the server already has them, identical
    assert report.status == "ok" and report.conflicts == 0
    assert len(server_tasks(server, walk.id)) == 7
    assert b.sync.list_conflicts() == [] and not b.dirty()
    assert {task.id for task in b.planning.occurrences_of_series([walk.id])[walk.id]} == {
        occurrence_task_id(walk.id, MON + timedelta(days=offset)) for offset in range(7)}


def test_a_pulled_copy_of_a_slot_this_device_also_materialized_converges_without_conflict(pair) -> None:
    server, a, b = pair
    a.sign_in("alice@example.com")
    b.sign_in("alice@example.com")
    walk = add_series(a)
    a.sync_now()
    b.sync_now()
    series_ops.expand_occurrences(a.planning, MON, MON + timedelta(days=2))
    series_ops.expand_occurrences(b.planning, MON, MON + timedelta(days=2))
    a.sync_now()
    engine, account = b.sync._engine, b.sync._engine.store.account(b.sync._account_key)
    page = b.transport.pull(b.sync._token, account.pull_cursor, 200)  # B pulls before it pushed its own copies
    outcome = engine.apply_pull_page(account, page)
    assert outcome.conflicts == [] and b.sync.list_conflicts() == []
    assert not [entry for entry in b.dirty() if entry[0] == "task"]  # nothing left to send for those slots
    assert b.sync_now().status == "ok" and len(server_tasks(server, walk.id)) == 3


def test_a_slot_skipped_before_it_ever_synced_stays_reserved_everywhere(pair) -> None:
    server, a, b = pair
    a.sign_in("alice@example.com")
    walk = add_series(a)
    created = series_ops.expand_occurrences(a.planning, MON, MON + timedelta(days=2)).created
    series_ops.delete_occurrence(a.planning, created[1].id, expected_version=1, skip=True)
    assert a.sync_now().status == "ok"
    on_server = {task["occurrence_slot"]: task for task in server_tasks(server, walk.id)}
    skipped = on_server[(MON + timedelta(days=1)).isoformat()]
    assert skipped["deleted_at"] is not None and skipped["occurrence_state"] == "skipped"

    b.sign_in("alice@example.com")
    b.sync_now()  # a fresh device
    assert series_ops.expand_occurrences(b.planning, MON, MON + timedelta(days=2)).created == []
    stored = b.planning.get_tasks_including_deleted([created[1].id])[created[1].id]
    assert stored.deleted_at is not None and stored.occurrence_state == OccurrenceState.SKIPPED
    assert b.sync_now().conflicts == 0


def test_concurrent_rule_edits_conflict_instead_of_minting_another_occurrence(pair) -> None:
    server, a, b = pair
    a.sign_in("alice@example.com")
    b.sign_in("alice@example.com")
    walk = add_series(a)
    series_ops.expand_occurrences(a.planning, MON, MON + timedelta(days=5))
    a.sync_now()
    b.sync_now()

    stored_a = a.planning.get_task(walk.id)
    series_ops.edit_series(a.planning, stored_a.model_copy(update={"name": "Run"}), expected_version=stored_a.version,
                           scope=EditScope.FUTURE, cutoff=MON + timedelta(days=3))
    stored_b = b.planning.get_task(walk.id)
    series_ops.edit_series(b.planning, stored_b.model_copy(update={"priority": 9}), expected_version=stored_b.version,
                           scope=EditScope.SERIES)
    a.transport.pushed.clear()
    assert a.sync_now().status == "ok"
    pushed_groups = [op["group"] for batch in a.transport.pushed for op in batch if op["entity_type"] == "task"]
    assert pushed_groups and len(set(pushed_groups)) == 1 and None not in pushed_groups  # one atomic unit

    report = b.sync_now()
    assert report.conflicts > 0  # B's edit was based on the old series version: refused, never merged silently
    live = [task for task in server_tasks(server, walk.id) if task["deleted_at"] is None]
    assert len({task["occurrence_slot"] for task in live}) == len(live) == 3  # nothing minted twice
    for conflict in b.sync.list_conflicts():
        b.sync.resolve_conflict(conflict.id, "accept_remote")
    b.sync_now()
    assert b.planning.get_task(walk.id).recurrence.end_date == MON + timedelta(days=2)
    successors = b.planning.series_successors([walk.id]).get(walk.id, [])
    assert [task.name for task in successors] == ["Run"]


def test_a_cross_date_move_of_an_occurrence_reaches_the_other_device_as_one_unit(pair) -> None:
    server, a, b = pair
    a.sign_in("alice@example.com")
    b.sign_in("alice@example.com")
    walk = add_series(a)
    assert a.controller.schedule_range(MON, MON).ok
    a.sync_now()
    occurrence = a.planning.get_task(occurrence_task_id(walk.id, MON))
    [placement] = a.planning.active_placements_for_tasks([occurrence.id])[occurrence.id]
    tuesday = MON + timedelta(days=1)
    start = datetime(tuesday.year, tuesday.month, tuesday.day, 18, tzinfo=timezone.utc)
    workflow.reschedule_placement(a.planning, placement.id, expected_version=placement.version, planned_date=tuesday,
                                  timezone_name="UTC", planned_start=start, planned_end=start + timedelta(minutes=60))
    a.transport.pushed.clear()
    assert a.sync_now().status == "ok"
    [batch] = [batch for batch in a.transport.pushed if any(op["kind"] == "action" for op in batch)]
    unit = [(op["entity_type"], op["kind"], op["group"]) for op in batch if op["group"] is not None]
    # The occurrence's new date and the move are one atomic group: neither can apply without the other.
    assert ("task", "update", unit[0][2]) in unit and ("placement", "action", unit[0][2]) in unit

    b.sync_now()
    moved = b.planning.get_task(occurrence.id)
    assert (moved.required_date, moved.occurrence_slot, moved.occurrence_state) == (
        tuesday, MON, OccurrenceState.MODIFIED)
    assert [p.planned_date for p in b.planning.active_placements_for_tasks([occurrence.id])[occurrence.id]] == [tuesday]


class LegacyServerTransport(InProcessTransport):
    """A server from before recurrence expansion: no /sync/capabilities features."""

    def capabilities(self, token: str) -> dict:
        return dict(LEGACY_CAPABILITIES)


def test_an_older_server_never_receives_recurrence_data_it_would_drop(alice_server, make_device) -> None:
    legacy = make_device("legacy", LegacyServerTransport(alice_server.client))
    legacy.sign_in("alice@example.com")
    ordinary = legacy.add_task("Report")
    walk = add_series(legacy)
    series_ops.expand_occurrences(legacy.planning, MON, MON + timedelta(days=1))
    report = legacy.sync_now()
    # The series, its two occurrences -- and the two task types (the report's and the series'), which a server
    # without "task_types" does not know either.
    assert report.status == "ok" and report.held == 5 and "recurring" in report.message
    ids = {task["id"] for task in alice_server.get("alice@example.com", "/tasks", limit=500)["items"]}
    assert str(ordinary.id) in ids and str(walk.id) not in ids  # held here, not sent to be stripped
    assert {entry[1] for entry in legacy.dirty() if entry[0] == "task"} >= {str(walk.id)}

    legacy.transport.__class__ = InProcessTransport  # the server is upgraded
    legacy.sync._capabilities = None
    assert legacy.sync_now().held == 0
    on_server = server_tasks(alice_server, walk.id)
    assert len(on_server) == 2
    stored_series = alice_server.get("alice@example.com", f"/tasks/{walk.id}")
    assert stored_series["recurrence"]["start_date"] == MON.isoformat()


def test_capability_answers_are_asked_once_per_session(pair) -> None:
    server, a, _ = pair
    calls = []
    original = a.transport.capabilities

    def counting(token: str) -> dict:
        calls.append(token)
        return original(token)

    a.transport.capabilities = counting
    a.sign_in("alice@example.com")
    a.sync_now()
    a.sync_now()
    assert len(calls) == 1  # cached per transport and token
