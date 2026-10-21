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
    app.show_page("week")
    pump(app)
    assert app.shell.current == "week"


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
