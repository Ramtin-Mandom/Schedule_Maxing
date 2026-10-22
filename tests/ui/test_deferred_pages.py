"""
Start-up builds only the Day page; every other page is an app/ui/pages.py
DeferredPage built the first time it is shown (real windows).
"""

from __future__ import annotations

import pytest

from tests.ui.test_desktop_app import close_app, dialogs, open_app, pump  # noqa: F401 - fixture

DEFERRED = ("week", "month", "projects", "productivity", "budget", "settings", "account", "guide", "about")


@pytest.fixture
def app(tmp_path, dialogs, monkeypatch):  # noqa: F811 - the fixture imported above
    from app.app import ScheduleOptimizerApp

    monkeypatch.setattr(ScheduleOptimizerApp, "PREBUILD_PAGES", ())  # these tests check what is NOT built
    window = open_app(tmp_path / "app.db", tmp_path, build_pages=False)
    yield window
    close_app(window)


def test_week_is_pre_built_in_parts_while_the_app_is_idle(app, monkeypatch) -> None:
    import app.app as app_module
    import app.ui.shell as shell_module

    monkeypatch.setattr(shell_module, "_input_idle_ms", lambda: 10_000)
    week = app.pages["week"]
    monkeypatch.setattr(app, "PREBUILD_PAGES", ("week",))
    monkeypatch.setattr(app_module, "_input_idle_ms", lambda: 0)  # someone is typing: it waits
    app._prebuild()
    pump(app)
    assert not week.built and week._building is None

    monkeypatch.setattr(app_module, "_input_idle_ms", lambda: 10_000)
    steps = []
    original = week.prebuild_step
    monkeypatch.setattr(week, "prebuild_step", lambda: (steps.append(1), original())[1])
    app._prebuild()
    pump(app, lambda: week.built, timeout=15)
    assert len(steps) > 3  # one part per idle callback, not one block
    assert app.shell.current == "day" and week.page.snapshot is not None  # built hidden, its period read
    pump(app, lambda: str(week) in app.shell._detached, timeout=5)  # and parked out of the main window
    app.show_page("week")
    pump(app)
    assert app.shell.current == "week"


def test_a_hidden_page_lives_in_its_own_withdrawn_window(app, monkeypatch) -> None:
    import time

    import app.ui.shell as shell_module

    monkeypatch.setattr(shell_module, "_input_idle_ms", lambda: 10_000)  # nobody is typing
    app.show_page("week")
    pump(app, lambda: app.pages["week"].built)
    week = app.pages["week"]
    app.show_page("month")
    assert str(week) not in app.shell._detached and "week" in app.shell._detach_pending  # the page just left: kept
    app.show_page("projects")  # leaving another page: Week is no longer the last one, so it goes promptly
    deadline = time.monotonic() + 5
    while str(week) not in app.shell._detached and time.monotonic() < deadline:
        pump(app)
    # Not a hidden child of the main window: the window's native child windows exclude the page's.
    assert str(week) in app.shell._detached and week.winfo_toplevel() is week
    assert app.tk.call("wm", "state", week._w) == "withdrawn"
    assert str(app.pages["day"]) not in app.shell._detached  # the home page always stays attached
    assert str(app.pages["month"]) not in app.shell._detached  # and so does the page just left
    app.show_page("week")
    pump(app)
    assert week.winfo_toplevel() is app and week.winfo_manager() == "place" and str(week) not in app.shell._detached
    assert week.page.snapshot is not None


def test_a_hidden_page_is_detached_only_while_the_user_is_idle(app, monkeypatch) -> None:
    import app.ui.shell as shell_module

    app.show_page("week")
    pump(app, lambda: app.pages["week"].built)
    app.show_page("day")
    pump(app)
    week = app.pages["week"]
    monkeypatch.setattr(shell_module, "_input_idle_ms", lambda: 0)  # the user is busy: not now
    app.shell._detach_when_idle("week")
    assert str(week) not in app.shell._detached and "week" in app.shell._detach_pending
    monkeypatch.setattr(shell_module, "_input_idle_ms", lambda: 60_000)
    app.shell._detach_when_idle("week")
    pump(app, lambda: str(week) in app.shell._detached, timeout=5)
    app.show_page("week")  # shown again: attached, and no detach left pending for it
    assert "week" not in app.shell._detach_pending and str(week) not in app.shell._detached


def test_a_detach_waits_until_no_idle_work_is_pending(app, monkeypatch) -> None:
    import app.ui.shell as shell_module

    monkeypatch.setattr(shell_module, "_input_idle_ms", lambda: 10_000)
    assert app.shell._quiet()
    pending = app.after_idle(lambda: None)  # e.g. a page building its rows in idle slices
    assert not app.shell._quiet()  # a `wm` command would run it (and every slice after it) at once
    app.after_cancel(pending)
    monkeypatch.setattr(shell_module, "_input_idle_ms", lambda: 0)
    assert not app.shell._quiet()  # nor while someone is typing or moving the mouse


def test_showing_a_page_while_it_is_pre_built_finishes_it_at_once(app) -> None:
    week = app.pages["week"]
    assert week.prebuild_step() and week._building is not None and not week.built
    app.show_page("week")
    assert week.built and week._building is None
    pump(app)
    assert week.page.snapshot is not None


def test_only_the_day_page_is_built_at_start_up(app) -> None:
    assert not hasattr(app.pages["day"], "built")  # Day is the real page
    assert [key for key in DEFERRED if app.pages[key].built] == []


def test_navigation_shortcuts_and_the_group_bar_build_a_page_when_it_is_shown(app) -> None:
    app.shell.navigate("week")
    pump(app, lambda: app.pages["week"].built and app.pages["week"].snapshot is not None)
    assert app.shell.current == "week" and not app.pages["month"].built

    app.shell.navigate("month")  # what the sidebar and Ctrl+3 call
    pump(app, lambda: app.pages["month"].built)
    assert app.shell.current == "month"

    app.show_page("settings")
    pump(app)
    app.shell.group_bar.buttons["guide"].invoke()  # the Settings | Account | How to Use bar
    pump(app)
    assert app.shell.current == "guide" and app.pages["guide"].built and not app.pages["account"].built


def test_the_status_bar_account_button_builds_the_account_page(app) -> None:
    app.shell.status_bar.account_button.invoke()
    pump(app)
    assert app.shell.current == "account" and app.pages["account"].built
    assert app.pages["account"].view is not None  # it read the connection status when shown


def test_a_page_used_before_it_was_shown_is_built_on_demand(app) -> None:
    week = app.pages["week"]
    assert getattr(week, "_busy", False) is False and not week.built  # a private check never builds it
    assert week.page_controller.mode == "week" and week.built  # a public attribute is the built page's


def test_replacing_a_built_page_method_through_the_wrapper_reaches_the_page(app) -> None:
    projects = app.pages["projects"]
    projects.ensure()
    projects._ask_date = lambda task: "asked"  # what a test does to avoid the real dialog
    assert "_ask_date" in vars(projects.page) and projects.page._ask_date(None) == "asked"
    projects.marker = 1  # a name the page does not have stays on the wrapper
    assert vars(projects)["marker"] == 1 and not hasattr(projects.page, "marker")


def test_appearance_and_layout_reach_a_page_built_later(app) -> None:
    from app.ui.shell_state import LayoutMode

    assert app.set_appearance("dark") is not None
    assert not app.pages["settings"].built  # switching appearance does not build Settings
    app.show_page("settings")
    pump(app)
    assert app.pages["settings"].appearance_select.get() == "Dark"

    app.shell.state.layout = LayoutMode.NARROW
    app.show_page("projects")  # the layout given before the build is applied right after it
    assert app.pages["projects"].page.layout == LayoutMode.NARROW
    pump(app)
    app.set_appearance("light")


def test_a_workspace_switch_replaces_pages_without_building_unopened_ones(app) -> None:
    app.show_page("week")
    pump(app, lambda: app.pages["week"].built)
    old_week = app.pages["week"]
    app.apply_workspace()
    pump(app)
    assert app.pages["week"] is not old_week and app.pages["week"].built  # the shown page is rebuilt now
    assert not app.pages["month"].built and not app.pages["productivity"].built


def test_light_caching_keeps_only_day_and_the_shown_page_built(app, monkeypatch) -> None:
    import app.ui.shell as shell_module

    monkeypatch.setattr(shell_module, "_input_idle_ms", lambda: 10_000)  # nobody is typing
    assert app.set_light_caching(True) and app.ui_store.load().light_caching  # saved for next time
    app.show_page("week")
    pump(app, lambda: app.pages["week"].built)
    week_page = app.pages["week"].page
    app.show_page("settings")
    pump(app, lambda: not app.pages["week"].built, timeout=5)  # another section: Week is released
    pump(app, lambda: app.pages["week"].winfo_children() == [], timeout=5)  # its widgets go in slices
    assert not week_page.winfo_exists()

    app.shell.group_bar.buttons["account"].invoke()  # another page of the same group: Settings goes too
    pump(app, lambda: not app.pages["settings"].built, timeout=5)
    app.shell.group_bar.buttons["guide"].invoke()
    pump(app, lambda: not app.pages["account"].built, timeout=5)
    assert app.pages["guide"].built

    app.show_page("day")
    pump(app, lambda: not app.pages["guide"].built, timeout=5)
    assert app.pages["day"].winfo_exists() and app.shell.current == "day"

    app.show_page("week")  # shown again: built anew, at the remembered date, with its data read
    pump(app, lambda: app.pages["week"].built and app.pages["week"].page.snapshot is not None)
    assert app.shell.current == "week"


def test_light_caching_returning_before_the_release_keeps_the_page(app, monkeypatch) -> None:
    import app.ui.shell as shell_module

    monkeypatch.setattr(shell_module, "_input_idle_ms", lambda: 10_000)
    app.set_light_caching(True)
    app.show_page("projects")
    pump(app, lambda: app.pages["projects"].built)
    built = app.pages["projects"].page
    app.show_page("day")
    assert "projects" in app.shell._detach_pending  # released once it is safe, not during the switch
    app.show_page("projects")  # back before then: nothing was released
    pump(app)
    assert app.pages["projects"].page is built and "projects" not in app.shell._detach_pending


def test_turning_light_caching_on_releases_pages_outside_the_open_section(app, monkeypatch) -> None:
    import app.ui.shell as shell_module

    monkeypatch.setattr(shell_module, "_input_idle_ms", lambda: 10_000)
    app.show_page("week")
    pump(app, lambda: app.pages["week"].built)
    app.show_page("settings")
    pump(app)
    app.pages["settings"].light_caching_box.toggle()  # the Settings checkbox
    pump(app, lambda: not app.pages["week"].built, timeout=5)
    assert app.shell.light_caching and app.ui_settings.light_caching and app.pages["settings"].built
    app.pages["settings"].light_caching_box.toggle()  # off again: pages stay built as before
    assert not app.shell.light_caching
    app.show_page("week")
    pump(app, lambda: app.pages["week"].built)
    app.show_page("settings")
    pump(app)
    assert app.pages["week"].built
