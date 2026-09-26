"""Deadlines are judged in each date's planning timezone, never by the deadline's
UTC calendar date: range eligibility (repository/service), allocation
feasibility, and the day engine's intraday constraint (still authoritative)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.planning.allocation import AllocationReasonCode, allocate_tasks
from app.planning.application import PlanningService, RangeScope
from app.planning.models import Task, TaskRegistry
from app.planning.preferences import DayWindowSpec, PreferenceOverrides, resolve_day_preferences
from app.ui.planning_controller import PlanningController

TOKYO, LOS_ANGELES, NEW_YORK = ZoneInfo("Asia/Tokyo"), ZoneInfo("America/Los_Angeles"), ZoneInfo("America/New_York")
SEP_22, SEP_23, SEP_24 = date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)


def make_task(name: str, deadline: datetime, duration: int = 30, **extra) -> Task:
    return Task(name=name, category="study", estimated_duration_minutes=duration, priority=5, deadline=deadline, **extra)


def allocate(tasks: list[Task], dates: list[date], tz: str, window: DayWindowSpec | None = None):
    overrides = PreferenceOverrides(day_window=window) if window else None
    prefs = {d: resolve_day_preferences(date=d, timezone=tz, date_overrides=overrides) for d in dates}
    return allocate_tasks(
        start_date=dates[0], end_date=dates[-1], tasks=TaskRegistry(tasks={t.id: t for t in tasks}),
        task_ids=[t.id for t in tasks], preferences_by_date=prefs,
    )


def test_the_tokyo_morning_deadline_is_allocated_and_generated(planning_service: PlanningService) -> None:
    """Reproduction: due 2026-09-23 08:00 +09:00 (= 09-22 23:00 UTC), available 00:00-07:00 Tokyo on the 23rd."""
    task = planning_service.create_task(make_task("Submit form", datetime(2026, 9, 23, 8, tzinfo=TOKYO)))
    planning_service.save_date_preferences(
        SEP_23, PreferenceOverrides(day_window=DayWindowSpec(start_minute=0, end_minute=7 * 60))
    )
    controller = PlanningController(service=planning_service, timezone="Asia/Tokyo")

    assert [t.id for t in planning_service.tasks_for_range(SEP_23, SEP_23, timezone_name="Asia/Tokyo")] == [task.id]
    allocation = controller.allocate_range(SEP_23, SEP_23).value
    assert allocation.assignments == {task.id: SEP_23} and allocation.unallocated == []

    output = controller.generate_day(SEP_23).value
    [placement] = output.placements
    assert placement.task_id == task.id and placement.planned_end <= task.deadline
    assert placement.planned_start >= datetime(2026, 9, 23, 0, tzinfo=TOKYO)


def test_the_utc_date_is_no_longer_what_counts() -> None:
    task = make_task("Submit form", datetime(2026, 9, 23, 8, tzinfo=TOKYO))
    # In a UTC planning timezone the same instant is 09-22 23:00: the 22nd is the last possible date.
    utc = allocate([task], [SEP_22, SEP_23], "UTC")
    assert utc.assignments == {task.id: SEP_22}
    tokyo = allocate([task], [SEP_23], "Asia/Tokyo", DayWindowSpec(start_minute=0, end_minute=7 * 60))
    assert tokyo.assignments == {task.id: SEP_23}


def test_a_negative_offset_deadline_does_not_reach_the_next_utc_date(planning_service: PlanningService) -> None:
    """Due 2026-09-23 20:00 -07:00 is 09-24 03:00 UTC, but still the 23rd in Los Angeles."""
    task = planning_service.create_task(make_task("Report", datetime(2026, 9, 23, 20, tzinfo=LOS_ANGELES)))

    assert planning_service.tasks_for_range(SEP_24, SEP_24, timezone_name="America/Los_Angeles") == []
    assert planning_service.tasks_for_range(SEP_24, SEP_24, timezone_name="UTC") == [task]  # its UTC date is the 24th
    result = allocate([task], [SEP_23, SEP_24], "America/Los_Angeles")
    assert result.assignments == {task.id: SEP_23}

    late = allocate([task], [SEP_24], "America/Los_Angeles")
    [entry] = late.unallocated
    assert entry.reason_code == AllocationReasonCode.DEADLINE_INFEASIBLE and entry.proven_infeasible


def test_deadline_equality_is_feasible_and_one_minute_less_is_not() -> None:
    start = datetime(2026, 9, 23, 0, tzinfo=TOKYO)
    exact = make_task("Exactly enough", start + timedelta(minutes=30))
    short = make_task("One minute short", start + timedelta(minutes=29))
    result = allocate([exact, short], [SEP_23], "Asia/Tokyo")
    assert result.assignments == {exact.id: SEP_23}
    assert [e.task_id for e in result.unallocated] == [short.id]


def test_a_deadline_at_local_midnight_is_eligible_for_that_date_but_not_allocatable(
    planning_service: PlanningService,
) -> None:
    task = planning_service.create_task(make_task("Midnight", datetime(2026, 9, 23, 0, tzinfo=TOKYO)))
    planning_service.create_task(make_task("Before", datetime(2026, 9, 22, 23, 59, tzinfo=TOKYO)))

    names = {t.name for t in planning_service.tasks_for_range(SEP_23, SEP_24, timezone_name="Asia/Tokyo")}
    assert names == {"Midnight"}  # the coarse filter shows it (overdue explanations), "Before" is not eligible
    result = allocate([task], [SEP_23, SEP_24], "Asia/Tokyo")
    assert result.assignments == {} and result.unallocated[0].reason_code == AllocationReasonCode.DEADLINE_INFEASIBLE
    planned = planning_service.load_range(SEP_23, SEP_24, scope=RangeScope.PLANNED, timezone_name="Asia/Tokyo")
    assert planned.task_ids == [task.id]


def test_the_day_windows_start_counts_not_just_the_date() -> None:
    task = make_task("Early", datetime(2026, 9, 23, 7, 30, tzinfo=TOKYO), duration=60)
    result = allocate([task], [SEP_23], "Asia/Tokyo", DayWindowSpec(start_minute=7 * 60, end_minute=20 * 60))
    assert result.assignments == {} and result.unallocated[0].reason_code == AllocationReasonCode.DEADLINE_INFEASIBLE


def test_unsupported_dst_windows_stay_unsupported(planning_service: PlanningService) -> None:
    """A deadline on a DST date is judged by date and window start; generating that date is still refused."""
    spring = date(2026, 3, 8)  # New York skips 02:00-03:00
    task = planning_service.create_task(make_task("Taxes", datetime(2026, 3, 8, 12, tzinfo=NEW_YORK)))
    result = allocate([task], [spring], "America/New_York")
    assert result.assignments == {task.id: spring}

    # A window starting inside the gap cannot be resolved: allocation falls back to the date rule only...
    gap = allocate([task], [spring], "America/New_York", DayWindowSpec(start_minute=150, end_minute=1440))
    assert gap.assignments == {task.id: spring}

    # ...and the day engine keeps refusing the date rather than scheduling across the change.
    controller = PlanningController(service=planning_service, timezone="America/New_York")
    assert controller.allocate_range(spring, spring).ok
    generated = controller.generate_day(spring)
    assert not generated.ok and "daylight-saving" in generated.error


@pytest.mark.parametrize("tz", ["Asia/Tokyo", "America/Los_Angeles", "UTC"])
def test_range_start_uses_the_first_local_instant(planning_service: PlanningService, tz: str) -> None:
    zone = ZoneInfo(tz)
    on_boundary = planning_service.create_task(make_task("On", datetime(2026, 9, 23, 0, tzinfo=zone)))
    just_before = planning_service.create_task(make_task("Off", datetime(2026, 9, 23, 0, tzinfo=zone) - timedelta(minutes=1)))
    eligible = {t.id for t in planning_service.tasks_for_range(SEP_23, SEP_23, timezone_name=tz)}
    assert on_boundary.id in eligible and just_before.id not in eligible
