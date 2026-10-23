"""
Switching back to a page whose data did not change neither re-queries nor
re-renders it; a write anywhere makes the next show re-read (app/ui/page_freshness.py).
Real windows.
"""

from __future__ import annotations

import pytest

from app.planning.models import Task
from tests.ui.test_desktop_app import WEDNESDAY, close_app, dialogs, open_app, pump  # noqa: F401 - fixture


@pytest.fixture
def app(tmp_path, dialogs):  # noqa: F811 - the fixture imported above
    window = open_app(tmp_path / "app.db", tmp_path, build_pages=False)
    yield window
    close_app(window)


def _show(app, key: str) -> None:
    app.show_page(key)
    pump(app)


def test_revisiting_pages_with_unchanged_data_reads_and_draws_nothing(app) -> None:
    for key in ("week", "month", "projects", "productivity", "day"):
        _show(app, key)
    day, week, month = app.pages["day"], app.pages["week"].page, app.pages["month"].page
    projects, productivity = app.pages["projects"].page, app.pages["productivity"].content
    seen = {"day": (day._load_token, day.snapshot), "week": (week._load_token, week.snapshot),
            "month": (month._load_token, month.snapshot), "projects": projects.snapshot}
    requests = dict(productivity._requests)

    for key in ("week", "month", "projects", "productivity", "day"):
        _show(app, key)
    assert (day._load_token, day.snapshot) == seen["day"]  # the same snapshot object: not re-rendered either
    assert (week._load_token, week.snapshot) == seen["week"]
    assert (month._load_token, month.snapshot) == seen["month"]
    assert projects.snapshot is seen["projects"]
    assert productivity._requests == requests

    # A write (here: a task saved by another page's controller) makes each page read again when shown.
    assert app.services.planning_controller.add_or_update_task(Task(
        name="Study", category="study", estimated_duration_minutes=30, preferred_dates=[WEDNESDAY])).ok
    for key in ("week", "projects", "productivity", "day"):
        _show(app, key)
    assert week._load_token > seen["week"][0] and "Study" in [row.name for row in week.snapshot.rows]
    assert projects.snapshot is not seen["projects"]
    assert productivity._requests != requests
    assert day._load_token > seen["day"][0]


def test_reload_always_re_reads(app) -> None:
    week = app.pages["week"]
    _show(app, "week")
    token = week._load_token
    week.reload()  # the manual way, e.g. after a failure
    pump(app)
    assert week._load_token == token + 1


def test_a_filter_shown_before_comes_from_the_report_cache(app, monkeypatch) -> None:
    _show(app, "productivity")
    page = app.pages["productivity"].content
    page.show_section("Specific")
    pump(app)
    built = []
    original = page._controller.build_tracker
    monkeypatch.setattr(page._controller, "build_tracker", lambda *args: (built.append(args), original(*args))[1])
    page.days_var.set("Last 7 days")
    page._load("Time-based")
    pump(app)
    page.days_var.set("All time")
    page._load("Time-based")  # shown a moment ago for the same data: no worker, no new report
    pump(app)
    assert len(built) == 1


def test_fonts_are_shared_per_size_and_weight_and_drop_callbacks_cheaply(app) -> None:
    from app.ui.components import font

    body, again, bold = font(12), font(12), font(12, "bold")
    assert body is again and body is not bold
    callbacks = [lambda index=index: index for index in range(3)]
    for callback in callbacks:
        body.add_size_configure_callback(callback)
    body.remove_size_configure_callback(callbacks[1])
    body.remove_size_configure_callback(callbacks[1])  # already gone: no error
    remaining = list(body._size_configure_callback_list)
    assert callbacks[0] in remaining and callbacks[2] in remaining and callbacks[1] not in remaining
    for callback in callbacks:
        body.remove_size_configure_callback(callback)
