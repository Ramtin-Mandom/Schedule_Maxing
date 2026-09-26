"""Headless tests of the Week and Month calendars (Milestone 4, Prompt 5): real month lengths
(28, leap 29, 30, 31), Monday-first weekday placement, week/month/year boundaries, the
current-year month choice and planning-timezone today, then the CalendarController through the
desktop's own services: unscheduled tasks in input order without invented times versus saved
work in chronological order, fixed-block categories, preserved past data, selected-date task
creation, and previewed, atomic Reset Week/Month that never touches out-of-month cells."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.planning.models import FixedBlock, LocalTimeWindow, Task
from app.ui import background
from app.ui.app_services import open_app_services
from app.ui.calendar_controller import CalendarController
from app.ui.calendar_model import (
    Period,
    days_in_month,
    month_grid,
    move_months,
    shift_month,
    week_of,
    week_start,
)
from app.ui.day_controller import DayScheduleController

TZ = "Asia/Tokyo"
WED = date(2026, 9, 23)


# -----------------------------------------------------------------------------
# Calendar arithmetic
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("year, month, length", [
    (2026, 2, 28), (2024, 2, 29), (2000, 2, 29), (1900, 2, 28), (2026, 4, 30), (2026, 1, 31), (2026, 12, 31)])
def test_every_month_length_and_weekday_alignment(year: int, month: int, length: int) -> None:
    assert days_in_month(year, month) == length
    grid = month_grid(year, month)
    flat = [day for week in grid for day in week]
    assert 4 <= len(grid) <= 6 and all(len(week) == 7 for week in grid)
    assert all(day.weekday() == index % 7 for index, day in enumerate(flat))  # Monday first, every row
    in_month = [day for day in flat if day.month == month]
    assert in_month == [date(year, month, number) for number in range(1, length + 1)]
    period = Period.for_date("month", date(year, month, min(15, length)))
    assert (period.start, period.end) == (date(year, month, 1), date(year, month, length))
    assert len(period.dates) == length and len(period.grid_dates) == 7 * len(grid) <= 42


def test_a_month_starting_on_monday_needs_only_four_rows_and_out_of_month_cells_are_neighbours() -> None:
    assert len(month_grid(2027, 2)) == 4  # Feb 1 2027 is a Monday; 28 days
    grid = month_grid(2026, 9)  # Sep 1 2026 is a Tuesday
    assert grid[0][0] == date(2026, 8, 31) and grid[-1][-1] == date(2026, 10, 4)


def test_week_month_and_year_boundaries() -> None:
    assert week_start(date(2026, 1, 1)) == date(2025, 12, 29)  # Monday start, across the year
    assert week_of(date(2025, 12, 31)) == [date(2025, 12, 29) + timedelta(days=n) for n in range(7)]
    period = Period.for_date("week", date(2026, 1, 1))
    assert period.title == "Week of Dec 29, 2025 – Jan 4, 2026"
    assert Period.for_date("week", WED).title == "Week of Sep 21 – 27, 2026"
    assert shift_month(2026, 12, 1) == (2027, 1) and shift_month(2026, 1, -1) == (2025, 12)
    assert move_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert move_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    december = Period.for_date("month", date(2026, 12, 10))
    assert december.shifted(1).title == "January 2027" and december.shifted(1).selected == date(2027, 1, 10)
    assert Period.for_date("week", date(2026, 12, 30)).shifted(1).start == date(2027, 1, 4)


# -----------------------------------------------------------------------------
# The presenter
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_installed_registry():
    previous = background.current_registry()
    yield
    background.install_registry(previous)


@pytest.fixture
def services(tmp_path: Path):
    opened = open_app_services(tmp_path / "calendar.db", timezone=TZ, project_root=str(tmp_path))
    yield opened
    opened.close()


def ok(result):
    assert result.ok, result.error
    return result.value


def calendar(services, mode: str, selected: date = WED, today: date = WED) -> CalendarController:
    return CalendarController(services.planning_controller, mode=mode, selected=selected, timezone=TZ,
                              today=lambda: today)


def add(services, name: str, day: date | None, *, window=(540, 720), duration=60, **metadata) -> Task:
    return ok(services.planning_controller.add_or_update_task(Task(
        name=name, category="study", estimated_duration_minutes=duration, priority=5, **metadata,
        preferred_dates=[day] if day else [],
        preferred_time_window=LocalTimeWindow(start_minute=window[0], end_minute=window[1]))))


def block(services, label: str, day: date, start: int, end: int, category: str = "exercise") -> FixedBlock:
    tz = ZoneInfo(TZ)
    return ok(services.planning_controller.save_fixed_block(FixedBlock(
        label=label, category=category, planned_date=day, timezone=TZ,
        planned_start=datetime(day.year, day.month, day.day, start, tzinfo=tz),
        planned_end=datetime(day.year, day.month, day.day, end, tzinfo=tz))))


def test_today_and_the_current_years_months(services) -> None:
    real = CalendarController(services.planning_controller, mode="month", selected=WED, timezone=TZ)
    assert real.today() == datetime.now(ZoneInfo(TZ)).date()  # the planning timezone's date
    choices = calendar(services, "month").month_choices()
    assert [label for *_, label in choices][:2] == ["January 2026", "February 2026"] and len(choices) == 12
    page = calendar(services, "month", selected=date(2026, 3, 31))
    assert not page.go_to_month(2026, 3) and page.go_to_month(2026, 2)
    assert page.selected_date == date(2026, 2, 28)  # the day of the month is clamped


def test_unscheduled_in_input_order_then_chronological_work_with_fixed_categories(services) -> None:
    # Explicit creation instants avoid Windows clock-resolution ties (whose documented
    # repository tie-breaker is UUID, not insertion order).
    created = datetime(2026, 9, 1, tzinfo=ZoneInfo("UTC"))
    for index, name in enumerate(("Zeta", "Alpha", "Mid")):
        add(services, name, WED, created_at=created + timedelta(seconds=index))
    block(services, "Swim", WED, 7, 8)
    week = calendar(services, "week")
    cell = ok(week.load()).day(WED)
    assert [(item.kind, item.name) for item in cell.items] == [
        ("fixed", "Swim"), ("unscheduled", "Zeta"), ("unscheduled", "Alpha"), ("unscheduled", "Mid")]
    assert cell.items[0].category == "exercise" and cell.items[0].time_text == "7:00 AM – 8:00 AM"
    assert all(item.start_minute is None for item in cell.untimed)  # never an invented time

    ok(DayScheduleController(services.planning_controller, anchor_date=WED, timezone=TZ).make_schedule())
    cell = ok(week.load()).day(WED)
    starts = [item.start_minute for item in cell.items]
    assert all(item.timed for item in cell.items) and starts == sorted(starts)
    assert {item.kind for item in cell.items} == {"fixed", "scheduled"}
    assert cell.freshness_label == "Current"
    assert cell.items[1].text.startswith("9:00 AM – 10:00 AM  ")

    month = ok(calendar(services, "month").load())
    assert month.day(WED).items == cell.items and len(month.days) == 35
    assert not month.day(date(2026, 8, 31)).in_period and month.day(date(2026, 9, 1)).in_period


def test_past_days_stay_visible_and_new_tasks_start_on_the_selected_date(services) -> None:
    add(services, "Old work", date(2026, 9, 21))
    later = calendar(services, "week", today=date(2026, 9, 25))
    snapshot = ok(later.load())
    monday = snapshot.day(date(2026, 9, 21))
    assert monday.is_past and [item.name for item in monday.items] == ["Old work"]  # muted, never removed
    assert snapshot.day(date(2026, 9, 25)).is_today

    assert not later.select(date(2026, 9, 26))  # inside the week: no reload needed
    assert later.blank_draft().date == "2026-09-26"
    assert later.select(date(2026, 10, 2))  # another week
    assert later.period.start == date(2026, 9, 28)


def test_reset_month_is_previewed_atomic_and_leaves_out_of_month_cells_alone(services) -> None:
    add(services, "September", WED)
    outside = add(services, "Visible in the grid, but October", date(2026, 10, 2))
    block(services, "Swim", WED, 7, 8)
    month = calendar(services, "month")
    grid = ok(month.load())
    assert grid.day(date(2026, 10, 2)).items[0].name.startswith("Visible")

    plan = ok(month.reset_plan())
    assert plan.message.startswith("Reset September 2026?") and "1 task(s) planned for this month" in plan.message
    stored = {task.name for task in ok(services.planning_controller.list_tasks())}
    # Cancelling is simply not applying the plan: nothing was written.
    assert stored == {"September", "Visible in the grid, but October"}

    add(services, "Added after the preview", WED)
    refused = month.reset_period(plan)
    assert not refused.ok and "changed since the reset was previewed" in refused.error
    assert len(ok(services.planning_controller.list_tasks())) == 3  # all or nothing

    reset = ok(month.reset_period(ok(month.reset_plan())))
    assert [task.id for task in ok(services.planning_controller.list_tasks())] == [outside.id]
    assert reset.day(WED).items == [] and reset.day(date(2026, 10, 2)).items


def test_a_week_reset_that_would_break_a_dependency_outside_it_is_refused(services) -> None:
    inside = add(services, "Inside", WED)
    ok(services.planning_controller.add_or_update_task(Task(
        name="Next week", category="study", estimated_duration_minutes=30, priority=5,
        preferred_dates=[WED + timedelta(days=7)], dependency_ids=[inside.id])))
    plan = ok(calendar(services, "week").reset_plan())
    assert plan.blocked and "Inside is needed by Next week" in plan.message
