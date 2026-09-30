"""The Day board's outcomes through synchronization: every move (including a reopen, and a
reopen followed by a different finish) is sent as the execution's lifecycle actions, reaches a
second device, survives signing out and in again, and the server keeps each step in its
change-log history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.execution.lifecycle import TaskOutcome
from app.execution.models import ExecutionStatus
from app.planning.models import ScheduledTask
from tests.sync.conftest import MON, InProcessTransport


@pytest.fixture
def pair(alice_server, make_device):
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    return alice_server, a, b


def placed(device, *specs: tuple[str, int]):
    """Tasks saved on MON at these hours (name, hour), and their saved placements, in order."""
    pairs = []
    for name, hour in specs:
        task = device.add_task(name, required_date=MON)
        start = datetime(MON.year, MON.month, MON.day, hour, tzinfo=timezone.utc)
        pairs.append((task, ScheduledTask(task_id=task.id, planned_date=MON, timezone="UTC", planned_start=start,
                                          planned_end=start + timedelta(hours=1))))
    device.planning.replace_placements(MON, MON, [placement for _, placement in pairs])
    return [item for pair in pairs for item in pair]


def statuses(device, *placements) -> list[str | None]:
    found = device.executions.executions_for_placements([placement.id for placement in placements])
    return [found[p.id].status.value if p.id in found else None for p in placements]


def test_every_move_reaches_another_device_and_survives_signing_in_again(pair) -> None:
    server, a, b = pair
    a.sign_in("alice@example.com")
    first_task, first, second_task, second = placed(a, ("Read", 9), ("Read", 13))  # same name, own placements
    a.executions.set_outcome(first_task, first, TaskOutcome.COMPLETED)
    a.executions.set_outcome(second_task, second, TaskOutcome.UNCOMPLETED)
    assert a.sync_now().status == "ok"

    b.sign_in("alice@example.com")
    assert b.sync_now().status == "ok"
    assert statuses(b, first, second) == ["completed", "skipped"]

    # Back to Tasks on A (a reopen), then finished differently: skipped -> completed, completed -> pending.
    a.executions.set_outcome(first_task, first, TaskOutcome.PENDING)
    a.executions.set_outcome(second_task, second, TaskOutcome.COMPLETED)
    report = a.sync_now()
    assert report.status == "ok" and a.dirty() == []
    b.sync_now()
    assert statuses(b, first, second) == ["scheduled", "completed"]
    assert b.dirty() == []  # applying pulled records produced nothing to send back

    # B answers too; A sees it. A then signs out and in again: the saved statuses come back unchanged.
    b.executions.set_outcome(first_task, first, TaskOutcome.UNCOMPLETED)
    b.sync_now()
    a.sync_now()
    assert statuses(a, first, second) == ["skipped", "completed"]
    a.reopen(keep_session=False)
    a.sign_in("alice@example.com", associate=False)
    a.sync_now()
    assert statuses(a, first, second) == ["skipped", "completed"]

    remote = {item["scheduled_task_id"]: item["status"]
              for item in server.get("alice@example.com", "/executions")["items"]}
    assert remote == {str(first.id): "skipped", str(second.id): "completed"}
    history = [change["record"]["status"] for change in server.changes("alice@example.com")
               if change["entity_type"] == "execution" and change["entity_id"] == str(
                   a.executions.find_execution_for_placement(second.id).id)]
    assert history[-3:] == ["skipped", "scheduled", "completed"]  # the reopen is a recorded step, not a rewrite


def test_a_timed_attempt_reopened_offline_keeps_its_sessions_on_the_server(pair) -> None:
    server, a, b = pair
    a.sign_in("alice@example.com")
    task, placement = placed(a, ("Deep work", 9))
    execution = a.executions.get_or_create_canonical_execution(task, placement)
    a.executions.start(execution.id)
    a.executions.complete(execution.id)
    a.sync_now()
    a.executions.set_outcome(task, placement, TaskOutcome.PENDING)  # reopened: paused, sessions kept
    assert a.executions.get_execution(execution.id).status == ExecutionStatus.PAUSED
    assert a.sync_now().status == "ok"
    [remote] = server.get("alice@example.com", "/executions")["items"]
    assert remote["status"] == "paused" and len(remote["sessions"]) == 1 and remote["actual_final_end_at"] is None
    assert remote["actual_first_start_at"] is not None


def test_points_and_their_snapshot_reach_the_server_and_another_device(pair) -> None:
    server, a, b = pair
    a.sign_in("alice@example.com")
    task, placement = placed(a, ("Essay", 9))
    stored = a.planning.get_task(task.id)
    a.planning.update_task(stored.model_copy(update={"points": 8}), expected_version=stored.version)
    a.executions.set_outcome(a.planning.get_task(task.id), placement, TaskOutcome.COMPLETED)
    assert a.sync_now().status == "ok"
    assert server.get("alice@example.com", f"/tasks/{task.id}")["points"] == 8
    [remote] = server.get("alice@example.com", "/executions")["items"]
    assert remote["points"] == 8 and remote["status"] == "completed"

    b.sign_in("alice@example.com")
    b.sync_now()
    assert b.planning.get_task(task.id).points == 8
    assert b.executions.find_execution_for_placement(placement.id).points == 8
