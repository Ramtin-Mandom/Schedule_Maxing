"""The Week/Month selected-day panel's model through the desktop's own services (no display): the
bulk "All Tasks Complete" / "No Tasks Complete" on the SAME state as the Day page's board, only
scheduled tasks affected, one transaction, persistence across a restart; the calendar's past-only
classification from one batched read; and task points -- stored, kept through scheduling,
aggregated, and never confused with the optimizer's placement score."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from app.execution.lifecycle import TaskOutcome
from app.planning.models import LocalTimeWindow, Task
from app.productivity.day_summary import DayStatusClass
from app.ui import background
from app.ui.app_services import open_app_services
from app.ui.calendar_controller import CalendarController
from app.ui.day_controller import DayScheduleController
from app.ui.day_outcomes import DayOutcomeController
from app.ui.task_status import TaskStatusController
from app.ui.task_form_model import TaskDraft, build_task

TODAY = date(2026, 9, 24)
PAST = TODAY - timedelta(days=2)
TZ = "America/Vancouver"
P, C, U = TaskOutcome.PENDING, TaskOutcome.COMPLETED, TaskOutcome.UNCOMPLETED


@pytest.fixture(autouse=True)
def _restore_installed_registry():
    previous = background.current_registry()
    yield
    background.install_registry(previous)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "outcomes.db"


@pytest.fixture
def services(db_path: Path, tmp_path: Path):
    opened = open_app_services(db_path, timezone=TZ, project_root=str(tmp_path))
    yield opened
    opened.close()


def ok(result):
    assert result.ok, result.error
    return result.value


def plan_day(services, day: date, names: list[str], points: int = 2) -> None:
    for index, name in enumerate(names):
        start = 480 + 70 * index
        ok(services.planning_controller.add_or_update_task(Task(
            name=name, category="study", estimated_duration_minutes=60, priority=5, points=points,
            preferred_dates=[day], preferred_time_window=LocalTimeWindow(start_minute=start, end_minute=start + 60))))
    ok(DayScheduleController(services.planning_controller, anchor_date=day, timezone=TZ).make_schedule())


def board_columns(services, day: date) -> dict[TaskOutcome, list[str]]:
    page = DayScheduleController(services.planning_controller, anchor_date=day, timezone=TZ)
    board = ok(TaskStatusController(services.execution_controller).board(day, ok(page.load()).executables))
    return {outcome: sorted(card.name for card in board.column(outcome)) for outcome in (U, P, C)}


def outcomes(services) -> DayOutcomeController:
    return DayOutcomeController(services.planning_controller, services.execution_controller)


def test_bulk_actions_share_the_day_boards_state(services) -> None:
    plan_day(services, PAST, ["A", "B", "C", "D", "E"])
    status = TaskStatusController(services.execution_controller)
    page = DayScheduleController(services.planning_controller, anchor_date=PAST, timezone=TZ)
    cards = {card.name: card for card in ok(status.board(PAST, ok(page.load()).executables)).cards}
    for name, target in (("A", C), ("B", C), ("C", C), ("D", U)):  # 3 completed, 1 uncompleted, 1 pending
        ok(status.move(cards[name], target))

    detail = ok(outcomes(services).detail(PAST))  # Week/Month read the same state the Day page wrote
    assert (detail.summary.completed_count, detail.summary.uncompleted_count, detail.summary.pending_count) == (3, 1, 1)
    assert detail.summary.status_class == DayStatusClass.MOSTLY_COMPLETED  # 60% completed
    assert {card.name: card.outcome for card in detail.board.cards}["E"] == P

    run = ok(outcomes(services).set_day(PAST, C))  # All Tasks Complete
    assert len(run.result.changed) == 2 and len(run.result.unchanged) == 3 and run.result.skipped == ()
    assert run.detail.summary.completed_count == 5
    assert run.detail.summary.status_class == DayStatusClass.MOSTLY_COMPLETED_STRONG
    assert board_columns(services, PAST) == {U: [], P: [], C: ["A", "B", "C", "D", "E"]}  # Day shows it too

    run = ok(outcomes(services).set_day(PAST, U))  # No Tasks Complete
    assert run.detail.summary.uncompleted_count == 5
    assert board_columns(services, PAST) == {U: ["A", "B", "C", "D", "E"], P: [], C: []}
    executions = ok(services.execution_controller.list_executions())
    assert len(executions) == 5  # one execution per placement, never a duplicate


def test_only_scheduled_tasks_are_affected_and_an_empty_day_is_a_no_op(services) -> None:
    plan_day(services, PAST, ["Placed"])
    ok(services.planning_controller.add_or_update_task(Task(  # on the date but never scheduled
        name="Not placed", category="study", estimated_duration_minutes=30, priority=5, preferred_dates=[PAST])))
    run = ok(outcomes(services).set_day(PAST, C))
    assert run.detail.summary.scheduled_count == 1 and run.detail.summary.completed_count == 1
    assert [e.task_name for e in ok(services.execution_controller.list_executions())] == ["Placed"]

    empty = ok(outcomes(services).set_day(PAST - timedelta(days=1), C))
    assert empty.result.changed == () and empty.detail.summary.scheduled_count == 0
    assert empty.detail.summary.status_class == DayStatusClass.NO_TASKS


def test_bulk_changes_persist_across_a_restart(db_path: Path, tmp_path: Path) -> None:
    first = open_app_services(db_path, timezone=TZ, project_root=str(tmp_path))
    try:
        plan_day(first, PAST, ["Kept", "Also kept"])
        ok(outcomes(first).set_day(PAST, C))
    finally:
        first.close()
    again = open_app_services(db_path, timezone=TZ, project_root=str(tmp_path))
    try:
        summary = ok(outcomes(again).detail(PAST)).summary
        assert summary.completed_count == 2 and summary.status_class == DayStatusClass.MOSTLY_COMPLETED_STRONG
    finally:
        again.close()


def test_calendar_colours_only_past_days_from_one_batched_read(services) -> None:
    plan_day(services, PAST, ["Old"])
    plan_day(services, TODAY, ["Now"])
    plan_day(services, TODAY + timedelta(days=1), ["Later"])
    ok(outcomes(services).set_day(PAST, U))
    ok(outcomes(services).set_day(TODAY, C))

    calls = []
    executions = services.execution_controller
    original = executions.executions_for_placements

    def counted(ids):
        calls.append(len(list(ids)) if not isinstance(ids, list) else len(ids))
        return original(ids)

    executions.executions_for_placements = counted
    try:
        month = CalendarController(services.planning_controller, mode="month", selected=TODAY, timezone=TZ,
                                   today=lambda: TODAY, executions=executions)
        snapshot = ok(month.load())
    finally:
        del executions.executions_for_placements
    assert len(calls) == 1  # the whole 42-day grid: one execution query, never one per date

    cells = {cell.date: cell for cell in snapshot.days}
    assert cells[PAST].status_class == DayStatusClass.MOSTLY_UNCOMPLETED_STRONG
    assert cells[PAST - timedelta(days=1)].status_class == DayStatusClass.NO_TASKS  # past, nothing scheduled
    assert cells[TODAY].status_class is None and cells[TODAY].summary.completed_count == 1  # today: normal look
    assert cells[TODAY + timedelta(days=1)].status_class is None  # future: normal look
    outside = next(cell for cell in snapshot.days if not cell.in_period and cell.date < TODAY)
    assert outside.status_class is None  # another month's days keep their quiet look

    week = CalendarController(services.planning_controller, mode="week", selected=TODAY, timezone=TZ,
                              today=lambda: TODAY, executions=executions)
    week_cells = {cell.date: cell for cell in ok(week.load()).days}
    assert week_cells[PAST].status_class == cells[PAST].status_class  # Week and Month agree


def test_points_are_stored_scheduled_and_aggregated_but_never_the_optimizer_score(services) -> None:
    draft = TaskDraft(name="Essay", duration="60", points="8", date=PAST.isoformat())
    essay = ok(services.planning_controller.add_or_update_task(build_task(draft, timezone_name=TZ)))
    assert essay.points == 8 and ok(services.planning_controller.get_task(essay.id)).points == 8
    plan_day(services, PAST, ["Reading"], points=3)
    placements = ok(services.planning_controller.get_placements(PAST))
    assert {p.task_id for p in placements} >= {essay.id}  # the task keeps its identity through scheduling
    scores = {p.task_id: p.score for p in placements}

    page = DayScheduleController(services.planning_controller, anchor_date=PAST, timezone=TZ)
    assert ok(page.load()).freshness.value == "current"
    ok(services.planning_controller.add_or_update_task(
        ok(services.planning_controller.get_task(essay.id)).model_copy(update={"points": 50}),
        expected_version=ok(services.planning_controller.get_task(essay.id)).version))
    assert ok(page.load()).freshness.value == "current"  # points are not a scheduling input
    assert {p.task_id: p.score for p in ok(services.planning_controller.get_placements(PAST))} == scores

    status = TaskStatusController(services.execution_controller)
    card = next(c for c in ok(status.board(PAST, ok(page.load()).executables)).cards if c.name == "Essay")
    execution = ok(status.move(card, C))
    assert execution.points == 50  # the snapshot of the task's points when the answer was recorded
    ok(services.planning_controller.add_or_update_task(
        ok(services.planning_controller.get_task(essay.id)).model_copy(update={"points": 1}),
        expected_version=ok(services.planning_controller.get_task(essay.id)).version))
    summary = ok(outcomes(services).detail(PAST)).summary
    assert (summary.points_completed, summary.points_pending, summary.points_scheduled) == (50, 3, 53)
