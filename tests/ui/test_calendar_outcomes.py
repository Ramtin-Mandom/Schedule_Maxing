"""The Week and Month pages' selected-day panel and historical colours as real widgets against a
temporary database: the panel follows the selected date, "All Tasks Complete" / "No Tasks Complete"
change exactly what the Day page's board shows, only past dates are tinted (today and future keep
their look), the colours are explained in words (legend, panel, cell text), and the task form's
Points field is saved and validated. Skipped without a display (tests/ui/test_desktop_app.py)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app.productivity.day_summary import DAY_STATUS_TEXT, DayStatusClass
from app.ui import theme
from tests.ui.test_desktop_app import WEDNESDAY, board_names, close_app, fill_form, open_app, pump
from tests.ui.test_desktop_app import dialogs as dialogs  # noqa: F401 - the dialog-recorder fixture
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401 - skip without a display

MONDAY = WEDNESDAY - timedelta(days=2)


def schedule_monday(app, names: tuple[str, ...]):
    """Plan and schedule tasks on Monday (a past date: "today" is Wednesday) through the Day page."""
    app.open_day(MONDAY)
    day = app.pages["day"]
    pump(app)
    for index, name in enumerate(names):
        start = 540 + 90 * index
        fill_form(day, name=name, duration="30", start=str(start), end=str(start + 60))
        day.form.submit_button.invoke()
    day.make_schedule_button.invoke()
    pump(app, until=lambda: not day._busy)
    pump(app)
    return day


def test_week_bulk_actions_match_the_day_board_and_colour_only_the_past(tmp_path: Path, dialogs) -> None:
    app = open_app(tmp_path / "week.db", tmp_path)
    try:
        day = schedule_monday(app, ("Read", "Write", "Review"))
        week = app.pages["week"]
        app.show_page("week")
        week.select_date(MONDAY)
        pump(app)
        panel = week.day_panel
        assert "Monday, June 3, 2024" in panel.day_label.cget("text")
        assert "3 scheduled · 0 completed · 0 uncompleted · 3 pending" in panel.stats_label.cget("text")
        assert panel.status_label.cget("text") == "Light white: 50% or more still pending"  # in words, not colour only

        panel.all_button.invoke()
        pump(app)
        assert "3 scheduled · 3 completed" in panel.stats_label.cget("text")
        assert panel.status_label.cget("text") == "Dark green: 80% or more completed"
        cell = week.snapshot.day(MONDAY)
        assert cell.status_class == DayStatusClass.MOSTLY_COMPLETED_STRONG
        assert week.calendar._day_colors(cell)[0] == theme.resolve(theme.DAY_STATUS_FILLS["mostly_completed_strong"])
        today, tomorrow = week.snapshot.day(WEDNESDAY), week.snapshot.day(WEDNESDAY + timedelta(days=1))
        assert today.status_class is None and tomorrow.status_class is None
        assert week.calendar._day_colors(tomorrow)[0] == theme.resolve(theme.CARD_BG)  # the normal look

        app.open_day(MONDAY)  # the Day page shows the same state: one model
        pump(app)
        assert board_names(day) == {"uncompleted": [], "pending": [], "completed": ["Read", "Review", "Write"]}

        app.show_page("week")
        week.select_date(MONDAY)
        pump(app)
        panel.none_button.invoke()
        pump(app)
        assert panel.status_label.cget("text") == "Dark red: 80% or more uncompleted"
        app.open_day(MONDAY)
        pump(app)
        assert board_names(day)["uncompleted"] == ["Read", "Review", "Write"]
        assert dialogs.errors == []
    finally:
        close_app(app)


def test_month_panel_follows_the_selection_and_the_legend_explains_every_colour(tmp_path: Path, dialogs) -> None:
    app = open_app(tmp_path / "month.db", tmp_path)
    try:
        schedule_monday(app, ("Only",))
        month = app.pages["month"]
        app.show_page("month")
        month.select_date(MONDAY + timedelta(days=1))  # a past date without scheduled tasks
        pump(app)
        panel = month.day_panel
        assert panel.status_label.cget("text") == "Neutral, dark tint: No scheduled tasks"
        assert panel.all_button.cget("state") == "disabled" and panel.none_button.cget("state") == "disabled"
        month.select_date(MONDAY)
        pump(app)
        assert panel.all_button.cget("state") == "normal"
        panel.all_button.invoke()
        pump(app)
        assert month.snapshot.day(MONDAY).status_class == DayStatusClass.MOSTLY_COMPLETED_STRONG
        month.select_date(WEDNESDAY)
        pump(app)
        assert panel.status_label.cget("text") == "Today"

        legend = {status: label.cget("text") for status, label in month.legend.entries.items()}
        assert legend == {status: DAY_STATUS_TEXT[status][1] for status in DayStatusClass}
        assert not hasattr(month, "added_tasks_panel")  # "Tasks this month" is replaced by the selected day
    finally:
        close_app(app)


def test_the_task_form_saves_and_validates_points(tmp_path: Path, dialogs) -> None:
    app = open_app(tmp_path / "points.db", tmp_path)
    try:
        day = app.pages["day"]
        form = day.form
        assert form.points_field.get() == "1"  # the default
        fill_form(day, name="Essay", duration="45")
        form.points_field.variable.set("oops")
        form.submit_button.invoke()
        assert "whole number" in form.points_field.error and "Essay" not in [row.name for row in day.snapshot.rows]
        form.points_field.variable.set("12")
        form.submit_button.invoke()
        [task] = app.services.planning_controller.list_tasks().value
        assert task.points == 12 and form.points_field.get() == "1"  # saved; the next form starts at the default
    finally:
        close_app(app)
