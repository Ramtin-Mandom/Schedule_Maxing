"""Reset All Task Data across devices, plus the end-to-end integration sequence of this series.

The reset runs on the server first and only then on this device; an offline or signed-out device
deletes nothing; afterwards nothing this device had queued is ever sent again, another device drops
its copies on its next pull, and a stale offline edit there becomes a conflict -- never a revival."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.execution.lifecycle import TaskOutcome
from app.planning.models import LocalTimeWindow, Task
from app.planning.preferences import DayWindowSpec, PreferenceOverrides
from app.productivity.day_summary import DayStatusClass
from app.sync.transport import TransportError
from app.ui.calendar_controller import CalendarController
from app.ui.day_controller import DayScheduleController
from app.ui.day_outcomes import DayOutcomeController
from app.ui.execution_controller import ExecutionController
from app.ui.task_status import TaskStatusController
from tests.sync.conftest import MON, FlakyTransport, InProcessTransport


@pytest.fixture
def pair(alice_server, make_device):
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    return alice_server, a, b


def counts(device) -> dict[str, int]:
    tables = ("tasks", "scheduled_tasks", "fixed_blocks", "executions", "work_sessions", "schedule_generations",
              "projects", "preference_overrides")
    return {table: device.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def add(device, name: str, *, window: tuple[int, int] | None = None, points: int = 1, day: date = MON) -> Task:
    fields = dict(preferred_dates=[day], points=points)
    if window is not None:
        fields["preferred_time_window"] = LocalTimeWindow(start_minute=window[0], end_minute=window[1])
    return device.add_task(name, **fields)


def test_reset_clears_server_and_device_and_nothing_is_sent_again(pair) -> None:
    server, a, b = pair
    a.sign_in("alice@example.com")
    b.sign_in("alice@example.com")
    add(a, "Synced")
    a.add_block()
    a.controller.set_date_overrides(MON, PreferenceOverrides(day_window=DayWindowSpec(start_minute=480, end_minute=1260)))
    a.sync_now()
    b.sync_now()
    add(a, "Offline, never sent")  # queued on A only
    stored = b.planning.list_tasks()[0]
    b.planning.update_task(stored.model_copy(update={"priority": 9}), expected_version=stored.version)  # B, offline

    removed = a.sync.reset_task_data()
    assert removed["task"] == 2 and removed["fixed_block"] == 1
    after = counts(a)
    assert {k: v for k, v in after.items() if k != "preference_overrides"} == dict.fromkeys(after.keys() - {
        "preference_overrides"}, 0)
    assert after["preference_overrides"] == 1  # the per-date day window is a setting: kept
    assert a.dirty() == [] and a.sync._engine.store.pending_ops(a.sync.account.account_key) == []
    assert a.sync_now().pushed == 0  # the offline-created task is not sent after all
    assert server.get("alice@example.com", "/tasks")["items"] == []
    assert server.get("alice@example.com", "/fixed-blocks")["items"] == []
    assert len(server.get("alice@example.com", "/preferences")["items"]) == 1

    report = b.sync_now()  # B's stale edit meets the server's tombstone: a conflict, not a revival
    assert server.get("alice@example.com", "/tasks")["items"] == []
    # The server refused it (the record is a tombstone there): B shows a conflict for the user to resolve.
    assert report.status == "ok" and report.conflicts == 1
    [conflict] = b.sync.list_conflicts()
    assert conflict.kind == "push_conflict" and conflict.entity_type == "task"
    b.sync.resolve_conflict(conflict.id, "accept_remote")  # taking the server's side removes B's copy too
    assert b.planning.list_tasks() == []
    assert b.planning.fixed_blocks_for_date(MON) == []  # the untouched block is removed on B too


def test_a_device_that_cannot_reach_the_server_deletes_nothing(alice_server, make_device) -> None:
    flaky = FlakyTransport(alice_server.client)
    a = make_device("a", flaky)
    a.sign_in("alice@example.com")
    add(a, "Kept")
    a.sync_now()
    def unreachable(token):
        raise TransportError("connection refused (injected)")

    flaky.reset_task_data = unreachable
    with pytest.raises(TransportError):
        a.sync.reset_task_data()
    assert [task.name for task in a.planning.list_tasks()] == ["Kept"]
    assert len(alice_server.get("alice@example.com", "/tasks")["items"]) == 1

    a.sync._drop_token(a.sync._token)  # the sign-in expired; the account's workspace is still the active one
    with pytest.raises(RuntimeError, match="Sign in"):
        a.sync.reset_task_data()
    assert [task.name for task in a.planning.list_tasks()] == ["Kept"]  # refused: nothing deleted anywhere

    a.sync.sign_out()  # signed out: the device works in the ownerless workspace, which is all a reset touches
    assert a.sync.reset_task_data() == dict.fromkeys(("execution", "placement", "schedule_generation", "fixed_block",
                                                      "task", "project"), 0)
    assert [task.name for task in a.planning.list_tasks()] == ["Kept"]  # the account's data is not this scope's


def test_the_ownerless_local_workspace_is_reset_on_this_device_only(alice_server, make_device) -> None:
    a = make_device("a", InProcessTransport(alice_server.client))
    add(a, "Local")  # no account: local data only
    a.add_block()
    a.controller.set_user_overrides(PreferenceOverrides(day_window=DayWindowSpec(start_minute=420, end_minute=1320)))
    removed = a.sync.reset_task_data()
    assert removed["task"] == 1 and removed["fixed_block"] == 1
    assert a.planning.list_tasks() == []
    assert [mark[0] for mark in a.dirty()] == ["preference"]  # only the kept setting still waits to be sent
    assert a.controller.user_preferences().value is not None  # settings stay


def test_the_whole_series_end_to_end(pair) -> None:
    server_, a, b = pair
    server_.register("bob@example.com")  # another user on the same server, whose data must stay untouched
    bob_task = server_.client.post("/tasks", json={"name": "Bob's", "category": "study", "estimated_duration_minutes": 30,
                                                   "priority": 5}, headers=server_.headers("bob@example.com"))
    assert bob_task.status_code == 201

    # 1-4. A user; tasks for "today" (MON); Make Schedule; every scheduled task starts pending.
    a.sign_in("alice@example.com")
    executions = ExecutionController(a.executions)
    day_page = DayScheduleController(a.controller, anchor_date=MON, timezone="UTC", today=lambda: MON)
    status = TaskStatusController(executions)
    add(a, "A", window=(480, 540), points=5)
    add(a, "B", window=(600, 660), points=3)
    assert day_page.make_schedule().ok

    def board():
        return status.board(MON, day_page.load().value.executables).value

    assert {card.name: card.outcome for card in board().cards} == {"A": TaskOutcome.PENDING, "B": TaskOutcome.PENDING}

    # 5-8. A completed, B uncompleted, C added, the schedule made again.
    cards = {card.name: card for card in board().cards}
    assert status.move(cards["A"], TaskOutcome.COMPLETED).ok
    assert status.move(cards["B"], TaskOutcome.UNCOMPLETED).ok
    add(a, "C", window=(720, 780), points=2)
    assert day_page.make_schedule().ok

    # 9-13. A stays completed, B uncompleted, C enters pending -- also after a sync and a re-read on B.
    expected = {"A": TaskOutcome.COMPLETED, "B": TaskOutcome.UNCOMPLETED, "C": TaskOutcome.PENDING}
    assert {card.name: card.outcome for card in board().cards} == expected
    assert a.sync_now().status == "ok"
    b.sign_in("alice@example.com")
    b.sync_now()
    b_page = DayScheduleController(b.controller, anchor_date=MON, timezone="UTC", today=lambda: MON)
    b_board = TaskStatusController(ExecutionController(b.executions)).board(MON, b_page.load().value.executables).value
    assert {card.name: card.outcome for card in b_board.cards} == expected

    # 14-16. Week and Month agree; MON is past from Wednesday's view; the points add up.
    wednesday = MON + timedelta(days=2)
    for mode in ("week", "month"):
        calendar = CalendarController(a.controller, mode=mode, selected=MON, timezone="UTC", today=lambda: wednesday,
                                      executions=executions)
        cell = calendar.load().value.day(MON)
        assert (cell.summary.completed_count, cell.summary.uncompleted_count, cell.summary.pending_count) == (1, 1, 1)
        assert cell.status_class == DayStatusClass.MIXED  # 1/3 each: none of the thresholds -> yellow
    detail = DayOutcomeController(a.controller, executions).detail(MON).value
    assert (detail.summary.points_scheduled, detail.summary.points_completed, detail.summary.points_uncompleted,
            detail.summary.points_pending) == (10, 5, 3, 2)
    remote = server_.get("alice@example.com", "/days/summary", start_date=MON.isoformat(), end_date=MON.isoformat())
    assert remote["days"][0]["points_completed"] == 5 and remote["days"][0]["status_class"] == "mixed"

    # 17-20. Reset: task, schedule and execution data gone everywhere; account and settings valid; Bob untouched.
    a.controller.set_user_overrides(PreferenceOverrides(day_window=DayWindowSpec(start_minute=480, end_minute=1200)))
    a.sync_now()
    a.sync.reset_task_data()
    assert a.planning.list_tasks() == [] and executions.list_executions().value == []
    assert board().cards == []
    for path in ("/tasks", "/placements", "/executions", "/schedule-generations"):
        assert server_.get("alice@example.com", path)["items"] == [], path
    assert server_.get("alice@example.com", "/me")["email"] == "alice@example.com"
    assert a.controller.user_preferences().value.overrides.day_window.start_minute == 480
    b.sync_now()
    assert b.planning.list_tasks() == [] and b.executions.list_executions() == []
    assert [task["name"] for task in server_.get("bob@example.com", "/tasks")["items"]] == ["Bob's"]
    a.sign_in("alice@example.com", associate=False)  # signing in again still works
    assert a.sync_now().status == "ok" and a.planning.list_tasks() == []
