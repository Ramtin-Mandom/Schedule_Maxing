"""Headless tests of the per-date Day Window (app/ui/day_window.py) through the desktop's own
services: the Settings default, a date's own override (only its day_window field; saved in the
date-scoped preference layer, so it persists and syncs), the default moving inheriting dates
but never overridden ones, Use default, refusals that write nothing (bad times, end not after
start, fixed blocks outside), version conflicts, the scheduler honouring the overridden window,
and Week/Month adding tasks on the selected real date. No display; every database is temporary."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.planning.models import FixedBlock, LocalTimeWindow, Task
from app.planning.preferences import DayWindowSpec, OptimizerMode, PreferenceOverrides
from app.ui import background
from app.ui.app_services import open_app_services
from app.ui.calendar_controller import CalendarController
from app.ui.day_controller import DayScheduleController
from app.ui.day_window import DayWindowController, parse_window
from app.ui.task_form_model import TaskDraft

DAY = date(2026, 9, 24)
NEXT = DAY + timedelta(days=1)
TZ = "America/Vancouver"


@pytest.fixture(autouse=True)
def _restore_installed_registry():
    previous = background.current_registry()
    yield
    background.install_registry(previous)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "window.db"


@pytest.fixture
def services(db_path: Path, tmp_path: Path):
    opened = open_app_services(db_path, timezone=TZ, project_root=str(tmp_path))
    yield opened
    opened.close()


def ok(result):
    assert result.ok, result.error
    return result.value


def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(TZ))


def windows(controller: DayWindowController, *days: date) -> list[tuple[int, int, bool]]:
    states = [ok(controller.state(day)) for day in days]
    return [(state.start_minute, state.end_minute, state.overridden) for state in states]


# -----------------------------------------------------------------------------
# Default and override
# -----------------------------------------------------------------------------


def test_every_date_starts_with_the_default_window(services) -> None:
    window = DayWindowController(services.planning_controller)
    state = ok(window.state(DAY))
    assert (state.start_minute, state.end_minute) == (0, 1440) and not state.overridden and state.version is None
    assert state.text == "12:00 AM – 12:00 AM (next day)" and state.timezone == TZ

    ok(services.planning_controller.set_user_overrides(
        PreferenceOverrides(day_window=DayWindowSpec(start_minute=7 * 60, end_minute=22 * 60))))
    assert windows(window, DAY, NEXT) == [(420, 1320, False), (420, 1320, False)]  # Settings' default, everywhere
    assert ok(window.state(DAY)).default_text == "7:00 AM – 10:00 PM"


def test_a_date_override_changes_that_date_only_and_the_default_moves_the_others(services) -> None:
    planning = services.planning_controller
    window = DayWindowController(planning)
    ok(planning.set_user_overrides(PreferenceOverrides(day_window=DayWindowSpec(start_minute=480, end_minute=1260))))

    state = ok(window.save(DAY, "9:15 AM", "5:30 PM", expected_version=None))
    assert (state.start_minute, state.end_minute, state.overridden) == (555, 1050, True)
    assert (state.default_start, state.default_end) == (480, 1260)
    assert windows(window, NEXT) == [(480, 1260, False)]  # another date is untouched

    user = ok(planning.user_preferences())
    ok(planning.set_user_overrides(PreferenceOverrides(day_window=DayWindowSpec(start_minute=360, end_minute=1380)),
                                   expected_version=user.version))
    # The new default moves the inheriting date; the customised date keeps its own window.
    assert windows(window, DAY, NEXT) == [(555, 1050, True), (360, 1380, False)]
    assert ok(window.state(DAY)).default_text == "6:00 AM – 11:00 PM"


def test_only_the_day_window_field_of_the_date_layer_changes(services) -> None:
    planning = services.planning_controller
    ok(planning.set_date_overrides(DAY, PreferenceOverrides(optimizer_mode=OptimizerMode.ADHD_FRIENDLY,
                                                            category_multipliers={"study": 2.0})))
    window = DayWindowController(planning)
    version = ok(window.state(DAY)).version
    ok(window.save(DAY, "8:00 AM", "12:00 PM", expected_version=version))
    layer = ok(planning.date_preferences(DAY)).overrides
    assert layer.optimizer_mode == OptimizerMode.ADHD_FRIENDLY and layer.category_multipliers == {"study": 2.0}
    assert layer.day_window == DayWindowSpec(start_minute=480, end_minute=720)

    state = ok(window.use_default(DAY, expected_version=ok(window.state(DAY)).version))
    assert not state.overridden and (state.start_minute, state.end_minute) == (0, 1440)
    assert ok(planning.date_preferences(DAY)).overrides.optimizer_mode == OptimizerMode.ADHD_FRIENDLY  # kept

    # A layer holding nothing but the window is removed entirely by Use default.
    ok(window.save(NEXT, "8:00 AM", "9:00 PM", expected_version=None))
    ok(window.use_default(NEXT, expected_version=ok(window.state(NEXT)).version))
    assert ok(planning.date_preferences(NEXT)) is None


def test_a_window_may_end_at_midnight_and_minutes_are_exact(services) -> None:
    window = DayWindowController(services.planning_controller)
    state = ok(window.save(DAY, "6:07 AM", "12:00 AM", expected_version=None))
    assert (state.start_minute, state.end_minute) == (367, 1440)  # not rounded to a half-hour grid
    assert parse_window("12:00 PM", "12:00 AM") == DayWindowSpec(start_minute=720, end_minute=0, end_day_offset=1)


def test_the_override_persists_across_a_restart(db_path: Path, tmp_path: Path) -> None:
    first = open_app_services(db_path, timezone=TZ, project_root=str(tmp_path))
    try:
        ok(DayWindowController(first.planning_controller).save(DAY, "10:00 AM", "4:00 PM", expected_version=None))
    finally:
        first.close()
    again = open_app_services(db_path, timezone=TZ, project_root=str(tmp_path))
    try:
        assert windows(DayWindowController(again.planning_controller), DAY, NEXT) == [(600, 960, True), (0, 1440, False)]
    finally:
        again.close()


# -----------------------------------------------------------------------------
# Refusals: nothing is written
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("start, end, message", [
    ("5:00 PM", "9:00 AM", "must end after it starts"),
    ("9:00 AM", "9:00 AM", "must end after it starts"),
    ("13:00 PM", "5:00 PM", "Start: With AM/PM"),
    ("9:00 AM", "5:75 PM", "End: Minutes go from 00 to 59"),
    ("", "5:00 PM", "Start: Enter a time"),
])
def test_invalid_windows_are_refused_and_nothing_is_saved(services, start, end, message) -> None:
    window = DayWindowController(services.planning_controller)
    result = window.save(DAY, start, end, expected_version=None)
    assert not result.ok and message in result.error
    assert ok(services.planning_controller.date_preferences(DAY)) is None


def test_a_window_that_would_leave_a_fixed_block_outside_is_refused(services) -> None:
    planning = services.planning_controller
    ok(planning.save_fixed_block(FixedBlock(label="Shift", category="work", planned_date=DAY, timezone=TZ,
                                            planned_start=local(DAY, 7), planned_end=local(DAY, 9, 30))))
    window = DayWindowController(planning)
    result = window.save(DAY, "8:00 AM", "6:00 PM", expected_version=None)
    assert not result.ok and "“Shift” (7:00 AM – 9:30 AM)" in result.error and "Nothing was saved" in result.error
    assert ok(planning.date_preferences(DAY)) is None
    assert ok(window.save(DAY, "7:00 AM", "6:00 PM", expected_version=None)).overridden  # touching the edge is inside


def test_a_stale_version_is_a_conflict_not_an_overwrite(services) -> None:
    window = DayWindowController(services.planning_controller)
    ok(window.save(DAY, "8:00 AM", "8:00 PM", expected_version=None))
    stale = window.save(DAY, "9:00 AM", "5:00 PM", expected_version=None)  # read before the first save
    assert not stale.ok
    assert windows(window, DAY) == [(480, 1200, True)]


# -----------------------------------------------------------------------------
# Scheduling and the task form's date
# -----------------------------------------------------------------------------


def test_the_scheduler_places_work_only_inside_the_dates_overridden_window(services) -> None:
    planning = services.planning_controller
    for name in ("Read", "Write", "Review"):
        ok(planning.add_or_update_task(Task(name=name, category="study", estimated_duration_minutes=60, priority=5,
                                            preferred_dates=[DAY],
                                            preferred_time_window=LocalTimeWindow(start_minute=0, end_minute=180))))
    window = DayWindowController(planning)
    ok(window.save(DAY, "1:30 PM", "4:45 PM", expected_version=None))  # 3 h 15 min: room for exactly three tasks

    run = ok(DayScheduleController(planning, anchor_date=DAY, timezone=TZ).make_schedule())
    assert run.status == "generated"
    placed = ok(planning.get_placements(DAY))
    assert len(placed) == 3
    for placement in placed:
        assert local(DAY, 13, 30) <= placement.planned_start and placement.planned_end <= local(DAY, 16, 45)

    ok(planning.add_or_update_task(Task(name="Extra", category="study", estimated_duration_minutes=60, priority=5,
                                        preferred_dates=[DAY])))
    run = ok(DayScheduleController(planning, anchor_date=DAY, timezone=TZ).regenerate_for(DAY))
    assert len(ok(planning.get_placements(DAY))) == 3  # the fourth hour does not fit: it stays unscheduled
    # Exactly one of the four is left out, with its genuine reason (which one depends on the tie-break).
    assert len(run.reasons) == 1 and ("free capacity" in run.reasons[0] or "Could not be placed" in run.reasons[0])


def test_day_week_and_month_add_tasks_on_their_selected_real_date(services) -> None:
    planning = services.planning_controller
    today = date(2026, 9, 27)
    day = DayScheduleController(planning, anchor_date=today, timezone=TZ, today=lambda: today)
    assert day.today() == today and day.form_date == today and day.blank_draft().date == "2026-09-27"

    week = CalendarController(planning, mode="week", selected=today, timezone=TZ, today=lambda: today)
    week.select(date(2026, 9, 23))
    assert week.form_date == date(2026, 9, 23) and week.blank_draft("block").date == "2026-09-23"
    month = CalendarController(planning, mode="month", selected=today, timezone=TZ, today=lambda: today)
    month.select(date(2026, 9, 3))
    assert month.form_date == date(2026, 9, 3)

    for page, name in ((day, "Day task"), (week, "Week task"), (month, "Month task")):
        ok(page.save_draft(TaskDraft(name=name, duration="30 min", date=page.form_date.isoformat())))
    stored = {task.name: task for task in ok(planning.list_tasks())}
    assert stored["Day task"].preferred_dates == [today]
    assert stored["Week task"].preferred_dates == [date(2026, 9, 23)]
    assert stored["Month task"].preferred_dates == [date(2026, 9, 3)]
