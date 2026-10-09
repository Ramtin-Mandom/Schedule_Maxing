"""Real-widget tests of the desktop shell (Milestone 4, Prompt 1) against a temporary
database: the app opens on Day with a collapsed, keyboard-operable sidebar; every page is
reachable and the old workflows (Execute, Productivity, legacy reward config) remain;
selected dates survive navigation; light/dark and the interface size persist; moving,
resizing, minimizing and scaling settle into a stable layout without redraw loops or
clipped pages; dialogs, drawers and the Day timeline work from the keyboard.

Skipped automatically when no display is available (see tests/ui/test_desktop_app.py);
the headless parts are in tests/ui/test_shell_foundation.py.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import customtkinter as ctk
import pytest

from app.ui import background, theme
from app.ui.components import ConfirmDialog, Drawer, Notice, StateView, focus_target
from app.ui.shell import COLLAPSED_WIDTH, EXPANDED_WIDTH
from app.ui.shell_state import NAV_ITEMS, LayoutMode
from tests.ui.test_desktop_app import WEDNESDAY, close_app, fill_form, open_app, pump, tree_names
from tests.ui.test_desktop_app import dialogs as dialogs  # noqa: F401 - the dialog-recorder fixture
from tests.ui.test_desktop_app import pytestmark as pytestmark  # noqa: F401 - skip without a display
from tests.window_placement import place


@pytest.fixture(autouse=True)
def _default_look():
    yield
    ctk.set_appearance_mode("light")
    ctk.set_widget_scaling(1.0)


def shown(app, tmp_path: Path):
    """Map the window (layout, focus and <Configure> need a visible window)."""
    app.deiconify()
    app.geometry(place("1440x880+30+30"))
    settle(app)
    return app


#: How long the app must stay unchanged before settle() returns.
QUIET_SECONDS = 0.06


def _activity(app) -> tuple:
    """What changes while queued layout, paint or background work is still happening."""
    shell = getattr(app, "shell", None)
    return (
        background.current_registry().outstanding,
        bool(shell is not None and shell.layout_checks.pending),
        app.winfo_geometry(),
        shell.host.winfo_width() if shell is not None else None,
        getattr(getattr(app, "shell_state", None), "layout", None),
        str(app.focus_get()) if app.focus_get() is not None else None,
    )


def settle(app, seconds: float = 0.4) -> None:
    """
    Let queued layout, paint and worker callbacks run: pump the event loop until
    the app has stayed unchanged (no worker running, no layout check pending,
    the same geometry, layout and focus) for QUIET_SECONDS, at most `seconds`
    -- a safeguard, never a fixed wait. Idle callbacks run in every update();
    the app's delayed callbacks (e.g. 10 ms card batches) fall inside the quiet
    window. Use wait_fixed() to prove that nothing happens for a while.
    """
    started = time.monotonic()
    deadline = started + seconds
    last, quiet_since = None, started
    while True:
        app.update()
        now = time.monotonic()
        state = _activity(app)
        if state != last:
            last, quiet_since = state, now
        elif now - quiet_since >= QUIET_SECONDS:
            return
        if now >= deadline:
            return
        time.sleep(0.005)


def wait_fixed(app, seconds: float) -> None:
    """Pump the event loop for exactly `seconds` -- for checks that nothing (e.g. a late event) happens meanwhile."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.update()
        time.sleep(0.01)


def key(widget, keysym: str) -> None:
    """Press a key in `widget` (Tk delivers key events to the focused widget, as a real keypress would)."""
    widget.focus_force()
    widget.update()
    widget.event_generate(f"<{keysym}>")
    widget.update()


def test_the_app_opens_on_day_with_a_collapsed_keyboard_operable_sidebar(tmp_path: Path, dialogs) -> None:
    app = shown(open_app(tmp_path / "shell.db", tmp_path), tmp_path)
    try:
        shell, sidebar = app.shell, app.shell.sidebar
        assert shell.current == "day" and app.shell_state.page == "day"
        assert app.shell_state.sidebar_open is False and sidebar.winfo_width() <= COLLAPSED_WIDTH + 2
        assert sidebar.buttons["day"].cget("text") == "D"  # collapsed: symbols, with the label as tooltip
        assert sidebar.tooltips["day"].text == "Day Schedule"

        menu = focus_target(sidebar.menu_button)
        assert str(menu.cget("takefocus")) == "1"  # reachable with Tab
        key(menu, "Return")
        settle(app, 0.5)
        assert app.shell_state.sidebar_open is True
        assert sidebar._width == EXPANDED_WIDTH and sidebar.buttons["week"].cget("text").strip() == "Week Schedule"
        assert app.focus_get() == focus_target(sidebar.buttons["day"])  # focus moves into the navigation
        key(app, "Control-b")
        settle(app, 0.5)
        assert app.shell_state.sidebar_open is False and sidebar._width == COLLAPSED_WIDTH

        for item in NAV_ITEMS:
            sidebar.buttons[item.key].invoke()
            app.update()
            assert shell.current == item.key and app.pages[item.key].winfo_viewable()
            assert sum(page.winfo_viewable() for page in app.pages.values()) == 1  # only the visible page is laid out
        key(app, "Control-Key-1")
        assert shell.current == "day"

        day = app.pages["day"]
        assert day.status_board.winfo_manager() == "grid"  # Uncompleted | Tasks | Completed replaces Execute
        app.show_page("settings")
        assert app.pages["settings"].engine_select.values == ["Normal", "ADHD friendly", "Early finish", "Night owl", "Catch-up"]
        assert "reward" not in app.pages  # unsupported legacy weights are not exposed in the native app
        assert app.shell.status_bar.label.cget("text") == "Offline — guest mode, no server configured"
        assert dialogs.errors == []
    finally:
        close_app(app)


def test_each_page_keeps_its_date_while_navigating(tmp_path: Path, dialogs) -> None:
    app = open_app(tmp_path / "dates.db", tmp_path)
    try:
        week = app.pages["week"]
        week.start_date_var.set("2024-07-08")
        week.apply_start_date()
        app.show_page("month")
        app.show_page("day")
        app.show_page("week")
        assert week.page_controller.anchor_date == date(2024, 7, 8)
        assert week.start_date_var.get() == "2024-07-08"
        assert app.shell_state.selection("week") == date(2024, 7, 8)
        assert app.pages["day"].page_controller.anchor_date == WEDNESDAY
    finally:
        close_app(app)


def test_light_dark_and_interface_size_persist_across_restarts(tmp_path: Path, dialogs) -> None:
    db = tmp_path / "look.db"
    app = open_app(db, tmp_path)
    try:
        assert ctk.get_appearance_mode() == "Light"
        settings = app.pages["settings"]
        settings.appearance_select.choose("Dark")
        assert ctk.get_appearance_mode() == "Dark"
        assert "Done:" in settings.notice.text and "saved" in settings.notice.text
        canvas = app.pages["day"].schedule_canvas
        settle(app, 0.2)
        assert canvas.canvas.cget("background") == theme.resolve(theme.CARD_BG, theme.DARK)  # part of its card
        settings.scale_select.choose("115%")
    finally:
        close_app(app)
    ctk.set_appearance_mode("light")
    ctk.set_widget_scaling(1.0)

    app = open_app(db, tmp_path)
    try:
        assert app.ui_settings.appearance == "dark" and app.ui_settings.ui_scale == 1.15
        assert ctk.get_appearance_mode() == "Dark"
        assert app.pages["settings"].appearance_select.get() == "Dark"
        assert (tmp_path / "ui_settings.json").exists()  # beside the database, never the reward globals
    finally:
        close_app(app)


def test_moving_resizing_minimizing_and_scaling_settle_into_a_stable_layout(tmp_path: Path, dialogs) -> None:
    app = shown(open_app(tmp_path / "resize.db", tmp_path), tmp_path)
    try:
        day = app.pages["day"]
        fill_form(day, name="Study")
        day.form.submit_button.invoke()
        settle(app)  # the new task's (coalesced) paint happens here, before any resizing is measured
        events: list = []
        app.bind_all("<Configure>", lambda e: events.append(e), add="+")
        canvas = day.schedule_canvas

        expected = [(place("1440x880+30+30"), LayoutMode.WIDE), (place("1100x760+30+30"), LayoutMode.MEDIUM),
                    (place("700x700+30+30"), LayoutMode.NARROW), (place("700x700+160+120"), LayoutMode.NARROW),
                    (place("1100x760+60+40"), LayoutMode.MEDIUM), (place("1440x880+30+30"), LayoutMode.WIDE)]
        checks_before = app.shell.layout_checks.runs
        size = None
        for geometry, mode in expected:
            draws = canvas.draw_count
            app.geometry(geometry)
            settle(app)
            assert app.shell_state.layout == mode and day.layout == mode, geometry
            # The timeline is fitted to its width: a new width repaints it once the resize has settled (never
            # per Configure event), and moving the window without resizing it never repaints.
            moved_only = geometry.split("+")[0] == size
            assert canvas.draw_count - draws <= (0 if moved_only else 2), geometry
            size = geometry.split("+")[0]
            events.clear()
            wait_fixed(app, 0.4)  # measures that no late Configure arrives: a deliberate fixed wait
            if events:  # the window manager may deliver one late toplevel Configure; a feedback loop never stops
                assert all(str(event.widget) == "." for event in events), f"page widgets still relayout after {geometry}"
                events.clear()
                wait_fixed(app, 0.4)
            assert events == [], f"layout kept changing after {geometry}"  # settled: no Configure feedback loop
            assert not app.shell.layout_checks.pending
            page_width, host_width = day.winfo_reqwidth(), app.shell.host.winfo_width()
            assert page_width <= host_width + 2, f"{geometry}: page needs {page_width}px of {host_width}px"
        assert app.shell.layout_checks.runs - checks_before <= 3 * len(expected)  # coalesced, not per event

        app.iconify()
        settle(app)
        app.deiconify()
        settle(app)
        assert app.shell_state.layout == LayoutMode.WIDE  # a minimized (1px) window changed nothing

        # Narrow: Day, Week and Month stack their workspace in one scrolling column (calendar or timeline
        # first, then the task form, then the actions and task list) and still fit the window.
        app.geometry(place("700x700+30+30"))
        settle(app)
        for name in ("day", "week", "month"):
            app.show_page(name)
            settle(app)
            page = app.pages[name]
            assert page.layout == LayoutMode.NARROW
            assert page.form.grid_info()["row"] == 0 and int(page.side.grid_info()["row"]) == 1
            assert page.winfo_reqwidth() <= app.shell.host.winfo_width() + 2, name
        app.show_page("day")
        settle(app)

        # A larger interface size means fewer logical pixels: the layout follows (and is saved).
        app.geometry(place("1440x880+30+30"))
        settle(app)
        assert app.shell_state.layout == LayoutMode.WIDE
        assert app.set_ui_scale(1.3)
        settle(app, 0.6)
        assert app.shell_state.layout == LayoutMode.MEDIUM
        app.set_ui_scale(1.0)
        settle(app, 0.6)
        assert app.shell_state.layout == LayoutMode.WIDE
    finally:
        close_app(app)


def test_dialogs_drawers_and_the_task_list_work_from_the_keyboard(tmp_path: Path, dialogs) -> None:
    app = shown(open_app(tmp_path / "keys.db", tmp_path), tmp_path)
    try:
        day = app.pages["day"]
        entry = day.start_date_entry
        entry.focus_force()
        settle(app, 0.1)

        results: list[bool] = []
        dialog = ConfirmDialog(day, title="Confirm", message="Do it?", on_result=results.append).present()
        settle(app, 0.1)
        assert app.focus_get() == focus_target(dialog.primary_button)  # the focus starts inside
        key(dialog, "Escape")
        settle(app, 0.1)
        assert results == [False] and not dialog.winfo_exists()
        assert app.focus_get() == focus_target(entry)  # and comes back where it was

        dialog = ConfirmDialog(day, title="Confirm", message="Do it?", on_result=results.append).present()
        key(dialog, "Return")
        pump(app)
        assert results == [False, True]
        danger = ConfirmDialog(day, title="Delete", message="Delete it?", danger=True,
                               on_result=results.append).present()
        settle(app, 0.1)
        assert app.focus_get() == focus_target(danger.cancel_button)  # destructive: Cancel is the default
        key(danger, "Return")
        pump(app)
        assert danger.winfo_exists() and results == [False, True]  # Enter does not confirm a destructive action
        danger.cancel()
        pump(app)

        drawer = Drawer(day, "Details")
        entry.focus_force()
        settle(app, 0.1)
        drawer.open()
        settle(app, 0.1)
        assert drawer.is_open and app.focus_get() == focus_target(drawer.close_button)
        key(app, "Escape")
        settle(app, 0.1)
        assert not drawer.is_open and app.focus_get() == focus_target(entry)

        notice = Notice(day)
        notice.show("error", "The file is not UTF-8.")
        pump(app)
        assert notice.text == "Error: The file is not UTF-8."  # said in words, not only by color
        state = StateView(day)
        state.error("Offline", retry=lambda: None)
        pump(app)
        assert state.message_label.cget("text") == "Error: Offline"

        # The Day timeline is the keyboard way to act on scheduled items: Enter edits, Delete removes, the
        # menu lists both.
        fill_form(day, name="Read", fixed=True, start="540", end="600")
        day.form.submit_button.invoke()
        settle(app, 0.2)
        timeline = day.schedule_canvas
        timeline.canvas.focus_force()
        key(timeline.canvas, "Home")
        pump(app)
        entries = timeline._menu_items(None)
        assert [entry.label for entry in entries] == ["Edit...", "Release manual placement", "Remove..."]
        assert not entries[1].enabled  # a fixed block is not a manual placement
        key(timeline.canvas, "Return")
        pump(app)
        assert day.form.editing and day.form.name_field.get() == "Read"
        day.cancel_edit()
        pump(app)
        key(timeline.canvas, "Home")
        pump(app)
        key(timeline.canvas, "Delete")
        pump(app)
        assert tree_names(day) == [] and dialogs.errors == []
    finally:
        close_app(app)
