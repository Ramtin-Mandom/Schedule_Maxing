"""The Week and Month pages as real widgets (Milestone 4, Prompt 5) against a temporary database:
the current week/month by default, the prominent month name and the current year's month choice,
leap February and the year rollover through the real controls, background navigation where only
the newest result is shown, keyboard/click selection with an explicit Open Day and Back that
restores the week/month and day, tasks created on the selected date, category colors and muted
past days, and a previewed Reset that can be cancelled. Skipped without a display."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from app.planning.models import FixedBlock
from app.ui import theme
from app.ui.background import ControllerResult
from tests.ui.test_desktop_app import WEDNESDAY, close_app, open_app, pump, tree_names
from tests.ui.test_desktop_app import dialogs as dialogs  # noqa: F401 - the dialog-recorder fixture
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401 - skip without a display
from tests.ui.test_desktop_shell import key, shown

UTC = __import__("zoneinfo").ZoneInfo("UTC")


def settle_load(app, page) -> None:
    pump(app, until=lambda: not page.loading)


def cell_fill(page, day: date) -> str:
    found = page.calendar.canvas.find_withtag(f"cell:{day.isoformat()}")
    return page.calendar.canvas.itemcget(found[0], "fill")


def test_month_navigation_selection_and_the_way_back(tmp_path: Path, dialogs) -> None:
    app = shown(open_app(tmp_path / "month.db", tmp_path), tmp_path)
    try:
        month = app.pages["month"]
        app.show_page("month")
        pump(app)
        assert month.title_label.cget("text") == "June 2024" and month.page_controller.selected_date == WEDNESDAY
        assert month.month_select.values[0] == "January 2024" and len(month.month_select.values) == 12
        cells = [cell for cell in month.snapshot.days if cell.in_period]
        assert len(cells) == 30 and month.snapshot.days[0].date == date(2024, 5, 27)  # Monday-aligned grid

        month.month_select.choose("February 2024")
        settle_load(app, month)
        assert month.title_label.cget("text") == "February 2024"
        assert [cell.date for cell in month.snapshot.days if cell.in_period][-1] == date(2024, 2, 29)  # leap
        assert month.page_controller.selected_date == date(2024, 2, 5)
        pump(app)
        assert month.calendar.cell_bounds(date(2024, 2, 29)) is not None

        # Races: only the newest navigation's result is shown.
        month.shift(1)
        month.shift(1)
        settle_load(app, month)
        assert month.title_label.cget("text") == "April 2024" and month.snapshot.period.start == date(2024, 4, 1)
        stale = month.page_controller.load_for(month.page_controller.period.shifted(-2))
        month._loaded(month._load_token - 1, stale.value.period, stale)
        assert month.title_label.cget("text") == "April 2024"
        month._loaded(month._load_token, month.page_controller.period.shifted(5),
                      ControllerResult.failure("late"))
        assert month.title_label.cget("text") == "April 2024"

        for _ in range(9):
            month.shift(1)
        settle_load(app, month)
        assert month.title_label.cget("text") == "January 2025"  # across the new year

        # Keyboard selection and the explicit Open Day; Back restores the month and the day.
        month.select_date(date(2025, 1, 15))
        key(month.calendar.canvas, "Right")
        key(month.calendar.canvas, "Down")
        assert month.page_controller.selected_date == date(2025, 1, 23)
        assert month.details_title.cget("text").startswith("Thursday, January 23, 2025")
        month.open_day_button.invoke()
        day = app.pages["day"]
        assert app.shell.current == "day" and day.page_controller.anchor_date == date(2025, 1, 23)
        assert "Back to Month (Thu Jan 23)" in day.back_button.cget("text")
        day.back_button.invoke()
        assert app.shell.current == "month" and month.page_controller.selected_date == date(2025, 1, 23)
        assert month.title_label.cget("text") == "January 2025"

        month.go_today()
        settle_load(app, month)
        assert month.title_label.cget("text") == "June 2024"
    finally:
        close_app(app)


def test_week_shows_real_dates_colors_past_days_and_creates_on_the_selected_date(tmp_path: Path, dialogs) -> None:
    app = open_app(tmp_path / "week.db", tmp_path)
    try:
        week = app.pages["week"]
        app.show_page("week")
        pump(app)
        assert week.title_label.cget("text") == "Week of Jun 3 – 9, 2024"
        assert [cell.date for cell in week.snapshot.days] == [date(2024, 6, 3) + timedelta(days=n) for n in range(7)]

        controller = app.services.planning_controller
        controller.save_fixed_block(FixedBlock(
            label="Swim", category="exercise", planned_date=date(2024, 6, 4), timezone="UTC",
            planned_start=datetime(2024, 6, 4, 7, tzinfo=UTC), planned_end=datetime(2024, 6, 4, 8, tzinfo=UTC)))
        week.select_date(date(2024, 6, 6))
        assert week.form.date_field.get() == "2024-06-06"  # the form follows the selected date
        week.form.name_field.variable.set("Plan trip")
        week.form.duration_field.variable.set("25 min")
        week.form.submit_button.invoke()
        assert dialogs.errors == [] and "Plan trip" in tree_names(week)
        thursday = week.snapshot.day(date(2024, 6, 6))
        assert [(item.kind, item.name) for item in thursday.items] == [("unscheduled", "Plan trip")]
        assert "Plan trip — not scheduled" in week.details_label.cget("text")
        pump(app)

        swim = week.calendar.item_bounds(date(2024, 6, 4), "Swim")
        box = week.calendar.canvas.find_withtag("item:2024-06-04:Swim")[0]
        assert week.calendar.canvas.itemcget(box, "fill") == theme.resolve(theme.category_style("exercise").fill)
        assert swim[3] - swim[1] == 36  # one hour on the time axis
        assert cell_fill(week, date(2024, 6, 3)) == theme.resolve(theme.CANVAS_BG)  # a past day: muted, still drawn
        assert cell_fill(week, date(2024, 6, 6)) == theme.resolve(theme.CARD_BG)

        # Reset Week asks first; cancelling deletes nothing.
        dialogs.confirm = False
        week.reset_button.invoke()
        assert "Reset the week of Mon Jun 3" in dialogs.confirms[-1] and "nothing was deleted" in week.notice.text
        assert "Plan trip" in tree_names(week)
        dialogs.confirm = True
        week.reset_button.invoke()
        assert tree_names(week) == [] and "was reset" in week.notice.text

        week.shift(-1)
        settle_load(app, week)
        assert week.title_label.cget("text") == "Week of May 27 – Jun 2, 2024"
    finally:
        close_app(app)
