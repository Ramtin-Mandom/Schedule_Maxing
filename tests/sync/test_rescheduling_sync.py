"""Explicit rescheduling across devices (docs/execution-rescheduling.md): an
offline move reaches the server as one atomic operation and the other
device as one consistent set of records; a lost response is retried under
the same op_id with nothing duplicated; competing moves, work started
elsewhere, and duplicate execution creation or starts leave exactly one
winner and give the loser a clear conflict that never overwrites history;
a move the server refuses leaves nothing behind; regeneration provenance
travels too."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.planning import workflow
from app.planning.application import RangeScope
from app.planning.models import FixedBlock, ScheduledTask
from tests.sync.conftest import MON, FlakyTransport, InProcessTransport

EMAIL = "alice@example.com"


def at(hour: int) -> datetime:
    return datetime(MON.year, MON.month, MON.day, hour, tzinfo=timezone.utc)


def plan(device, name: str = "Study", hour: int = 9, *, execute: bool = True):
    """A task with a placement on MON and (unless execute=False) its scheduled execution, on `device`."""
    task = device.add_task(name, required_date=MON)
    existing = device.planning.placements_for_date(MON)
    placement = ScheduledTask(task_id=task.id, user_id=task.user_id, planned_date=MON, timezone="UTC",
                              planned_start=at(hour), planned_end=at(hour + 1))
    device.planning.replace_placements(MON, MON, [*existing, placement],
                                       expected_versions={p.id: p.version for p in existing})
    placement = device.planning.get_placement(placement.id)
    execution = device.executions.get_or_create_canonical_execution(task, placement) if execute else None
    return task, placement, execution


def move(device, placement_id, hour: int):
    placement = device.planning.get_placement(placement_id)
    return workflow.reschedule_placement(device.planning, placement.id, expected_version=placement.version,
                                         planned_date=MON, timezone_name="UTC", planned_start=at(hour),
                                         planned_end=at(hour + 1))


def server_placements(server) -> dict[str, dict]:
    return {item["id"]: item for item in server.get(EMAIL, "/placements", include_deleted=True, limit=500)["items"]}


def server_execution(server, execution_id: str) -> dict:
    return server.get(EMAIL, f"/executions/{execution_id}", include_deleted=True)


@pytest.fixture
def pair(alice_server, make_device):
    a = make_device("a", FlakyTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    a.sign_in(EMAIL)
    b.sign_in(EMAIL)
    return alice_server, a, b


def test_an_offline_move_is_one_unit_on_the_server_and_on_the_other_device(pair) -> None:
    server, a, b = pair
    _, placement, execution = plan(a)
    assert a.sync_now().status == "ok" and b.sync_now().status == "ok"
    feed_before = len(server.changes(EMAIL))

    moved = move(a, placement.id, 14)  # offline
    assert a.executions.get_execution(execution.id).status.value == "cancelled"
    report = a.sync_now()
    assert report.status == "ok" and report.conflicts == 0

    records = server_placements(server)
    assert records[str(placement.id)]["removal_reason"] == "rescheduled"
    assert records[str(placement.id)]["superseded_by_id"] == str(moved.replacement.id)
    assert records[str(moved.replacement.id)]["deleted_at"] is None
    assert server_execution(server, execution.id)["status"] == "cancelled"
    assert len(server.changes(EMAIL)) == feed_before + 3  # tombstone, replacement, attempt -- nothing else
    assert a.dirty() == [] and a.sync_now().pushed == 0  # the related records became their shadows

    assert b.sync_now().status == "ok"
    old = b.planning.get_placement(placement.id, include_deleted=True)
    assert old.deleted_at is not None and old.superseded_by_id == moved.replacement.id
    assert [p.id for p in b.planning.placements_for_date(MON)] == [moved.replacement.id]
    assert b.executions.get_execution(execution.id).status.value == "cancelled"
    assert b.dirty() == []


def test_a_lost_response_is_retried_under_the_same_op_id_without_duplicates(pair) -> None:
    server, a, _ = pair
    _, placement, execution = plan(a)
    a.sync_now()
    moved = move(a, placement.id, 14)

    a.transport.lose_push_response = 1
    assert a.sync_now().status == "offline"  # the server applied it; the answer was lost
    applied = (server_placements(server), server_execution(server, execution.id), len(server.changes(EMAIL)))

    report = a.sync_now()  # resent with the same op_id: the recorded outcome answers
    assert report.status == "ok" and report.conflicts == 0
    assert (server_placements(server), server_execution(server, execution.id), len(server.changes(EMAIL))) == applied
    assert set(applied[0]) == {str(placement.id), str(moved.replacement.id)}
    assert a.dirty() == []


def test_competing_moves_leave_one_winner_and_a_clear_conflict_for_the_loser(pair) -> None:
    server, a, b = pair
    _, placement, execution = plan(a)
    a.sync_now()
    b.sync_now()

    winner = move(a, placement.id, 14)
    loser = move(b, placement.id, 16)
    assert a.sync_now().status == "ok"
    report = b.sync_now()
    assert report.conflicts == 1
    [conflict] = b.sync.list_conflicts()
    assert conflict.entity_id == str(placement.id) and conflict.error["code"] == "deleted"
    assert conflict.remote_record["superseded_by_id"] == str(winner.replacement.id)
    assert str(loser.replacement.id) not in server_placements(server)  # the loser's move never half-applied

    b.sync.resolve_conflict(conflict.id, "accept_remote")
    assert b.sync_now().status == "ok"
    assert [p.id for p in b.planning.placements_for_date(MON)] == [winner.replacement.id]
    assert b.planning.get_placement(loser.replacement.id, include_deleted=True).deleted_at is not None
    assert b.executions.get_execution(execution.id).status.value == "cancelled"
    assert b.dirty() == [] and b.sync.list_conflicts() == []
    assert str(loser.replacement.id) not in server_placements(server)


def test_work_started_elsewhere_wins_over_an_offline_move(pair) -> None:
    server, a, b = pair
    _, placement, execution = plan(a)
    a.sync_now()
    b.sync_now()

    a.executions.start(execution.id)
    assert a.sync_now().status == "ok"
    move(b, placement.id, 14)  # B had not seen the start
    b.sync_now()

    kinds = {(c.entity_type, c.error["code"]) for c in b.sync.list_conflicts()}
    assert ("placement", "history_protected") in kinds
    started = server_execution(server, execution.id)
    assert started["status"] == "in_progress" and len(started["sessions"]) == 1  # history untouched
    assert server_placements(server)[str(placement.id)]["deleted_at"] is None

    for conflict in b.sync.list_conflicts():
        b.sync.resolve_conflict(conflict.id, "accept_remote")
    assert b.sync_now().status == "ok"
    assert [p.id for p in b.planning.placements_for_date(MON)] == [placement.id]
    assert b.executions.get_execution(execution.id).status.value == "in_progress"
    assert server_execution(server, execution.id) == started and b.sync.list_conflicts() == []


def test_two_devices_creating_an_execution_for_one_placement_keep_exactly_one(pair) -> None:
    server, a, b = pair
    task, placement, _ = plan(a, execute=False)
    a.sync_now()
    b.sync_now()

    mine = a.executions.get_or_create_canonical_execution(task, placement)  # both offline, the same placement
    theirs = b.executions.get_or_create_canonical_execution(b.planning.get_task(task.id), placement)
    assert mine.id != theirs.id
    assert a.sync_now().status == "ok"
    b.sync_now()

    assert "already_exists" in {conflict.error["code"] for conflict in b.sync.list_conflicts()}
    on_server = [e["id"] for e in server.get(EMAIL, "/executions", limit=500)["items"]
                 if e["scheduled_task_id"] == str(placement.id)]
    assert on_server == [mine.id]

    for conflict in b.sync.list_conflicts():
        b.sync.resolve_conflict(conflict.id, "accept_remote")
    assert b.sync_now().status == "ok"
    assert b.executions.find_execution_for_placement(placement.id).id == mine.id
    assert b.sync.list_conflicts() == []


def test_both_devices_starting_one_execution_have_one_winner(pair) -> None:
    server, a, b = pair
    _, _, execution = plan(a)
    a.sync_now()
    b.sync_now()

    a.executions.start(execution.id)
    b.executions.start(execution.id)  # offline, from the same scheduled state
    assert a.sync_now().status == "ok"
    b.sync_now()

    [conflict] = b.sync.list_conflicts()
    assert conflict.entity_type == "execution" and conflict.error["code"] == "version_conflict"
    assert len(server_execution(server, execution.id)["sessions"]) == 1  # one start, never two
    b.sync.resolve_conflict(conflict.id, "accept_remote")
    assert b.sync_now().status == "ok"
    assert len(b.executions.list_sessions(execution.id)) == 1
    assert b.executions.get_execution(execution.id).status.value == "in_progress"


def test_a_chain_of_offline_moves_reaches_the_server_in_order(pair) -> None:
    server, a, b = pair
    _, placement, _ = plan(a)
    a.sync_now()
    second = move(a, placement.id, 12).replacement
    third = move(a, second.id, 15).replacement

    assert a.sync_now().status == "ok"
    records = server_placements(server)
    assert records[str(placement.id)]["superseded_by_id"] == str(second.id)
    assert records[str(second.id)]["removal_reason"] == "rescheduled"
    assert records[str(second.id)]["superseded_by_id"] == str(third.id)
    assert [pid for pid, item in records.items() if item["deleted_at"] is None] == [str(third.id)]
    assert a.dirty() == []
    b.sync_now()
    assert [p.id for p in b.planning.placements_for_date(MON)] == [third.id]


def test_a_move_the_server_refuses_leaves_nothing_behind(pair) -> None:
    server, a, b = pair
    _, placement, execution = plan(a)
    a.sync_now()
    b.sync_now()
    b.planning.create_fixed_block(FixedBlock(label="Class", category="event", planned_date=MON, timezone="UTC",
                                             planned_start=at(14), planned_end=at(16)))
    b.sync_now()
    before = (server_placements(server), server_execution(server, execution.id))

    moved = move(a, placement.id, 14)  # valid for what A has seen; A has not pulled the class yet
    a.sync_now()
    [conflict] = a.sync.list_conflicts()
    assert conflict.error["code"] == "reschedule_rejected" and conflict.error["reason"] == "overlaps_fixed_block"
    assert (server_placements(server), server_execution(server, execution.id)) == before

    a.sync.resolve_conflict(conflict.id, "accept_remote")
    assert a.sync_now().status == "ok"
    assert [p.id for p in a.planning.placements_for_date(MON)] == [placement.id]
    assert a.planning.get_placement(moved.replacement.id, include_deleted=True).deleted_at is not None
    assert a.executions.get_execution(execution.id).status.value == "scheduled"
    assert a.dirty() == [] and (server_placements(server), server_execution(server, execution.id)) == before


def test_regeneration_provenance_reaches_the_other_device(pair) -> None:
    server, a, b = pair
    task = a.add_task("Study", required_date=MON)
    assert a.controller.schedule_range(MON, MON, scope=RangeScope.ELIGIBLE).ok
    [first] = a.planning.placements_for_date(MON)
    a.sync_now()
    a.controller.add_or_update_task(task.model_copy(update={"estimated_duration_minutes": 90}),
                                    expected_version=a.planning.get_task(task.id).version)
    assert a.controller.schedule_range(MON, MON, scope=RangeScope.ELIGIBLE).ok
    [second] = a.planning.placements_for_date(MON)
    assert a.sync_now().status == "ok"

    record = server_placements(server)[str(first.id)]
    assert (record["removal_reason"], record["superseded_by_id"]) == ("regenerated", str(second.id))
    b.sync_now()
    old = b.planning.get_placement(first.id, include_deleted=True)
    assert old.removal_reason.value == "regenerated" and old.superseded_by_id == second.id
    assert old.planned_end - old.planned_start == timedelta(minutes=60)  # the original estimate, as planned


def test_a_plan_moved_and_regenerated_before_the_first_sync_keeps_its_lineage(pair) -> None:
    server, a, b = pair
    task, placement, execution = plan(a)  # never synchronized yet
    moved = move(a, placement.id, 14).replacement
    third = move(a, moved.id, 16).replacement

    assert a.sync_now().status == "ok" and a.dirty() == []
    records = server_placements(server)
    assert records[str(placement.id)]["removal_reason"] == "rescheduled"
    assert records[str(placement.id)]["superseded_by_id"] == str(moved.id)
    assert records[str(moved.id)]["superseded_by_id"] == str(third.id)
    assert records[str(placement.id)]["deleted_at"] and records[str(third.id)]["deleted_at"] is None
    assert server_execution(server, execution.id)["status"] == "cancelled"  # history, linked to the original

    b.sync_now()
    original = b.planning.get_placement(placement.id, include_deleted=True)
    assert original.planned_start == at(9) and original.superseded_by_id == moved.id
    assert [p.id for p in b.planning.placements_for_date(MON)] == [third.id] and b.dirty() == []
