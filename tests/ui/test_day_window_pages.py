"""The Day Window bar, the shared time input and the How to Use page as real widgets, against a
temporary database: the Day page is today and its bar edits only that date (surviving a restart),
Week/Month bars follow the selected day, the Settings default moves inheriting dates, choosing Day
from the navigation returns to today, the [hour]:[minute] [AM/PM] input's keyboard behaviour and
the guide placed before About. Skipped without a display
(tests/ui/test_desktop_app.py)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from app.ui.clock_input import ClockInput
from app.ui.guide_content import GUIDE_SECTIONS
from app.ui.shell_state import NAV_KEYS
from tests.ui.test_desktop_app import WEDNESDAY, close_app, open_app, pump
from tests.ui.test_desktop_app import dialogs as dialogs  # noqa: F401 - the dialog-recorder fixture
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401 - skip without a display
from tests.ui.test_desktop_shell import key, settle, shown

THURSDAY = WEDNESDAY + timedelta(days=1)


def enter(field: ClockInput, hour: str, minute: str, meridiem: str) -> None:
    field.hour_var.set(hour)
    field.minute_var.set(minute)
    if field.meridiem != meridiem:
        field.meridiem_button.invoke()


def bar_window(page) -> tuple[date, int, int, bool]:
    state = page.window_bar.state
    return state.day, state.start_minute, state.end_minute, state.overridden


def test_the_day_bar_is_today_and_changes_only_that_date_surviving_a_restart(tmp_path: Path, dialogs) -> None:
    db = tmp_path / "bar.db"
    app = open_app(db, tmp_path)
    try:
        day = app.pages["day"]
        assert day.page_controller.anchor_date == WEDNESDAY == app.today()  # Day is today
        assert bar_window(day) == (WEDNESDAY, 0, 1440, False)
        assert day.window_bar.badge.cget("text") == "Default"
        assert day.window_bar.default_button.cget("state") == "disabled"

        enter(day.window_bar.start_input, "8", "15", "AM")
        enter(day.window_bar.end_input, "6", "00", "PM")
        day.window_bar.apply_button.invoke()
        pump(app)
        assert dialogs.errors == [] and bar_window(day) == (WEDNESDAY, 495, 1080, True)
        assert day.window_bar.badge.cget("text") == "Custom for this date"
        assert day.snapshot.window == (495, 1080)  # the timeline's window was re-read too
        assert "now runs 8:15 AM – 6:00 PM" in day.window_bar.notice.text

        enter(day.window_bar.end_input, "7", "00", "AM")  # before the start: refused, nothing saved
        day.window_bar.apply_button.invoke()
        pump(app)
        assert "must end after it starts" in day.window_bar.notice.text
        assert app.services.planning_controller.date_preferences(WEDNESDAY).value.overrides.day_window.end_minute == 1080

        day.shift_date(1)
        pump(app)
        assert bar_window(day) == (THURSDAY, 0, 1440, False)  # another date keeps the default
    finally:
        close_app(app)

    app = open_app(db, tmp_path)  # a restart: the override is still there, the Day page is today again
    try:
        assert bar_window(app.pages["day"]) == (WEDNESDAY, 495, 1080, True)
        app.pages["day"].window_bar.default_button.invoke()
        pump(app)
        assert bar_window(app.pages["day"]) == (WEDNESDAY, 0, 1440, False)
    finally:
        close_app(app)


def test_week_and_month_bars_follow_the_selected_day(tmp_path: Path, dialogs) -> None:
    app = open_app(tmp_path / "week.db", tmp_path)
    try:
        week, month = app.pages["week"], app.pages["month"]
        app.show_page("week")
        pump(app)
        assert bar_window(week)[0] == WEDNESDAY
        week.select_date(THURSDAY)
        pump(app)
        assert bar_window(week)[0] == THURSDAY and "Thu, Jun 6" in week.window_bar.title_label.cget("text")
        assert week.form.date_text == THURSDAY.isoformat()
        enter(week.window_bar.start_input, "9", "00", "AM")
        enter(week.window_bar.end_input, "12", "00", "AM")
        week.window_bar.apply_button.invoke()
        pump(app)
        assert bar_window(week) == (THURSDAY, 540, 1440, True)
        week.select_date(WEDNESDAY)
        pump(app)
        assert bar_window(week) == (WEDNESDAY, 0, 1440, False)

        app.show_page("month")
        month.select_date(THURSDAY)
        pump(app)
        assert bar_window(month) == (THURSDAY, 540, 1440, True)  # one stored override, seen from every view
    finally:
        close_app(app)


def test_the_settings_default_moves_every_date_without_its_own_window(tmp_path: Path, dialogs) -> None:
    app = open_app(tmp_path / "settings.db", tmp_path)
    try:
        day = app.pages["day"]
        enter(day.window_bar.start_input, "10", "00", "AM")
        enter(day.window_bar.end_input, "2", "00", "PM")
        day.window_bar.apply_button.invoke()
        pump(app)

        app.show_page("settings")
        pump(app)
        settings_page = app.pages["settings"]
        start, end = settings_page.editor.inputs["day_window"]
        assert isinstance(start, ClockInput) and isinstance(end, ClockInput)
        enter(start, "7", "30", "AM")
        enter(end, "9", "45", "PM")
        settings_page.editor.save("day_window")
        pump(app)
        assert settings_page.view.layer.day_window.start_minute == 450

        app.show_page("day")
        pump(app)
        assert bar_window(day) == (WEDNESDAY, 600, 840, True)  # the customised date keeps its window
        assert day.window_bar.state.default_text == "7:30 AM – 9:45 PM"
        day.shift_date(1)
        pump(app)
        assert bar_window(day) == (THURSDAY, 450, 1305, False)  # an inheriting date follows the new default
    finally:
        close_app(app)


def test_choosing_day_from_the_navigation_returns_to_today(tmp_path: Path, dialogs) -> None:
    app = shown(open_app(tmp_path / "nav.db", tmp_path), tmp_path)
    try:
        day = app.pages["day"]
        day.shift_date(3)
        pump(app)
        app.open_day(THURSDAY, return_to="week")  # opened for a date: that date, with a way back
        pump(app)
        assert day.page_controller.anchor_date == THURSDAY and day.back_button.winfo_manager() == "grid"
        app.shell.sidebar.buttons["week"].invoke()
        app.shell.sidebar.buttons["day"].invoke()
        pump(app)
        assert day.page_controller.anchor_date == WEDNESDAY and day.return_context is None
        assert day.form.date_text == WEDNESDAY.isoformat()
        assert day.back_button.winfo_manager() == ""
    finally:
        close_app(app)


def test_the_time_input_by_keyboard(tmp_path: Path, dialogs) -> None:
    app = shown(open_app(tmp_path / "keys.db", tmp_path), tmp_path)
    try:
        field = app.pages["day"].window_bar.start_input
        field.clear()
        field.hour_entry.focus_force()
        field.hour_entry.insert(0, "7")
        key(field.hour_entry, "KeyRelease-7")  # a single digit that cannot start a two-digit hour moves on
        settle(app, 0.1)
        assert str(app.focus_get()).startswith(str(field.minute_entry))
        field.minute_entry.insert(0, "5")
        key(field.minute_entry, "KeyPress-p")
        assert field.meridiem == "PM" and field.minute_var.get() == "5"  # "p" chose PM and was not typed
        key(field.minute_entry, "KeyPress-a")
        assert field.meridiem == "AM"
        app.pages["day"].window_bar.apply_button.focus_force()
        settle(app, 0.2)
        assert field.minute_var.get() == "05" and field.get() == "7:05 AM"  # normalized once the input is left
        field.hour_var.set("1")
        key(field.hour_entry, "KeyPress-colon")
        settle(app, 0.1)
        assert str(app.focus_get()).startswith(str(field.minute_entry))
    finally:
        close_app(app)


def test_how_to_use_sits_before_about_and_renders_every_section(tmp_path: Path, dialogs) -> None:
    assert NAV_KEYS.index("guide") == NAV_KEYS.index("about") - 1
    app = shown(open_app(tmp_path / "guide.db", tmp_path), tmp_path)
    try:
        app.shell.sidebar.buttons["guide"].invoke()
        pump(app)
        guide = app.pages["guide"]
        assert app.shell.current == "guide" and list(guide.cards) == [item.key for item in GUIDE_SECTIONS]
        guide.contents_buttons["day_status_colours"].invoke()
        settle(app, 0.2)
        assert guide.body._parent_canvas.yview()[0] > 0.5  # the contents jump to the section
    finally:
        close_app(app)
